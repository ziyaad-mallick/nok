# Miko

**Knock twice on your laptop chassis to summon an agent that sees your screen, listens, and acts.**

Miko turns the body of your laptop into a wake word. Two knuckle-knocks on the case are detected by the built-in microphone, Miko wakes, listens for a spoken command, asks a model what to do, and runs it — all through a whitelisted action layer so the model can never do anything you didn't allow. Built for OpenAI Build Week. Runs on Windows 11, Python 3.11+.

There is no hotkey and no wake phrase. The knock *is* the interface.

---

## How it works

```
 ┌──────────┐   audio    ┌───────────────┐  onset   ┌──────────────┐
 │  mic (WASAPI) ────────▶│ knock detector│─────────▶│ IOI state    │
 │  ring buffer │  512-blk │ numpy, <32ms  │          │ machine      │
 └──────────┘            └───────────────┘          └──────┬───────┘
                                                    double/triple tap
                                                           │
        ┌──────────────────────────────────────────────────▼─────────┐
        │  wake cycle (one worker thread)                             │
        │   chime → VAD endpoint → Whisper STT → brain.decide(JSON)   │
        │        → whitelisted dispatcher → TTS                       │
        └───────────────────────────┬───────────────────────────────┘
                                     │ state, detail
                              ┌──────▼───────┐
                              │  overlay HUD │  (bottom-center, always-on-top)
                              └──────────────┘
```

**Knock detection.** A `sounddevice` callback feeds a lock-free ring buffer. A consumer thread runs a numpy-only per-block classifier (peak amplitude + attack-ratio transient + spectral centroid) that stays under a hard 32ms/block budget, so nothing blocks the audio callback. Onsets feed an inter-onset-interval (IOI) state machine that recognizes double- and triple-tap patterns (120–450ms spacing) and rejects single taps, bounces, and storms. Thresholds are learned per-laptop by a calibration wizard that separates your knock from typing and room noise.

**The wake cycle** runs on one worker thread: a wake chime, then Silero VAD (ONNX, torch-free) endpoints your utterance, faster-whisper transcribes it, the brain returns a single JSON action, and a dispatcher validates it against a fixed action enum before executing. The overlay HUD subscribes to state transitions and renders them for the camera.

## The action contract

The model never gets free-form execution. It must return exactly:

```json
{"action": "open_app", "args": {"name": "spotify"}, "say": "Opening Spotify."}
```

The dispatcher re-checks `action` against a fixed whitelist and runs only the matching hand-written function. Anything off the list is refused.

| Action | Does |
|---|---|
| `open_app` / `close_app` | launch / kill a known app (`apps.json`) |
| `type_text` | type into the focused window |
| `media` | mute / play / pause / next / prev |
| `screenshot` | capture the screen |
| `macro` | run a saved sequence of the above (`macros.json`) |
| `skill` | run a teach-mode skill (see below) |

## Teach mode

Knock, then say **"learn a new trick: …"**. Miko sends the request to Codex, which writes a new skill file into `skills/` by editing a constrained template. A static import check rejects any skill that reaches outside a curated helpers module before it is ever imported; the skill is then hot-loaded on your next knock. This is the one place Codex-the-coding-agent writes real code that runs.

## Quickstart

```powershell
# from the repo root, in PowerShell
py -m venv .venv
.\.venv\Scripts\Activate.ps1              # activate the venv (prompt shows (.venv))
pip install -r requirements.txt
python scripts\download_silero.py         # one-time: fetch the VAD model
python -m miko.calibrate                  # one-time: pin the mic + learn your knock
python -m miko                            # start listening (MockBrain, zero spend)
```

Always run inside the activated venv (`python` must be the venv's). Then knock twice and say "open spotify".

Flags:

- default — offline **MockBrain**, no model spend (keyword-matches your speech).
- `--real` — use the **Codex brain** (GPT-5); spends model quota.
- `--thump` — SIMPLE mode: any single loud chassis thump wakes Miko (classifier off) — the fallback if calibration is off.
- `--no-overlay` — run headless without the on-screen HUD.
- `--device N` — override the input device index.
- `--calibrate` — run the knock calibration wizard and exit.

## The overlay

A bottom-center, always-on-top, click-through HUD shows Miko's state so a demo reads clearly on camera: a breathing orb when asleep, a sonar pulse when listening, a rotating arc with a live `thinking · 0:07` counter while the model decides (which honestly absorbs the ~17–38s real-model latency), a green confirmation chip when acting, and a distinct treatment for teaching and errors. It runs on the main thread; state updates are marshaled to it through a queue, so the audio/wake threads never touch the UI. If no display is available it degrades to a no-op and Miko still runs.

## Security & privacy

Miko is honest about what it is:

- **The action whitelist is a footgun-guard, not a security sandbox.** It constrains what the model can trigger to a fixed set of hand-written functions; it does not sandbox arbitrary code. Teach-mode skills are checked for disallowed imports statically, which blocks the obvious escapes, not every conceivable one. Real subprocess isolation is deferred (see `TODOS.md`).
- **`type_text` is a prompt-injection surface:** if a screen-referential command makes Miko read your screen, hostile on-screen text could influence what gets typed. Keep it to trusted contexts.
- **With `--real`, audio transcription is local (Whisper on your machine)**, but the command text — and a screenshot if a command is screen-referential — is sent to the model provider as part of the Codex call. The default MockBrain sends nothing anywhere.
- Every wake, transcript, decision, and dispatch is written to a local JSONL trail in `session-logs/` (gitignored) so you can see exactly what happened.

## Status

Runs end-to-end on the offline MockBrain with zero spend. Test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The knock detector's kill criterion (≥18/20 double-taps detected, 0 false wakes across typing + desk noise) is validated with a replay harness against recorded fixtures. The codex bridge, VAD, and calibration math are unit-tested; the live wake loop is exception-armored so a single failed beat recovers instead of killing the listener.
