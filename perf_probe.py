"""Measure the local brain's real speed and memory headroom (design §11).

The Phase 1 done-test asked for two numbers that never got recorded: how many
tokens/second the local model actually manages on this machine, and whether
the Phase 4 sandbox will fit alongside it. This probe produces both, prints a
report, and writes ``<home>/perf.json``.

The routing policy reads that file: on a CPU-only laptop a very slow local
model is not "local-first", it is just slow, so the router may prefer the
already-configured cloud engine and say why.

Run with the runtime venv from the JARVIS root:
  openjarvis_runtime\\.venv\\Scripts\\python.exe perf_probe.py
  openjarvis_runtime\\.venv\\Scripts\\python.exe perf_probe.py --model qwen3:8b
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("OPENJARVIS_HOME", ROOT / ".openjarvis"))
MIN_USEFUL_TPS = 4.0  # below this, a spoken answer arrives long after the question


def measure_local(model: str) -> dict:
    from openjarvis.core.types import Message, Role
    from openjarvis.engine.ollama import OllamaEngine

    engine = OllamaEngine()
    prompt = (
        "Count from one to twenty, separated by commas, with no other words."
    )
    msgs = [Message(role=Role.USER, content=prompt)]
    out: dict = {"model": model, "reachable": False}

    timings = []
    for attempt in range(2):  # first pays model load, second is steady state
        started = time.time()
        try:
            res = engine.generate(msgs, model=model, max_tokens=80, temperature=0.0)
        except Exception as exc:
            out["error"] = f"{exc.__class__.__name__}: {exc}"
            return out
        timing = res.get("engine_timing", {})
        eval_s = timing.get("eval_duration", 0) / 1e9
        tokens = res.get("usage", {}).get("completion_tokens", 0)
        prompt_eval_s = timing.get("prompt_eval_duration", 0) / 1e9
        prompt_tokens = res.get("usage", {}).get("prompt_tokens_evaluated", 0)
        timings.append({
            "attempt": "cold" if attempt == 0 else "warm",
            "wall_s": round(time.time() - started, 2),
            "completion_tokens": tokens,
            "tokens_per_second": round(tokens / eval_s, 2) if eval_s else None,
            "prompt_tokens_per_second": round(prompt_tokens / prompt_eval_s, 1) if prompt_eval_s else None,
            "load_s": round(timing.get("total_duration", 0) / 1e9 - eval_s - prompt_eval_s, 2),
        })
    engine.close()
    warm = timings[-1]
    out.update({
        "reachable": True,
        "cold": timings[0],
        "warm": warm,
        "tokens_per_second": warm["tokens_per_second"],
        "cold_start_s": timings[0]["wall_s"],
    })
    return out


def measure_memory() -> dict:
    try:
        import psutil

        vm = psutil.virtual_memory()
        return {
            "total_gb": round(vm.total / 2**30, 1),
            "available_gb": round(vm.available / 2**30, 1),
            "used_pct": vm.percent,
        }
    except ImportError:
        return {"error": "psutil not installed"}


def check_docker() -> dict:
    """Design §11 open item: can the Phase 4 sandbox coexist with the brain?"""
    try:
        from jarvis.skills.sandbox_test import docker_cli
        cli = docker_cli()
    except ImportError:
        cli = shutil.which("docker")
    if not cli:
        return {"available": False, "detail": "docker CLI not found — "
                "skill tests use the restricted-process fallback"}
    try:
        proc = subprocess.run(
            [cli, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "cli": cli, "detail": f"{exc.__class__.__name__}"}
    if proc.returncode != 0:
        return {"available": False, "cli": cli,
                "detail": (proc.stderr or "").strip()[:200] or "engine not running"}
    version, _, os_name = (proc.stdout or "").strip().partition("\t")
    return {"available": True, "cli": cli, "server_version": version.strip(),
            "os": os_name.strip()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jarvis-perf")
    ap.add_argument("--model", default=os.environ.get("JARVIS_LOCAL_MODEL", "qwen3:1.7b"))
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT / "openjarvis_runtime" / "src"))
    sys.path.insert(0, str(ROOT))

    print(f"measuring local model {args.model!r} …")
    local = measure_local(args.model)
    mem = measure_memory()
    docker = check_docker()

    report = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "local": local,
        "memory_gb": mem,
        "docker": docker,
        "min_useful_tps": MIN_USEFUL_TPS,
        "verdict": [],
    }
    tps = local.get("tokens_per_second")
    if not local.get("reachable"):
        report["verdict"].append(f"local model unreachable: {local.get('error')}")
    else:
        report["verdict"].append(
            f"local generation {tps} tok/s warm, cold start {local['cold_start_s']}s"
        )
        if tps is not None and tps < MIN_USEFUL_TPS:
            report["verdict"].append(
                f"below {MIN_USEFUL_TPS} tok/s — routing will prefer the cloud engine for non-trivial turns"
            )
    if docker["available"]:
        report["verdict"].append("Docker is live: skill tests get container isolation")
    else:
        report["verdict"].append(f"Docker unavailable ({docker['detail']}): skill tests run as a restricted process")
    if "available_gb" in mem:
        report["verdict"].append(
            f"{mem['available_gb']} GB free of {mem['total_gb']} GB right now "
            "(the 8B design target needs ~5 GB plus Whisper and Kokoro)"
        )

    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "perf.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print("\nsummary:")
    for line in report["verdict"]:
        print(f"  - {line}")
    print(f"\nwrote {HOME / 'perf.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
