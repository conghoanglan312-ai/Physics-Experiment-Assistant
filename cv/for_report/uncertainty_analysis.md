# Uncertainty analysis — final 20-droplet dataset

Same method as report §8.2.1, applied to the data team's filtered set: **20 droplets, 100 falls** (`charges_all (1)(2).csv`).

## Method

Uncertainties are decomposed into Type A (statistical) and Type B (systematic) contributions per the GUM (JCGM 100:2008); the combined standard uncertainty is their root-sum-square.

**Type A** — standard error of the mean over the droplet's falls:

$$u_A = \frac{s}{\sqrt{N}}$$

**Type B** — two sources, each taken as a uniform distribution:

- timing: frame interval Δt = 44.8 ms (22.4 fps); two crossings per fall, so  $u_B(t) = \sqrt{2}\,\Delta t/\sqrt{3} = 36.6\,$ms
- voltage: panel resolution ΔU = 1 V, so $u_B(U) = 1/\sqrt{3} = 0.58$ V

Since q ∝ t^(−3/2) · U^(−1):

$$\frac{u_B(e)}{e} = \sqrt{\left(1.5\,\frac{u_B(t)}{t}\right)^2 + \left(\frac{u_B(U)}{U}\right)^2}$$

**Combined:** $u_c = \sqrt{u_A^2 + u_B^2}$

## Per-droplet uncertainties

| Droplet | U (V) | t̄ (s) | u_A(t) (s) | u_A(e) (C) | u_B(e) (C) | u_c(e) (C) | u_c rel (%) |
| :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| 1 | 151 | 31.10 | 0.218 | 1.81×10⁻²¹ | 6.9×10⁻²² | 1.94×10⁻²¹ | 1.2 |
| 2 | 176 | 29.23 | 0.146 | 1.22×10⁻²¹ | 5.9×10⁻²² | 1.35×10⁻²¹ | 0.9 |
| 3 | 265 | 17.42 | 0.096 | 1.33×10⁻²¹ | 5.9×10⁻²² | 1.46×10⁻²¹ | 0.9 |
| 4 | 233 | 36.67 | 0.517 | 3.73×10⁻²¹ | 4.7×10⁻²² | 3.76×10⁻²¹ | 2.3 |
| 5 | 150 | 32.99 | 0.372 | 2.67×10⁻²¹ | 6.3×10⁻²² | 2.74×10⁻²¹ | 1.8 |
| 6 | 250 | 36.31 | 0.264 | 1.78×10⁻²¹ | 4.3×10⁻²² | 1.83×10⁻²¹ | 1.2 |
| 7 | 210 | 20.31 | 0.166 | 1.96×10⁻²¹ | 5.9×10⁻²² | 2.05×10⁻²¹ | 1.3 |
| 8 | 219 | 36.86 | 0.212 | 1.58×10⁻²¹ | 5.2×10⁻²² | 1.67×10⁻²¹ | 1.0 |
| 9 | 140 | 27.34 | 0.094 | 7.78×10⁻²² | 6.7×10⁻²² | 1.02×10⁻²¹ | 0.7 |
| 10 | 234 | 18.82 | 0.096 | 1.27×10⁻²¹ | 6.0×10⁻²² | 1.40×10⁻²¹ | 0.9 |
| 11 | 169 | 18.92 | 0.166 | 2.21×10⁻²¹ | 7.2×10⁻²² | 2.32×10⁻²¹ | 1.5 |
| 12 | 176 | 23.00 | 0.146 | 1.50×10⁻²¹ | 6.1×10⁻²² | 1.62×10⁻²¹ | 1.1 |
| 13 | 192 | 26.56 | 0.322 | 3.15×10⁻²¹ | 6.1×10⁻²² | 3.21×10⁻²¹ | 1.9 |
| 14 | 248 | 37.96 | 0.445 | 2.70×10⁻²¹ | 4.0×10⁻²² | 2.73×10⁻²¹ | 1.9 |
| 15 | 163 | 17.08 | 0.094 | 1.34×10⁻²¹ | 7.5×10⁻²² | 1.54×10⁻²¹ | 1.0 |
| 16 | 171 | 28.83 | 0.313 | 2.84×10⁻²¹ | 6.4×10⁻²² | 2.91×10⁻²¹ | 1.8 |
| 17 | 195 | 27.20 | 0.184 | 1.66×10⁻²¹ | 5.6×10⁻²² | 1.76×10⁻²¹ | 1.1 |
| 18 | 265 | 22.11 | 0.125 | 1.43×10⁻²¹ | 5.3×10⁻²² | 1.52×10⁻²¹ | 1.0 |
| 19 | 228 | 24.42 | 0.201 | 2.05×10⁻²¹ | 5.4×10⁻²² | 2.12×10⁻²¹ | 1.3 |
| 20 | 141 | 22.18 | 0.094 | 1.95×10⁻²¹ | 7.1×10⁻²² | 2.08×10⁻²¹ | 1.4 |

*Per-droplet measurement uncertainties, 20 droplets.*

## Worked example — droplet 1 (t̄ = 31.10 s, U = 151 V)

- timing term: 1.5 × 0.0366 / 31.10 = 0.001764
- voltage term: 0.58 / 151 = 0.00382
- $u_B(e)/e = \sqrt{0.001764^2 + 0.00382^2} = 0.00421 = 0.42\%$
- × e = 1.6400×10⁻¹⁹ C → u_B(e) = 6.9×10⁻²² C
- $u_c = \sqrt{(1.81×10⁻²¹)^2 + (6.9×10⁻²²)^2} = 1.94×10⁻²¹$ C, or **1.2%** relative

## Budget across the set

| source | median relative contribution | range |
| :-- | :-: | :-: |
| Type A — fall-to-fall scatter | 1.13% | 0.54–2.27% |
| Type B — voltage (±1 V) | 0.30% | 0.22–0.41% |
| Type B — timing (36.6 ms) | 0.20% | 0.14–0.32% |
| **Combined u_c** | **1.18%** | **0.7–2.3%** |

## Overall result

| quantity | value |
| :-- | :-: |
| droplets / falls | 20 / 100 |
| mean e (all falls) | 1.5709×10⁻¹⁹ C |
| standard deviation of e_i | 8.0×10⁻²¹ C |
| u_A of the mean | 8.0×10⁻²² C |
| error vs 1.602×10⁻¹⁹ C | **−1.94%** |
| per-droplet error: mean / sd | −1.96% / 4.38 pp |
| electron counts observed | 1, 2, 3, 4, 5 |

## What the numbers say

- Each droplet is measured to **1.2% median** combined uncertainty (range 0.7–2.3%).
- Droplets disagree with each other by **4.38 percentage points** (sd of per-droplet error) — about 4× the within-droplet uncertainty.
- Type A dominates the budget (1.13% median); voltage (0.30%) and timing (0.20%) resolution are both small. The limiting factor is not instrument resolution but something fixed per droplet.
- The overall mean sits at **−1.94%** from the accepted value, with u_A of the mean 8.0×10⁻²² C (0.51%).
