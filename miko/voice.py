"""Triple-knock -> say a command -> it happens.

Same three-method interface as RealtimeSession and LocalSession, so app.Miko
cannot tell the backends apart.

    knock knock knock
        |
        +-- record until you stop talking     energy endpointing
        +-- transcribe                        faster-whisper, local
        +-- match an intent                   commands.py, pure matching
        +-- do it                             actions.py
        |
    ~2s end to end, offline, free

There is no language model in this path. A local one measured 4.3s per turn and
can invent an action that was never requested; matching is microseconds and can
only return something from a fixed list. For a known command surface the model
buys flexibility nobody asked for and charges latency and risk for it.

An unrecognised phrase is reported as unrecognised. It is never typed into the
focused window on the theory that it might have been dictation — that is how a
stray sentence ends up executed in somebody's terminal.
"""
import asyncio
import time
from dataclasses import dataclass, field

from . import actions, commands
from .local import Recorder, transcribe
from .logs import log_event

MAX_TURNS = 6
CONFIRM_HOLD_S = 0.9


@dataclass
class VoiceStats:
    turns: int = 0
    first_audio_latency: float = 0.0
    errors: list = field(default_factory=list)
    transcript: str = ""
    last_intent: str = ""


def execute(intent, args):
    """Run one matched intent. Returns a user-visible string."""
    if intent == commands.SCREENSHOT:
        from . import screen
        return f"Screenshot saved to {screen.save_png().name}"
    if intent == commands.MEDIA:
        return actions.media(args.get("command", ""), args.get("repeat", 1))
    if intent == commands.OPEN_APP:
        return actions.open_app(args.get("name", ""))
    if intent == commands.TYPE_TEXT:
        return actions.type_text(args.get("text", ""))
    return "I didn't catch a command I know."


class VoiceCommandSession:
    """One knock-to-close run of spoken commands."""

    def __init__(self, *, device=None, on_state=None, max_turns=MAX_TURNS,
                 speak=False, **_ignored):
        self.on_state = on_state or (lambda s, d="": None)
        self.max_turns = max_turns
        self.speak = speak          # off by default: the action IS the feedback
        self.recorder = Recorder(device=device)
        self.stats = VoiceStats()
        self._stop = False

    # -- interface -------------------------------------------------------

    def levels(self):
        return self.recorder.level, 0.0

    def request_stop(self):
        self._stop = True
        self.recorder.stop()

    async def interrupt(self):
        self.request_stop()

    async def run(self, opening_prompt=None):
        t0 = time.perf_counter()
        try:
            for _ in range(self.max_turns):
                if self._stop or not await self._turn():
                    break
        except Exception as exc:
            log_event("voice_session_error", error=repr(exc))
            self.stats.errors.append(repr(exc))
            self.on_state("ERROR", str(exc))
            await asyncio.sleep(1.0)
        self.stats.first_audio_latency = time.perf_counter() - t0
        return self.stats

    # -- one command -----------------------------------------------------

    async def _turn(self):
        self.on_state("LISTENING", "say a command")
        audio = await asyncio.to_thread(self.recorder.record)
        if audio is None or self._stop:
            return False

        self.on_state("THINKING", "transcribing")
        text = await asyncio.to_thread(transcribe, audio)
        log_event("transcript", text=text)
        if not text:
            self.on_state("ERROR", "didn't catch that")
            await asyncio.sleep(CONFIRM_HOLD_S)
            return True

        self.stats.transcript = text
        intent, args = commands.parse(text)
        self.stats.last_intent = intent
        log_event("intent", transcript=text, intent=intent, args=args)

        if intent == commands.UNKNOWN:
            self.on_state("ERROR", f'"{text}" - not a command I know')
            await asyncio.sleep(CONFIRM_HOLD_S + 0.5)
            return True

        self.on_state("ACTING", commands.describe(intent, args))
        try:
            result = await asyncio.to_thread(execute, intent, args)
        except Exception as exc:
            log_event("action_failed", intent=intent, error=str(exc))
            self.on_state("ERROR", str(exc))
            await asyncio.sleep(CONFIRM_HOLD_S)
            return True

        log_event("action_done", intent=intent, result=result)
        self.stats.turns += 1
        if self.speak:
            await self._say(commands.describe(intent, args))
        await asyncio.sleep(CONFIRM_HOLD_S)
        return not self._stop

    async def _say(self, text):
        try:
            from .local import synthesize
            pcm, rate = await asyncio.to_thread(synthesize, text)
            from .realtime import AudioPlayer
            player = AudioPlayer(samplerate=rate)
            player.add(pcm.tobytes())
            await asyncio.sleep(min(pcm.size / rate, 4.0))
            player.close()
        except Exception as exc:
            log_event("tts_failed", error=str(exc))
