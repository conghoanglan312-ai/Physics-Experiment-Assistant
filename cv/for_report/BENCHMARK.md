# Millikan oil-drop measurements

Every droplet measured, across seven sessions. `n` is q/e rounded, per the manual
VI.2; `e_i = q_i / n_i`. Values recomputed from each fall's own time and voltage.

| droplet | falls | U_n (V) | mean t (s) | n | mean e_i (C) | scatter | error vs e | q/e |
|---|---|---|---|---|---|---|---|---|
| Droplet 1 | 5 | 151 | 31.10 | 2 | 1.6400e-19 | 2.5% | +2.50% | 2.05 |
| Droplet 2 | 5 | 176 | 29.23 | 2 | 1.5500e-19 | 1.8% | -3.02% | 1.94 |
| Droplet 3 | 5 | 200 | 27.90 | 2 | 1.4700e-19 | 1.2% | -8.13% | 1.84 |
| Droplet 4 | 5 | 265 | 17.42 | 3 | 1.5500e-19 | 1.9% | -3.14% | 2.91 |
| Droplet 5 | 5 | 233 | 36.67 | 1 | 1.6400e-19 | 5.1% | +2.48% | 1.02 |
| Droplet 6 | 5 | 150 | 32.99 | 2 | 1.5100e-19 | 3.9% | -5.95% | 1.88 |
| Droplet 7 | 5 | 250 | 36.31 | 1 | 1.5500e-19 | 2.6% | -3.09% | 0.97 |
| Droplet 8 | 5 | 210 | 20.31 | 3 | 1.5400e-19 | 2.8% | -3.89% | 2.88 |
| Droplet 9 | 5 | 219 | 36.86 | 1 | 1.7300e-19 | 2.0% | +7.99% | 1.08 |
| Droplet 10 | 5 | 140 | 27.34 | 3 | 1.4469e-19 | 1.2% | -9.68% | 2.71 |
| Droplet 11 | 5 | 271 | 36.28 | 1 | 1.4336e-19 | 2.0% | -10.51% | 0.89 |
| Droplet 12 | 5 | 234 | 18.82 | 3 | 1.5575e-19 | 1.8% | -2.78% | 2.92 |
| Droplet 13 | 5 | 169 | 18.92 | 4 | 1.6044e-19 | 3.1% | +0.15% | 4.01 |
| Droplet 14 | 5 | 176 | 23.00 | 3 | 1.5119e-19 | 2.2% | -5.63% | 2.83 |
| Droplet 15 | 5 | 192 | 26.56 | 2 | 1.6585e-19 | 4.2% | +3.53% | 2.07 |
| Droplet 16 | 5 | 248 | 37.96 | 1 | 1.4593e-19 | 4.1% | -8.90% | 0.91 |
| Droplet 17 | 5 | 163 | 17.08 | 5 | 1.5601e-19 | 1.9% | -2.61% | 4.87 |
| Droplet 18 | 5 | 171 | 28.83 | 2 | 1.6356e-19 | 3.9% | +2.09% | 2.04 |
| Droplet 19 | 5 | 140 | 20.99 | 4 | 1.6450e-19 | 2.6% | +2.68% | 4.11 |
| Droplet 20 | 5 | 195 | 27.20 | 2 | 1.5712e-19 | 2.4% | -1.92% | 1.96 |
| Droplet 21 | 5 | 250 | 22.69 | 2 | 1.6312e-19 | 3.6% | +1.82% | 2.04 |
| Droplet 22 | 5 | 265 | 22.11 | 2 | 1.6021e-19 | 2.0% | +0.00% | 2.00 |
| Droplet 23 | 5 | 228 | 24.42 | 2 | 1.5931e-19 | 2.9% | -0.56% | 1.99 |
| Droplet 24 | 5 | 141 | 22.18 | 4 | 1.4900e-19 | 2.9% | -6.80% | 3.74 |

**Electron counts observed: [1, 2, 3, 4, 5]**

Overall across 120 falls: mean e = 1.5667e-19 C, scatter 5.4%, error -2.20% against the accepted 1.602e-19 C.

The overall mean pools droplets carrying different n, so its scatter is a spread
across droplets, not a measurement uncertainty on e. The per-droplet rows are the
ones to quote.

## Why individual droplets disagree, and what does not explain it

Per-droplet errors run **mean -2.22%, sd 4.71%** across 24 droplets, from -10.51% to
+7.99%. Within a droplet the five falls agree to 1-5%, so the spread BETWEEN
droplets is larger than the timing noise inside one. Something that is fixed for a
droplet and varies between them carries most of the error -- the balance voltage,
read once per droplet, is the obvious candidate, and the radius correction the next.

Three explanations were tested against the data and **none of them predicts which**
**droplet will be wrong**:

| candidate | vs signed error | vs abs error |
|---|---|---|
| balance voltage | r = -0.01 (p = 0.86) | r = -0.04 (p = 0.86) |
| fall time | r = -0.05 (p = 0.82) | **r = +0.48 (p = 0.017)** |
| electrons carried | r = -0.07 (p = 0.74) | r = -0.24 (p = 0.27)

Nothing predicts the DIRECTION of a droplet's error -- no candidate reaches
|r| = 0.1 against the signed error, so which way a droplet misses is still
unexplained.

Its SIZE is a different matter, and here the data has changed. On 11 droplets, fall
time correlated with absolute error at r = +0.20 and the Brownian explanation was
withdrawn as a plausible story rather than a result. On 24 droplets it reaches
**r = +0.48, p = 0.017**: slow droplets are the inaccurate ones, and slow means small,
which is exactly where Brownian motion bites. Droplets averaging over 34 s miss by
**6.6%** against **3.5%** for the rest (n = 5 vs 19, Mann-Whitney p = 0.052).

Two cautions before this is quoted as a finding. Spearman's rho is +0.37 (p = 0.073),
so the Pearson figure leans on the slow tail rather than on a trend through the middle;
and thirteen droplets have been added since the claim was withdrawn, which is the kind
of re-test that finds significance by looking twice. It is now the best-supported
explanation on offer, not a settled one. The operational reading is the same either
way: prefer droplets that fall in the 17-30 s range.

The manual's own advice still points the same way and is still weak: droplets
balanced inside Ⅴ.5's 150-250 V band average **3.7%** absolute error (n=18), those
outside it **5.5%** (n=6). Consistent with following the manual, nowhere near enough
to prove it. Stated as an observation, not a finding.

What this does show is why the method needs many droplets: any single one is worth
about -2% give or take 5%, while the mean of all 24 lands at -2.20%. Thirteen more
droplets moved the mean by 0.9 points and the spread barely at all, which is what a
population of independent measurements around a systematic offset looks like.

## The q/e column is a live check on the balance voltage

q must be a whole number of electrons, so how far q/e sits from one tests U_n with
no reference measurement, the moment a fall completes. The pipeline shows it in the
measuring banner and warns past 0.20.

**Its sensitivity scales with n, which is a real limit.** A droplet carrying three
electrons turns a 10% error into 0.3 off an integer and the check fires; one
carrying a single electron turns the same 10% into 0.1 and it stays silent. It
catches a class of error, not all of them.
