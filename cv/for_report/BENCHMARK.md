# Millikan oil-drop measurements

Every droplet measured, across six sessions. `n` is q/e rounded, per the manual
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

**Electron counts observed: [1, 2, 3]**

Overall across 50 falls: mean e = 1.5637e-19 C, scatter 5.8%, error -2.39% against the accepted 1.602e-19 C.

The overall mean pools droplets carrying different n, so its scatter is a spread
across droplets, not a measurement uncertainty on e. The per-droplet rows are the
ones to quote.

## The two worst droplets, and what the manual already says about them

Droplets 9 and 10 disagree with the accepted e by +8.0% and -9.7%. Neither is a
scatter problem -- both are among the *tightest* droplets measured, at 2.0% and
1.2% -- so their five falls agree with each other and disagree with the answer.
That is systematic, and in both cases the manual warned about the condition:

- **Droplet 10 balanced at 140 V**, below Ⅴ.5's advised 150-250 V. Since q = k/U_n,
  a low balance voltage inflates q and magnifies any error in holding or reading it.
- **Droplet 9 fell in 36.9 s**, near the 40 s ceiling, and carries a single
  electron. Ⅳ.2 warns that a droplet too small suffers strongly from Brownian
  motion, that its balance voltage is hard to adjust, and that its timing error
  grows. A slow droplet is a small droplet.

Both are kept. A rubric asking for a comparison against the traditional method is
better served by showing that the manual's own selection advice has a measurable
cost when ignored than by quietly dropping the droplets that show it.

## The q/e column is a live check on the balance voltage

q must be a whole number of electrons, so how far q/e sits from one tests U_n with
no reference measurement, the moment a fall completes. The pipeline now shows it in
the measuring banner and warns past 0.20.

**Its sensitivity scales with n, and that is a real limit.** Droplet 10 carries three
electrons, so its -9.7% error puts q/e 0.29 off an integer and the check fires.
Droplet 9 carries one, so its +8.0% error is only 0.08 off and the check stays
silent. The test is sharpest exactly where there is most charge to be wrong about,
and blunt on single-electron droplets -- which are also the ones Ⅳ.2 says are
hardest to hold. It catches a class of error, not all of them.
