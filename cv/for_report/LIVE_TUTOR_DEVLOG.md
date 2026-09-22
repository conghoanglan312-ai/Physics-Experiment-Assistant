# Live tutor — dev log

What was built, what broke, and what was thrown away. `LIVE_TUTOR.md` describes
the thing as it stands; this is how it got there. Kept separate because the
report wants the design, not the archaeology.

## 2026-09-20 — built

`e4c55ae` The decision to speak is ordinary code, never the model: rules over the
live status recognise a stall, and only then is the model asked, and only to
explain what the rules already found. Same session, same triggers, every replay.

`cb96fd7` Rails, before any of it ran at a bench: an offline replay harness, the
message caps, and the voltage withheld from every prompt.

`aee71fb` Two things the student asks for — a **Why?** button and an end-of-
session **debrief**.

`33a8a1a` The debrief's first version judged the camera, not the operator. Three
faults in one feature:

- it pooled the scatter across droplets and announced "27.4% standard deviation"
  about six droplets that had one fall each — a number with no meaning;
- it counted every debris traverse as one of the student's attempts (29
  traverses on a 9-minute session, 5 of them the operator's);
- grounded on the whole manual, it called a slow droplet "too large" — the
  physics was wrong because the passage it quoted was about something else.

Fixed by computing scatter per droplet, grouping falls into droplets before
counting attempts, and slicing the manual to the question.

## 2026-09-21 — the debrief dropped, and the Why? made fast

`f7bd788` On a finished session there are about four facts worth stating, and
code states all four correctly, instantly, every time. The model added variance:
it padded, and it mis-ranked which fact mattered. **Dropped entirely** rather
than kept as decoration — the model is used where it explains, not where
arithmetic is already right.

The same commit took the **Why?** answer off the chat path. `app_js/quick_ai.py`
makes one call: no retrieval round trip, no session lock, the manual read once
when the process starts and the relevant passage scored against the question.
Measured against a running session: **2.2 s**, where the chat path took several
seconds and could be refused outright while the student was typing.

The answer also moved out of the chat window into the Droplet Analysis panel,
under the button — the operator has both hands on the apparatus and will not
open a window mid-measurement.

**Stall interval 4 minutes → 2.** Four minutes stuck at the bench with nothing
said is too long; the operator asked for it after sitting through it.

## 2026-09-22 — the two things it got wrong in front of an operator

**It argued against the experiment.** Asked why the aid kept losing a balanced
droplet, it answered that the software struggles to track stationary droplets
and suggested keeping the droplet in slow motion instead. The stillness *is* the
measurement. Both prompts now say so, and a reply matching that shape is
withheld whatever the prompt does — a model cannot be argued out of this
reliably.

**It answered the wrong question on a finished droplet.** `bannerKind()` had no
entry for "DONE — 5 of 5 falls", so Why? fell through to a generic answer about
balancing. Added the tallies, a held lift, an unreadable panel, a running fall
clock, RAISE/LOWER and the advisory band.

**The answer vanished about a second after it arrived.** The panel discarded it
whenever the guidance line changed, and that line carries live numbers — "falls
0.32 gr/s" changes every frame. It now compares the sentence with the digits
stripped out.

## Infrastructure, which cost more time than the tutor did

Gemini refuses Vietnamese IPs outright: `400 FAILED_PRECONDITION, User location
is not supported`. Everything routes through Clash, and three things about that
were learned the hard way:

- `prepend-rules` in the Merge file does not take; the same rules applied through
  `main(config)` in the **Global Extend Script** do;
- in **Global** mode rules are ignored entirely — all traffic takes the one
  selected node, so the script does nothing;
- Claude Code has to be excluded by rule (`anthropic.com`, `claude.ai` → DIRECT)
  or the session dies with a 403 the moment the proxy comes up.

The script is kept at `test/clash_extend_script.js`, because uninstalling Clash
takes the profile store — and the script — with it.

## What this cost, as a design argument

The tutor is about 390 lines of rules and prompts. The measurement aid it sits
on top of is the rest of the project. That ratio is the argument: the model is
given the one job the aid cannot do — noticing that being stuck is a shape in
time — and is kept away from every number the student is supposed to produce.
