# The live tutor — for the report

What it is, why it is built the way it is, and what can be claimed about it.
Written 2026-09-20. Everything here is implemented and tested off recorded
sessions; nothing has been through a live apparatus run yet.

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
same live status the side panel reads, and recognises four stalls:

| it speaks after | what that means |
| :-- | :-- |
| 90 s tuning one droplet without reaching balance | the droplet may not be worth the time |
| the pick lost 3 times in a minute | the aid is not holding on to it |
| the verdict flipping USABLE/DISCARD 3 times in a minute | the reading is not settling |
| 3 minutes on one droplet with no legal fall | something in the procedure is not landing |

Only then is the model asked anything, and only to explain the situation the
rules have already found. This keeps the triggering deterministic and reviewable:
the same session replays to the same triggers.

**Restraint is the design.** At most one message every 2 minutes, at most twice
for the same complaint, six per session, never while a fall clock is running.
Replaying two real 40-minute sessions, an unrestrained version spent all six of
its messages on one complaint inside the first quarter of an hour; with the caps
it speaks twice in forty minutes.

## The second half: "Why?"

Next to the aid's guidance line is a **Why?** button. It explains the instruction
the student is looking at — the physics behind it and what it means for the
measurement. It answers a button, so it cannot interrupt anything.

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

Two further layers, because a seal that is only as good as its author's care is
not a seal:

- a reply that states a voltage or a charge anyway — a model can invent one — is
  **withheld, not shown**;
- every trigger, the prompt it was given and what it said, including anything
  withheld, is written to `test/output/tutor_log.jsonl` for review after a run.

## What was tried and dropped

An end-of-session **debrief** was built and removed. On a finished session there
are about four facts worth stating — droplets measured against the target of 3,
falls against 5, the scatter of the repeats, and anything genuinely wasted — and
code states all four correctly, instantly, every time. The model added only
variance: it padded, it mis-ranked which fact mattered, and on one session it
read 19 traverses of debris as the student's failed attempts. It was dropped
rather than kept as decoration.

This is worth saying in the report as a design decision, not an omission: the
model is used where it explains, and not where arithmetic is already correct.

## Honest limits

- Not yet validated at the apparatus. The triggers fire correctly on recorded
  sessions; whether they fire *at the moments a student wants help* is unknown.
- One active model call per chat session: if the student is typing a question
  when a stall fires, the tutor stays silent rather than queueing.
- English only. Matching the student's language is future work.
