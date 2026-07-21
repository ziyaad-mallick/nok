# Miko

**Knock on your laptop. An agent wakes up, looks at your screen, and talks to you about it.**

No hotkey. No wake word. No window to find. You knock on the case with your knuckles — the same
way you'd knock on a door — and the machine answers. The knock *is* the interface.

Built for OpenAI Build Week. Windows 11, Python 3.11+.

```
  knock knock  (right side)  →  screenshot            instant, local
  knock knock  (left side)   →  play / pause          instant, local
  knock knock knock          →  live voice agent      sees your screen
```

Knock again at any time to cut the agent off. Interrupting shouldn't require aiming.

---

## Why a knock

A hotkey can already open an assistant. So the honest test for any gesture is: **could a keyboard
shortcut do this?** If yes, the gesture is decoration.

A knock passes that test in exactly two places, and Miko is built on both:

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
                 local actions            GPT Realtime session          overlay HUD
              screenshot · media       PCM16/24k · server VAD        audio-reactive
                 (no network)          screen attached on wake       invisible at rest
```

**The detector never runs the work.** It classifies blocks and pushes a `Gesture` onto a queue —
that's all. Everything else happens on an asyncio loop. This is the difference that makes
knock-to-interrupt possible: previously the wake pipeline ran *inside* the detector's own loop, so
while Miko was working it was deaf — knocks arriving mid-cycle were never classified at all.

**Knock detection.** A `sounddevice` callback feeds a lock-free ring buffer; a consumer thread runs
a numpy-only per-block classifier (peak amplitude, attack ratio against a trailing EMA, spectral
centroid) that stays under a hard 32ms/block budget so the audio callback is never blocked. Onsets
feed an inter-onset-interval state machine that recognises double- and triple-taps (120–450ms
spacing) and rejects single taps, mechanical bounces, and storms. Thresholds are learned per-laptop
by a calibration wizard that separates your knock from typing and room noise.

**The agent** is one WebSocket to the OpenAI Realtime API — PCM16 at 24kHz both directions, server
VAD for turn-taking so it stays a conversation rather than push-to-talk. A screenshot is captured
*in parallel with connecting*, so by the time you've finished asking your question the image is
already in context. Barge-in cancels the response, truncates conversation history to what you
actually heard, and drops the local playback queue — all three, or the model believes it was heard
in full.

The previous pipeline measured **17–38 seconds** per turn through Silero VAD → faster-whisper → a
`codex` subprocess, and regularly scored real speech as `no_speech`. That entire chain is deleted.

---

## Left and right

Knocking on the left and right sides of the chassis triggers different actions — where the hardware
allows it.

Laptop microphone arrays run DSP beamforming before audio reaches Windows. On some machines that
preserves enough inter-channel level difference to locate a knock; on others it flattens it
completely. Miko does not assume. `--calibrate-sides` records labelled knocks from each side, fits
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

## The overlay

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

---

## Install

```bash
git clone <repo> && cd miko
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

The agent needs an OpenAI **platform** API key with billing enabled (a ChatGPT subscription is a
different credential and will not work):

```bash
setx OPENAI_API_KEY "sk-..."
```

Roughly $0.06–0.11 per minute of conversation. Instant actions cost nothing — they never touch the
network.

## Run

```bash
.venv/Scripts/python -m miko --calibrate         # learn your knock (once)
.venv/Scripts/python -m miko --calibrate-sides   # test left vs right (once)
.venv/Scripts/python -m miko                     # go
```

| Flag | Effect |
|---|---|
| `--no-agent` | instant actions only, never connects |
| `--no-screen` | agent runs blind, no screenshots sent |
| `--no-sides` | ignore side detection |
| `--thump` | any loud chassis thump wakes it (fallback for a quiet mic) |
| `--no-overlay` | headless |

Preview the interface with no key, no mic and no knocking:

```bash
.venv/Scripts/python scripts/overlay_preview.py
```

---

## Safety

The model cannot execute arbitrary code. It has exactly two tools — `take_screenshot` and
`media_control`, the latter constrained to a five-value enum — and every tool result is a
hand-written string. There is no shell, no filesystem write, no app launcher.

Screenshots are sent to OpenAI when you triple-knock. Nothing is captured or transmitted at rest,
and `--no-screen` disables it entirely.

---

## Troubleshooting

Everything logs to `session-logs/*.jsonl`.

| Symptom | Look for | Fix |
|---|---|---|
| Knocks never register | no `onset` events | `--thump`, or lower the gate: `set MIKO_KNOCK_FLOOR=0.03` |
| Doubles read as two singles | `wake` with `NONE` | widen the window: `set MIKO_MAX_IOI=0.75` |
| Agent won't connect | `realtime_session_error` | key is missing, unfunded, or a ChatGPT (not platform) credential |
| Agent hears nothing | `mic_24k_open_failed` | the device refused 24kHz; it falls back and resamples — check `mic_fallback_rate` |
| Sides always wrong | `d_prime` under 2.0 in `calibration.json` | this array beamforms; run `--no-sides` |

---

## Tests

```bash
.venv/Scripts/python -m pytest
```

211 tests, no hardware and no network required. The knock classifier runs against recorded
fixtures; the Realtime layer is tested against a mock connection, because the bugs there live in
event handling and the barge-in sequence rather than in the socket.

---

## What's deliberately not here

- **Teach mode / learned skills.** An earlier version could generate new Python skills at runtime.
  It never produced a single real one, and it was a genuine security surface. Deleted.
- **An app launcher.** There used to be a whitelist of 11 apps, 8 of which were bare URLs. That's a
  bookmarks file with extra steps.
- **A four-knock gesture.** See above.
- **Adaptive light/dark contrast.** tkinter gives one chroma-keyed transparent colour, not real
  alpha. The card is opaque with a hairline edge and a shadow, which reads as a physical object on
  any background instead of blending badly with all of them.

---

## License

MIT.
