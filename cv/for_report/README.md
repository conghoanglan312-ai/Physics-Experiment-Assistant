# CV module — material for the report

**Start with `HANDOVER.md`.** It answers `Requirements_from_report.md` (chapters 5–8) and
has a plain-language "What this means" under each table of numbers.

## Files

| file | what it is |
|---|---|
| `HANDOVER.md` | chapters 5–8: methods, frameworks, integration diagrams, rejected methods, development history, specs and latency, validation, results |
| `HUNT_TIMES.md` | every hunt measured (spray → first legal fall), aided vs unaided, and what is not yet established |
| `BENCHMARK.md` | the per-droplet charge table, and what does and does not explain the spread between droplets |
| `charges_all.csv` | every fall of every droplet: time, voltage, radius, q, n, e_i, per-droplet and overall summaries |
| `falls_*.csv` | the raw pipeline output of each session, one row per traverse: duration, speed, column, voltage, legality |
| `ALGORITHMS.md` | every algorithm used, what for and what it produces; top five ranked |
| `WHY_CLASSICAL_CV.md` | where the AI is, why the measuring layer is classical CV, and the one model worth adding |
| `circularity_sweep.txt` | three full runs of the hand-timed session at three shape-gate settings |
| `detection_examples_tracker.jpg` | per-frame detection test: one random frame per phase |
| `detection_accuracy/` | raw data of the per-frame detection test: frames, labels, blind review, control |
| `../DESIGN_NOTES.md` | design decisions and dead ends, and what each cost |

## The result in one paragraph

24 droplets and 120 falls, carrying n = 1 to 5 electrons. Mean e = 1.5667×10⁻¹⁹ C,
−2.20% against the accepted 1.602×10⁻¹⁹ C. Pipeline timing agrees with a stopwatch to
−0.11 ± 0.23 s over 15 falls. Tracked droplets are detected with 95.8% precision and 85.4%
recall. With the hunt aid, the median time to find a usable droplet fell from 463 s to
219 s.

## Droplet 4, fall 5: measured from the recording

Droplet 4's fifth fall completed normally, but the live tracker lost the droplet part-way
down, so the pipeline never recorded it. It was measured afterwards from the session
recording: the droplet's position was fitted against time and both reticle crossings
interpolated with the same geometry the live code uses, giving **17.49 ± 0.23 s** (281
samples, rms 3.26 px on a 61 px division). Three independent checks agree: the operator's
stopwatch read 17.20 s, the live overlay showed 0.47–0.49 grid/s during the descent, and
the droplet's other four falls ran 17.20–17.74 s. The `falls_*.csv` files remain the
untouched live output.

## Two things worth a paragraph in the methodology

**The shape gate was chosen by measurement, not by eye.** A rejection census showed 17,855
contours failing only the shape test while sitting just under the 0.52 gate, which looked
like a large population of missed droplets. Three complete runs of the hand-timed session
at 0.52, 0.48 and 0.45 found **15 of 15 falls every time**, with identical fall times to
the hundredth of a second, while the loosest setting let in 269,000 more contours. The
near-misses were noise. `circularity_sweep.txt` is the evidence. A rejection count says
what a gate removes; only a run against known falls says whether it mattered.

**A display bug cost a real measurement.** On 2026-09-09 a droplet crossed the start line
with its clock running, but a filter meant to hide clutter during a measurement also hid
the marker on the droplet being measured. Seeing nothing, the operator concluded it had
not been detected and re-dropped it, discarding a fall the software had already timed
correctly. About 20 s were lost, and they are inside the 319 s aided hunt rather than
excluded from it. Fixed. A rule about what to *display* is not cosmetic, because the
operator acts on what they can see.
