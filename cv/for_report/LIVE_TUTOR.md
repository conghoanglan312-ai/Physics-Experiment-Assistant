# The live tutor — for the report

What it is, why it is built the way it is, and what can be claimed about it.
Written 2026-09-20, revised 2026-09-22 after four live apparatus sessions.

## The claim

The chat assistant answers questions. The **live tutor** is different: it watches
the session as it happens and speaks on its own when the student is stuck.

The interesting part is what it does *not* do. The measurement aid already tells
the operator what to do — every frame, measured, and never wrong about what it
measured. Restating that through a language model would be slower, cost tokens,
and add nothing. So the tutor is given only the job the aid cannot do: the aid
sees one frame, and **being stuck is a shape in time**.

## How it decides to speak

The decision is ordinary code (`app_js/tutor.py`), not the model. It watches the
same live status the side panel reads, and recognises five stalls:

| it speaks after | what that means |
| :-- | :-- |
| 90 s tuning one droplet without reaching balance | the droplet may not be worth the time |
| 3 minutes on one droplet with no legal fall | something in the procedure is not landing |
| 2 minutes of session with no legal fall at all | the hunt itself is not converging |
| the pick lost 3 times in a minute | the aid is not holding on to it |
| the verdict flipping USABLE/DISCARD 3 times in a minute | the reading is not settling |

Only then is the model asked anything, and only to explain the situation the
rules have already found. This keeps the triggering deterministic and reviewable:
the same session replays to the same triggers.

**Restraint is the design.** Nothing in the first 45 s, then at most one message
every 2 minutes, 7 minutes before the same complaint may repeat, at most twice
for one complaint, six per session, never while a fall clock is running.
Replaying two 40-minute sessions, an unrestrained version spent all six of its
messages on one complaint inside the first quarter of an hour; with the caps it
speaks twice in forty minutes.

## The second half: "Why?"

Next to the aid's guidance line is a **Why?** button. It explains the instruction
the student is looking at — the physics behind it and what it means for the
measurement — and the answer appears in the same panel, directly under the
button. The student never has to open a chat window mid-measurement.

Speed is the requirement, so it does not go through the chat path at all.
`app_js/quick_ai.py` makes **one** model call: no retrieval round trip, no
session lock, the manual read once when the process starts and the relevant
passage selected by scoring it against the question. Measured against a running
session: **2.2 s**, at most two sentences.

The instruction is passed as a *kind*, never as its words — the aid's banner
names the plate voltage in plain text, and that must not cross (see the seal).
Every line the aid can display has a kind, including the tallies (`MEASURING
n of 5`, `DONE`), a held lift, an unreadable panel, a running fall clock, and
the advisory-band warning.

Example, on a balanced droplet:

> The measurement aid is indicating that the droplet is currently balanced,
> meaning the upward electric force exactly counteracts the downward force of
> gravity, leaving the droplet essentially stationary. However, because you are
> using the static method, you cannot measure a falling time while the droplet is
> balanced. To proceed, you must switch the voltage to zero, which allows the
> droplet to fall at a constant terminal speed, providing the data needed to
> determine its charge.

## The seal

Rubric 三.1.4 forbids the AI producing the student's result. The charge q, the
electron count n and the elementary charge e are computed inside the CV module
and never cross to the model — enforced structurally by an allow-list
(`LLM_KEYS` in `cv/api.py`), not by asking the model nicely.

The **plate voltage** is part of that. q = K(t)·d/U_n, and the tutor has the lab
manual in retrieval, so a fall time together with the balance voltage *is* the
student's answer. The voltage therefore does not cross either — not the reading,
and not the aid's own banner text, which names it in plain words ("BALANCED to
0.0% at 153 V"). What the tutor sees is times, speeds, counts, field state, and a
boolean saying whether the working voltage is inside the manual's band.

Three further layers, because a seal that is only as good as its author's care is
not a seal:

- a reply that states a voltage or a charge anyway — a model can invent one — is
  **withheld, not shown**;
- so is a reply that argues against balancing the droplet. Asked why the aid
  kept losing a balanced droplet, the model once answered that the software
  struggles to track stationary droplets and suggested keeping it in slow motion
  instead. That is the experiment backwards: the stillness *is* the measurement.
  Both prompts now say so, and the pattern is refused whatever the prompt does;
- every trigger, the prompt it was given and what it said, including anything
  withheld, is written to `test/output/tutor_log.jsonl` for review after a run.

Both filters guard the volunteered messages and the **Why?** answers alike — the
same student reads both, on the same panel.

## Switching it off

`MILLIKAN_TUTOR=0` starts the app with the tutor silent. The measurement aid is
unaffected: nothing it does depends on the model being reachable, and a session
with no network produces the same falls, the same voltages and the same charges.
