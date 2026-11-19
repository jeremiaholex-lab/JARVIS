import json

import pytest

from jarvis.skills import sandbox_test as sb


def _no_docker(monkeypatch):
    """Neutralize both halves: detection AND the on-demand engine wake.

    Waking the engine is intentionally aggressive, so a test that only stubs
    _docker_available still ends up in a real container on a Docker machine.
    """
    monkeypatch.setattr(sb, "_docker_available", lambda: False)
    monkeypatch.setattr(sb, "ensure_engine", lambda wait_s=None: False)


def test_auto_selection_reports_the_isolation_that_actually_ran(tmp_path, monkeypatch):
    _no_docker(monkeypatch)
    result = sb.run_skill_tests("print('ok')", workdir=tmp_path, timeout=60)
    assert result.passed
    assert result.isolation == "fallback"
    assert "no container isolation" in result.note


def test_docker_path_is_used_when_available(tmp_path, monkeypatch):
    seen = {}

    def fake_docker(test_path, workdir, timeout, mem):
        seen["cmd_seen"] = True
        r = sb.SandboxResult()
        r.returncode, r.stdout = 0, "ok\n"
        return r

    monkeypatch.setattr(sb, "_docker_available", lambda: True)
    monkeypatch.setattr(sb, "_run_docker", fake_docker)
    result = sb.run_skill_tests("print('ok')", workdir=tmp_path)
    assert result.passed and result.isolation == "docker"
    assert seen["cmd_seen"]


def test_docker_required_refuses_instead_of_silently_downgrading(tmp_path, monkeypatch):
    _no_docker(monkeypatch)
    result = sb.run_skill_tests("print('ok')", workdir=tmp_path, prefer="docker")
    assert not result.passed
    assert result.isolation == "none"
    assert "unavailable" in result.stderr


def test_engine_wake_is_attempted_before_downgrading(tmp_path, monkeypatch):
    """Installed-but-asleep must not silently mean the weak sandbox."""
    calls = []

    def fake_available():
        return len(calls) > 0  # asleep until woken

    def fake_ensure(wait_s=None):
        calls.append("wake")
        return True

    seen = {}

    def fake_docker(test_path, workdir, timeout, mem):
        seen["ran"] = True
        r = sb.SandboxResult()
        r.returncode, r.stdout = 0, "ok\n"
        return r

    monkeypatch.setattr(sb, "_docker_available", fake_available)
    monkeypatch.setattr(sb, "ensure_engine", fake_ensure)
    monkeypatch.setattr(sb, "docker_cli", lambda: "docker")
    monkeypatch.setattr(sb, "_run_docker", fake_docker)
    result = sb.run_skill_tests("print('ok')", workdir=tmp_path)
    assert calls == ["wake"], "the engine should be woken, not skipped"
    assert result.isolation == "docker"
    assert seen.get("ran") is True
    assert "started on demand" in result.note


def test_windows_paths_become_docker_mounts(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.os, "name", "nt")
    assert sb._docker_mount(tmp_path).startswith("/")


def test_failing_test_does_not_pass_and_keeps_output(tmp_path, monkeypatch):
    _no_docker(monkeypatch)
    result = sb.run_skill_tests("raise SystemExit('nope')", workdir=tmp_path, timeout=60)
    assert not result.passed
    assert result.returncode != 0


def test_windows_resolves_the_exe_not_the_wsl_shim(tmp_path, monkeypatch):
    """Regression: Docker Desktop's bin dir holds both docker.exe and a POSIX
    shim named `docker`. Picking the shim makes CreateProcess fail (WinError
    193), detection returns False, and skill tests silently lose container
    isolation on a machine that has it installed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "docker.exe"
    exe.write_bytes(b"MZ fake")
    shim = bin_dir / "docker"          # the extension-less POSIX wrapper
    shim.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sb.os, "name", "nt")
    monkeypatch.delenv("JARVIS_DOCKER_CLI", raising=False)
    monkeypatch.setattr(sb.shutil, "which",
                        lambda name: str(shim) if name == "docker" else None)
    assert sb.docker_cli().endswith("docker.exe")


def test_explicit_cli_override_is_still_made_runnable(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker.exe").write_bytes(b"MZ fake")
    shim = bin_dir / "docker"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sb.os, "name", "nt")
    monkeypatch.setenv("JARVIS_DOCKER_CLI", str(shim))
    assert sb.docker_cli().endswith("docker.exe")


def test_std_install_dir_is_found_without_path_help(tmp_path, monkeypatch):
    """A process started before Docker was installed has a stale PATH."""
    exe = tmp_path / "docker.exe"
    exe.write_bytes(b"MZ fake")
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setattr(sb, "_DOCKER_STD_LOCATIONS", (str(exe),))
    assert sb.docker_cli() == str(exe)


def test_posix_does_not_rewrite_the_path(tmp_path, monkeypatch):
    shim = tmp_path / "docker"
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sb.os, "name", "posix")
    monkeypatch.setattr(sb.shutil, "which", lambda name: str(shim))
    assert sb.docker_cli() == str(shim)


def test_ensure_engine_is_truthful_when_engine_is_absent(tmp_path, monkeypatch):
    """It must not claim isolation it did not get."""
    monkeypatch.setattr(sb, "docker_cli", lambda: None)
    assert sb.ensure_engine() is False


def test_ensure_engine_returns_true_immediately_when_engine_is_up(monkeypatch):
    monkeypatch.setattr(sb, "docker_cli", lambda: "docker")
    monkeypatch.setattr(sb, "_engine_up", lambda cli, timeout=8.0: True)
    assert sb.ensure_engine() is True


def test_test_script_must_not_import_a_module_the_skill_does_not_ship():
    """Live finding: the cloud wrote a test doing `from skill import ...`,
    which can only ImportError - skills are markdown, not code."""
    from jarvis.skills.propose import ProposalError, check_test_script

    for bad in (
        "from skill import count_stats\nprint(1)",
        "import skill\n",
        "import os\nfrom skill import x\n",
    ):
        with pytest.raises(ProposalError):
            check_test_script(bad)

    # self-contained scripts are fine
    check_test_script("import pathlib\nassert len('a b'.split()) == 2\n")


def test_propose_rejects_malformed_local_output_and_escalates(monkeypatch):
    """A bad local draft must not crash the pipeline (it used to)."""
    from jarvis.skills import propose as mod
    from jarvis.skills.propose import ProposalError, propose_skill

    class Engine:
        def __init__(self, payloads):
            self.payloads = list(payloads)

        def generate(self, msgs, **kwargs):
            return {"content": self.payloads.pop(0)}

    local = Engine(["not json at all"])
    cloud = Engine(['{"skill_md": "---\\nname: ok-skill\\ndescription: d\\n---\\nbody\\n", '
                    '"test_script": "assert 1 == 1", "summary": "s"}'])
    proposal = propose_skill("a gap", local_engine=local, cloud_engine=cloud)
    assert proposal.name == "ok-skill"


def test_propose_raises_clearly_when_no_engine_is_valid(monkeypatch):
    from jarvis.skills.propose import ProposalError, propose_skill

    class Bad:
        def generate(self, msgs, **kwargs):
            return {"content": "garbage"}

    try:
        propose_skill("a gap", local_engine=Bad(), cloud_engine=Bad())
    except ProposalError as exc:
        assert "no engine produced a valid skill proposal" in str(exc)
        assert "qwen3" in str(exc) and "agnes" in str(exc), "says which engines failed"
    else:
        raise AssertionError("expected ProposalError")


def test_propose_rejects_tests_that_import_the_skill_before_spending_a_container_run():
    from jarvis.skills.propose import ProposalError, propose_skill

    payload = ('{"skill_md": "---\\nname: x-skill\\ndescription: d\\n---\\nb\\n", ' 
               '"test_script": "from skill import thing\\n", "summary": "s"}')

    class Engine:
        def generate(self, msgs, **kwargs):
            return {"content": payload}

    import pytest as _p
    with _p.raises(ProposalError):
        propose_skill("gap", local_engine=Engine(), cloud_engine=None)


def test_ledger_records_isolation_level(tmp_path, monkeypatch):
    """The approval ledger must show which boundary actually protected the user."""
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    from jarvis.skills.approve import SkillGovernance
    from jarvis.skills.propose import Proposal
    from jarvis.skills.sandbox_test import SandboxTestResult

    proposal = Proposal(
        name="demo-skill", manifest=None,
        skill_md="---\nname: demo-skill\ndescription: d\n---\nbody\n",
        test_script="print(1)", summary="counts", source_gap="gap",
    )
    test = SandboxTestResult(
        passed=True, returncode=0, stdout="", stderr="", timed_out=False,
        killed_for_memory=False, note="restricted process — no container isolation",
        isolation="fallback",
    )
    SkillGovernance(home=tmp_path).record(proposal, test, "sandbox_passed")
    ledger = json.loads((tmp_path / "skill-governance" / "jarvis-skill-approvals.json").read_text())
    assert ledger["demo-skill"]["isolation"] == "fallback"
