"""JARVIS Phase 1 — local voice loop driver.

    wake word ("hey jarvis", openWakeWord)
      -> record until Silero-VAD sees end-of-speech
      -> transcribe (faster-whisper small, int8, CPU)
      -> route: local qwen3:1.7b via Ollama  |  Agnes agnes-3.0-flash (escalation)
      -> speak reply (Kokoro-82M via OpenJarvis's KokoroTTSBackend)

Console input (typed lines) works in parallel with the mic. Ctrl-C exits.

Usage (run with the OpenJarvis venv, from this folder):
  %LOCALAPPDATA%\\OpenJarvis\\src\\.venv\\Scripts\\python.exe jarvis_voice_loop.py
  ... jarvis_voice_loop.py --no-mic         # typed input only
  ... jarvis_voice_loop.py --cloud "q"      # force cloud for one question
  ... jarvis_voice_loop.py --self-test      # automated done-test (no mic)

Agnes credentials are read from .env in this folder at runtime — never
hard-coded here or in any committed file.
"""

from __future__ import annotations

import argparse
import io
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
VENV = os.environ.get("OPENJARVIS_HOME", str(HERE / "openjarvis_runtime"))
if os.path.isdir(os.path.join(VENV, "src")):
    sys.path.insert(0, os.path.join(VENV, "src"))

LOCAL_MODEL = "qwen3:1.7b"
AGNES_MODEL = "agnes-3.0-flash"
WAKE_WORD = "hey jarvis"
TTS_VOICE = "af_heart"
SAMPLE_RATE = 16000
OWW_FRAME = 1280  # 80 ms of 16 kHz audio
MAX_UTTERANCE_S = 30

SYSTEM_PROMPT = (
    "You are JARVIS, a personal voice assistant. Answer concisely, in one to "
    "three plain sentences unless asked to elaborate. No markdown, no bullet "
    "lists — your reply will be spoken aloud."
)

# Explicit escalation triggers (design §6.4: user request is a first-class trigger).
_ESCALATE_RE = re.compile(r"\b(think hard|think deeply|escalate|use the cloud|use agnes|cloud only)\b", re.I)


def load_env_file(path: Path) -> dict[str, str]:
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def say(msg: str) -> None:
    print(f"  [jarvis] {msg}", flush=True)


class ConsoleFeed:
    """Background thread: typed lines become queued questions."""

    def __init__(self) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        while True:
            try:
                line = input("  [you]    > ")
            except EOFError:
                return
            if line.strip():
                self.q.put_nowait(line.strip())


class Router:
    """Local-first routing with transparent cloud escalation."""

    def __init__(self, local_engine, cloud_engine, cloud_model: str, local_model: str) -> None:
        self.local_engine = local_engine
        self.cloud_engine = cloud_engine
        self.cloud_model = cloud_model
        self.local_model = local_model

    def answer(self, question: str, force_cloud: bool = False) -> tuple[str, str]:
        """Return (answer_text, engine_used). Transparent fallback: a local
        failure escalates to cloud and says so."""
        from openjarvis.core.types import Message, Role

        messages = [
            Message(role=Role.SYSTEM, content=SYSTEM_PROMPT),
            Message(role=Role.USER, content=question),
        ]
        use_cloud = force_cloud or _ESCALATE_RE.search(question) is not None
        if use_cloud:
            res = self.cloud_engine.generate(messages, model=self.cloud_model, max_tokens=400, temperature=0.3)
            return _content_of(res), "cloud"
        try:
            res = self.local_engine.generate(messages, model=self.local_model, max_tokens=200, temperature=0.3)
            return _content_of(res), "local"
        except Exception as exc:  # engine down / network blip -> escalate
            say(f"local engine failed ({exc.__class__.__name__}); escalating to cloud…")
            res = self.cloud_engine.generate(messages, model=self.cloud_model, max_tokens=400, temperature=0.3)
            return "Local model was unavailable, so I asked the cloud engine instead. " + _content_of(res), "cloud (fallback)"


def _content_of(res) -> str:
    """Extract reply text from OpenJarvis engine result shapes."""
    if isinstance(res, dict):
        if "content" in res:
            return str(res["content"]).strip()
        for k in ("text", "output", "answer"):
            if res.get(k):
                return str(res[k]).strip()
        if "choices" in res and res["choices"]:
            return str(res["choices"][0].get("message", {}).get("content", "")).strip()
    return str(res)


class TTS:
    def __init__(self) -> None:
        from openjarvis.speech.kokoro_tts import KokoroTTSBackend

        self.backend = KokoroTTSBackend()

    def speak(self, text: str) -> None:
        import soundfile as sf
        import sounddevice as sd

        from jarvis.speech import split_sentences

        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        # Sentence by sentence, so a long answer does not become a long
        # silence: Kokoro on this CPU synthesizes ~1.5-3x slower than realtime.
        for sentence in split_sentences(text):
            res = self.backend.synthesize(sentence, voice_id=TTS_VOICE)
            audio, sr = sf.read(io.BytesIO(res.audio), dtype="float32")
            sd.play(audio, sr)
            sd.wait()


class WakeWord:
    def __init__(self, threshold: float = 0.4) -> None:
        import openwakeword

        self.model = openwakeword.Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
        self.threshold = threshold

    def detect(self, frame: np.ndarray) -> bool:
        scores = self.model.predict(frame)
        return scores.get(WAKE_WORD, 0.0) >= self.threshold


def capture_until_end(stream, vad_model) -> np.ndarray | None:
    """Record from the wake moment until Silero VAD confirms end-of-speech.

    Runs get_speech_timestamps over the growing buffer every 800 ms; a
    segment only closes once enough trailing silence has accumulated, which
    is what makes this a real end-of-speech detector rather than a timer.
    """
    from silero_vad import get_speech_timestamps
    import torch

    tail_s = 1.2  # silence required to close a segment (relaxed from 0.6 for accented/deliberate speech)
    chunks: list[np.ndarray] = []
    deadline = time.time() + MAX_UTTERANCE_S
    frame_s = 512
    pad_lead = int(0.25 * SAMPLE_RATE)
    pad_trail = int(0.30 * SAMPLE_RATE)
    while time.time() < deadline:
        data, _ = stream.read(frame_s, exception=False)
        chunks.append(np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0)
        buf = np.concatenate(chunks)
        if buf.size < 16000:  # need ~1s before VAD can see a closed segment
            continue
        segs = __import__("silero_vad").get_speech_timestamps(
            torch.from_numpy(buf), vad_model, sampling_rate=SAMPLE_RATE,
            threshold=0.5, min_silence_duration_ms=800,
        )
        if segs:
            end = int(segs[-1]["end"])
            if len(buf) - end >= int(tail_s * SAMPLE_RATE):
                start = max(0, int(segs[0]["start"]) - pad_lead)
                slice_end = min(len(buf), end + pad_trail)
                say(f"VAD end-of-speech: {start / SAMPLE_RATE:.1f}s–{slice_end / SAMPLE_RATE:.1f}s")
                return buf[start:slice_end]
    audio = np.concatenate(chunks)
    energy = np.abs(audio).mean()
    return audio if energy > 0.005 else None


class Transcriber:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        say(f"loading faster-whisper 'small' (int8, CPU)…")
        t0 = time.time()
        self.model = WhisperModel("small", device="cpu", compute_type="int8")
        say(f"whisper ready in {time.time() - t0:.1f}s")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self.model.transcribe(
            audio,
            beam_size=5,
            best_of=5,
            condition_on_previous_text=False,
            initial_prompt="JARVIS, personal voice assistant. System commands and conversational queries in English.",
            language="en",
            temperature=(0.0, 0.2, 0.4),
        )
        return " ".join(s.text.strip() for s in segments).strip()


def build_components():
    say("loading engines & TTS…")
    from openjarvis.engine.ollama import OllamaEngine
    from openjarvis.engine.openai_compat_engines import OpenAICompatEngine

    cfg = load_env_file(HERE / ".env")
    if not cfg.get("AGNES_API_KEY"):
        raise SystemExit("AGNES_API_KEY missing from .env — cannot configure cloud escalation")
    base = cfg.get("AGNES_BASE_URL", "").rstrip("/").removesuffix("/v1")  # engine re-appends /v1

    local_engine = OllamaEngine()
    cloud_engine = OpenAICompatEngine(host=base, api_key=cfg["AGNES_API_KEY"])
    router = Router(local_engine, cloud_engine, AGNES_MODEL, LOCAL_MODEL)

    tts = TTS()
    wake = WakeWord()
    stt = Transcriber()
    from silero_vad import load_silero_vad

    vad_model = load_silero_vad()
    say("all components loaded")
    return router, tts, wake, stt, vad_model


def process_question(q: str, router, tts, force_cloud=False) -> None:
    from phase3 import dispatch_voice_request

    try:
        if dispatch_voice_request(q):
            return
    except Exception as exc:
        say(f"Phase 3 action failed ({exc.__class__.__name__}): {exc}")
        tts.speak("I couldn't complete that Phase 3 action. Check the console for details.")
        return
    say(f"question: {q}")
    t0 = time.time()
    answer, engine = router.answer(q, force_cloud=force_cloud)
    say(f"reply via {engine} ({time.time() - t0:.1f}s): {answer}")
    tts.speak(answer)


def self_test(router, tts) -> None:
    """Automated done-test: synthesize a question, transcribe it with
    Whisper, route it, speak the answer — full pipeline, no mic."""
    say("SELF-TEST: synthesizing test question with Kokoro…")
    import soundfile as sf

    stt = Transcriber()  # load Whisper once for the whole test
    q_local = "What time is it, roughly?"
    q_hard = "Think hard and compare the economic impact of the 1973 oil embargo to the 2022 energy shock."
    for q in (q_local, q_hard):
        r = tts.backend.synthesize(q, voice_id="am_michael")  # male voice to differ from replies
        pcm, sr = sf.read(io.BytesIO(r.audio), dtype="float32")
        heard = stt.transcribe(pcm)
        say(f"whisper heard: {heard!r}")
        process_question(heard, router, tts, force_cloud="think hard" in q.lower())
    say("SELF-TEST complete")


def main() -> None:
    ap = argparse.ArgumentParser(description="JARVIS Phase 1 voice loop")
    ap.add_argument("--no-mic", action="store_true", help="typed input only")
    ap.add_argument("--cloud", metavar="Q", help="answer one question via cloud, then exit")
    ap.add_argument("--local", metavar="Q", help="answer one question via local, then exit")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    router, tts, wake, stt, vad_model = build_components()

    if args.local:
        process_question(args.local, router, tts)
        return
    if args.cloud:
        process_question(args.cloud, router, tts, force_cloud=True)
        return
    if args.self_test:
        self_test(router, tts)
        return

    console = ConsoleFeed()
    import sounddevice as sd

    say("listening — say 'hey jarvis', or type a question (console). Ctrl-C to quit.")
    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=OWW_FRAME) as stream:
        while True:
            # Typed input takes priority so the loop stays responsive.
            try:
                q = console.q.get_nowait()
                process_question(q, router, tts)
                continue
            except queue.Empty:
                pass
            frame = np.frombuffer(stream.read(OWW_FRAME, exception=False)[0], dtype=np.int16).astype(np.float32) / 32768.0
            if wake.detect(frame):
                say("wake word detected — listening for your question…")
                audio = capture_until_end(stream, vad_model)
                if audio is None:
                    say("(no speech — back to listening)")
                    continue
                q = stt.transcribe(audio)
                if not q:
                    say("(could not understand that — back to listening)")
                    continue
                process_question(q, router, tts)


if __name__ == "__main__":
    main()
