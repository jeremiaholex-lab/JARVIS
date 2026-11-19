"""Phase 3 entry points for Google briefing, CodeAct, deadlines, and reminders."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "openjarvis_runtime"
VENV_PYTHON = RUNTIME / ".venv" / "Scripts" / "python.exe"
JARVIS = RUNTIME / ".venv" / "Scripts" / "jarvis.exe"
CONFIG = ROOT / "jarvis-phase3.toml"
DB = ROOT / "jarvis_projects.db"
MORNING_TIME = os.environ.get("JARVIS_MORNING_TIME", "08:00")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["OPENJARVIS_CONFIG"] = str(CONFIG)
    env["OPENJARVIS_HOME"] = str(ROOT / ".openjarvis")
    env["JARVIS_WORKSPACE_ROOT"] = str(ROOT)
    env["JARVIS_PROJECT_DB"] = str(DB)
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    return env


def _jarvis(args: list[str], *, check: bool = False) -> int:
    return subprocess.run([str(JARVIS), *args], cwd=RUNTIME, env=_env(), check=check).returncode


def _mark_digest_delivered(day=None) -> None:
    day = day or datetime.now().astimezone().date()
    home = Path(os.environ.get("OPENJARVIS_HOME", ROOT / ".openjarvis")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    (home / "phase3-digest-delivered.txt").write_text(day.isoformat(), encoding="utf-8")


def _project_store():
    from project_store import ProjectStore
    store = ProjectStore(DB)
    store.add_project("JARVIS", str(ROOT))
    return store


def digest() -> int:
    result = _jarvis(["digest", "--fresh"])
    if result:
        return result
    fallback = (
        "from openjarvis.agents.digest_store import DigestStore; "
        "a=DigestStore().get_latest(); "
        "assert a is not None, 'OpenJarvis did not save a digest'; "
        "(None if a.audio_path.is_file() else (__import__('sys').path.insert(0, "
        + repr(str(ROOT))
        + "), __import__('phase3')._speak(a.text)))"
    )
    result = subprocess.run([str(VENV_PYTHON), "-c", fallback], cwd=RUNTIME, env=_env()).returncode
    if result == 0:
        _mark_digest_delivered()
    return result


def connect_google() -> int:
    print("Google OAuth uses OpenJarvis's GDrive connector for Gmail, Calendar, and Tasks.")
    print("You will need the Google Cloud OAuth desktop client ID and secret.")
    client_file = ROOT / "google-oauth-client.json"
    if client_file.is_file():
        payload = json.loads(client_file.read_text(encoding="utf-8"))
        client = payload.get("installed") or payload.get("web") or payload
        client_id, client_secret = client.get("client_id"), client.get("client_secret")
        if not client_id or not client_secret:
            raise ValueError("OAuth JSON needs client_id and client_secret under installed or web.")
        setup = (
            "from openjarvis.connectors.oauth import get_provider_for_connector, save_client_credentials; "
            "p=get_provider_for_connector('gdrive'); save_client_credentials(p, "
            + repr(client_id) + ", " + repr(client_secret) + ")"
        )
        subprocess.run([str(VENV_PYTHON), "-c", setup], cwd=RUNTIME, env=_env(), check=True)
    return _jarvis(["connect", "gdrive"])


def code_assist(request: str) -> int:
    prompt = (f"Work only inside this project directory: {ROOT}. "
              "You are OpenJarvis native_openhands CodeAct. Read project files freely. "
              "Before every file write, show the proposed target and change; the runtime will ask me to confirm. "
              "Run tests and commands only through the confirmed shell tool. Never delete files, reset/clean git, "
              "or perform external/publishing actions without the runtime's separate typed confirmation. "
              "Do not access paths outside the project root. Request: " + request)
    args = ["ask", "--agent", "native_openhands", "--tools", "file_read,file_write,jarvis_file_undo,shell_exec", prompt]
    # Keep the design's configured 8B default, while allowing a smaller local
    # model on CPU-constrained machines without changing the global brain.
    code_model = os.environ.get("JARVIS_CODE_MODEL", "").strip()
    if code_model:
        args[1:1] = ["--model", code_model]
        if code_model.casefold().startswith("qwen3:"):
            args[-1] = "/no_think " + args[-1]
    code_max_tokens = os.environ.get("JARVIS_CODE_MAX_TOKENS", "").strip()
    if code_max_tokens.isdigit():
        args[1:1] = ["--max-tokens", code_max_tokens]
    return _jarvis(args)


_TTS_BACKEND = None


def _speak(text: str) -> None:
    """Speak through Kokoro, sentence by sentence.

    Two costs were paid on every announcement: a fresh KokoroTTSBackend per
    call (measured ~17-22s of one-time model init each time) and one huge
    synthesis call (~45s of silence for a morning digest). Reusing the backend
    and speaking the opening sentences fixes both.
    """
    global _TTS_BACKEND
    cleaned = (text or "").strip()
    if not cleaned:
        return
    sys.path.insert(0, str(RUNTIME / "src"))
    import io
    import sounddevice as sd
    import soundfile as sf

    from jarvis.speech import split_sentences
    from openjarvis.speech.kokoro_tts import KokoroTTSBackend

    if _TTS_BACKEND is None:
        _TTS_BACKEND = KokoroTTSBackend()
    for sentence in split_sentences(cleaned):
        result = _TTS_BACKEND.synthesize(sentence, voice_id="af_heart")
        audio, rate = sf.read(io.BytesIO(result.audio), dtype="float32")
        sd.play(audio, rate)
        sd.wait()


def worker() -> None:
    """At login, run the daily digest and announce each new deadline once."""
    store = _project_store()
    today_digest = None
    # A worker restart after the morning run should not replay a briefing that
    # was already generated today. If no artifact exists, the normal catch-up
    # check below runs it as soon as the worker starts after the scheduled time.
    try:
        sys.path.insert(0, str(RUNTIME / "src"))
        from openjarvis.agents.digest_store import DigestStore

        digest_store = DigestStore()
        try:
            now = datetime.now().astimezone()
            marker = Path(os.environ.get("OPENJARVIS_HOME", ROOT / ".openjarvis")) / "phase3-digest-delivered.txt"
            delivered_today = marker.is_file() and marker.read_text(encoding="utf-8").strip() == now.date().isoformat()
            artifact = digest_store.get_today("America/Toronto")
            if delivered_today:
                today_digest = now.date()
            elif artifact is not None:
                # Recover delivery after a restart between generation and the
                # delivery marker; speak the saved briefing once, then mark it.
                _speak(artifact.text)
                _mark_digest_delivered(now.date())
                today_digest = now.date()
        finally:
            digest_store.close()
    except Exception as exc:
        print(f"Could not check today's digest state ({exc.__class__.__name__}); will retry generation.", flush=True)
    print(f"JARVIS Phase 3 worker active. Digest time: {MORNING_TIME} America/Toronto.", flush=True)
    while True:
        now = datetime.now().astimezone()
        due = store.get_proactive_deadlines(now)
        due_ids = {item["id"] for item in due}
        # Alert state is persisted, so a restart or a re-register does not
        # re-announce a deadline the owner already heard about.
        try:
            already = store.alerted_deadline_ids()
        except Exception as exc:
            print(f"Could not read deadline alert state ({exc.__class__.__name__}).", flush=True)
            already = set()
        new_alerts = [item for item in due if item["id"] not in already]
        if new_alerts:
            parts = []
            for item in new_alerts:
                due_dt = datetime.fromisoformat(item["due_date"])
                if due_dt < now:
                    parts.append(f"{item['title']} for {item['project_name']} is OVERDUE (was due {item['due_date']})")
                else:
                    parts.append(f"{item['title']} for {item['project_name']} is due {item['due_date']}")
            message = "Reminder: " + "; ".join(parts)
            _speak(message)
            for item in new_alerts:
                try:
                    store.mark_deadline_alerted(item["id"])
                except Exception as exc:
                    print(f"Could not record deadline alert ({exc.__class__.__name__}).", flush=True)
        # A deadline that leaves the window (completed/cancelled) drops its
        # alert record so reopening it can remind again.
        for gone in already - due_ids:
            try:
                store.clear_deadline_alert(gone)
            except Exception:
                pass
        # Use >= so a delayed wake/resume or a 30-second polling boundary
        # cannot skip the daily run.  Record completion only after success so
        # a transient connector or audio error is retried on the next poll.
        if now.strftime("%H:%M") >= MORNING_TIME and today_digest != now.date():
            if digest() == 0:
                today_digest = now.date()
        time.sleep(30)


def dispatch_voice_request(text: str) -> bool:
    """Handle Phase 3 intents in the existing JARVIS voice/console loop."""
    cleaned = text.strip()
    lower = cleaned.lower()
    _project_store()
    if re.search(r"\b(morning digest|daily briefing|read my (?:email|calendar|tasks)|good morning)\b", lower):
        # Use the wrapper so Kokoro speaks the saved text when OpenJarvis's
        # digest pipeline fails to persist a playable audio artifact.
        digest()
        return True
    if re.search(r"\b(fix|debug|test|read|review|change|modify)\b", lower) and re.search(r"\b(project|code|bug|file|test|script)\b", lower):
        code_assist(cleaned)
        return True
    if lower in {"list projects", "show projects"}:
        from project_store import ProjectStore
        print("\n".join(f"{p['name']}: {p['path']}" for p in ProjectStore(DB).list_projects()) or "No projects registered.")
        return True
    if lower in {"list deadlines", "show deadlines", "what deadlines do i have"}:
        from deadline_cli import DeadlineCLI
        from project_store import ProjectStore
        print(DeadlineCLI(ProjectStore(DB)).run(["list"]))
        return True
    match = re.fullmatch(r"add deadline\s+(.+?)\s+for\s+(.+?)\s+due\s+(\d{4}-\d\d-\d\d(?:t\S+)?)", cleaned, re.I)
    if match:
        from deadline_cli import DeadlineCLI
        from project_store import ProjectStore
        title, project, due = match.groups()
        if input(f"Add deadline {title!r} for {project!r}, due {due}? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
        else:
            print(DeadlineCLI(ProjectStore(DB)).run(["add", project, title, due]))
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("digest", help="Generate and speak the digest now")
    sub.add_parser("speak-latest", help="Speak the most recently saved digest")
    sub.add_parser("connect-google", help="Authorize Google through OpenJarvis")
    code = sub.add_parser("code", help="Run a CodeAct request in this project")
    code.add_argument("request", nargs="+")
    sub.add_parser("worker", help="Run the scheduled digest and deadline monitor")
    args = parser.parse_args()
    if args.command == "digest":
        raise SystemExit(digest())
    if args.command == "speak-latest":
        sys.path.insert(0, str(RUNTIME / "src"))
        from openjarvis.agents.digest_store import DigestStore
        artifact = DigestStore().get_latest()
        if artifact is None:
            raise SystemExit("No saved digest to speak.")
        _speak(artifact.text)
        return
    if args.command == "connect-google":
        raise SystemExit(connect_google())
    if args.command == "code":
        raise SystemExit(code_assist(" ".join(args.request)))
    worker()


if __name__ == "__main__":
    os.environ["OPENJARVIS_HOME"] = str(ROOT / ".openjarvis")
    os.environ["OPENJARVIS_CONFIG"] = str(CONFIG)
    main()
