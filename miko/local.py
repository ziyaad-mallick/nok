"""Fully local agent: no API key, no account, no network.

Implements the same three-method interface as RealtimeSession (`run`, `levels`,
`request_stop`) so `app.Miko` cannot tell them apart. Pick one at startup.

    triple-knock
        |
        +-- screenshot (screen.py, already downscaled for a model)
        +-- record until you stop talking      energy endpointing
        +-- transcribe                         faster-whisper, local
        +-- answer, with the screenshot        Ollama vision model, local
        +-- speak                              Windows SAPI via pyttsx3
        |
    knock again -> stop

Slower than speech-to-speech (roughly 3-8s per turn against ~1.5s) because it is
four sequential stages rather than one duplex stream. In exchange it runs on a
laptop with no credentials and costs nothing, forever.

Endpointing is energy-based on purpose. v1 used Silero here and it scored real
speech at ~0.001 on this machine's microphone, which is how the `no_speech` bug
happened. A trailing-noise-floor energy gate is cruder and far harder to break.

TTS is rendered to a WAV and played through the same AudioPlayer the realtime
path uses, rather than pyttsx3's own blocking playback. That buys real output
amplitude for the overlay and real barge-in, instead of a synthetic envelope.
"""
import asyncio
import os
import re
import tempfile
import time
import wave
from dataclasses import dataclass, field

import numpy as np

from .logs import log_event
from .realtime import AudioPlayer

SAMPLE_RATE = 16000          # what whisper wants
CHUNK_MS = 30

# Endpointing
CALIBRATE_S = 0.35           # sample the room before listening for speech
NOISE_MULT = 3.0             # speech must exceed noise floor by this factor
MIN_SPEECH_S = 0.35          # ignore clicks and door bumps
SILENCE_HANG_S = 0.9         # silence this long after speech ends the turn
MAX_UTTERANCE_S = 15.0       # hard cap so a stuck mic cannot hang the turn
START_TIMEOUT_S = 6.0        # give up if nobody speaks

OLLAMA_URL = os.environ.get("MIKO_OLLAMA_URL", "http://localhost:11434")
VISION_MODEL = os.environ.get("MIKO_VISION_MODEL", "moondream")
TEXT_MODEL = os.environ.get("MIKO_TEXT_MODEL", "")

WHISPER_SIZE = os.environ.get("MIKO_WHISPER", "base.en")

SYSTEM_PROMPT = (
    "You are MIKO, a desktop assistant summoned by knocking on the laptop. "
    "You are shown the user's screen. Answer in one or two short sentences, "
    "spoken aloud, so no markdown and no lists. Be direct."
)


@dataclass
class LocalStats:
    turns: int = 0
    first_audio_latency: float = 0.0
    errors: list = field(default_factory=list)
    transcript: str = ""
    reply: str = ""


# -- microphone -----------------------------------------------------------

class Recorder:
    """Blocking capture with energy endpointing. Runs in a worker thread."""

    def __init__(self, device=None, samplerate=SAMPLE_RATE):
        self.device = device
        self.samplerate = samplerate
        self.level = 0.0
        self._stop = False

    def stop(self):
        self._stop = True

    def record(self):
        """Return float32 mono audio of one utterance, or None."""
        import sounddevice as sd

        block = int(self.samplerate * CHUNK_MS / 1000)
        frames = []
        with sd.InputStream(samplerate=self.samplerate, blocksize=block,
                            device=self.device, channels=1, dtype="float32") as stream:
            # 1. learn the room
            noise = []
            for _ in range(int(CALIBRATE_S * 1000 / CHUNK_MS)):
                data, _ = stream.read(block)
                noise.append(float(np.sqrt(np.mean(data ** 2))))
            floor = float(np.median(noise)) if noise else 0.0
            gate = max(floor * NOISE_MULT, 0.006)
            log_event("endpoint_gate", noise_floor=round(floor, 5), gate=round(gate, 5))

            # 2. wait for speech, then capture until it stops
            started = None
            last_voice = None
            t0 = time.monotonic()
            while not self._stop:
                data, _ = stream.read(block)
                mono = data[:, 0]
                rms = float(np.sqrt(np.mean(mono ** 2)))
                self.level = rms
                now = time.monotonic()

                if started is None:
                    if rms > gate:
                        started = now
                        last_voice = now
                        frames.append(mono.copy())
                    elif now - t0 > START_TIMEOUT_S:
                        log_event("endpoint_no_speech", waited=round(now - t0, 2))
                        return None
                    continue

                frames.append(mono.copy())
                if rms > gate:
                    last_voice = now
                elif now - last_voice > SILENCE_HANG_S:
                    break
                if now - started > MAX_UTTERANCE_S:
                    break

        self.level = 0.0
        if started is None or not frames:
            return None
        audio = np.concatenate(frames)
        if audio.size / self.samplerate < MIN_SPEECH_S:
            log_event("endpoint_too_short", seconds=round(audio.size / self.samplerate, 2))
            return None
        log_event("endpoint_ok", seconds=round(audio.size / self.samplerate, 2))
        return audio


# -- speech to text -------------------------------------------------------

_whisper = None


def transcribe(audio, samplerate=SAMPLE_RATE):
    """Local faster-whisper. The model is cached across turns and sessions."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log_event("whisper_loading", size=WHISPER_SIZE)
        _whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
    segments, _info = _whisper.transcribe(
        audio, language="en", beam_size=1, vad_filter=False,
    )
    return " ".join(s.text for s in segments).strip()


# -- the model ------------------------------------------------------------

def _ollama_models():
    import requests
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def pick_model():
    """Prefer a vision model; fall back to any text model that is installed."""
    try:
        names = _ollama_models()
    except Exception as exc:
        log_event("ollama_unreachable", error=str(exc))
        return None, False

    def match(want):
        for n in names:
            if n.split(":")[0] == want.split(":")[0]:
                return n
        return None

    if TEXT_MODEL:
        got = match(TEXT_MODEL)
        if got:
            return got, False
    got = match(VISION_MODEL)
    if got:
        return got, True
    for known in ("llava", "llama3.2-vision", "qwen2.5vl", "bakllava", "minicpm-v"):
        got = match(known)
        if got:
            return got, True
    return (names[0], False) if names else (None, False)


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_thinking(text):
    """Remove chain-of-thought blocks a reasoning model emits inline.

    Reasoning models (qwen3, deepseek-r1 and friends) spend their token budget
    inside <think>...</think> before answering. Left alone, a modest num_predict
    is consumed entirely by hidden reasoning and the user hears silence. Strip
    the block; if the budget ran out mid-thought and there is no closing tag,
    drop everything after the opener rather than speaking raw reasoning aloud.
    """
    text = _THINK_RE.sub("", text or "")
    if "<think>" in text.lower():
        text = text[:text.lower().index("<think>")]
    return text.strip()


def ask(model, prompt, image_b64=None, timeout=120):
    """One Ollama generate call. `image_b64` is raw base64, no data: prefix."""
    import requests

    payload = {
        "model": model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        # think=false is honoured by reasoning models and ignored by the rest.
        "think": False,
        # Headroom so that a model which reasons anyway still has budget left
        # to actually answer after the <think> block is stripped.
        "options": {"temperature": 0.4, "num_predict": 512},
    }
    if image_b64:
        payload["images"] = [image_b64]

    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
    if r.status_code == 400 and "think" in r.text.lower():
        payload.pop("think", None)  # older ollama rejects the field outright
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
    r.raise_for_status()
    return strip_thinking(r.json().get("response"))


# -- text to speech -------------------------------------------------------

def synthesize(text):
    """Render speech to a WAV and return (int16 samples, samplerate).

    Rendered rather than spoken directly so playback goes through AudioPlayer:
    that gives the overlay real output amplitude and makes barge-in possible.
    """
    import pyttsx3

    path = os.path.join(tempfile.gettempdir(), f"miko-tts-{os.getpid()}.wav")
    engine = pyttsx3.init()
    engine.setProperty("rate", 185)
    engine.save_to_file(text, path)
    engine.runAndWait()
    try:
        engine.stop()
    except Exception:
        pass

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("TTS produced no audio")
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            pcm = pcm.reshape(-1, w.getnchannels())[:, 0].copy()
    try:
        os.remove(path)
    except OSError:
        pass
    return pcm, rate


# -- session --------------------------------------------------------------

class LocalSession:
    """One knock-to-close conversation, entirely on this machine."""

    def __init__(self, *, device=None, on_state=None, attach_screen=True,
                 model=None, max_turns=8):
        self.device = device
        self.on_state = on_state or (lambda s, d="": None)
        self.attach_screen = attach_screen
        self.max_turns = max_turns

        self.recorder = Recorder(device=device)
        self.player = AudioPlayer(samplerate=22050)
        self.stats = LocalStats()

        self._model = model
        self._vision = False
        self._stop = False
        self._woke_at = 0.0

    # -- interface (matches RealtimeSession) -----------------------------

    def levels(self):
        return self.recorder.level, self.player.level

    def request_stop(self):
        self._stop = True
        self.recorder.stop()
        self.player.flush()

    async def interrupt(self):
        self.player.flush()

    async def run(self, opening_prompt=None):
        self._woke_at = time.perf_counter()
        if self._model is None:
            self._model, self._vision = await asyncio.to_thread(pick_model)
        if self._model is None:
            self.on_state("ERROR", "no local model - is ollama running?")
            self.stats.errors.append("ollama unavailable")
            await asyncio.sleep(1.5)
            return self.stats

        log_event("local_session", model=self._model, vision=self._vision)
        try:
            for _ in range(self.max_turns):
                if self._stop:
                    break
                if not await self._turn():
                    break
        finally:
            self.player.close()
        return self.stats

    # -- one turn --------------------------------------------------------

    async def _turn(self):
        # Grab the screen first so it is ready the moment speech ends.
        shot = (asyncio.create_task(asyncio.to_thread(self._grab))
                if (self.attach_screen and self._vision) else None)

        self.on_state("LISTENING", "speak now")
        audio = await asyncio.to_thread(self.recorder.record)
        if audio is None or self._stop:
            if shot:
                shot.cancel()
            return False

        self.on_state("THINKING", "transcribing")
        text = await asyncio.to_thread(transcribe, audio)
        log_event("transcript", text=text)
        if not text:
            self.on_state("THINKING", "didn't catch that")
            await asyncio.sleep(0.6)
            return True
        self.stats.transcript = text

        image = await shot if shot else None
        self.on_state("THINKING", text)
        try:
            reply = await asyncio.to_thread(ask, self._model, text, image)
        except Exception as exc:
            log_event("ollama_failed", error=str(exc))
            self.on_state("ERROR", "the local model failed")
            self.stats.errors.append(str(exc))
            await asyncio.sleep(1.2)
            return False

        if self._stop:
            return False
        if not reply:
            reply = "I don't have an answer for that."
        self.stats.reply = reply
        log_event("reply", text=reply[:300])

        await self._speak(reply)
        self.stats.turns += 1
        return not self._stop

    async def _speak(self, text):
        try:
            pcm, rate = await asyncio.to_thread(synthesize, text)
        except Exception as exc:
            log_event("tts_failed", error=str(exc))
            self.on_state("ERROR", "speech failed")
            await asyncio.sleep(1.0)
            return

        if not self.stats.first_audio_latency:
            self.stats.first_audio_latency = time.perf_counter() - self._woke_at
            log_event("first_audio", seconds=round(self.stats.first_audio_latency, 2))

        self.player.samplerate = rate
        self.on_state("SPEAKING", text)
        # Feed in slices so the overlay's level tracks the speech envelope and
        # a knock can cut it off part-way.
        step = max(1, rate // 20)
        for i in range(0, pcm.size, step):
            if self._stop:
                self.player.flush()
                return
            self.player.add(pcm[i:i + step].tobytes())
            await asyncio.sleep(step / rate * 0.85)
        await asyncio.sleep(0.35)

    def _grab(self):
        from . import screen
        try:
            return screen.grab_jpeg_b64()
        except Exception as exc:
            log_event("screenshot_failed", error=str(exc))
            return None
