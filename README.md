# Miko

Miko is a knock-summoned Windows desktop agent: knock twice on your laptop chassis to summon an agent that sees your screen, listens, and acts. Built for OpenAI Build Week.

Runs on Windows 11, Python 3.11+.

## Quickstart

```
pip install -r requirements.txt
python -m miko --calibrate      # one-time: pin the mic + learn your knock
python -m miko                  # start listening (MockBrain, zero spend)
```

Flags:

- `--mock` (default) — run against the offline MockBrain; no model spend.
- `--real` — use the Codex brain (spends model quota).
- `--thump` — SIMPLE mode: any loud chassis thump wakes Miko (classifier off).
- `--device N` — override the input device index.
- `--calibrate` — run the knock calibration wizard and exit.

Teach mode: knock, then say "learn a new trick: …" and Codex writes a new
skill into `skills/`; it is hot-loaded on your next knock.
