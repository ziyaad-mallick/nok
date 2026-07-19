# Codex Call Ledger

Per Amendment 1 (Eng Review): every `codex exec` call is wall-clock timed and the
served model id is checked. This file rations the scarce free-tier quota across
build phases and records what has actually been spent.

**Auth:** ChatGPT login (`auth_mode: chatgpt`, no API key). **Model:** GPT-5 (self-reported).
**Zero-spend rule:** `codex exec` never runs from an automated/CI path — only manual,
capped, user-driven probes and the deliberate filming spends.

---

## Verification probe — 2026-07-19 (contest-fatal check, Open Risk #1)

`scripts/verify_codex.py` (default 4 calls). Ran clean after two Windows portability
fixes (`.cmd`-shim resolution + UTF-8 output decoding). **Verdict: PASS — the
zero-spend architecture is viable on this machine.**

| Call | Prompt intent | Latency | Result | Model output |
|---|---|---|---|---|
| 1 | Strict JSON action contract | 17.2s | **PASS** | Byte-exact `{"action":"open_app","args":{"name":"spotify"},"say":"opening spotify"}` |
| 2 | Natural-language, JSON not demanded | 38.0s | Free-formed | Conversational prose, no JSON — confirms output must always be JSON-pinned |
| 3 | Model self-ID | 20.3s | **PASS** | "I'm Codex, based on GPT-5. My model ID/name is `GPT-5`." |
| 4 | Vision (screenshot attach) | n/a* | **PASS** | Correctly described the attached screen capture |

\* Call 4 timing not captured — the first probe run crashed while saving debug logs
(cp1252 decode bug, since fixed); codex's `-o` output itself was written fine.

**Budget spent to date: 4 calls** (one probe run; the earlier crash was before the
encoding fix but codex still executed all 4, so they count as spent).

### What the probe settled
- **Open Risk #1 (was: "does `codex exec` work at all on the free login") → RESOLVED.**
  JSON contract holds, vision works, auth is live.
- **Open Risk #5 (latency) → partially measured:** real intent-call latency here is
  ~17–38s, consistent with the plan's ~10–30s assumption (call 2's 38s is the ceiling
  seen so far). Confirms the overlay's job is to absorb this as visible "thinking."
- **Design implication (from call 2):** the model reverts to prose when output isn't
  explicitly constrained. The dispatcher's parser must fail loudly when no JSON block
  is found — never assume the model self-structures.

### Still unverified (not answered by this probe)
- **Free-tier quota caps** (Open Risk #2): whether a fresh window exists Sun→Mon.
  Weekly caps may bind. The Monday-filming "fresh quota" assumption remains unverified.

---

## Budget allocation (remaining phases)

Filled in as phases execute; all non-Codex demo beats run against mocked/cached output.

| Phase | Planned calls | Spent | Notes |
|---|---|---|---|
| Verification probe | ≤6 | 4 | done — PASS |
| Real-`exec` pipeline swap (Sun eve) | TBD | 0 | after mock pipeline works end-to-end |
| Teach-mode codegen (Sun eve) | TBD | 0 | compliance-floor beat; pre-record a good take |
| Filming (Mon) | TBD | 0 | assumes fresh window — unverified |
