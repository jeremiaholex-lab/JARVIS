"""Sandboxed skill test execution (§6.6 step 2).

Two isolation levels, chosen automatically and reported honestly:

  docker   — Docker on WSL2 (the design's first choice): `--network none`,
             `--memory` cap, read-only rootfs with one writable scratch dir.
             A real boundary against the user's files and accounts.
  fallback — restricted process: credential-stripped env, poisoned proxy
             vars, memory watchdog, fresh OPENJARVIS_HOME. NOTE: this is
             NOT an OS-level boundary — the test still runs as the current
             user and can read anything that user can read. The approval
             gate surfaces which level actually ran so nobody mistakes the
             fallback for container isolation.

The framework's run_sandboxed() is not Windows-safe (preexec_fn=os.setsid),
so process handling lives here while reusing SandboxResult/build_safe_env.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openjarvis.security.subprocess_sandbox import SandboxResult, build_safe_env

_logger = logging.getLogger(__name__)

MAX_MEMORY_BYTES = 4 * 1024**3  # design §6.6: ~4 GB cap
CHECK_INTERVAL_S = 0.25
_MAX_OUTPUT_BYTES = 204800
_SANDBOX_IMAGE = os.environ.get("JARVIS_SANDBOX_IMAGE", "python:3.12-slim")


@dataclass(slots=True)
class SandboxTestResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    killed_for_memory: bool
    note: str = ""
    isolation: str = "fallback"  # "docker" | "fallback"


_DOCKER_STD_LOCATIONS = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
)


def _win_executable(path: str) -> str:
    """Make sure a Windows resolution is actually runnable.

    Real bug found here: Docker Desktop's bin directory ships both
    ``docker.exe`` and a POSIX shell shim named ``docker`` for WSL use. Under
    a Git-Bash-flavoured PATH, ``shutil.which("docker")`` returns the shim,
    CreateProcess fails with WinError 193 ("not a valid Win32 application"),
    and skill tests silently fall back to the same-user process on a machine
    that has real container isolation installed. Prefer the .exe.
    """
    if os.name != "nt" or not path:
        return path
    lowered = path.casefold()
    if lowered.endswith((".exe", ".com")):
        return path
    sibling = path + ".exe"
    if Path(sibling).is_file():
        return sibling
    return path


def docker_cli() -> Optional[str]:
    """Locate the docker CLI.

    PATH lookup first, then the standard install directory: Docker Desktop
    adds itself to the machine PATH at install time, but a process that was
    already running — the orb shell, a scheduled-task worker — inherits the
    old environment and cannot see it until it restarts. Without this, JARVIS
    would silently keep using the weaker sandbox on a machine that now has
    real isolation installed.
    """
    override = os.environ.get("JARVIS_DOCKER_CLI")
    if override and Path(override).is_file():
        return _win_executable(override)
    # .exe first on Windows so the WSL shim cannot win the lookup.
    names = ("docker.exe", "docker") if os.name == "nt" else ("docker",)
    for name in names:
        found = shutil.which(name)
        if found:
            return _win_executable(found)
    for candidate in _DOCKER_STD_LOCATIONS:
        if Path(candidate).is_file():
            return candidate
    return None


def docker_app_path() -> Optional[str]:
    """The Docker Desktop GUI executable, if installed."""
    for candidate in (
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _engine_up(cli: str, timeout: float = 8.0) -> bool:
    try:
        return subprocess.run(
            [cli, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=timeout,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_engine(wait_s: float | None = None) -> bool:
    """Ask Docker Desktop to start, and wait briefly for an engine that is
    already coming up.

    Measured on this machine: a *warm* restart answers in ~20s, but a cold
    boot after the VM was killed took 4-6 minutes. So this does not block
    that long — it requests the start, gives a short grace window for the
    common case (engine already booting, or a quick warm restart), and
    reports honestly. AutoStart is enabled on this install, so after a
    sign-in the engine is normally already there.
    """
    cli = docker_cli()
    if not cli:
        return False
    if _engine_up(cli):
        return True
    if wait_s is None:
        try:
            wait_s = float(os.environ.get("JARVIS_DOCKER_WAIT", "45"))
        except ValueError:
            wait_s = 45.0
    app = docker_app_path()
    if not app:
        return False
    try:
        if os.name == "nt":
            # ShellExecute, not Popen with detached flags: Docker Desktop.exe
            # is a launcher that re-invokes itself, and a detached process
            # group kills it before the VM ever comes up (measured here).
            os.startfile(app)  # noqa: S606 - fixed, verified path
        else:
            subprocess.Popen([app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        _logger.info("asked Docker Desktop to start (%s)", app)
    except OSError as exc:
        _logger.warning("could not start Docker Desktop: %s", exc)
        return False
    deadline = time.time() + wait_s
    booted = False
    while time.time() < deadline:
        if _engine_up(cli, timeout=5.0):
            _logger.info("docker engine came up on demand")
            booted = True
            break
        time.sleep(5)
    if not booted:
        _logger.warning(
            "docker engine not up within %.0fs; Docker Desktop was asked to start "
            "and a cold boot on this machine takes several minutes — container "
            "isolation applies from the next run", wait_s)
    return booted


def _docker_available() -> bool:
    if os.environ.get("JARVIS_SANDBOX_DISABLE_DOCKER") == "1":
        return False
    cli = docker_cli()
    if not cli:
        return False
    return _engine_up(cli)


def _docker_mount(workdir: Path) -> str:
    """Docker Desktop (WSL2 engine) wants POSIX-style drive paths."""
    raw = str(workdir).replace("\\", "/")
    if os.name == "nt" and len(raw) > 2 and raw[1] == ":":
        return "/" + raw[0].lower() + raw[2:]
    return raw


def _run_docker(test_path: Path, workdir: Path, timeout: float,
                max_memory_bytes: int) -> SandboxResult:
    """Run the test inside a container with no network and no host mounts."""
    result = SandboxResult()
    cli = docker_cli()
    if not cli:
        result.returncode = -1
        result.stderr = "docker CLI not found"
        return result
    cmd = [
        cli, "run", "--rm",
        "--network", "none",
        "--memory", f"{max(1, max_memory_bytes // 2**30)}g",
        "--memory-swap", f"{max(1, max_memory_bytes // 2**30)}g",
        "--cpus", "2",
        "--read-only",
        "--tmpfs", "/tmp",
        "-v", f"{_docker_mount(workdir)}:/scratch:rw",
        "-w", "/scratch",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        _SANDBOX_IMAGE, "python", f"/scratch/{test_path.name}", "/scratch",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=build_safe_env())
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.killed = True
        result.returncode = -1
        result.stdout = "(timed out)"
        return result
    except OSError as exc:
        result.stderr = f"docker launch failed: {exc}"
        result.returncode = -1
        return result
    result.stdout = (proc.stdout or "")[:_MAX_OUTPUT_BYTES]
    result.stderr = (proc.stderr or "")[:_MAX_OUTPUT_BYTES]
    result.returncode = proc.returncode
    if proc.returncode != 0 and "memory" in (result.stderr or "").casefold():
        result.killed = True
    return result


def _block_network(env: dict) -> None:
    """Poison the proxy vars so any accidental socket call fails fast."""
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[var] = "http://127.0.0.1:9"  # port 9: discard, nothing there
    env["no_proxy"] = ""


def _memory_watchdog(limit_bytes: int, done: "threading.Event", marker: Path):
    try:
        import psutil
    except ImportError:
        print("NOTE: psutil unavailable — memory cap disabled for this run")
        return

    while not done.wait(CHECK_INTERVAL_S):
        total = 0
        procs: list = []
        try:
            me = psutil.Process()
            procs = me.children(recursive=True)
            total = me.memory_info().rss + sum(
                p.memory_info().rss for p in procs
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except psutil.Error:
            continue
        if total > limit_bytes:
            for p in procs:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            try:
                marker.write_text(f"limit={limit_bytes}", encoding="utf-8")
            except OSError:
                pass
            return


def _run_restricted(
    argv: list[str],
    *,
    cwd: str,
    env: dict,
    timeout: float,
) -> SandboxResult:
    """Cross-platform replacement for the framework's run_sandboxed.

    Windows: CREATE_NEW_PROCESS_GROUP so the tree can be torn down as a unit.
    POSIX: preexec_fn=os.setsid (what the framework already did).
    """
    result = SandboxResult()
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": env,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen(argv, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        result.stdout = (stdout or "")[:_MAX_OUTPUT_BYTES]
        result.stderr = (stderr or "")[:_MAX_OUTPUT_BYTES]
        result.returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.SubprocessError:
            pass
        result.timed_out = True
        result.killed = True
        result.returncode = -1
        result.stdout = "(timed out)"
        result.stderr = ""
    return result


def run_skill_tests(
    test_script: str,
    *,
    workdir: Optional[Path] = None,
    timeout: float = 120.0,
    max_memory_bytes: int = MAX_MEMORY_BYTES,
    prefer: str = "auto",
) -> SandboxTestResult:
    """Run a proposed skill's test script, container-first.

    `prefer`: "auto" (docker when available, else fallback), "docker"
    (refuse to run without it), or "fallback" (force the restricted process).
    """
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="jarvis-skilltest-"))
    workdir.mkdir(parents=True, exist_ok=True)
    test_path = workdir / "test_skill.py"
    test_path.write_text(test_script, encoding="utf-8")

    use_docker = prefer != "fallback" and _docker_available()
    engine_note = ""
    if prefer != "fallback" and not use_docker and docker_cli():
        # Installed but the engine is asleep (typical right after a reboot):
        # ask it to start rather than quietly downgrading the boundary.
        if ensure_engine():
            use_docker = True
            engine_note = "docker engine started on demand"
        else:
            engine_note = ("docker installed, engine still booting — "
                           "container isolation applies from the next run")
    if prefer == "docker" and not use_docker:
        return SandboxTestResult(
            passed=False, returncode=-1,
            stdout="",
            stderr="docker isolation requested but unavailable; nothing was executed",
            timed_out=False, killed_for_memory=False,
            note=engine_note or "docker isolation requested but unavailable",
            isolation="none",
        )

    if use_docker:
        result = _run_docker(test_path, workdir, timeout, max_memory_bytes)
        isolation = "docker"
        note = ""
        if result.timed_out:
            note = "killed (timeout)"
        passed = result.returncode == 0 and not result.killed
        return SandboxTestResult(
            passed=passed, returncode=result.returncode, stdout=result.stdout,
            stderr=result.stderr, timed_out=result.timed_out,
            killed_for_memory=False,
            note=(note or ("clean" if passed else "failed")) +
                 (f" ({engine_note})" if engine_note else ""),
            isolation=isolation,
        )

    # --- restricted-process fallback -------------------------------------
    scratch_home = workdir / "scratch_home"
    scratch_home.mkdir(exist_ok=True)

    env = build_safe_env(
        extra={
            # Isolate the sandbox from the real agent home — no connectors, no keys.
            "OPENJARVIS_HOME": str(scratch_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    _block_network(env)

    done = threading.Event()
    memory_kill_marker = scratch_home / "memory-kill"
    watchdog = threading.Thread(
        target=_memory_watchdog,
        args=(max_memory_bytes, done, memory_kill_marker),
        daemon=True,
    )
    watchdog.start()

    result = _run_restricted(
        [sys.executable, str(test_path), str(workdir)],
        cwd=str(workdir),
        env=env,
        timeout=timeout,
    )
    done.set()
    time.sleep(0.05)  # give a just-fired watchdog a beat to write its marker

    killed_for_memory = memory_kill_marker.is_file()
    passed = result.returncode == 0 and not result.killed and not killed_for_memory
    note = ""
    if result.timed_out:
        note = "killed (timeout)"
    elif killed_for_memory:
        note = f"killed (memory cap {max_memory_bytes // 2**30} GB exceeded)"
    return SandboxTestResult(
        passed=passed,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        killed_for_memory=killed_for_memory,
        note=note or "restricted process — no container isolation",
        isolation="fallback",
    )


__all__ = ["SandboxTestResult", "run_skill_tests", "MAX_MEMORY_BYTES",
           "docker_cli", "ensure_engine", "_docker_available"]
