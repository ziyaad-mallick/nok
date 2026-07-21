# OpenAI Realtime API — Technical Reference for MIKO

Researched 2026-07-21. Sources verified against `developers.openai.com/api/docs` (current GA docs — the old `platform.openai.com/docs/guides/realtime-api-beta` path is **dead**; the Realtime Beta was deprecated and removed 2026-05-12) and the `openai/openai-python` repo (`main` branch, package version 2.46.0, examples verified by pulling raw source).

Goal: replace MIKO's broken Silero VAD → faster-whisper → `codex` subprocess chain (17-38s latency, frequent `no_speech` misfires) with a direct Realtime API connection that can also see the screen.

---

## 0. TL;DR decisions for today's build

- **Transport:** raw `websockets`, driven through the **official SDK's realtime helper** (`client.realtime.connect(...)`), not hand-rolled `websockets.connect`. It's first-class, small, and battle-tested — use it.
- **Model:** `gpt-realtime` (GA, stable, image input supported). `gpt-realtime-2` / `gpt-realtime-2.1` exist and are newer/cheaper-ish per token but are moving targets released within the last ~10 weeks — **stick with plain `gpt-realtime`** for a 13-hour build unless you hit a specific problem it fixes. `gpt-realtime-mini` is a fallback if you get rate-limited or want cheaper demo runs.
- **Audio:** 24 kHz, mono, PCM16, base64 over the wire. Mic capture at 16k/48k must be resampled to 24k (use `soxr`, already in `requirements.txt`).
- **Turn detection:** Use **manual mode** (`turn_detection: null`) driven by the knock trigger — MIKO is push-to-talk-shaped (triple-knock opens the mic, silence or a second signal closes it), not open-mic. Server VAD config is documented below in case you want auto-detected end-of-utterance while the mic is open.
- **Vision:** Realtime API **does** accept images directly in-session via `conversation.item.create` with an `input_image` content part (base64 data URL). No separate vision-completions hop needed. This was the single biggest open question and it resolved favorably — confirmed via `input_image` content-part support in `gpt-realtime` and `gpt-realtime-2`.
- **Function calling:** standard `tools` array in `session.update`, arguments stream via `response.function_call_arguments.delta/.done` or arrive whole in `response.done`, results go back via `conversation.item.create` with `type: function_call_output`.
- **Interruption:** `response.cancel` + (for WebSocket specifically) `conversation.item.truncate` to tell the server how much audio the user actually heard before cutting it off.
- **Install today:** `pip install "openai[realtime]"` — this pulls in `websockets>=13,<16` as an extra. `requirements.txt` currently has no `openai` or `websockets` entry; both are needed.

---

## 1. Transport

### Recommended: SDK helper over WebSocket

`openai-python` (2.46.0, current as of 2026-07-17) ships `client.realtime.connect(...)` as a first-class async context manager. Under the hood it uses the `websockets` library — it is **not** a thin unofficial wrapper, it's maintained in-tree by OpenAI (`src/openai/resources/realtime/realtime.py`, type `AsyncRealtimeConnection`). Confirmed real, current, minimal example from the SDK's own README:

```python
import asyncio
from openai import AsyncOpenAI

async def main():
    client = AsyncOpenAI()  # reads OPENAI_API_KEY from env

    async with client.realtime.connect(model="gpt-realtime") as connection:
        await connection.session.update(
            session={"type": "realtime", "output_modalities": ["text"]}
        )
        await connection.conversation.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Say hello!"}],
            }
        )
        await connection.response.create()

        async for event in connection:
            if event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
            elif event.type == "response.done":
                break

asyncio.run(main())
```

Install: `pip install "openai[realtime]"` (confirmed in `openai-python/pyproject.toml`: `realtime = ["websockets >= 13, < 16"]`). Add to `requirements.txt`:

```
openai>=2.46.0
websockets>=13,<16
```

(the extras syntax pulls websockets automatically, but pin it explicitly too since `requirements.txt` here is a flat pinned list, not using extras markers.)

### Raw endpoint (for reference / fallback if SDK has a bug)

```
wss://api.openai.com/v1/realtime?model=gpt-realtime
```

Headers:
```
Authorization: Bearer <OPENAI_API_KEY>
```

No `OpenAI-Beta: realtime=v1` header anymore — that was the beta-era requirement and the beta endpoint is gone (removed 2026-05-12). If any tutorial you find online shows that header, it's stale.

There's also `/v1/realtime/client_secrets` for minting short-lived tokens for browser/mobile clients, and `/v1/realtime/calls` for WebRTC/SIP. **Not relevant here** — MIKO is a trusted desktop process holding the real API key, so a direct WebSocket with the full key is correct and simplest. Don't build ephemeral-token plumbing for a hackathon demo.

### Model strings currently live (July 2026)

| Model | Notes |
|---|---|
| `gpt-realtime` | GA, general-purpose, image input supported. **Use this.** |
| `gpt-realtime-mini` | Cheaper/faster, less capable at instruction-following and tool use. Fallback if rate-limited. |
| `gpt-realtime-1.5` | Newer generation (Feb 2026). |
| `gpt-realtime-2` | Newer still (May 2026), image input supported. |
| `gpt-realtime-2.1` / `gpt-realtime-2.1-mini` | Newest (July 2026), improved interruption handling and noise handling — tempting given MIKO's barge-in requirement, but released ~2 weeks ago; higher risk of doc/SDK mismatch under time pressure. |
| `gpt-realtime-translate`, `gpt-realtime-whisper` | Not relevant (translation / STT-only). |

Recommendation stands: `gpt-realtime` for the build, with `gpt-realtime-2.1` as a one-line swap to try if you have spare time and want better interruption behavior — it's config-compatible, same event shapes.

---

## 2. Audio format

- **Input:** PCM16, 24000 Hz, mono. Configured via `session.audio.input.format`.
- **Output:** PCM16 (`audio/pcm`) or G.711 µ-law (`audio/pcmu`) at 24000 Hz for PCM. Use PCM16 — you're playing back through `sounddevice`, not a telephony trunk.
- **Wire encoding:** raw PCM16 bytes, base64-encoded, sent as the `audio` field of `input_audio_buffer.append`. Max 15 MB per chunk (irrelevant at 20-50ms chunk sizes).
- **Framing:** no fixed requirement, but the reference examples chunk at 20ms (`read_size = int(SAMPLE_RATE * 0.02)`), which balances latency and event overhead well. Don't send whole-utterance blobs — stream continuously.

Explicit session audio config:

```python
await connection.session.update(session={
    "type": "realtime",
    "model": "gpt-realtime",
    "audio": {
        "input": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "turn_detection": None,   # see section 3
        },
        "output": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "voice": "marin",         # any current realtime voice; pick one and move on
        },
    },
})
```

### `sounddevice` float32 → PCM16 @ 24kHz

MIKO's existing capture is presumably float32 (`sounddevice` default `dtype='float32'`) at 16000 or 48000 Hz. Two options:

**A. Capture directly at 24000 Hz, int16** (simplest — matches the SDK examples exactly, do this unless the knock-detector needs a specific rate on the same stream):

```python
import sounddevice as sd

stream = sd.InputStream(channels=1, samplerate=24000, dtype="int16")
stream.start()
read_size = int(24000 * 0.02)  # 20ms
while True:
    if stream.read_available < read_size:
        continue
    data, _ = stream.read(read_size)   # int16 numpy array, already PCM16
    # data.tobytes() -> base64 -> input_audio_buffer.append
```

**B. Resample from an existing 16k/48k float32 stream** (needed if the knock-detection VAD stays on its own stream/rate and you don't want two open input streams). `soxr` is already in `requirements.txt` and is high-quality/fast:

```python
import numpy as np
import soxr

def float32_to_pcm16_24k(chunk_f32: np.ndarray, src_rate: int) -> bytes:
    # chunk_f32: mono float32 in [-1, 1] at src_rate (16000 or 48000)
    resampled = soxr.resample(chunk_f32, src_rate, 24000)  # float32 out
    pcm16 = np.clip(resampled * 32767, -32768, 32767).astype(np.int16)
    return pcm16.tobytes()
```

Then base64-encode `pcm16` bytes and send via `input_audio_buffer.append`. Note: opening two simultaneous `InputStream`s on the same physical mic works fine on Windows/WASAPI in shared mode (default) — you don't need to tear down the knock-detector stream to open the Realtime one, but it's simpler to just stop the knock-listener stream once triggered and hand the device to the realtime capture loop.

### Playback (low latency, matches the reference `AudioPlayerAsync` pattern)

Use a callback-driven `sd.OutputStream` with a small thread-safe queue, not `sd.play()` per chunk (that adds device-open latency per response delta and causes glitches). This is the real pattern from `openai-python/examples/realtime/audio_util.py`:

```python
import threading
import numpy as np
import sounddevice as sd

class AudioPlayerAsync:
    def __init__(self, samplerate=24000, channels=1):
        self.queue: list[np.ndarray] = []
        self.lock = threading.Lock()
        self.stream = sd.OutputStream(
            callback=self._callback,
            samplerate=samplerate,
            channels=channels,
            dtype=np.int16,
            blocksize=int(0.05 * samplerate),  # 50ms
        )
        self.playing = False

    def _callback(self, outdata, frames, time, status):
        with self.lock:
            data = np.empty(0, dtype=np.int16)
            while len(data) < frames and self.queue:
                item = self.queue.pop(0)
                need = frames - len(data)
                data = np.concatenate((data, item[:need]))
                if len(item) > need:
                    self.queue.insert(0, item[need:])
            if len(data) < frames:
                data = np.concatenate((data, np.zeros(frames - len(data), dtype=np.int16)))
        outdata[:] = data.reshape(-1, 1)

    def add_data(self, pcm16_bytes: bytes):
        with self.lock:
            self.queue.append(np.frombuffer(pcm16_bytes, dtype=np.int16))
            if not self.playing:
                self.playing = True
                self.stream.start()

    def stop(self):
        self.playing = False
        self.stream.stop()
        with self.lock:
            self.queue = []
```

Feed it from `response.output_audio.delta`:

```python
if event.type == "response.output_audio.delta":
    player.add_data(base64.b64decode(event.delta))
```

---

## 3. Turn detection

`turn_detection` lives at `session.audio.input.turn_detection`. Three modes:

### `server_vad` (energy/silence-based — this is what most demos default to)

```json
{
  "type": "server_vad",
  "threshold": 0.5,
  "prefix_padding_ms": 300,
  "silence_duration_ms": 500,
  "create_response": true,
  "interrupt_response": true
}
```
Defaults: `threshold=0.5` (0-1, higher = needs louder speech, better for noisy rooms), `prefix_padding_ms=300` (audio kept before detected speech onset), `silence_duration_ms=500` (silence needed to call end-of-turn — lower = snappier but more clipped endings). `create_response`/`interrupt_response` control whether the server auto-fires a response and auto-interrupts the current one on new speech.

### `semantic_vad` (newer — uses the model's judgment of utterance completion, not just silence)

```json
{
  "type": "semantic_vad",
  "eagerness": "medium",
  "create_response": true,
  "interrupt_response": true
}
```
`eagerness`: `low` (lets user ramble, waits longer), `medium` (default-equivalent, `auto` maps here), `high` (cuts in fast). Good for natural conversation; adds a little model-side latency vs raw silence detection since it's judgment-based, not just amplitude-based.

### Manual / push-to-talk (`turn_detection: null`) — **recommended for MIKO**

```python
await connection.session.update(session={
    "audio": {"input": {"turn_detection": None}}
})
```

You control turns explicitly:
```python
# while knock-triggered listening window is open: just append audio
await connection.input_audio_buffer.append(audio=b64_chunk)

# when the listening window closes (silence timeout you implement, or fixed duration, or user gesture):
await connection.input_audio_buffer.commit()
await connection.response.create()
```

**Why manual mode fits MIKO:** the knock is already the "start listening" signal — that's literally push-to-talk semantics, server VAD's job of *detecting speech onset* is redundant with the knock trigger. What you still need is an end-of-turn heuristic (e.g. your own lightweight silence timeout on the amplitude of the 20ms chunks you're already reading, or a fixed max window, or a second knock to end). You can implement a trivial energy-based silence timer client-side (a few lines, using `np.abs(data).mean()` on the int16 chunks) rather than depending on the server's VAD — this avoids the `no_speech` misfire class of bug entirely, because you're not asking Whisper to classify short/quiet clips anymore; you're just streaming continuously to a server that transcribes as it goes.

**Middle ground** if you want the server to still auto-detect end-of-speech once the knock opens the mic, but not have it eagerly interrupt: keep `server_vad` but set `interrupt_response: false` and drive `response.cancel` yourself only on an explicit second knock. This is a one-line config change from the fully manual version, worth having as a toggle.

---

## 4. Vision / screen context — CAN send images directly

This resolves favorably: **the Realtime API accepts images as a content part in `conversation.item.create`.** Confirmed for `gpt-realtime` and `gpt-realtime-2`/`2.1` — image input support was added to the realtime session itself, you do not need a side-channel to a separate vision-completions call.

Event and shape:

```python
import base64, mss

def grab_screenshot_b64() -> str:
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])  # primary monitor
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
    return base64.b64encode(png_bytes).decode("utf-8")

await connection.conversation.item.create(item={
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "What's on my screen right now?"},
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{grab_screenshot_b64()}",
        },
    ],
})
await connection.response.create()
```

Practical notes:
- Full raw JSON shape:
  ```json
  {
    "type": "conversation.item.create",
    "item": {
      "type": "message",
      "role": "user",
      "content": [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,<...>"}
      ]
    }
  }
  ```
- **Downscale before sending.** A raw 1440p/4K PNG screenshot is large (multi-MB) and costs image tokens + upload latency over the WebSocket. Resize to something like 1280px on the long edge and encode as JPEG (quality ~80) before base64 — this is the single biggest lever on "how fast does the model see the screen." `mss` gives you raw pixels; pipe through Pillow (`PIL.Image`) to resize/re-encode. Pillow isn't currently in `requirements.txt` — add it (`Pillow`), it's a hard dependency for this feature unless you want to ship uncompressed BMP-sized base64 blobs.
- **Latency cost:** attaching an image adds a real but bounded delay before the model's next response starts — budget roughly 1-3 seconds extra above the audio round-trip for the model to ingest a screenshot-sized image at the start of a turn, more if the image is large/unscaled. For a demo, capture the screenshot the instant the triple-knock fires (in parallel with opening the mic), not after the user finishes talking — by the time they finish their question, the image is already uploaded and in-context.
- You do not need to re-send the image every turn — it stays in conversation history like any other item. But screen content is stale the moment it's sent, so for "what's on my screen" style queries you generally want a **fresh screenshot per question**, not a cached one from session start.
- This is a `message`/`input_image` content part — it is **not** a function-call/tool-result. Don't route it through function calling; that's a separate mechanism (section 5) for the model to *request* a screenshot (e.g. mid-conversation, model decides it needs to look again). Both patterns are legitimate: (a) always attach a fresh screenshot on every user turn (simple, deterministic, what you want for a demo), or (b) give the model a `take_screenshot` tool and let it decide when to look (more agentic, more moving parts, more can go wrong in 13 hours). **Pick (a) for today.**

---

## 5. Function calling

Declared in `session.update`, same shape as Chat Completions tools:

```python
await connection.session.update(session={
    "tools": [
        {
            "type": "function",
            "name": "take_screenshot",
            "description": "Capture the user's current screen.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "type": "function",
            "name": "media_control",
            "description": "Control media playback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["play_pause", "next", "prev", "mute"]}
                },
                "required": ["action"],
            },
        },
    ],
    "tool_choice": "auto",
})
```

Event sequence when the model decides to call a tool:

1. `response.output_item.added` — item of type `function_call` appears (has `call_id`, `name`).
2. `response.function_call_arguments.delta` — streamed partial JSON args (optional to consume; you can just wait for `.done`).
3. `response.function_call_arguments.done` — full `arguments` JSON string, keyed by `call_id`.
4. `response.done` — response output array also contains the complete `function_call` item (name, call_id, arguments) as a fallback if you didn't track deltas.

You execute the local action, then send the result back and ask for a follow-up response:

```python
if event.type == "response.function_call_arguments.done":
    result = run_local_tool(event.name, json.loads(event.arguments))  # your dispatcher
    await connection.conversation.item.create(item={
        "type": "function_call_output",
        "call_id": event.call_id,
        "output": json.dumps(result),
    })
    await connection.response.create()
```

Note the item `type` for the result is `function_call_output` at the top level (not wrapped in a `message`/`content` array like text/image items).

---

## 6. Session lifecycle, interruption, cost

### Session config (`session.update`)

Single event, send it right after connecting (and again anytime you want to change instructions/voice/tools mid-session):

```python
await connection.session.update(session={
    "type": "realtime",
    "model": "gpt-realtime",
    "instructions": "You are MIKO, a terse desktop voice assistant...",
    "audio": {
        "input": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "turn_detection": None,
        },
        "output": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "voice": "marin",
        },
    },
    "tools": [...],
    "tool_choice": "auto",
})
```

### Interruption / barge-in

Two things need to happen when the user starts talking over MIKO:

1. **Stop generation server-side:** `await connection.send({"type": "response.cancel"})`
2. **Tell the server how much of the last audio item the user actually heard**, so it doesn't think you heard the full response in conversation history (matters for WebSocket specifically — WebRTC/SIP handle this automatically):
   ```python
   await connection.send({
       "type": "conversation.item.truncate",
       "item_id": last_audio_item_id,   # from response.output_audio.delta events, event.item_id
       "content_index": 0,
       "audio_end_ms": ms_of_audio_actually_played,  # track via your player's frame count
   })
   ```
   The reference `AudioPlayerAsync` tracks `_frame_count` for exactly this reason — use it to compute `audio_end_ms = frame_count / sample_rate * 1000`.
3. Also clear your own playback queue (`player.stop()`) so stale audio doesn't keep playing after cancel.

If you're in `server_vad` mode with `interrupt_response: true`, the server does step 1 automatically on detecting new speech — you still want to clear the local playback queue yourself and should still send `conversation.item.truncate` for correctness. In manual mode, you own the whole sequence (bind it to whatever "shut up" gesture MIKO uses — a knock again, a keypress, whatever).

### Pricing (per official pricing, `gpt-realtime`/`gpt-realtime-2.1` family, July 2026)

| | Input | Output | Cached input |
|---|---|---|---|
| Text tokens (/1M) | $4.00 | $16.00 | $0.40 |
| Audio tokens (/1M) | $32.00 | $64.00 | $0.40 |

Converted to per-minute (audio billed ~1 token/100ms in, ~1 token/50ms out): **~$0.019/min listening, ~$0.077/min speaking.** A typical back-and-forth demo session runs roughly **$0.06–$0.11/min** all-in. For a hackathon demo day this is a non-issue (a few dollars total) — just don't leave a session open-mic idling for hours.

`gpt-realtime-mini` is meaningfully cheaper if you need to stress-test without burning budget, at some cost to instruction-following/tool-use reliability.

### Rate limits

As of Feb 2025 OpenAI removed the simultaneous-session cap on the Realtime API entirely — current docs don't call out a realtime-specific RPM/TPM ceiling separate from your account tier's general limits. **Not a realistic blocker for a single-user hackathon demo.** If you're on a fresh/low-tier API key, the only real risk is the *general* low TPM ceiling on Tier 1 accounts (thousands of RPM/TPM range) — irrelevant at one session, one user, one demo.

---

## 7. Event reference (only what MIKO needs)

### Client events you send

| Event | Mandatory? | Purpose |
|---|---|---|
| `session.update` | **Mandatory** | Configure model, audio format, turn_detection, tools, voice, instructions. Send once on connect. |
| `input_audio_buffer.append` | **Mandatory** | Stream base64 PCM16 mic chunks continuously while listening. |
| `input_audio_buffer.commit` | Mandatory in manual mode only | Close out the current audio turn so the model can respond. Not needed in `server_vad` mode (server auto-commits). |
| `response.create` | Mandatory in manual mode only | Ask the model to generate a reply. Auto-fired by server in VAD modes (`create_response: true`). |
| `conversation.item.create` | **Mandatory** (for images/tool results) | Inject a screenshot (`input_image`), text, or a `function_call_output`. |
| `response.cancel` | Mandatory (interruption) | Stop an in-flight response — barge-in. |
| `conversation.item.truncate` | Recommended (interruption, WebSocket) | Tell server how much audio was actually heard before cancel. |
| `input_audio_buffer.clear` | Optional | Discard buffered-but-uncommitted audio (rarely needed with WebSocket; more relevant to WebRTC). |
| `output_audio_buffer.clear` | Optional | WebRTC/SIP only — not applicable to plain WebSocket transport. |
| `conversation.item.delete` | Optional | Prune conversation history to manage cost/context. |

### Server events you must handle

| Event | Mandatory? | Meaning |
|---|---|---|
| `session.created` | Mandatory to observe | Confirms connection + gives session id. Good place to log/verify config landed. |
| `session.updated` | Recommended | Confirms your `session.update` was applied — check before assuming turn_detection/tools took effect. |
| `input_audio_buffer.speech_started` | Recommended (VAD modes) | Server detected user started talking — use to trigger your own barge-in logic even before you see model audio stop. |
| `input_audio_buffer.speech_stopped` | Recommended (VAD modes) | Server detected end of user speech. |
| `response.output_audio.delta` | **Mandatory** | Streamed base64 PCM16 audio chunk — feed straight into the player. |
| `response.output_audio.done` | Optional | Marks end of audio for this item. |
| `response.output_audio_transcript.delta` | Recommended | Live text transcript of what the model is saying — useful for on-screen captioning/logging/debugging without extra STT. |
| `response.function_call_arguments.delta` | Optional | Streamed tool-call argument JSON — only needed if you want live progress UI. |
| `response.function_call_arguments.done` | **Mandatory** (if using tools) | Full arguments — trigger your local tool dispatcher here. |
| `response.done` | **Mandatory** | End of a full response turn; also carries the complete output array as a fallback/verification (includes any `function_call` items). Good place to reset per-turn state. |
| `conversation.item.added` / `conversation.item.done` | Optional | New/finalized conversation items (GA renamed these from the old beta `conversation.item.created` — if you see `conversation.item.created` in any tutorial online, it's stale beta-era naming). |
| `error` | **Mandatory** | Errors arrive as *events*, not exceptions — the connection stays open. You must check `event.type == "error"` yourself (`event.error.type`, `.code`, `.message`); the SDK will not raise on API-level errors. |
| `rate_limits.updated` | Optional | Informational quota tracking. |

---

## 8. Failure modes on Windows — read this before 2am

1. **`asyncio` + Windows event loop policy:** Since Python 3.8, `ProactorEventLoop` is the default on Windows. This is what `websockets` needs (it does NOT need the old `SelectorEventLoop` workaround some very old blog posts recommend for SSL — that limitation was from early asyncio SSL-over-Proactor days and current `websockets`/CPython handles SSL fine over Proactor by default). **Do not** manually set `WindowsSelectorEventLoopPolicy()` — some old tutorials do this reflexively and it will silently break `asyncio.create_subprocess_exec` and other Proactor-only features if MIKO still shells out to anything (e.g. launching apps as a function-call tool). Leave the default loop alone unless you hit a concrete SSL error, and if you do, fix it narrowly rather than switching the whole app's loop policy.
2. **`KeyboardInterrupt` inside `stream.read()` loops:** `sounddevice`'s blocking `.read()` on Windows doesn't always propagate Ctrl+C cleanly inside a tight `while True` polling loop — wrap your mic-capture loop in a real cancellation mechanism (an `asyncio.Event` you set from your knock/hotkey handler) rather than relying on `KeyboardInterrupt`/`try/finally` alone, especially since this will run as a background listener, not a foreground CLI script.
3. **`sounddevice` device default sample rate mismatches:** not every Windows mic/interface supports 24000 Hz natively in exclusive mode; WASAPI shared mode (the `sounddevice` default) will resample host-side for you, but verify by actually opening `sd.InputStream(samplerate=24000, dtype="int16")` and catching `sd.PortAudioError` — fall back to capturing at the device's native rate (query via `sd.query_devices()`) and resample yourself with `soxr` if 24000 direct-open fails on the target machine. Test this on the actual demo laptop, not just your dev machine — device support varies.
4. **Two audio streams (knock-detector + realtime mic) fighting over the same device:** opening two independent `InputStream`s on the same physical device works under WASAPI shared mode but adds a small amount of extra latency/jitter to both; cleanest is to **stop the knock-listener stream the instant a knock-triggered session starts**, then reopen it when the realtime session ends, rather than running both concurrently.
5. **Firewall/AV interception of outbound WSS:** corporate or aggressive consumer AV (Windows Defender is usually fine, some third-party suites are not) can MITM-inspect TLS and break the WebSocket handshake with cryptic `SSL: CERTIFICATE_VERIFY_FAILED` — if you hit this on the demo machine specifically and not your dev machine, it's almost always this, not your code. Quick sanity check: `python -c "import ssl,websockets"`-level connectivity test to `wss://api.openai.com/v1/realtime` in isolation before debugging your app logic.
6. **Base64 overhead + `websockets` max message size:** default `websockets` client max frame size is generous (1MB+) but a full-resolution uncompressed screenshot as a data URL can exceed sane bounds fast — this is another reason to downscale/JPEG-compress screenshots (section 4) rather than a transport limit you'll hit first, but if you skip that step you may see connection resets that look like network flakiness and are actually payload size.
7. **Playback underrun clicks/pops:** the callback-based `sd.OutputStream` pattern (section 2) zero-pads on queue-empty rather than blocking — this avoids audio driver underrun crashes on Windows but you will hear brief silence/glitch if network jitter empties the queue faster than deltas arrive; increasing `blocksize` slightly (50-100ms) trades a bit of latency for smoothness if the demo network is flaky (conference wifi).
8. **`mss` + multi-monitor:** `sct.monitors[0]` is the "all monitors combined" virtual bounding box, not a real screen — use `sct.monitors[1]` for the primary display, verify this matches the demo laptop's actual setup (single external monitor plugged in changes the index).

---

## 9. Minimal working client

Complete, runnable single file. Connects, streams mic audio in manual (push-to-talk) mode, plays responses back, supports interruption via a keypress, and does one screenshot-attached turn on trigger. Wire the "trigger" (currently an Enter-keypress stand-in) to MIKO's actual triple-knock detector.

```python
"""
minimal_realtime_client.py — MIKO Realtime API reference client.

Connects to OpenAI's Realtime API, push-to-talk style:
  - Press ENTER to start talking (stands in for triple-knock trigger)
  - Press ENTER again to stop talking and get a response
  - Type 's' + ENTER instead of talking to send a screenshot + text question
  - Press 'c' + ENTER at any time to cancel/interrupt the current response (barge-in)
  - Ctrl+C to quit

Install: pip install "openai[realtime]" sounddevice numpy mss Pillow soxr
Env: set OPENAI_API_KEY
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import threading

import mss
import mss.tools
import numpy as np
import sounddevice as sd
from PIL import Image

from openai import AsyncOpenAI
from openai.resources.realtime.realtime import AsyncRealtimeConnection

MODEL = "gpt-realtime"
SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_S = 0.02  # 20ms


class AudioPlayer:
    def __init__(self, samplerate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.queue: list[np.ndarray] = []
        self.lock = threading.Lock()
        self.frame_count = 0
        self.stream = sd.OutputStream(
            callback=self._callback,
            samplerate=samplerate,
            channels=channels,
            dtype=np.int16,
            blocksize=int(0.05 * samplerate),
        )
        self.playing = False

    def _callback(self, outdata, frames, time, status):  # noqa: ARG002
        with self.lock:
            data = np.empty(0, dtype=np.int16)
            while len(data) < frames and self.queue:
                item = self.queue.pop(0)
                need = frames - len(data)
                data = np.concatenate((data, item[:need]))
                if len(item) > need:
                    self.queue.insert(0, item[need:])
            self.frame_count += len(data)
            if len(data) < frames:
                data = np.concatenate((data, np.zeros(frames - len(data), dtype=np.int16)))
        outdata[:] = data.reshape(-1, 1)

    def add_data(self, pcm16_bytes: bytes):
        with self.lock:
            self.queue.append(np.frombuffer(pcm16_bytes, dtype=np.int16))
            if not self.playing:
                self.playing = True
                self.stream.start()

    def audio_end_ms(self) -> int:
        return int(self.frame_count / SAMPLE_RATE * 1000)

    def reset(self):
        with self.lock:
            self.queue = []
            self.frame_count = 0

    def stop(self):
        self.playing = False
        self.stream.stop()
        self.reset()


def grab_screenshot_data_url(max_edge: int = 1280) -> str:
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    scale = max_edge / max(img.size)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


class MikoRealtimeClient:
    def __init__(self):
        self.client = AsyncOpenAI()
        self.player = AudioPlayer()
        self.conn: AsyncRealtimeConnection | None = None
        self.recording = asyncio.Event()
        self.want_screenshot_next_turn = False
        self.last_audio_item_id: str | None = None
        self.stop_flag = asyncio.Event()

    async def run(self):
        async with self.client.realtime.connect(model=MODEL) as conn:
            self.conn = conn
            await conn.session.update(session={
                "type": "realtime",
                "model": MODEL,
                "instructions": (
                    "You are MIKO, a terse desktop voice assistant. "
                    "Answer concisely. If a screenshot is attached, ground your answer in it."
                ),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "turn_detection": None,  # manual / push-to-talk
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "voice": "marin",
                    },
                },
            })

            await asyncio.gather(
                self._event_loop(),
                self._mic_loop(),
                self._console_loop(),
            )

    async def _console_loop(self):
        loop = asyncio.get_event_loop()
        while not self.stop_flag.is_set():
            line = await loop.run_in_executor(None, input, "")
            cmd = line.strip().lower()
            if cmd == "c":
                await self._interrupt()
            elif cmd == "s":
                self.want_screenshot_next_turn = True
                text = await loop.run_in_executor(None, input, "question> ")
                await self._send_screenshot_turn(text)
            elif self.recording.is_set():
                self.recording.clear()
                print("[stopped listening, committing]")
                assert self.conn is not None
                await self.conn.input_audio_buffer.commit()
                await self.conn.response.create()
            else:
                self.recording.set()
                print("[listening... press ENTER to stop]")

    async def _mic_loop(self):
        read_size = int(SAMPLE_RATE * CHUNK_S)
        stream = sd.InputStream(channels=CHANNELS, samplerate=SAMPLE_RATE, dtype="int16")
        stream.start()
        try:
            while not self.stop_flag.is_set():
                if stream.read_available < read_size:
                    await asyncio.sleep(0.005)
                    continue
                data, _ = stream.read(read_size)
                if self.recording.is_set() and self.conn is not None:
                    await self.conn.input_audio_buffer.append(
                        audio=base64.b64encode(data.tobytes()).decode("utf-8")
                    )
                await asyncio.sleep(0)
        finally:
            stream.stop()
            stream.close()

    async def _send_screenshot_turn(self, text: str):
        assert self.conn is not None
        data_url = grab_screenshot_data_url()
        await self.conn.conversation.item.create(item={
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": text or "What's on my screen?"},
                {"type": "input_image", "image_url": data_url},
            ],
        })
        await self.conn.response.create()

    async def _interrupt(self):
        assert self.conn is not None
        await self.conn.send({"type": "response.cancel"})
        if self.last_audio_item_id:
            await self.conn.send({
                "type": "conversation.item.truncate",
                "item_id": self.last_audio_item_id,
                "content_index": 0,
                "audio_end_ms": self.player.audio_end_ms(),
            })
        self.player.stop()
        print("[interrupted]")

    async def _event_loop(self):
        assert self.conn is not None
        async for event in self.conn:
            if event.type == "session.created":
                print(f"[session {event.session.id}]")

            elif event.type == "response.output_audio.delta":
                if event.item_id != self.last_audio_item_id:
                    self.player.reset()
                    self.last_audio_item_id = event.item_id
                self.player.add_data(base64.b64decode(event.delta))

            elif event.type == "response.output_audio_transcript.delta":
                print(event.delta, end="", flush=True)

            elif event.type == "response.function_call_arguments.done":
                print(f"\n[tool call] {event.name}({event.arguments})")
                # dispatch to your local tool handler, then:
                await self.conn.conversation.item.create(item={
                    "type": "function_call_output",
                    "call_id": event.call_id,
                    "output": json.dumps({"ok": True}),
                })
                await self.conn.response.create()

            elif event.type == "response.done":
                print()  # newline after transcript

            elif event.type == "error":
                print(f"[ERROR] {event.error.type}: {event.error.message}")


if __name__ == "__main__":
    asyncio.run(MikoRealtimeClient().run())
```

---

## GOTCHAS (summary — see section 8 for full detail)

- Beta endpoint/headers are **dead**. If a tutorial mentions `OpenAI-Beta: realtime=v1` or `platform.openai.com/docs/guides/realtime-api-beta`, it's stale post-2026-05-12.
- Event names changed at GA: `response.text.delta` → `response.output_text.delta`, `conversation.item.created` → `conversation.item.added`/`conversation.item.done`, output audio under `response.output_audio.*` not `response.audio.*`. Don't trust event names from pre-2026 blog posts without cross-checking against `developers.openai.com/api/reference/resources/realtime/server-events`.
- SDK **does not raise exceptions on API errors** — errors are just another event type (`event.type == "error"`); the connection stays open. You must check for this explicitly or you'll silently swallow failures.
- `turn_detection: null` is a real, supported, documented mode — don't assume you must use VAD. This is the right default for a knock-triggered app.
- Images work in-session (`input_image` content part) — you do NOT need a side-channel GPT-4V call. This was the biggest unknown going in and it resolved in the easy direction.
- Function-call results use `function_call_output` as the item **type itself**, not wrapped in a `content` array like messages are — easy to get this shape wrong.
- Downscale/JPEG-compress screenshots before sending — raw screen PNGs are large enough to add real latency and are the most likely thing to make "attach the screen" feel slow in the demo.
- Add `openai>=2.46.0`, `websockets>=13,<16`, and `Pillow` to `requirements.txt` — none of the three are currently present, and `Pillow` specifically is new (not implied by anything else already installed).
- Test the exact demo laptop's mic at 24000 Hz `sd.InputStream` open **before** the event, not day-of — device/driver support for that exact rate varies and `soxr` (already installed) is your fallback resampling path if it fails.
