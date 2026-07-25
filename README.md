# Nok

**Knock twice on your laptop. Something happens.**

No hotkey. No wake word. No window to find. You knock on the case with your knuckles — the same way
you'd knock on a door — and the machine does the thing. The knock *is* the input, not a prelude to
one.

Windows 11, Python 3.11+.

```
  knock knock  (right side)  →  screenshot        instant · local · no network
  knock knock  (left side)   →  play / pause      instant · local · no network
                             →  or open an app you name with --fav
```

That is the whole product, and it needs **no API key, no model, no account, and no internet**. It
answers in under 200ms because there is nothing in the path to be slow.

There is also a third gesture, and it is optional:

```
  knock knock knock          →  an agent  (four kinds — see below)
```

**Nok has no user interface.** It runs as a tray icon and tells you what it did with a Windows
notification. A background listener does not earn a piece of your screen, and a HUD that appears for
400ms to say "Screenshot" is a heavier way of saying something the OS already has a channel for.
There *is* a rather nice overlay — `--overlay` — but it is opt-in, and it is not the point.

The tray icon is not decoration either. An always-listening microphone daemon with no window and no
tray presence is indistinguishable from spyware; this way there is always one visible thing saying
Nok is on, and one place to quit it.

---

## The 60-second version

```bash
git clone <repo> && cd nok
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m nok --calibrate     # teach it your knock, once
.venv/Scripts/python -m nok --no-agent      # gestures only, nothing else
```

Knock twice. A notification tells you where the screenshot went.

If you want to see the overlay without a microphone, an API key, or any knocking:
`.venv/Scripts/python scripts/overlay_preview.py`.

---

## Why a knock

A hotkey can already open an assistant. So the honest test for any gesture is: **could a keyboard
shortcut do this?** If yes, the gesture is decoration.

A knock passes that test in exactly two places, and Nok is built on both:

1. **Your hands are not on the keyboard.** You're holding a coffee, eating, on a call, across the
   desk. `Alt+Space` requires you to already be in the position the gesture is meant to save you
   from.
2. **The gesture carries the whole command.** Double-knock takes a screenshot. That's it — no
   speech, no round-trip, no waiting. The knock isn't a prelude to the real input; it *is* the
   input.

An earlier version of this project got that wrong: it made you knock *and then* speak a command,
which is strictly worse than a hotkey. v2 deletes that idea. Two of the three gestures never open
their mouth at all.

---

## Four brains, and which one you get

Triple-knock opens a session. What answers depends on the flag, and the difference is large enough
that guessing is not fair to you:

| Flag | What triple-knock gets you | Needs |
|---|---|---|
| *(none)* | **Spoken commands.** A closed set, matched locally and executed. No LLM, no screen reading. | nothing |
| `--vision` | **It reads your screen and answers in a notification.** No microphone, no talking. | `OPENAI_API_KEY` |
| `--chat` (alias `--local`) | **Conversational agent that reads your screen**, running on your own machine. | Ollama + a vision model |
| `--realtime` | **GPT Realtime**, speech-to-speech, sees your screen, sub-second. | `OPENAI_API_KEY` with billing |

`--no-agent` removes the third gesture entirely, and is a completely reasonable way to run this.

The default is deliberately the one that needs nothing and never touches the network. It
understands roughly this much:

```
  "open spotify"    "take a screenshot"    "type hello world"
  "pause"   "next track"   "volume up"   "mute"
```

### `--vision`, the one with no microphone

The other three brains all listen first — open a mic, wait for you to finish a sentence, then act.
That chain is the slowest part of every path, and it is where this project broke on real hardware.
`--vision` deletes it:

```
  knock knock knock  →  screenshot  →  one vision call  →  the answer, in a notification
```

You never say anything, because the screen already is the question: a failing test gets the cause,
a dialog gets what to press, a question on the page gets answered. The system prompt caps the reply
at 40 words — it lands in a notification, and a model that writes five paragraphs there has
produced nothing you can read.

Defaults to `gpt-4o`; override with `NOK_VISION_MODEL_OPENAI`. One request per knock, so this costs
a fraction of a cent rather than `--realtime`'s per-minute rate.

Instant actions cost nothing on any of these paths. They never touch the network, whichever brain
is configured.

---

## Binding a double-knock to an app

```bash
.venv/Scripts/python -m nok --fav spotify
```

`--fav` takes the **left** double-knock, replacing play/pause — media control is the weaker of the
two things a knock can do, and you asked for this one by name. The name goes through the Windows
shell, so anything the Start menu resolves works: `spotify`, `notepad`, `code`, a URL, a protocol
handler.

On hardware where left/right detection doesn't work there is only one double-knock to give, and an
explicit `--fav` wins it — which costs you the screenshot gesture, and the startup banner says so
out loud. A flag that silently does nothing on most laptops would be worse.

---

## Architecture

```
 ┌──────────────────┐  512-sample blocks  ┌──────────────────┐
 │ mic → RingBuffer │────────────────────▶│ knock classifier │  numpy only, <32ms
 │      (WASAPI)    │                     │ peak · attack ·  │
 │    → SideBuffer  │──── per-ch peaks ──▶│ spectral centroid│
 └──────────────────┘                     └────────┬─────────┘
                                                   │ onsets (+ side)
                                          ┌────────▼──────────┐
                                          │ IOI state machine │  double / triple
                                          └────────┬──────────┘
                                                   │ Gesture
                                          ┌────────▼──────────┐
                            ╔═════════════│   gesture queue   │═════════════╗
                            ║             └───────────────────┘             ║
                    thread boundary — the detector never blocks on anything
                            ║             ┌───────────────────┐             ║
                            ╚════════════▶│   asyncio loop    │◀════════════╝
                                          └────────┬──────────┘
                        ┌──────────────────────────┼──────────────────────────┐
                        ▼                          ▼                          ▼
                 local actions                agent session              overlay HUD
              screenshot · media          local, or GPT Realtime        audio-reactive
                 (no network)             screen attached on wake      invisible at rest
```

**The detector never runs the work.** It classifies blocks and pushes a `Gesture` onto a queue —
that's all. Everything else happens on an asyncio loop. This is the difference that makes
knock-to-interrupt possible: previously the wake pipeline ran *inside* the detector's own loop, so
while Nok was working it was deaf — knocks arriving mid-cycle were never classified at all.

**Knock detection.** A `sounddevice` callback feeds a mutex-guarded ring buffer; a consumer thread
runs a numpy-only per-block classifier (peak amplitude, attack ratio against a trailing EMA,
spectral centroid) that stays under a hard 32ms/block budget so the audio callback is never
blocked. Onsets feed an inter-onset-interval state machine that recognises double- and triple-taps
(120–450ms spacing) and rejects single taps, mechanical bounces, and storms. Thresholds are learned
per-laptop by a calibration wizard that separates your knock from typing and room noise.

**The Realtime path** is one WebSocket to the OpenAI Realtime API — PCM16 at 24kHz both directions,
server VAD for turn-taking so it stays a conversation rather than push-to-talk. A screenshot is
captured *in parallel with connecting*, so by the time you've finished asking your question the
image is already in context. Barge-in cancels the response, truncates conversation history to what
you actually heard, and drops the local playback queue — all three, or the model believes it was
heard in full.

An earlier pipeline measured **17–38 seconds** per turn through Silero VAD → faster-whisper → a
`codex` subprocess, and regularly scored real speech as `no_speech`. Silero and the subprocess are
gone. faster-whisper stayed: it is still the transcriber on both local paths.

---

## Left and right

Knocking on the left and right sides of the chassis triggers different actions — where the hardware
allows it.

Laptop microphone arrays run DSP beamforming before audio reaches Windows. On some machines that
preserves enough inter-channel level difference to locate a knock; on others it flattens it
completely. Nok does not assume. `--calibrate-sides` records labelled knocks from each side, fits
a decision boundary, and computes **d′** (class separation over within-class spread). Side
detection turns on only if d′ ≥ 2.0.

If it doesn't clear that bar, the gesture map degrades honestly:

```
  knock knock         →  screenshot
  knock knock knock   →  wake the agent
  (media control is dropped, not reassigned)
```

Media control is *cut* rather than rebound to a four-knock chain. IOI reliability falls off past
three onsets, and a gesture that misfires during a demo is worse than one that doesn't exist.

---

## Notifications, which are the default

With no flags there is no window anywhere. Nok puts an icon in the tray, and every completed action
raises a balloon: *Screenshot saved — nok-20260725-190210.png*. On Windows 10/11 that renders as a
real toast and lands in the Action Center, so the confirmation is still there thirty seconds later
when you look back.

If the tray can't be created for any reason, confirmations print to the console instead and Nok
carries on answering knocks. Losing the icon is a cosmetic failure and is treated as one.

Right-click the icon (or double-click it) to quit.

---

## The overlay, which is opt-in

Run with `--overlay` to get the HUD instead of notifications. It is the better demo and the worse
default.

Invisible 99.9% of the time. It has no business sitting on your screen telling you how to use it.

When summoned it surfaces bottom-center — the one placement structurally unlikely to cover the
content you're asking about. States are one continuous morph rather than four separate screens:

| State | Reads as |
|---|---|
| **WOKE** | a near-white spark, overshoot-and-settle — "I heard you" |
| **LISTENING** | teal; rings collapse *inward*, driven by your live mic RMS |
| **SPEAKING** | gold; rings expand *outward*, driven by the model's output audio |
| **THINKING** | amethyst arc with an honest elapsed timer |
| **ACTING** | jade flash and a chip naming the action |
| **ERROR** | coal-red, damped shake, sticky for 4s so a screen recording catches it |

The inward/outward ring flip is the whole signal for who currently holds the floor, and it costs
one inverted interpolation.

Everything is driven by real amplitude — RMS, normalised against a noise floor, smoothed with a
one-pole EMA that rises in ~50ms and falls in ~250ms so it tracks speech onsets without strobing
between syllables. Rings spawn on onsets, not on a metronome.

The window is layered, click-through, hidden from alt-tab, and `WS_EX_NOACTIVATE` — it can never
take focus. An overlay that steals your caret while you're typing in another app is a broken
product, not a cosmetic bug.

Every value it draws comes from `nok/tokens.py`: one colour family, a four-size type scale off a
single ratio, a 4px spacing unit, and a `Layout` built against the display's real DPI so the HUD is
the same physical size at 100% and 150% scaling.

---

## Install

```bash
git clone <repo> && cd nok
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

Then pick a brain. **You do not need an API key.**

### Local (free, offline, no account)

Install [Ollama](https://ollama.com) and pull a model:

```bash
ollama pull qwen2.5vl          # can read your screen
ollama pull granite3.2-vision  # smaller, document/UI focused
```

Run with `--chat` (or `--local`, the same thing). Everything stays on your machine — unplug the
network and it still works.

A warning worth heeding: **general image-captioning models cannot read a screen.** Reading a
desktop is OCR plus layout understanding, and captioners fail it confidently rather than
gracefully. Asked to describe a terminal full of code, `moondream` replied *"irtf.com profile page
for Joshua Taylor Bennett"* — fast, fluent, entirely invented. Nok ranks document/UI models first
and logs `vision_model_unreliable` when it has to fall back to a captioner. If you see that in the
log, don't trust anything it says about your screen.

Text-only models work too; Nok detects the lack of vision and skips the screenshot rather than
pretending to look.

### OpenAI Realtime (paid, much faster)

```bash
setx OPENAI_API_KEY "sk-..."
```

Needs a **platform** key with billing enabled — a ChatGPT subscription is a different credential
and will not work. Roughly $0.06–0.11 per minute. Sub-second responses and true speech-to-speech
instead of the local path's staged pipeline.

## Run

```bash
.venv/Scripts/python -m nok --calibrate         # learn your knock (once)
.venv/Scripts/python -m nok --calibrate-sides   # test left vs right (once)
.venv/Scripts/python -m nok                     # go
```

| Flag | Effect |
|---|---|
| `--fav APP` | left double-knock opens APP instead of play/pause |
| `--vision` | triple-knock reads the screen and answers in a notification (needs `OPENAI_API_KEY`) |
| `--chat`, `--local` | conversational agent via Ollama, reads your screen, no key |
| `--realtime` | OpenAI Realtime voice agent (needs `OPENAI_API_KEY` + quota) |
| `--speak` | speak a confirmation after each command |
| `--no-agent` | instant actions only, never connects |
| `--no-screen` | agent runs blind, no screenshots sent |
| `--no-sides` | ignore side detection |
| `--thump` | any loud chassis thump wakes it (fallback for a quiet mic) |
| `--overlay` | show the HUD instead of notifications |
| `--device N` | pick the input device by index |

---

## Safety

**On the Realtime path**, the model cannot execute arbitrary code. It has exactly two tools —
`take_screenshot` and `media_control`, the latter constrained to a five-value enum — and every tool
result is a hand-written string. There is no shell, no filesystem write, no app launcher.

**The local spoken-command path is more capable, on purpose**, because it is matching your own
speech against a fixed list rather than letting a model choose. It can launch an app by name
(through the Windows shell) and type text into the focused window. There is still no shell and no
filesystem write, but "open" and "type" are real capabilities — worth knowing before you leave it
running in a room with other people.

**`--fav` launches an app on a double-knock**, through the same Windows shell call. That is a real
capability sitting behind a gesture a passer-by can trigger by knocking on your desk. It is off
unless you pass the flag and name the app yourself.

Screenshots are sent to OpenAI only on `--realtime` and `--vision`, and only when you triple-knock.
Nothing is captured or transmitted at rest, and `--no-screen` disables it on the paths that use it.

---

## Troubleshooting

Everything logs to `session-logs/*.jsonl`.

| Symptom | Look for | Fix |
|---|---|---|
| Knocks never register | no `onset` events | `--thump`, or lower the gate: `set NOK_KNOCK_FLOOR=0.03` |
| Doubles read as two singles | `wake` with `NONE` | widen the window: `set NOK_MAX_IOI=0.75` |
| It stops hearing you entirely | `audio_stream_error_state` | the mic stream died; Nok prints this and shows an error rather than sitting there looking idle |
| Mic opens at the wrong rate | `capture_fallback_rate` | the device refused 16kHz; it opened natively and resamples |
| Agent won't connect | `realtime_session_error` | key is missing, unfunded, or a ChatGPT (not platform) credential |
| Speech never registers | `endpoint_no_speech`, `endpoint_gate_capped` | the room is loud; the gate is capped so it degrades instead of going deaf |
| Sides always wrong | `d_prime` under 2.0 in `calibration.json` | this array beamforms; run `--no-sides` |
| No notifications appear | `tray_unavailable`, `balloon_failed` | confirmations fall back to the console; check Windows notification settings and Focus Assist |
| `--vision` says nothing | `vision_timeout`, `vision_failed` | key missing or unfunded; the failure is shown as a notification too |

---

## Tests

```bash
.venv/Scripts/python -m pytest
```

321 tests, no hardware and no network required.

Worth being precise about what that does and doesn't prove. The pure layers are genuinely covered:
the knock classifier runs against recorded fixtures, the IOI state machine, side detection, the
overlay's state model and the design tokens are all real algorithmic assertions, and the Realtime
layer is tested against a mock connection because the bugs there live in event handling and the
barge-in sequence rather than in the socket.

What a green suite does **not** prove is that the audio hardware behaves. `CaptureLoop` and the
speech gate are covered against fake devices and synthetic levels — including the stream dying, a
device that refuses 16kHz, and a room too loud to endpoint in — but device enumeration and the real
tkinter window are exercised by hand. That seam is where this broke once before, so it is called
out rather than papered over.

The tray sits on the same seam. What the balloon *says* is tested — truncation against the Win32
field limits, and the intent-to-message mapping — but `Shell_NotifyIconW` putting a real icon in a
real tray is verified by running it. The first version of that code registered its window fine and
still failed, because ctypes defaults an undeclared return type to `c_int` and quietly cut the top
32 bits off a 64-bit `HWND`. No test would have caught that; declaring the signatures did.

---

## What's deliberately not here

- **Teach mode / learned skills.** An earlier version could generate new Python skills at runtime.
  It never produced a single real one, and it was a genuine security surface. Deleted.
- **A curated app whitelist.** There used to be a list of 11 apps, 8 of which were bare URLs. That's
  a bookmarks file with extra steps. Launching now goes through the Windows shell instead, which
  resolves more and hides less (see Safety).
- **A four-knock gesture.** See above.
- **Adaptive light/dark contrast.** tkinter gives one chroma-keyed transparent colour, not real
  alpha. The card is opaque with a hairline edge and a shadow, which reads as a physical object on
  any background instead of blending badly with all of them.

---

## License

MIT.
