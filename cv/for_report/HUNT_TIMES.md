# Time to find a measurable droplet

**Hunt time** = spray → first legal fall: spray, search, charge-test, balance.
It is the target because measuring is already fast. On 2026-09-08 the fifteen timed
falls took 7.5 minutes of a 40-minute session; the other **81%** was hunting, and it is
the only place a saving can come from.

Every hunt starts at the spray that began it — the session's first for the first
droplet, the first after the previous droplet's last fall thereafter. Reading off and
recording the previous droplet's numbers is not hunting, and neither is anything else
done between runs. Nothing in a recording says "sprayed", but a spray is the only thing
that puts droplets in an empty chamber, so the first traverse to begin afterwards
stands in for it. On 2026-09-09 that lands at 6:11 against an operator note of 6:02.

All figures measured from the recordings, so sessions predating the question can still
be scored. Reproduce with `python test/tools/hunttime.py <session dirs>`.

## Every hunt measured

| session | droplet | spray (s) | 1st fall (s) | **hunt (s)** | aid |
|---|---|---|---|---|---|
| bench_0908 | 1 | 81 | 588 | **508** | no |
| bench_0908 | 2 | 958 | 1401 | **443** | no |
| bench_0908 | 3 | 1750 | 2141 | **392** | no |
| live 0908 19:51 | 1 | 35 | 378 | **343** | no |
| live 0908 19:51 | 2 | 792 | 2035 | **1243** | no |
| live 0909 15:20 | 1 | 171 | 712 | **541** | no |
| live 0909 15:20 | 2 | 1097 | 1561 | **463** | no |
| live 0909 21:45 | 1 | 371 | 690 | **319** | **yes** |
| live 0910 12:52 | 1 | 24 | 178 | **154** | **yes** |
| live 0910 14:58 | 1 | 9 | 152 | **143** | **yes** |
| live 0910 14:58 | 2 | 610 | 914 | **304** | **yes** |
| live 0910 18:49 | 1 | 12 | 231 | **219** | **yes** |

| | n | median | mean | range |
|---|---|---|---|---|
| unaided | 7 | 463 s | 562 s | 343–1243 |
| **aided** | 5 | **219 s** | 228 s | **143–319** |

## Result

| comparison | saving |
|---|---|
| aided median vs unaided median | **−244 s, −53%** |
| slowest aided (319 s) vs fastest unaided (343 s) | −24 s, −7% |
| aided–unaided pairs in which the aided hunt is faster | **35 of 35** (Mann–Whitney U = 0, p ≈ 0.003 two-sided) |

**The two groups do not overlap.** Every aided hunt is faster than every unaided hunt.
That is the statement to quote: it uses no mean and no pooling, and the second row is
what makes it hold at the worst case rather than the best.

Roughly: hunting takes about **8 minutes unaided and about 4 aided**, while measuring a
droplet takes 4–8 minutes either way (236–483 s). Over a five-droplet session that is
close to 20 minutes returned.

## The like-for-like check

A session's first droplet is found in an empty chamber; a later one is found while the
previous droplet's debris is still settling, and the operator has just finished a
measurement. Only later-in-session droplets compare exactly.

| | n | median | range |
|---|---|---|---|
| unaided, later in session | 4 | 453 s | 392–1243 |
| aided, later in session | 1 | **304 s** | — |

304 s beats the fastest unaided later-in-session hunt by 87 s (−22%). This was the
comparison missing from every earlier version of this table, and it was the one that
could have been argued to read in the aid's favour. It no longer can.

## What is not yet established

- **n = 5 aided, one of them later-in-session.** The separation is clean but the counts
  are small.
- **The mechanism rests partly on the operator's account.** On 0909 the operator states
  they would have re-sprayed past the droplet the aid ringed. That matches what the aid
  is for, but it is a counterfactual and cannot be recovered from the recording.
- **A learning curve runs alongside.** Unaided hunts fell 508 → 443 → 392 inside one
  session, so operator practice explains part of any improvement. It does not explain
  all of it: every aided figure sits below the whole unaided range, including the
  operator's best unaided hunt (343 s).
- **The 1243 s hunt is genuine** and is kept. It is the single worst hunt on record and
  it belongs in the unaided range.

## Known costs

The aid has cost measurable time twice, and both are inside the figures above rather
than excluded from them:

- 0909: a completed fall was not drawn, the operator judged it undetected and
  re-dropped — about 20 s, inside the 319 s. Fixed, DESIGN_NOTES §17.
- 0910: a completed legal traverse (25.3 s at 0.316 grid/s) was never recorded, because
  track identity broke mid-descent. It cost droplet 2 its second fall.

Two sessions also ended short of five falls when a droplet drifted sideways into a grid
line and the track was lost. That is an unlevelled platform (manual Ⅳ.1), not the aid,
but it bounds how much of a session the aid can currently save.
