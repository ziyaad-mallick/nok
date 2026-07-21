# MIKO Overlay — Design Review v2

**Reviewer:** design pass, autonomous, 2026-07-21 (Build Week deadline day)
**Subject:** replace the "Ember pill" tkinter overlay (`miko/overlay.py`, 771 lines) with a visual
language for the new gesture spec — double-knock (screenshot), double-knock other side
(play/pause), triple-knock (live GPT Realtime voice agent with screen vision).
**Owner verdict on v1:** "the ui is shit." This review does not throw v1 away — the *engine*
(model/view split, click-through, DPI awareness, 30fps loop) is sound and reusable. What reads as
"shit" is a subset of visual choices, not the architecture. Section 2 audits exactly what to
keep and what to cut.

---

## 0. TL;DR

| Question | Answer |
|---|---|
| Toolkit | **tkinter.** Not PyQt6. See §1 — this is the load-bearing call. |
| Core visual | A dark glass pill, invisible at rest, that surfaces bottom-center only while something is happening — never a persistent HUD. |
| Idle render cost | Zero paint work per tick (queue-drain only) until state changes. See §9. |
| Two instant actions | A **separate, tiny ephemeral badge window** — not the conversation pill — flashes 400ms total and never blocks the action. See §6. |
| Biggest gap in v1 | `WS_EX_NOACTIVATE` is missing from the exstyle bitmask — a real violation of the "never steal focus" hard constraint. Fixed in §9. |
| Biggest net-new build item | Wiring **real** mic/output RMS into the existing (currently decorative) sonar-ring code. ~45 min. |

---

## 1. TOOLKIT VERDICT: tkinter

**Keep tkinter. Do not move to PyQt6 tonight.**

The honest tradeoff:

| | tkinter (`-transparentcolor` chroma-key) | PyQt6 (`WA_TranslucentBackground`) |
|---|---|---|
| Per-pixel alpha / soft shadows / blur | No — chroma-key hack, hard edges | Yes — real thing |
| Antialiasing | Canvas ovals alias at small radii | QPainter antialiases properly |
| New dependency | None — already in the venv | `pip install PyQt6` (~40–60MB wheel), untested on this machine tonight |
| Existing code to reuse | 771 lines, tested, working: click-through, DPI awareness, always-on-top reassertion, model/view split | Zero — full rewrite of the window layer |
| Time to a *working* window | 0 min (already exists) | 30–60 min of first-time Windows-specific Qt debugging (exstyle interop between Qt's flags and raw `ctypes` win32 calls is exactly the kind of thing that eats an hour at midnight) |
| Time to *this spec's* states | ~2–2.5 hrs (repaint logic, new states, real amplitude wiring) | ~3.5–4.5 hrs realistically, and that's the *entire* budget with zero slack |
| Failure mode if it goes wrong | Worst case, ship v1's engine with the new palette/states — degrades gracefully | Worst case, an unfamiliar Qt event-loop bug at 1am with no working fallback |

The honest case *for* PyQt6: it would look measurably better — true soft glow instead of nested-oval
fake gradients, real antialiasing on the orb edge, actual blur on the shadow. If this were a
week-long project, PyQt6 wins outright.

It isn't. The brief is explicit: ~3 hours of UI budget, out of ~10 hours for the *entire app*, on
the day the thing ships. A rewrite of the render stack is the single highest-risk, lowest-certainty
use of that budget. The existing tkinter file already solved the hard, non-obvious parts (DPI
awareness via `shcore`, click-through via raw `SetWindowLongPtrW`, a deterministic
clock-injected model that's independently unit-testable, a 30fps loop that drains a thread-safe
queue). Throwing that away to buy soft shadows is not a trade a senior engineer makes hours before
a deadline. The fix for "the UI is shit" is almost entirely a *palette, motion, and state-design*
problem, not a rendering-fidelity problem — §4–§6 solve it inside tkinter's real constraints.

One caveat: v1's chroma-key trick (`transparentcolor` on `#010203`) means any canvas item that
should be see-through must never accidentally render that exact color, and any item that should be
*opaque black* must be nudged off pure `#010203`. This already bit the ERROR glyph once (see the
"BUG FIX" comment at `overlay.py:752-757` — a near-black `!` on a near-black orb was invisible).
Keep every dark fill at least a few RGB steps off the key color as a standing rule.

---

## 2. Audit of `overlay.py` — keep / cut

| Element | Verdict | Why |
|---|---|---|
| `OverlayModel` (pure, clock-injected, headless) | **KEEP entirely** | This is the best part of v1 — deterministic, unit-testable, no tkinter import. Extend it, don't replace it. |
| Thread-safe `set_state` via `queue.Queue` | **KEEP** | Correct pattern; the detector/worker thread must never touch Tk. |
| Click-through (`WS_EX_LAYERED\|TRANSPARENT\|TOOLWINDOW`) | **KEEP, but add `WS_EX_NOACTIVATE`** | Missing flag is a real gap against the hard constraint. See §9. |
| DPI awareness (`SetProcessDpiAwareness`) | **KEEP** | Needed or the pill renders soft/blurry on scaled displays. |
| 30fps `root.after` tick | **KEEP the loop, CUT its always-on cost** | Currently repaints every 33ms forever, including a fully idle SLEEPING state. Add a dirty-check (§9). |
| Compact/expanded pill with `_ease_out_cubic` size morph | **KEEP** | Good, cheap, reads as "considered." |
| Nested-oval fake gradient halo (`halo3`/`halo2`/`glow`/`orb`) | **KEEP the technique** | It's the correct workaround for no real blur on Canvas. Reuse for every state's core. |
| Sonar rings (`_rings`) | **KEEP shape, REPLACE the driver** | Currently a fixed 0.9s metronome, not audio-reactive. This is the single biggest "feels alive vs feels fake" gap — wire it to real RMS (§5). |
| Ember warm palette (charcoal panel, warm off-white text, one hue per state) | **KEEP the system, remap the states** | The palette *reasoning* (one un-repeated hue per state, muted rather than saturated) is good UI thinking; it is not what made the owner call v1 "shit." Keep it, just add SPEAKING and drop TEACHING/DONE-as-designed (§6). |
| Persistent SLEEPING pill ("MIKO / knock twice") always visible | **CUT** | The new spec says IDLE is invisible. A pill sitting on screen 99.9% of the time reading "knock twice" is exactly the kind of "why is this here" clutter that reads as amateur HUD design. Removing it is also what makes "no busy render loop when idle" trivial (§9). |
| `_THINK_VERBS` cycling copy, `thinking · 0:07` timer | **KEEP** | Genuinely good idea — turns 17–38s of real latency into a legible, honest wait instead of a frozen spinner. Still applies to THINKING under the new spec. |
| ERROR shake + sticky hold | **KEEP** | Sells "something is actually wrong" without a modal. |
| DONE checkmark, TEACHING candle-flicker | **CUT the states, KEEP the technique for reuse** | Teach-mode is not in the new gesture spec's core loop; the checkmark stroke-draw technique is worth keeping for a generic "confirmed" beat if one is ever needed. |

---

## 3. THE STATE MACHINE, VISUALLY

New state list (per brief): **IDLE → WOKE → LISTENING ⇄ THINKING ⇄ SPEAKING → (ACTING) → IDLE**,
with **ERROR** reachable from any state. Two more ephemeral, parallel, non-conversational states
handle the instant actions: **FLASH_SHOT** and **FLASH_MEDIA** (§6).

| State | What the user sees | Duration | Enters from |
|---|---|---|---|
| **IDLE** | **Nothing.** Window alpha 0 / withdrawn. Zero screen presence. | Indefinite (99.9% of runtime) | default |
| **WOKE** | A single bright flash-ring bursts outward from screen-bottom-center and the pill snaps into existence mid-flash (scale 0.6→1.15→1.0, overshoot). No text yet, just "I heard you." | 150–250ms, then auto-advances | knock detected (any pattern that isn't an instant action) |
| **LISTENING** | Compact pill, teal accent. Core orb + 1–2 sonar rings driven by **live mic RMS** (§5). Label: "Listening…". | Until VAD endpoints the utterance or GPT Realtime signals `input_audio_buffer.speech_stopped` | WOKE |
| **THINKING** | Expands to the wider card. Rotating arc (kept from v1, it's good), amethyst accent, cycling verb ("Thinking…" / "Reading the screen…"), `thinking · 0:0N` timer once past 2s. | Until first response token/audio | LISTENING (network/tool-call gaps only — Realtime is mostly speech-to-speech, so this state should often be *brief or skipped entirely*, not a mandatory beat) |
| **SPEAKING** | Same compact pill as LISTENING but **gold accent** and rings that **emanate outward** (vs. LISTENING's inward pulse) — the directionality is the whole signal for "who's talking." Driven by **live output-audio RMS** (§5). Label: "Miko" + a one-line live caption of what's being said if the Realtime transcript stream is available. | Until output audio stream ends | THINKING or directly from LISTENING |
| **ACTING** | Brief flash + green chip: "→ screenshot" / "→ paste that in Notion" etc. Reuses v1's flash+chip exactly. | ~400ms–a few seconds for the real op | SPEAKING (tool call fires mid-turn) or THINKING |
| **ERROR** | Reuse v1 exactly: red accent, 350ms damped shake, sticky through the following IDLE for 4s so a demo/screen-recording actually catches it. | 4s sticky, then fades | any state |
| *(exit)* | Card holds its last frame ~600ms so the final word/result is readable, then fades+shrinks to IDLE over 300ms ease-in-cubic. | — | SPEAKING/ACTING → IDLE |

**Transition feel, not just checkboxes:** the thing that makes this "feel like a real AI agent"
instead of a status widget is that *every* transition is a **push**, not a **cut** — nothing pops
or vanishes instantly except IDLE↔invisible at the very ends. LISTENING→THINKING→SPEAKING is one
continuous morph (accent color lerps, ring behavior flips direction, size eases) so the eye reads
it as one living object changing its mind, not four separate screens.

---

## 4. FORM + POSITION

**Form:** a rounded "glass pill" — dark card, one soft-faked-gradient orb on the left, text on the
right, exactly as v1 already draws it. Not a full orb-only design (à la Siri) because the text
line (live transcript / current action) is genuinely useful information during a 17–38s THINKING
wait — an orb-only design would need a *second* surface for that text, which costs more build time
for less clarity. The hybrid (orb = state-at-a-glance, text = detail-on-demand) is the right call
for a 3-hour budget.

**Position: bottom-center, ~96px above the taskbar** (kept from v1). Justification against the
occlusion constraint:
- The user is discussing *what's on their screen* — which is overwhelmingly the center/upper 80%
  of the display (browser content, editor, document). A strip anchored to the bottom edge is the
  one placement that's structurally unlikely to sit on top of the content being discussed,
  matching the macOS Siri and mobile ChatGPT voice-mode convention for the same reason.
- Center-of-screen or near-cursor placement was considered and rejected: center guarantees
  occlusion of exactly the thing being pointed at; near-cursor is unpredictable (cursor may not
  even be on the relevant monitor) and adds real implementation cost (multi-monitor geometry,
  cursor tracking) for no clarity gain in a "look at the screen" product.
- Right-edge (Windows Copilot's convention) was considered and rejected: it's the single spot most
  likely to collide with browser bookmark sidebars, IDE panels, and Windows' own notification
  toasts.

**Size:** two states only (down from v1's already-reasonable two):
- **Compact** (WOKE/LISTENING/SPEAKING): 400×72px.
- **Expanded** (THINKING/ACTING/ERROR, i.e. whenever there's a full sentence of context to show):
  560×104px.

Both stay comfortably under 1/6 of a 1080p-height screen's vertical space and are horizontally
centered, so even worst case they cover a thin strip of taskbar-adjacent desktop, never content.

**Does it move?** No panning/dragging — fixed anchor. The only motion is the shake (ERROR) and the
size morph between compact/expanded, both already vertical/horizontal-symmetric around the same
center point so the pill never "walks" across the screen.

---

## 5. VOICE VISUALIZATION — exact algorithm

Two independent taps, same math, different accent + ring direction.

**Signal — LISTENING (user's mic):**
The app already has a live mono float32 ring buffer at 16kHz (`miko/ring_buffer.py`,
`SAMPLE_RATE = 16000`) feeding the VAD. Reuse it directly — do not open a second audio stream.
Each render tick, read the last **50ms window** (800 samples):

```python
chunk = ring_buffer.read_last(0.05)          # np.ndarray[float32], ~800 samples @16kHz
rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
```

**Signal — SPEAKING (Realtime's output audio):**
GPT Realtime streams output audio as base64 PCM16, default 24kHz mono. Decode each
`response.audio.delta` chunk before/as it's queued for playback, compute RMS the same way over
each chunk (chunks arrive roughly every 20–100ms depending on the SDK's internal buffering — do
not resample, just RMS whatever chunk size arrives and let the smoother in step 2 absorb the
irregular cadence).

**Step 1 — perceptual normalize** (same formula both directions; tune the two constants once
against a real mic during calibration, these are sane defaults borrowed from a working reference
implementation — clicky_windows' `AudioCaptureService`/`OverlayWindow.UpdateWaveformBars`, a
sibling knock-free voice-overlay product that solved this exact problem):

```python
NOISE_FLOOR = 0.008   # below this, treat as silence
GAIN        = 2.85    # raw RMS rarely exceeds ~0.35 in practice; this maps that to ~1.0
CURVE       = 0.76    # <1.0 = perceptual compression, small sounds still read as "some" motion

norm = max(rms - NOISE_FLOOR, 0.0) * GAIN
norm = min(norm, 1.0) ** CURVE      # 0.0..1.0
```

**Step 2 — attack/decay smoothing** (one-pole EMA, evaluated every render tick at `dt = 1/30s`,
i.e. the existing 33ms tick — do NOT open a separate audio-rate loop, the render loop's cadence is
plenty for how this looks, and a second timer is wasted complexity):

```python
TAU_ATTACK = 0.05   # ~50ms to rise — feels instant, not laggy
TAU_DECAY  = 0.25   # ~250ms to fall — avoids flicker between syllables, still feels responsive

attack = 1.0 - math.exp(-dt / TAU_ATTACK)
decay  = 1.0 - math.exp(-dt / TAU_DECAY)
coeff = attack if norm > level else decay
level += (norm - level) * coeff
```

`level` (0.0–1.0, smoothed) is the single number that drives everything below.

**What it drives:**
- **Core orb scale:** `1.0 + level * 0.35` (reuse v1's existing halo-ovals-plus-core technique,
  just replace the sine-driven `pulse` scale with `level`-driven scale).
- **Sonar ring radius/opacity:** reuse v1's `_rings()` exactly, but the ring spawn *interval*
  becomes silence-gated instead of a fixed 0.9s metronome — spawn a new ring only when `level`
  crosses above 0.15 from below (an onset), capped at one new ring per 250ms so fast speech
  doesn't spam rings. Ring **direction** is the LISTENING/SPEAKING disambiguator: LISTENING rings
  spawn small and grow outward (something coming *in* to the mic, visualized as gathering *toward*
  the core — start large, shrink to the core over 400ms); SPEAKING rings spawn at the core and
  expand outward (something going *out*, the familiar ripple-from-source read). This single
  direction flip is doing a lot of the "feels like a real conversational partner, not a VU meter"
  work, for near-zero extra code (same interpolation, `p` vs `1-p`).
- **Target frame rate:** 30fps (33ms tick) for the render loop, matching v1 — no faster; the ear
  and eye are both being fed a *smoothed* signal by this point, higher frame rate buys nothing.

---

## 6. COLOR + TYPE + MOTION

**Palette** (kept from v1's Ember system — the reasoning is sound, only the state mapping changes):

| Token | Hex | Use |
|---|---|---|
| `PANEL` | `#15120E` | Card background (opaque — see the light/dark-content note below) |
| `TEXT_PRIMARY` | `#F4EEE4` | Title line |
| `TEXT_SECONDARY` | `#A79C8C` | Secondary line, timer |
| `TEXT_TRANSCRIPT` | `#E6DCC8` | Live caption / transcript italic line |
| `SHADOW` | `#0A0705` | Offset drop-shadow sliver |
| **WOKE** accent | `#F4EEE4` | Near-white flash — deliberately *not* a hue, reads as "spark," not "mode" |
| **LISTENING** accent | `#3FD1C0` | Bright teal — kept from v1 verbatim |
| **THINKING** accent | `#9B80E8` | Amethyst — kept from v1 verbatim |
| **SPEAKING** accent | `#E8A83D` | Warm gold — repurposed from v1's TEACHING (TEACHING is cut, the hue is too good to waste) |
| **ACTING** accent | `#5BC98A` | Jade — kept from v1 verbatim |
| **ERROR** accent | `#E1604A` | Coal-red — kept from v1 verbatim |
| **FLASH_SHOT** accent | `#6FB8FF` | Ice-blue — new, dedicated to the screenshot flourish, deliberately outside the conversational palette so it reads as "a different kind of thing happened" |
| **FLASH_MEDIA** accent | `#FF8FA3` | Soft rose — new, dedicated to the play/pause flourish, same reasoning |

**Type:** Segoe UI throughout (system font, zero asset risk). Title 15px bold. Secondary/label
10px bold. Transcript/caption 13px italic. Timer in Cascadia Mono 11px (falls back to Consolas) —
kept from v1, monospace reads as "instrumentation," which is the right register for a live timer.

**Motion (durations + easing, an implementer can type these in directly):**

| Transition | Duration | Easing |
|---|---|---|
| IDLE → WOKE flash-in | 150ms | ease-out-back (overshoot 1.15× then settle to 1.0) |
| WOKE → LISTENING | 150ms | ease-out-cubic, crossfade |
| Compact ↔ Expanded size morph | 180ms | ease-out-cubic (kept from v1's `SIZE_EASE`) |
| Accent color lerp on any state change | 180ms | linear (color lerps read fine linear; easing color is not worth the complexity) |
| Sonar ring lifetime | 400ms (LISTENING, inward) / 550ms (SPEAKING, outward, slightly slower so it reads as "settling" rather than "urgent") | ease-out-cubic |
| ACTING flash | 120ms | ease-out |
| ERROR shake | 350ms | damped sine, `8·sin(40t)·e^(-t/0.12)` (kept from v1 verbatim, it already works) |
| Final-state hold before dismiss | 600ms | — (static hold, not an animation) |
| Dismiss → IDLE | 300ms | ease-in-cubic, fade + shrink to 0 |

**Light-vs-dark content underneath — how this is actually solved:** the brief is right that this
is harder than it looks, but the correct solution here is to **not attempt it**. tkinter's
`-transparentcolor` gives one binary knocked-out color, not real alpha — there is no way to sample
"what's under the pill" and adapt. Trying to fake adaptive contrast (e.g., screen-sampling the
region under the pill and choosing light/dark text) is exactly the kind of speculative
"configurability nobody asked for" that eats the 3-hour budget for a marginal win. The actual fix,
already present in v1 and worth keeping deliberately: make the card **opaque** (`PANEL` is a solid
fill, not a blend) with a **1px lightened hairline outline** (`lerp_hex(TEXT_SECONDARY, PANEL,
0.55)`) and a **crisp offset shadow sliver**. An opaque card with a defined edge and a shadow reads
as "a physical object resting on top of the desktop" on *any* background, light or dark, precisely
because it never blends with what's under it. This sidesteps the alpha problem instead of solving
it, and is the right trade for the toolkit chosen.

---

## 7. THE TWO INSTANT ACTIONS

Both `screenshot` (`mss`, `miko/actions.py:_grab_screenshot`) and `media` play/pause
(`SendInput` VK toggle, `miko/actions.py:media`) are already synchronous, single-call, sub-100ms
OS operations in the codebase today. The flourish must never sit in front of that call — it fires
in parallel, after the action is already dispatched, purely as a "yes, that happened" confirmation.

**Design: a second, separate, tiny always-on-top window** — not the conversation pill. Reasons:
1. It must never collide with or interrupt whatever the conversation pill is mid-animation doing
   (e.g., a screenshot fired while THINKING is on screen for an unrelated turn — this can't happen
   given one busy-gated cycle at a time, but architecturally two independent windows means the
   flourish code never has to reason about the conversation state machine at all — zero coupling,
   less code, less risk).
2. It's genuinely a different *kind* of feedback (an instant utility ack, not a conversational
   state) — giving it a dedicated tiny 72×72 circular badge window keeps that distinction honest
   instead of overloading one state machine with two unrelated meanings.

**The flourish itself:** a 72×72 circular badge, same bottom-center anchor as the conversation
pill (consistent "home"), holding a single glyph (camera outline for screenshot, ▶/⏸ glyph for
media) tinted in its dedicated accent (§6).

- 0ms: action already dispatched (fire-and-forget, non-blocking call from the detector thread via
  the same thread-safe queue pattern as `set_state`).
- 0–120ms: scale-in 0.5→1.1 ease-out-back, alpha 0→1.
- 120–280ms: hold at scale 1.0.
- 280–400ms: fade out, alpha 1→0, no scale change (a shrinking *and* fading exit reads as
  "dismissed"/hurried; a pure fade reads as "settled" — settled is the right feeling for a
  confirmation, not an alarm).

Total: 400ms, entirely decorative, zero gating. This satisfies "felt but not annoying": it's
on-screen for less time than a single blink-to-blink cycle, never queues (a second instant action
firing mid-flourish just restarts the same badge's clock — no stacking, no queue of flourishes).

---

## 8. WHAT TO CUT — ranked for the 3-hour budget

**MUST (genuinely buildable tonight, in order of build time):**
1. Add `WS_EX_NOACTIVATE` to the existing exstyle bitmask (§9) — 5 min, closes a real hard-constraint gap.
2. Remap state names/colors: drop `TEACHING`/`DONE`-as-primary, add `SPEAKING`, add `WOKE` — 15 min.
3. IDLE = fully invisible (alpha 0 or `withdraw()`), remove the persistent "knock twice" pill — 15 min.
4. Idle-cheap tick: skip repaint when nothing changed (§9 dirty-check) — 20 min.
5. Wire real mic RMS into the existing `_rings()`/orb-scale machinery for LISTENING — 45 min (the biggest item).
6. Wire real output-audio RMS for SPEAKING using the same code path, gold accent, outward ring direction — 20 min (mostly copy-paste of #5 once it exists).
7. WOKE flash-in beat — 15 min (adapt existing `flash`/`check` glyph timing).
8. Two instant-action flourish badges (screenshot/media) as a second tiny window — 30 min.
9. Keep THINKING arc, ACTING chip, ERROR shake exactly as v1 — 0 min, already done.

Running total: **~2h45m**, inside the 3-hour budget with ~15 min slack.

**SHOULD (cut first if time runs out, in cut-first order):**
- Live-caption text during SPEAKING (needs the Realtime transcript stream wired to the label —
  nice but the audio-reactive orb alone communicates "it's talking" even with no caption).
- Ring-spawn-on-onset gating (§5) — fall back to a flat "always spawn every 400ms while level > 0"
  if the onset-detection logic isn't worth the debug time; visually close enough.
- Distinct SPEAKING vs LISTENING ring *direction* — fall back to same-direction rings, differ by
  color alone if out of time. Loses some of the "feels alive" quality but is not broken.

**NICE (do not attempt tonight, list only so nobody feels bad cutting them):**
- Real Gaussian blur / soft shadow (needs PyQt6 — explicitly rejected in §1).
- 5-bar waveform instead of ring-based visualization (clicky_windows' approach — genuinely nice,
  but rings are already built in v1 and bars would be a from-scratch Canvas item set).
- Cursor-following or multi-monitor-aware placement.
- Adaptive light/dark contrast sampling (§6 explains why this is actively not worth doing).
- Custom logo mark / bespoke font.
- Particle systems, spectral/frequency-band color shift (RMS-only is enough; band-splitting adds
  an FFT per tick for a difference most viewers won't consciously register).

---

## 9. Code sketches — the hard parts

### 9.1 Window setup, with the `WS_EX_NOACTIVATE` fix

```python
def _make_click_through(self):
    """Click-through AND focus-proof: layered+transparent (clicks pass through),
    toolwindow (hidden from alt-tab), and — the fix — WS_EX_NOACTIVATE so the
    window can never become the foreground window, even via SetForegroundWindow
    calls elsewhere in the process or a stray click landing during a frame where
    TRANSPARENT hasn't fully applied yet. v1 was missing this."""
    import ctypes
    GWL_EXSTYLE       = -20
    WS_EX_LAYERED     = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW  = 0x00000080
    WS_EX_NOACTIVATE  = 0x08000000   # <-- new
    hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
    get_long = ctypes.windll.user32.GetWindowLongPtrW
    set_long = ctypes.windll.user32.SetWindowLongPtrW
    style = get_long(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    set_long(hwnd, GWL_EXSTYLE, style)
```

### 9.2 Idle-cheap tick (dirty-check, "no busy render loop when idle")

```python
def _tick(self):
    if self._closed or self._root is None:
        return
    self._drain()                      # cheap: pops the queue, always runs
    now = time.monotonic()
    v = self.model.view(now)

    # Only touch the Canvas (the expensive part — triggers a Tk redraw pass)
    # when the view actually changed, OR the current mode is still animating
    # (has a live orb/ring/shake). IDLE with an empty queue produces the exact
    # same `v` every tick, so this skips ~30 no-op Canvas writes/sec at rest.
    changed = (v != self._last_view)
    animating = v["mode"] != "IDLE"
    if changed or animating:
        self._paint(v, now)
        self._last_view = v

    # Idle AND static: no point polling at 33ms either — the knock detector
    # runs on its own thread regardless, so slowing the overlay's own poll
    # doesn't add wake latency, it just stops burning a timer 30x/sec for
    # nothing. Snap back to 33ms the instant something is queued.
    interval = 33 if (animating or not self._queue.empty()) else 200
    self._root.after(interval, self._tick)
```

### 9.3 Amplitude-driven level, feeding the model

```python
# In the audio-tap thread (or a tiny per-tick pull from ring_buffer — no new
# thread needed for the mic side, it reuses the existing ring buffer):

NOISE_FLOOR, GAIN, CURVE = 0.008, 2.85, 0.76
TAU_ATTACK, TAU_DECAY = 0.05, 0.25

class LevelSmoother:
    def __init__(self):
        self.level = 0.0
        self._last_t = None

    def push(self, rms: float, now: float) -> float:
        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        self._last_t = now
        norm = max(rms - NOISE_FLOOR, 0.0) * GAIN
        norm = min(norm, 1.0) ** CURVE
        tau = TAU_ATTACK if norm > self.level else TAU_DECAY
        coeff = 1.0 - math.exp(-dt / tau) if dt > 0 else 0.0
        self.level += (norm - self.level) * coeff
        return self.level

# Overlay.set_level(source, rms) is a third thread-safe queue push, exactly
# like set_state — the app-side callers:
#   LISTENING: every ~50ms, mic_smoother.push(rms_of(ring_buffer.read_last(0.05)), now)
#   SPEAKING:  on every Realtime output audio delta, out_smoother.push(rms_of(pcm16_chunk), now)
# OverlayModel.view(now) reads whichever smoother is live for the current mode
# and uses `.level` to drive orb scale + ring radius/spawn exactly as in §5.
```

### 9.4 State model additions (extends `OverlayModel`, does not replace it)

```python
_ACCENTS = {
    "IDLE":      "#726A5C",   # unused (nothing renders), kept for NEUTRAL fallback parity
    "WOKE":      "#F4EEE4",
    "LISTENING": "#3FD1C0",
    "THINKING":  "#9B80E8",
    "SPEAKING":  "#E8A83D",   # was TEACHING's hue in v1
    "ACTING":    "#5BC98A",
    "ERROR":     "#E1604A",
}
_EXPANDED_MODES = frozenset({"THINKING", "ACTING", "ERROR"})

# apply() gains two branches:
#   state == "WOKE"      -> self._enter("WOKE", detail, now)          # ~200ms, auto-advances
#   state == "SPEAKING"  -> self._enter("SPEAKING", detail, now)      # gold, outward rings
# _rings() gains a `direction` field ("in" for LISTENING, "out" for SPEAKING)
# so the paint layer can flip which end of the interpolation is the spawn point.
```

---

## 10. IMPLEMENTATION ORDER

Ordered so that if time runs out at any point, what exists still looks *finished*, not
half-built — always ship the version where every state that exists is fully polished, rather than
every state existing but half-polished.

1. **Fix `WS_EX_NOACTIVATE`** (§9.1) — 5 min. Do this first; it's a correctness fix independent of
   everything else and trivial to forget once the rest gets exciting.
2. **State remap + IDLE-invisible** (§8 items 2–3) — the app is now *correct but plain*: right
   states, right colors, nothing shows at rest. This alone is already a big visible improvement
   over v1's "always-on pill."
3. **Idle-cheap tick** (§9.2) — do this while the diff is small and the states are simple; it's
   much easier to verify "does the CPU actually go quiet at rest" before amplitude-driven
   animation makes every state look busy.
4. **WOKE flash-in** — the demo's first "wow, it woke up" beat. Cheap, high-value, do it before
   the harder audio wiring so there's a good fallback if #5 runs long.
5. **LISTENING real mic RMS** (§5, §9.3) — the single biggest-effort, biggest-payoff item. If this
   is where the clock runs out, the fallback is v1's existing fixed-metronome rings, which still
   look intentional (just not audio-reactive) — never leave LISTENING with *no* rings.
6. **SPEAKING** — copy #5's machinery, gold accent, flip ring direction. Should be fast once #5
   exists; if not, fall back to same-direction rings, differ by color only (still reads fine).
7. **Two instant-action flourishes** — independent window, no dependency on 2–6, can be built by a
   second person in parallel if pairing, or slotted in whenever there's a natural break.
8. **THINKING/ACTING/ERROR** — verify only; these are already correct from v1 and shouldn't need
   changes beyond the accent remap done in step 2.

If the clock runs out after step 5: ship steps 1–5 plus v1's untouched THINKING/ACTING/ERROR. That
is a coherent, demo-able product — invisible at rest, a real "it heard me" moment, a genuinely
audio-reactive listening state, and three already-solid states carried over unchanged. Nothing in
that set looks unfinished.

---

## 11. References consulted

- **macOS Siri orb** — bottom/edge-anchored ambient glow, informed the position decision (§4) and
  the "opaque card, don't fight translucency" resolution (§6).
- **ChatGPT Advanced Voice Mode** — animated pulsing circle/orb whose motion directly reflects
  listening vs. speaking, the strongest precedent for the LISTENING/SPEAKING directionality choice
  in §5. ([ToolChase, "ChatGPT Voice Mode: Complete Guide"](https://toolchase.com/blog/chatgpt-voice-mode-guide/); [OpenAI Developer Community thread on voice-mode visuals](https://community.openai.com/t/voice-mode-visual-improvements/1386614))
- **Windows Copilot** — right-edge placement, considered and explicitly rejected in §4 (sidebar
  collision risk).
- **Wispr Flow / Superwhisper** — hold-to-talk / system-wide dictation overlays; confirmed the
  "small, bottom-anchored, non-blocking" convention this design follows.
  ([Wispr Flow vs Superwhisper comparison, getvoibe.com](https://www.getvoibe.com/resources/wispr-flow-vs-superwhisper/))
- **`clicky_windows`** (local reference, C#/WPF, sibling product — knock-free voice overlay with
  screen capture) — the RMS-floor/gain/curve constants in §5 and the 5-bar waveform technique
  (listed as a NICE-to-have in §8) are taken directly from its
  `Services/AudioCaptureService.cs` (`CalculateRms`) and `OverlayWindow.xaml.cs`
  (`UpdateWaveformBars`), a working implementation of exactly this problem.
- **`porcupine`** (local reference) — wake-word engine, not a UI reference; reviewed and found not
  applicable to the visual design (Miko's wake signal is a knock classifier, not a keyword
  spotter, and porcupine ships no overlay/UI code to borrow).
- **PyQt6 frameless/translucent overlay patterns** (`PyQt-Frameless-Window`, Qt Forum threads) —
  reviewed to make the §1 toolkit call honestly rather than assert it; confirms PyQt6 needs the
  same raw `ctypes`/`WS_EX_*` interop as tkinter for true click-through, so it does not remove the
  Windows-specific risk, only adds a rendering-fidelity upside.
