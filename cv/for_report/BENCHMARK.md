# Millikan oil-drop measurements

Every droplet measured, across seven sessions. `n` is q/e rounded, per the manual
VI.2; `e_i = q_i / n_i`. Values recomputed from each fall's own time and voltage.

| droplet | falls | U_n (V) | mean t (s) | n | mean e_i (C) | scatter | error vs e | q/e |
|---|---|---|---|---|---|---|---|---|
| Droplet 1 | 5 | 151 | 31.10 | 2 | 1.6421e-19 | 2.5% | +2.50% | 2.05 |
| Droplet 2 | 5 | 176 | 29.23 | 2 | 1.5536e-19 | 1.8% | -3.02% | 1.94 |
| Droplet 3 | 5 | 200 | 27.90 | 2 | 1.4718e-19 | 1.2% | -8.13% | 1.84 |
| Droplet 4 | 5 | 265 | 17.42 | 3 | 1.5515e-19 | 1.9% | -3.15% | 2.91 |
| Droplet 5 | 5 | 233 | 36.67 | 1 | 1.6417e-19 | 5.1% | +2.48% | 1.02 |
| Droplet 6 | 5 | 150 | 32.99 | 2 | 1.5067e-19 | 4.0% | -5.95% | 1.88 |
| Droplet 7 | 5 | 250 | 36.31 | 1 | 1.5525e-19 | 2.6% | -3.09% | 0.97 |
| Droplet 8 | 5 | 210 | 20.31 | 3 | 1.5397e-19 | 2.8% | -3.89% | 2.88 |
| Droplet 9 | 5 | 219 | 36.86 | 1 | 1.7300e-19 | 2.0% | +7.99% | 1.08 |
| Droplet 10 | 5 | 140 | 27.34 | 3 | 1.4469e-19 | 1.2% | -9.68% | 2.71 |
| Droplet 11 | 5 | 271 | 36.28 | 1 | 1.4336e-19 | 2.0% | -10.51% | 0.89 |

**Electron counts observed: [1, 2, 3]**

Overall across 55 falls: mean e = 1.5518e-19 C, scatter 6.1%, error -3.13% against the accepted 1.602e-19 C.

The overall mean pools droplets carrying different n, so its scatter is a spread
across droplets, not a measurement uncertainty on e. The per-droplet rows are the
ones to quote.

## Why individual droplets disagree, and what does not explain it

Per-droplet errors run **mean -3.13%, sd 5.64%** across 11 droplets, from -10.51% to
+7.99%. Within a droplet the five falls agree to 1-5%, so the spread BETWEEN
droplets is larger than the timing noise inside one. Something that is fixed for a
droplet and varies between them carries most of the error -- the balance voltage,
read once per droplet, is the obvious candidate, and the radius correction the next.

Three explanations were tested against the data and **none of them predicts which**
**droplet will be wrong**:

| candidate | vs signed error | vs abs error |
|---|---|---|
| balance voltage | r = +0.05 | r = -0.04 |
| fall time | r = +0.27 | r = +0.20 |
| electrons carried | r = -0.36 | r = -0.07 |

An earlier version of this file explained the two worst droplets by Brownian motion
on slow, small droplets. **That does not survive the data**: fall time correlates
with absolute error at only r = +0.19, and droplets slower than 34 s average 6.0%
against 5.2% for the rest. It was a plausible story, not a result, and it is
withdrawn.

The one signal that does point somewhere is the manual's own advice, and it is
weak: droplets balanced inside Ⅴ.5's 150-250 V band average **4.6%** absolute
error (n=8), those outside it **7.8%** (n=3). Consistent with following the
manual, nowhere near enough to prove it. Stated as an observation, not a finding.

What this does show is why the method needs many droplets: any single one is worth
about -3% give or take 6%, while the mean of all 11 lands at -3.13%.

## The q/e column is a live check on the balance voltage

q must be a whole number of electrons, so how far q/e sits from one tests U_n with
no reference measurement, the moment a fall completes. The pipeline shows it in the
measuring banner and warns past 0.20.

**Its sensitivity scales with n, which is a real limit.** A droplet carrying three
electrons turns a 10% error into 0.3 off an integer and the check fires; one
carrying a single electron turns the same 10% into 0.1 and it stays silent. It
catches a class of error, not all of them.
