# HANDOFF — NOK ship-log post (LinkedIn + X), staged for manual posting

## KICKOFF PROMPT — paste this into Opus verbatim

> Read `30-work/nok/HANDOFF-shiplog-post.md` in full and execute it exactly.
> Follow Section 1 (hard rules) literally. Do not deviate from the deliverables
> in Section 3. Do not post anything anywhere — you stage files, the user posts.
> When done, list the files you produced and stop.

---

## 1. Hard rules for Opus

1. **You never post, publish, or log into anything.** You produce text files and
   image-selection instructions. The user posts manually.
2. **Never download anything.** No models, no libraries, no stock images.
3. **All facts about NOK come from `30-work/nok/README.md` and the repo itself.**
   If a claim isn't verifiable in the repo (test count, features), don't write it.
4. **Read the clock, don't infer dates.** Use the system date for any date you write.
5. Stay inside `30-work/nok/`. Create outputs in `30-work/nok/shiplog/`.
6. No emojis in LinkedIn copy beyond at most one. X copy may use zero or one.
7. Do not soften Section 4's honesty framing into generic hype. The missed
   deadline IS the story. Hype-flavored rewrites are a failure.

## 2. Context (do not re-research this)

NOK is a knock-summoned desktop agent: knock twice on the laptop chassis → it
wakes, listens, and acts through a whitelisted action layer (open apps, media,
type, screenshot, teach-mode where GPT writes a new skill live). Built solo in
~3 days (Jul 18–20), ~200 tests green, working end-to-end demo — then broke
late on real-hardware audio issues, and the OpenAI Build Week deadline
(Jul 21) was missed after debugging up to 10 minutes before cutoff. The post is
the fallback the plan already requires: ship the artifact publicly anyway,
paired with 5 DMs (the DMs are the user's job; you prepare the template).

Author: Ziyaad Mallick, 19, CS student, Karachi. Voice: direct, technical,
self-aware, zero LinkedIn-guru cadence, no "I'm humbled to announce."

## 3. Deliverables (all in `30-work/nok/shiplog/`)

1. `post-linkedin.md` — final LinkedIn post copy (see Section 4).
2. `post-x.md` — X version: a 3–5 tweet thread. Tweet 1 must stand alone.
3. `images.md` — exact shot list the user captures (Section 5), with crop specs.
4. `dm-template.md` — one 3-sentence DM template referencing the post, with
   blanks for [name] and [specific reason for contacting them]; plus a note
   that the user fills the 5-name target list himself.
5. `checklist.md` — post-order: capture images → post LinkedIn → post X →
   pin X thread → send 5 DMs same day → reply to every comment within 24h.

## 4. The post — structure to write (LinkedIn ~150–220 words)

Hook (line 1, before the fold): the honest paradox. Reference draft — improve,
don't inflate:

> I built a desktop agent you summon by knocking on your laptop. It worked.
> I still missed the deadline I built it for.

Then, in order:
- What NOK is, in two lines, concrete ("knock twice → it wakes, listens, and
  acts — open apps, control media, teach it new skills by talking to it").
- The three-day build in one line with one real number (tests green, commits).
- What actually happened: it was working; real-hardware audio (mic gain, VAD,
  Windows audio stack) broke it late; debugged to 10 minutes before cutoff.
- The lesson, stated plainly, no moral-of-the-story tone: demos break where
  software meets hardware; reliability is the feature.
- Close: repo is public, link; "building in public from here on."
- End with 3–5 niche hashtags (#buildinpublic #ai #agents), never 10.

X thread: same story, tighter; tweet 1 = hook + demo GIF; last tweet = repo
link + "follow along."

## 5. Images (engagement guidance — include this in `images.md`)

Priority order (native-looking screenshots outperform designed graphics):
1. **Demo GIF/video ≤30s** — knock-knock → Ember HUD wakes → "open spotify" →
   it opens. Screen-record with phone-camera-visible knock if possible (the
   physical knock is the scroll-stopper). LinkedIn: upload as native video.
2. **Ember HUD screenshot** — the glowing overlay pill on a clean desktop.
3. **Terminal screenshot** — the test suite green summary line (crop tight,
   dark theme, readable at phone width).
LinkedIn: 1200×627 landscape or 1080×1080 square; video beats image beats text.
X: 16:9, tweet 1 gets the GIF.
Never: stock images, AI-generated art, screenshots with notification clutter.

## 6. Definition of done

All 5 files exist, LinkedIn copy ≤220 words, every factual claim traceable to
the repo, no posting attempted.
