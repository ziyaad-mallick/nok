"""The agent: a live voice conversation with GPT that can see the screen.

Replaces v1's Silero VAD -> faster-whisper -> `codex` subprocess chain, which
measured 17-38s per turn and regularly scored real speech as `no_speech`. One
WebSocket does all three jobs now, streaming, at conversational latency.

Shape of a session
------------------

    triple-knock
        |
        +-- screenshot capture starts IMMEDIATELY, in parallel with the mic
        |   (by the time the user finishes asking, the image is already
        |    uploaded -- this is the whole trick behind "explains your screen")
        |
        +-- connect -> session.update -> stream mic -> play audio back
        |
        +-- server_vad handles turn-taking, so it is a real back-and-forth
        |   conversation rather than one-shot push-to-talk
        |
    knock again (or timeout) -> close

Turn detection is server-side VAD rather than manual commit: the knock opens
the session, and within it the user should be able to just talk. Manual mode
would force a gesture to end every single turn.

Audio is PCM16/24kHz/mono in both directions. Errors arrive as *events*, not
exceptions -- the SDK does not raise on API-level errors, so `error` events
must be handled explicitly or failures are silent.
"""
import asyncio
import base64
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .logs import log_event

SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_MS = 20
PLAYBACK_BLOCK_MS = 50

MODEL = "gpt-realtime"
VOICE = "marin"

INSTRUCTIONS = (
    "You are NOK, a desktop voice assistant summoned by knocking on the laptop. "
    "You can see the user's screen when a screenshot is attached. "
    "Be terse and conversational -- you are being spoken to, not read. "
    "Answer in one or two sentences unless asked to go deeper. "
    "Never narrate what you are about to do; just do it. "
    "If you cannot see something the user is referring to, say so plainly."
)

TOOLS = [
    {
        "type": "function",
        "name": "take_screenshot",
        "description": (
            "Capture the user's screen again. Use when the user refers to "
            "something that may have changed since the last screenshot."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "media_control",
        "description": "Control media playback on the user's machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["play", "pause", "next", "prev", "mute"],
                }
            },
            "required": ["command"],
        },
    },
]


# -- playback -------------------------------------------------------------

class AudioPlayer:
    """Callback-driven output stream with a queue.

    A callback stream (rather than one `sd.play()` per delta) avoids per-chunk
    device-open latency and the clicks that come with it. On an empty queue it
    zero-pads instead of blocking, which keeps the Windows audio driver happy
    when network jitter starves it.

    `frames_played` is tracked because barge-in needs to tell the server how
    much audio the user actually HEARD -- otherwise the model believes its
    whole interrupted answer was delivered.
    """

    def __init__(self, samplerate=SAMPLE_RATE, channels=CHANNELS):
        self.samplerate = samplerate
        self._queue = []
        self._lock = threading.Lock()
        self._stream = None
        self._playing = False
        self.frames_played = 0
        self.level = 0.0  # RMS of the last block, for the overlay

    def _callback(self, outdata, frames, time_info, status):
        with self._lock:
            data = np.empty(0, dtype=np.int16)
            while len(data) < frames and self._queue:
                item = self._queue.pop(0)
                need = frames - len(data)
                data = np.concatenate((data, item[:need]))
                if len(item) > need:
                    self._queue.insert(0, item[need:])
            if len(data) < frames:
                data = np.concatenate(
                    (data, np.zeros(frames - len(data), dtype=np.int16))
                )
            else:
                self.frames_played += frames
        self.level = float(np.sqrt(np.mean((data / 32768.0) ** 2))) if data.size else 0.0
        outdata[:] = data.reshape(-1, 1)

    def _ensure_stream(self):
        if self._stream is not None:
            return
        import sounddevice as sd

        self._stream = sd.OutputStream(
            callback=self._callback,
            samplerate=self.samplerate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=int(self.samplerate * PLAYBACK_BLOCK_MS / 1000),
        )

    def add(self, pcm16: bytes) -> None:
        self._ensure_stream()
        with self._lock:
            self._queue.append(np.frombuffer(pcm16, dtype=np.int16))
        if not self._playing:
            self._playing = True
            self._stream.start()

    def played_ms(self) -> int:
        return int(self.frames_played / self.samplerate * 1000)

    def flush(self) -> None:
        """Drop queued audio without closing the device (barge-in)."""
        with self._lock:
            self._queue = []
        self.level = 0.0

    def close(self) -> None:
        self.flush()
        self._playing = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# -- microphone -----------------------------------------------------------

class MicStream:
    """Dedicated 24kHz int16 capture for the agent.

    Opened only while a session is live, and the knock detector's own stream is
    paused for the duration -- two input streams on one device works under
    WASAPI shared mode but adds jitter to both, and the detector must not hear
    the agent's voice anyway.

    Falls back to the device's native rate + soxr resampling if the hardware
    refuses a direct 24kHz open.
    """

    def __init__(self, device=None, samplerate=SAMPLE_RATE):
        self.device = device
        self.target_rate = samplerate
        self.native_rate = samplerate
        self._stream = None
        self._q = queue.Queue()
        self.level = 0.0

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0] if indata.ndim > 1 else indata
        self._q.put(mono.copy())

    def start(self):
        import sounddevice as sd

        blocksize = int(self.target_rate * CHUNK_MS / 1000)
        try:
            self._stream = sd.InputStream(
                samplerate=self.target_rate, blocksize=blocksize,
                device=self.device, channels=1, dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            self.native_rate = self.target_rate
        except Exception as exc:
            log_event("mic_24k_open_failed", error=str(exc))
            info = sd.query_devices(self.device, "input")
            self.native_rate = int(info["default_samplerate"])
            self._stream = sd.InputStream(
                samplerate=self.native_rate,
                blocksize=int(self.native_rate * CHUNK_MS / 1000),
                device=self.device, channels=1, dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            log_event("mic_fallback_rate", rate=self.native_rate)

    def read_pcm16(self, timeout=0.1):
        """Next chunk as PCM16 bytes at 24kHz, or None if nothing is ready."""
        try:
            chunk = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        self.level = float(np.sqrt(np.mean(chunk ** 2))) if chunk.size else 0.0
        if self.native_rate != self.target_rate:
            import soxr
            chunk = soxr.resample(chunk, self.native_rate, self.target_rate)
        return np.clip(chunk * 32767, -32768, 32767).astype(np.int16).tobytes()

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# -- session --------------------------------------------------------------

@dataclass
class SessionStats:
    turns: int = 0
    first_audio_latency: float = 0.0
    errors: list = field(default_factory=list)


class RealtimeSession:
    """One knock-to-close conversation.

    `on_state(state, detail)` drives the overlay. `levels()` exposes live mic
    and output RMS so the overlay's rings can be driven by real audio rather
    than a decorative animation.
    """

    def __init__(self, *, api_key=None, device=None, on_state=None,
                 model=MODEL, voice=VOICE, attach_screen=True):
        self.api_key = api_key
        self.device = device
        self.model = model
        self.voice = voice
        self.attach_screen = attach_screen
        self.on_state = on_state or (lambda s, d="": None)

        self.player = AudioPlayer()
        self.mic = MicStream(device=device)
        self.stats = SessionStats()

        self._conn = None
        self._stop = asyncio.Event()
        self._responding = False
        self._last_item_id = None
        self._woke_at = 0.0
        self._got_first_audio = False

    # -- public ----------------------------------------------------------

    def levels(self):
        """(mic_rms, output_rms) for the overlay."""
        return self.mic.level, self.player.level

    def request_stop(self):
        self._stop.set()

    async def run(self, opening_prompt=None):
        """Own the whole session. Returns when the user closes it or it errors."""
        from openai import AsyncOpenAI

        self._woke_at = time.perf_counter()
        client = AsyncOpenAI(api_key=self.api_key) if self.api_key else AsyncOpenAI()

        # Fire the screenshot NOW, concurrently with connecting — it is the
        # slowest thing in the wake path and nothing else depends on it.
        shot_task = (
            asyncio.create_task(asyncio.to_thread(self._grab))
            if self.attach_screen else None
        )

        self.on_state("LISTENING", "connecting")
        try:
            async with client.realtime.connect(model=self.model) as conn:
                self._conn = conn
                await self._configure(conn)
                self.mic.start()

                if shot_task is not None:
                    await self._attach_screen(conn, shot_task, opening_prompt)

                pump = asyncio.create_task(self._pump_mic(conn))
                try:
                    await self._consume(conn)
                finally:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
        except Exception as exc:
            log_event("realtime_session_error", error=repr(exc))
            self.stats.errors.append(repr(exc))
            self.on_state("ERROR", str(exc))
            raise
        finally:
            self.mic.close()
            self.player.close()
            self._conn = None
        return self.stats

    async def interrupt(self):
        """Barge-in: stop the model, truncate history, drop queued audio.

        All three are required. Cancelling without truncating leaves the model
        believing the user heard its whole answer; flushing without cancelling
        leaves the server still generating.
        """
        if not self._responding or self._conn is None:
            return
        played = self.player.played_ms()
        self.player.flush()
        try:
            await self._conn.send({"type": "response.cancel"})
            if self._last_item_id:
                await self._conn.send({
                    "type": "conversation.item.truncate",
                    "item_id": self._last_item_id,
                    "content_index": 0,
                    "audio_end_ms": played,
                })
        except Exception as exc:
            log_event("interrupt_failed", error=str(exc))
        self._responding = False
        log_event("interrupted", played_ms=played)

    # -- internals -------------------------------------------------------

    def _grab(self):
        from . import screen
        try:
            return screen.data_url()
        except Exception as exc:
            log_event("screenshot_failed", error=str(exc))
            return None

    async def _configure(self, conn):
        await conn.session.update(session={
            "type": "realtime",
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    # Server VAD keeps this a conversation: the knock opens the
                    # session, then the user just talks. interrupt_response
                    # gives barge-in for free when they talk over the model.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "voice": self.voice,
                },
            },
            "tools": TOOLS,
            "tool_choice": "auto",
        })

    async def _attach_screen(self, conn, shot_task, opening_prompt):
        url = await shot_task
        if url is None:
            return
        content = [{"type": "input_image", "image_url": url}]
        if opening_prompt:
            content.insert(0, {"type": "input_text", "text": opening_prompt})
        await conn.conversation.item.create(item={
            "type": "message", "role": "user", "content": content,
        })
        log_event("screen_attached", bytes=len(url))
        if opening_prompt:
            await conn.response.create()

    async def _pump_mic(self, conn):
        """Stream mic chunks until cancelled. Blocking reads go to a thread so
        they never stall the event loop."""
        while not self._stop.is_set():
            try:
                chunk = await asyncio.to_thread(self.mic.read_pcm16)
            except Exception as exc:
                # The read itself can throw (resampler, device yanked). This
                # task is only awaited in run()'s finally, so an unguarded
                # raise here left the overlay sitting on "your turn" with a
                # dead microphone until the 120s idle timeout.
                log_event("mic_read_failed", error=str(exc))
                self.on_state("ERROR", "microphone stopped")
                return
            if chunk is None:
                continue
            try:
                await conn.input_audio_buffer.append(
                    audio=base64.b64encode(chunk).decode("ascii")
                )
            except Exception as exc:
                log_event("mic_append_failed", error=str(exc))
                return

    async def _consume(self, conn):
        async for event in conn:
            if self._stop.is_set():
                break
            await self._handle(conn, event)

    async def _handle(self, conn, event):
        kind = event.type

        if kind == "response.output_audio.delta":
            if not self._got_first_audio:
                self._got_first_audio = True
                self.stats.first_audio_latency = time.perf_counter() - self._woke_at
                log_event("first_audio", seconds=round(self.stats.first_audio_latency, 3))
            self._responding = True
            self._last_item_id = getattr(event, "item_id", self._last_item_id)
            self.on_state("SPEAKING")
            self.player.add(base64.b64decode(event.delta))

        elif kind == "input_audio_buffer.speech_started":
            self.on_state("LISTENING", "user speaking")
            # Server VAD already cancels server-side; drop local audio so the
            # model does not keep talking out of the speakers.
            self.player.flush()

        elif kind == "input_audio_buffer.speech_stopped":
            self.on_state("THINKING")

        elif kind == "response.function_call_arguments.done":
            await self._run_tool(conn, event)

        elif kind == "response.done":
            self._responding = False
            self.stats.turns += 1
            self.on_state("LISTENING", "your turn")

        elif kind == "error":
            err = getattr(event, "error", None)
            msg = getattr(err, "message", str(err))
            log_event("realtime_error", message=msg)
            self.stats.errors.append(msg)
            self.on_state("ERROR", msg)

    async def _run_tool(self, conn, event):
        import json

        name = getattr(event, "name", "")
        call_id = getattr(event, "call_id", None)
        try:
            args = json.loads(getattr(event, "arguments", "") or "{}")
        except ValueError:
            args = {}

        self.on_state("ACTING", name)
        log_event("tool_call", name=name, args=args)

        # A tool that throws used to propagate all the way out of _consume and
        # tear down the whole conversation, so one failed screenshot cost you
        # the session and another triple-knock. Report it to the model instead
        # and let the conversation carry on.
        try:
            if name == "take_screenshot":
                url = await asyncio.to_thread(self._grab)
                if url:
                    await conn.conversation.item.create(item={
                        "type": "message", "role": "user",
                        "content": [{"type": "input_image", "image_url": url}],
                    })
                result = "Screenshot captured." if url else "Screenshot failed."
            elif name == "media_control":
                from .actions import _MEDIA_VK, _tap_vk
                cmd = (args.get("command") or "").lower()
                vk = _MEDIA_VK.get(cmd)
                if vk is None:
                    result = f"Unknown media command: {cmd}"
                else:
                    await asyncio.to_thread(_tap_vk, vk)
                    result = f"Media: {cmd}."
            else:
                result = f"Unknown tool: {name}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("tool_failed", name=name, error=repr(exc))
            result = f"That didn't work: {exc}"

        if call_id:
            await conn.conversation.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            })
        await conn.response.create()
