# NOK v2 — Engineering Review

**Date:** 2026-07-21 · **Reviewer:** eng-review · **Deadline:** 23:59 PKT today
**Baseline:** 197 tests green, 23 modules, ~4113 LOC

---

## 1. Verdict

The v2 spec is buildable tonight **only because it deletes more than it adds.**

The knock path (`detector.py`, `ioi.py`, `ring_buffer.py`, `audio.py`) is genuinely
good code — pure, tested, allocation-free on the hot path, with a documented
extension point exactly where v2 needs one. It survives.

Everything between "a knock happened" and "something happened on screen" gets
deleted and replaced by one WebSocket. That is ~1,700 LOC removed against maybe
~600 added. **v2 is smaller than v1.** That is the only reason 13 hours is a real
number and not a fantasy.

The owner's framing — "nok was us starting from 0, this is us starting from 1" —
is correct, but the asset isn't the code volume. It's the detector and the 197
tests that prove it works. Do not touch them.

---

## 2. Disposition of every module

| Module | LOC | Fate | Why |
|---|---|---|---|
| `ioi.py` | 142 | **KEEP + extend** | Pure, no deps, correct. `_classify` already takes the full IOI vector as a documented extension point. Extend to carry side. |
| `detector.py` | 274 | **KEEP + extend** | Classifier logic is sound. Needs a multi-channel path for side detection. |
| `ring_buffer.py` | — | **KEEP** | Works. |
| `audio.py` | 130 | **MODIFY** | Must capture N channels, not mono. |
| `config.py` | 91 | **KEEP** | Thin. |
| `logs.py` / `jsonx.py` | — | **KEEP** | The JSONL evidence trail is the debugging lifeline. |
| `actions.py` | 181 | **SHRINK** | Keep screenshot + media. Drop the app-launch whitelist. |
| `dispatcher.py` | 93 | **REPLACE** | Becomes the Realtime tool-call handler. |
| `vad.py` | 255 | **DELETE** | Realtime server-side VAD replaces it. This is where the `no_speech` bug lived. |
| `stt.py` | 127 | **DELETE** | Realtime does STT. This is where the missing timeout lived. |
| `brain.py` | 275 | **DELETE** | Realtime replaces the `codex` subprocess. This is where 17–38s latency lived. |
| `tts.py` | — | **DELETE** | Realtime streams audio out. |
| `teach.py` | — | **DELETE** | Teach mode never produced a single real skill — `skills/` contains only `_template.py`. It is unshipped scope. |
| `skills_loader.py` | — | **DELETE** | Only existed to serve teach mode. |
| `matcher.py` | — | **DELETE** | Offline fallback for a brain that no longer exists. |
| `helpers.py` | — | **DELETE** | Only existed to serve skills. |
| `overlay.py` | 771 | **REPLACE** | See design review. |

**Deleted: ~1,700 LOC.** Every deletion removes a subsystem that produced a
measured bug or shipped zero user value. Nothing here is a judgment call.

---

## 3. The one architectural defect that must be fixed

### It exists today

`app.py:558` starts the detector thread with `on_wake=app.on_wake`.
`detector.py:273` calls `on_wake(decision, now)` **from inside the detector's own
run loop.** `App._pipeline` then runs the entire listen → transcribe → think → act
cycle *synchronously on that thread*.

Consequence: **while NOK is working, NOK is deaf.** The detector loop is blocked
and cannot classify a single block.

The evidence this was known and papered over is in the code itself —
`detector.py:108`, `_consumed_after_chunk`:

> *"A wake runs the whole listen/act pipeline synchronously on the detector
> thread... Resuming from `total_before` would replay all of that through the
> classifier... When the buffer grew by seconds we were blocked in a wake, so
> discard that backlog."*

That function exists solely to discard audio the system was too busy to hear. It is
a workaround for the architecture, not a feature.

### Why v2 cannot inherit it

v2's headline is a *live, interruptible voice conversation*. The user must be able
to knock to cut the agent off mid-sentence. With a blocking pipeline that is
impossible by construction.

### The fix

Three threads, one queue, one event loop:

```
[audio callback] --> RingBuffer          (sounddevice, never blocks)
                          |
[detector thread] -- reads blocks, classifies, NEVER calls into app
                          |
                     asyncio.Queue.put_nowait(Gesture(pattern, side, t))
                          |
[asyncio loop] ----- owns the Realtime session, the overlay state,
                     and every action. Consumes gestures.
```

The detector thread's only job becomes: block in, gesture out. It never blocks on
anything. `_consumed_after_chunk` can then be deleted, because the backlog it
discards will no longer exist.

Use `loop.call_soon_threadsafe` or `janus` to cross the thread/asyncio boundary.
Given the time budget: a plain `queue.Queue` polled by an asyncio task with a
small sleep is acceptable and has fewer failure modes at 2am.

---

## 4. Gesture map: the risk that decides the spec

### Spec
| Gesture | Action | Path |
|---|---|---|
| Double-knock, RIGHT | Screenshot | **Local, <200ms, no network** |
| Double-knock, LEFT | Media play/pause | **Local, <200ms, no network** |
| Triple-knock, either | Wake Realtime agent | Network |

### The dependency

Side detection is **unproven**. Stage 1 of `scripts/probe_stereo.py` confirms the
Intel Smart Sound array exposes **4 channels** (WASAPI dev 9; raw WDM-KS on 16/17).
So the channels exist. Whether they carry *usable spatial information* after Intel's
DSP beamforming is what Stage 2 measures.

**Run before committing to the spec:**
```
.venv/Scripts/python.exe scripts/probe_stereo.py --stage2
```
Needs ~12 knocks. Decision rule: d′ > 2.0 ⇒ buildable.

### Fallback ladder — decide now, not at 9pm

| If Stage 2 says | Gesture map becomes |
|---|---|
| **SEPARABLE** | Ship the spec as written. |
| **NOT SEPARABLE** | Double = screenshot · Triple = agent · **media play/pause is cut.** |

Do **not** substitute a quad-knock for media control. IOI reliability degrades with
chain length, `MAX_PATTERN_LEN` is 3, and v1 already logged spurious triples on a
hot mic (`app.py:488`). Cutting a feature beats shipping one that misfires on stage.

### Extension shape (clean, ~40 LOC)

`_classify` already receives the full chain and is documented as the extension
point. Onsets become `(timestamp, side)`; `_classify` returns `(pattern, side)`
where side is the majority vote across the chain. No restructuring.

---

## 5. Latency budget

The whole product claim is "works fast." Budget it explicitly.

| Path | Target | Notes |
|---|---|---|
| Knock → onset emitted | ~32 ms | One block. Already met. |
| Onset → gesture decided | ~600 ms | `CLOSE_OUT`. **Unavoidable** — must wait to see if a double grows into a triple. |
| Gesture → screenshot on disk | < 200 ms | `mss` is fast. Pure local. |
| Gesture → media key sent | < 50 ms | Pure local. |
| Triple → agent's first audio out | **< 1.5 s** | The number that matters. v1 was 17–38 s. |

**The `CLOSE_OUT` tax is the sharpest UX issue in the spec.** Every instant action
carries a mandatory 600ms wait because the system must rule out a triple.

Mitigation: fire the overlay's visual acknowledgement on the *second onset*,
before the chain closes. The user sees a response in ~32ms; the action commits at
600ms. Perceived latency collapses; correctness is unchanged.

---

## 6. Ordered risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **No `OPENAI_API_KEY`.** `~/.codex/auth.json` is ChatGPT-plan OAuth, wrong backend entirely. | **BLOCKER** | Owner action. platform.openai.com/api-keys + billing. Nothing live works until then. |
| 2 | Realtime API may not accept images → "explains your screen" fails | **HIGH** | Under research. Fallback: screenshot → vision chat-completion → inject summary as conversation context. Costs ~1s, keeps the feature. |
| 3 | Side detection fails Stage 2 | MEDIUM | Fallback ladder above. Pre-decided. |
| 4 | asyncio + WebSocket + sounddevice on Windows (ProactorEventLoop) | MEDIUM | Keep audio in threads, never in coroutines. Only the socket is async. |
| 5 | Barge-in — cutting the agent off mid-speech | MEDIUM | Needs `response.cancel` + local playback-buffer flush. Both required; flushing only one leaves the agent talking. |
| 6 | Self-hearing: agent's own voice retriggers the knock detector | **HIGH** | v1 solved this with `playback_gate`. **Carry it over.** Regression risk if forgotten. |
| 7 | Demo video not cut | HIGH | Non-negotiable 1h reserve at the end. |

Risk 6 is the sleeper. It is already solved in v1 and is the easiest thing to lose
in a rewrite.

---

## 7. Build order

Each step ends shippable. If time dies, the last completed step is the demo.

| # | Step | Est | Gate |
|---|---|---|---|
| 0 | API key + Stage 2 probe | 15m | *Owner.* Unblocks 3 and locks the gesture map. |
| 1 | Delete the 7 dead modules; fix tests | 45m | Suite green. Repo halves. |
| 2 | Thread→queue architecture; detector stops blocking | 1.5h | Knock during a running action is still heard. |
| 3 | Realtime client: connect, stream mic, play audio, barge-in | 3h | Hold a voice conversation from the CLI. |
| 4 | Screen capture + vision context | 1h | Ask "what's on my screen", get a true answer. |
| 5 | Local instant actions (screenshot, media) | 45m | Both fire < 200ms, no network. |
| 6 | New overlay | 3h | Per design review. |
| 7 | Wire + end-to-end | 1h | All three gestures work live. |
| 8 | **Demo video** | 1h | **Reserved. Does not slip.** |

**Total: ~12h against 13h available.** One hour of slack. That is too thin, so:

**Pre-authorized cuts, in order:** (a) side detection → single double-knock action;
(b) vision → voice-only agent; (c) overlay drops to MUST-list only.

The kill rule: **if step 6 has not started by 20:00 PKT, cut to the MUST list and
start the video at 22:00 regardless of state.** A submitted rough demo beats an
unsubmitted good one.

---

## 8. What this review will not approve

- **Reviving teach mode / skills.** Zero shipped value, meaningful complexity.
- **Keeping the matcher as an "offline fallback."** Its purpose died with the brain.
- **A quad-knock.** See §4.
- **Any refactor of `detector.py`'s classifier.** It works, it is tested, and the
  owner explicitly said the knock detector is the good part. Extend it; do not
  clean it.
- **Starting the overlay before step 3 works.** A beautiful UI over a broken
  pipeline is exactly how v1 got here.
