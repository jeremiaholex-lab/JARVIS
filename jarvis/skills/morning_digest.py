"""Spoken daily digest and local deadline monitor."""
from __future__ import annotations
import argparse
import asyncio
import io
import os
import time
import unicodedata
from datetime import datetime
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from project_store import ProjectStore
from .google_daily import GoogleDailyConnector


def make_agent(store: ProjectStore) -> "MorningDigestAgent":
    connector = GoogleDailyConnector()
    return MorningDigestAgent(store, google=connector if connector.client_file.exists() else None)


def _plain(text: str) -> str:
    """Make the digest safe to print and safe to speak.

    Live failure: one email subject carried an emoji, and printing the digest
    raised UnicodeEncodeError under the Windows cp1252 console encoding — which
    killed the morning briefing for the whole day. Kokoro cannot speak emoji
    either, so the briefing is normalized here rather than at every call site.

    Two passes, so behaviour does not depend on which console happens to be
    attached:
      1. drop symbol/pictographic characters (emoji, dingbats, private-use) —
         they are never speakable and never load-bearing;
      2. drop anything this process's stdout cannot encode — real letters in
         other scripts survive wherever the terminal supports them.
    """
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)

    dropped = ("So", "Sk", "Cs", "Co", "Cn")
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in dropped:
            continue
        if category == "Cc" and ch not in "\n\t":
            continue
        kept.append(ch)
    text = "".join(kept)

    encoding = (getattr(sys.stdout, "encoding", None) or "utf-8")
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(encoding, errors="ignore").decode(encoding, errors="ignore")
    return " ".join(text.split())


class MorningDigestAgent:
    def __init__(self, project_store: ProjectStore, google=None, speaker=None):
        self.project_store, self.google, self.speaker = project_store, google, speaker

    async def generate_digest(self) -> str:
        sections = []
        if self.google:
            try:
                data = await asyncio.to_thread(self.google.fetch_daily)
                for key, label in (("email", "Email"), ("calendar", "Calendar"), ("tasks", "Tasks")):
                    items = data.get(key, [])
                    sections.append(label + ": " + ("; ".join(map(str, items)) if items else "nothing needing attention"))
            except Exception as exc:
                sections.append(f"Google data unavailable ({type(exc).__name__}).")
        else:
            sections.append("Google email, calendar, and tasks are not connected.")
        projects = self.project_store.list_projects()
        if projects:
            sections.append("Projects: " + "; ".join(p["name"] for p in projects))
        deadlines = self.project_store.get_proactive_deadlines()
        if deadlines:
            now = datetime.now().astimezone()
            parts = []
            for d in deadlines:
                due = datetime.fromisoformat(d["due_date"])
                if due < now:
                    parts.append(f"{d['title']} for {d['project_name']} is overdue, was due {d['due_date'][:10]}")
                else:
                    parts.append(f"{d['title']} for {d['project_name']} due {d['due_date'][:10]}")
            sections.append("Deadlines needing attention: " + "; ".join(parts))
        else:
            sections.append("No open project deadlines need attention.")
        return _plain(
            f"Good morning. Today is {datetime.now().astimezone():%A, %B %d}. " + " ".join(sections)
        )

    async def speak_digest(self, text: str) -> None:
        text = _plain(text)
        if not text:
            return
        if self.speaker:
            # Speakers get the whole text; they are the test seam and any
            # host-side player, and capping here would hide content from them.
            await asyncio.to_thread(self.speaker, text)
            return
        from jarvis.speech import split_sentences
        from openjarvis.speech.kokoro_tts import KokoroTTSBackend
        import sounddevice as sd
        import soundfile as sf

        backend = KokoroTTSBackend()
        sentences = split_sentences(text)
        # Sentence by sentence: one synthesis call for a whole digest means the
        # owner hears nothing for ~40s while Kokoro grinds through it on CPU.
        for sentence in sentences:
            result = backend.synthesize(sentence, voice_id="af_heart")
            samples, rate = sf.read(io.BytesIO(result.audio), dtype="float32")
            sd.play(samples, rate)
            sd.wait()
        unsaid = len(text) - sum(len(s) for s in sentences)
        if unsaid > 60:
            print(f"[JARVIS] (spoke the opening {len(sentences)} sentence(s); "
                  f"{unsaid} more characters unsaid)", flush=True)

    async def run_morning_cycle(self) -> str:
        digest = await self.generate_digest()
        print(f"[JARVIS] {digest}", flush=True)
        await self.speak_digest(digest)
        return digest


async def serve() -> None:
    store = ProjectStore()
    agent = make_agent(store)
    morning_time = os.environ.get("JARVIS_MORNING_TIME", "08:00")
    last_digest_day = None
    print(f"JARVIS digest scheduler active; daily time {morning_time}.", flush=True)
    while True:
        now = datetime.now().astimezone()
        upcoming = store.get_proactive_deadlines()
        current_ids = {d["id"] for d in upcoming}
        # Alert state is persisted, so restarting the scheduler does not
        # re-speak every reminder the owner already heard.
        already = store.alerted_deadline_ids()
        new = [d for d in upcoming if d["id"] not in already]
        if new:
            await agent.speak_digest("Reminder: " + "; ".join(
                f"{d['title']} for {d['project_name']} is due {d['due_date']}" for d in new))
            for d in new:
                store.mark_deadline_alerted(d["id"])
        for gone in already - current_ids:
            store.clear_deadline_alert(gone)
        # Compare with >= so a laptop asleep at 08:00 still gets the briefing
        # when it wakes, instead of skipping the day on an exact-minute miss.
        if now.strftime("%H:%M") >= morning_time and now.date() != last_digest_day:
            await agent.run_morning_cycle()
            last_digest_day = now.date()
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Speak the digest now")
    args = parser.parse_args()
    if args.once:
        asyncio.run(make_agent(ProjectStore()).run_morning_cycle())
    else:
        asyncio.run(serve())
