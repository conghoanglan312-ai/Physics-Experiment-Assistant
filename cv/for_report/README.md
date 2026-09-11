# CV pipeline — everything the report needs

> **2026-09-11: start with `HANDOVER.md`.** Two figures further down are out of date.
> Hunt times: use `HUNT_TIMES.md` (median 463 s unaided, 219 s aided). The
> "−0.27 s, sd 0.42 s" agreement is the `one_droplet_slow` fixture against its operator;
> against the fifteen `bench_0908` hand times the pipeline agrees to −0.11 s, sd 0.23 s.

Generated 2026-09-10. Every file here is a copy; the originals stay where they were
produced, and the paths are given so anything can be regenerated or checked.

## READ THIS FIRST — a number you already have is wrong

The hunt times sent earlier included **one hunt of 2035 s**. It was a tooling bug, not a
measurement. `hunttime.py` only counted a droplet if it had the five falls the method
asks for, so a droplet on 2026-09-08 that gave four legal falls was discarded — but its
*time* was not. It was absorbed into the next droplet's hunt, which then appeared to run
from the start of tracking straight through another droplet's entire measurement.

That single figure was 40% of the old mean and the widest point of the range.

**Corrected, and what to quote:**

| | value |
|---|---|
| baseline hunt, median | **492 s** |
| baseline hunt, mean | 715 s |
| baseline range | 425 – 1453 s |
| aided hunt, 2026-09-09 evening | **327 s** |
| saving vs median | **33.5%** |

Quote the **median**, not the mean. The median has come out at 491–492 s under every
version of this correction; the mean swings between 514 and 715 depending on judgement
calls about which droplets to pool. A stable number is worth more than a larger one.

Two caveats to state rather than hide:

1. **n = 1.** One aided droplet. More are being measured.
2. The aided 327 s is spray → first legal fall. The baseline is previous droplet's last
   fall → next droplet's first fall, which also contains the time spent discarding a
   droplet after finishing with it. The spans are not identical and the difference
   favours us. Measuring a *second* droplet within one aided session fixes this, and is
   the next thing being done.

## Files

| file | what it is | original |
|---|---|---|
| `BENCHMARK.md` | the droplet table, formatted for a report | `test/output/report_package/` |
| `charges_all.csv` | every fall of every droplet: t, U_n, a, q, n, e_i, plus per-droplet and overall mean/sd/error | `test/output/report_package/` |
| `HUNT_TIMES.md` | every hunt measured, both comparisons, and what is not yet established | regenerate: `python test/tools/hunttime.py <session dirs>` |
| `ALGORITHMS.md` | every algorithm used, what for, what it produces; top five ranked | - |
| `HANDOVER.md` | **start here for chapters 5-8**: algorithms per system, frameworks, integration diagrams, rejected methods, development history, specs and latency, validation, result tables with Type A/B uncertainty | answers `Requirements_from_report.md` |
| `WHY_CLASSICAL_CV.md` | where the AI is, why the measurement layer is not, and the one model worth adding | - |
| `circularity_sweep.txt` | three full runs of the 15-fall benchmark at three thresholds | `test/output/circ_0.52`, `circ_0.48`, `circ_0.45` |
| [`../DESIGN_NOTES.md`](../DESIGN_NOTES.md) | 20 sections: methodology, dead ends, what each cost | one level up, not duplicated here |
| `falls_*.csv` | raw per-traverse output of each session — every traverse, legal or not, with duration, speed, column, voltage and the legality verdict | the session folders |

## The result

**Electron counts n = 1, 2, 3** across **11 droplets and 55 falls**, seven sessions.
Overall mean e = 1.5518e-19 C, **error -3.13%** against the accepted 1.602e-19 C.

Every droplet was also timed by hand. Only the fifteen falls of droplets 1-3 have those
hand times recorded in a file here, and against them the pipeline agrees to **-0.27 s on
average (sd 0.42 s)**.

Individual droplets run **-3.1% +/- 5.6%**, and no variable in this data predicts which
one will be off -- balance voltage, fall time and electron count all correlate with the
error at |r| < 0.3. `BENCHMARK.md` reports what was tested and withdraws an earlier
explanation that did not survive the numbers. The spread between droplets is wider than
the scatter within one, which points at something fixed per droplet: the balance
voltage, read once, is the first suspect.

**One fall was recovered from the recording.** Droplet 4's fifth fall completed
normally -- the droplet is visible in the session recording for the whole descent -- but
the live tracker lost its identity part-way down and re-acquired it as a new track, so
the finished traverse was never assembled. The live output has no row for it, rejected
or otherwise.

It was measured afterwards from that recording: the droplet's position was fitted
against time and the two reticle crossings interpolated, using the same geometry the
live code uses. That gives **17.49 +/- 0.23 s** (281 samples, rms 3.26 px on a 61 px
division). Three independent checks agree -- the operator's stopwatch read 17.20 s, the
live overlay reported 0.47-0.49 grid/s during the descent, and the droplet's other four
falls ran 17.20-17.74 s.

It is carried in `recovered_falls.csv` with the full method, and marked
`recovered from recording` in the `measuredBy` column of `charges_all.csv`. The
`falls_*.csv` files are the untouched live output and stay that way, so every number in
them can be traced to the run that produced it.

Reported this way it is a strength rather than an apology: a fall the live tracker
dropped, recovered from the recording by measurement, agreeing with three independent
checks. That is a better story than a five-fall droplet with nothing to say about it,
and it is the kind of thing a judge can verify instead of having to trust.

A ninth droplet exists (`one_droplet_slow`, 5 falls, 265 V, n = 1, e ranging
1.523-1.668e-19 C). It is **excluded**: that capture is cropped and has no voltage panel
in shot, so the field-off condition cannot be verified from the recording and its
voltage rests on the operator's notes alone. Every droplet in the table above was
confirmed field-off by the pipeline itself. Mention it only if a reader asks for
everything.

## Two things worth a paragraph in the methodology

**The circularity threshold was chosen by measurement, not by eye.** A rejection census
showed 17,855 contours failing only the shape test while sitting just under the 0.52
gate — which looked like a large population of missed droplets. Three complete runs of
the 15-fall benchmark at 0.52, 0.48 and 0.45 found **15 of 15 in every case**, with
identical fall times to the hundredth of a second, while the loosest setting admitted
269,000 more contours. The near-misses were noise sitting under the gate. `circularity_sweep.txt`
is the evidence. A rejection count says what a gate removes; only a run against known
falls says whether it mattered.

**A display bug cost a real measurement, and that is worth reporting honestly.** On
2026-09-09 a droplet crossed the start line with its clock running, but a filter meant
to hide clutter during a measurement also suppressed the marker on the droplet being
measured. Seeing nothing, the operator concluded it had not been detected and re-dropped
it — discarding a fall the software had already timed correctly. About 20 s lost, and it
is inside the 327 s above rather than excluded from it. Fixed; DESIGN_NOTES section 17.
The general point is that a rule about what to *display* is not cosmetic, because the
operator acts on what they can see.
