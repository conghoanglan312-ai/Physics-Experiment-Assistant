# Algorithms used in the CV pipeline

## The five that matter most

Ranked by how much the result depends on them. If the report only has room for a few,
take them from the top of this list.

| # | algorithm | why it is on this list |
|---|---|---|
| 1 | **Otsu's method** | Decides what counts as a droplet at all. Every later stage sees only what this passes, and it adapts per frame, so the pipeline survives the lamp and focus changing between sessions. |
| 2 | **Hungarian algorithm** (Kuhn-Munkres) | Turns per-frame blobs into droplets that persist. Solves the whole frame's assignment at once instead of greedily, which is what keeps two nearby droplets from swapping identity. |
| 3 | **Linear interpolation of line crossings** | Produces the actual measurement. The fall time is the interpolated instant the droplet crosses `0` and `1.6`, not a frame count, which is why the result is independent of frame rate and of dropped frames. |
| 4 | **Least-squares fitting** | Separates a falling droplet from stitched noise. Speed comes from the slope, and the RMS residual is the test: a real fall is straight, chained speckle is not. |
| 5 | **Rolling median background subtraction** | Defines "a droplet is what is not permanently there". The grid, the labels and the panel are all in the background and vanish; a 300 s window lets it re-learn after the focus is touched. |

---

## Everything else

### Detection: frame -> candidate blobs

| algorithm | used for | produces |
|---|---|---|
| Rolling median background estimation | a reference frame of what is static | background image |
| Background subtraction (absolute difference) | isolating what moved | difference image |
| Gaussian blur | suppressing single-pixel sensor noise | smoothed image |
| Otsu's method | choosing the bright/dark threshold per frame | threshold level |
| Median absolute deviation (MAD) | a noise floor Otsu cannot drop below on an empty chamber | minimum threshold |
| Morphological opening / closing | removing specks, closing droplet interiors | cleaned binary mask |
| Top-hat transform | finding the reticle lines and panel text | mask of drawn furniture |
| Connected-component labelling | isolating static glyphs to mask out | component list |
| Suzuki-Abe contour tracing | outlining each blob | contours |
| Image moments | blob centre | (x, y) centroid |
| Isoperimetric quotient (circularity) | rejecting streaks and clumps | shape score |

### Tracking: blobs -> droplets over time

| algorithm | used for | produces |
|---|---|---|
| Constant-velocity motion model | predicting where each droplet goes next | predicted position |
| Nearest-neighbour gating | discarding impossible pairings before the solve | reduced cost matrix |
| Hungarian algorithm | matching blobs to tracks, whole frame at once | assignment |
| Track coasting and re-acquisition | surviving the droplet going dark on a grid line | continuous track |

### Measurement: tracks -> fall times

| algorithm | used for | produces |
|---|---|---|
| Linear interpolation | the exact instant of each reticle crossing | crossing time |
| Ordinary least squares | descent speed, and the straightness test | slope + RMS residual |
| Iterative trimmed refitting | discarding outlying points before the final fit | robust slope |

### Panel reading: pixels -> volts

| algorithm | used for | produces |
|---|---|---|
| Connected-component glyph segmentation | cutting the readout into digit cells | digit images |
| Template matching, zero-mean NCC | identifying each digit | digit + score |
| Majority voting | one answer from many frames of the same constant | balance voltage, or none |

### Analysis: falls -> charge

| algorithm | used for | produces |
|---|---|---|
| Single-linkage clustering by position | grouping falls into droplets without track identity | droplet groups |
| Median-based iterative trimming | dropping strays that share a column | clean groups |
| Rounding to nearest integer | q/e -> n, the quantisation step | electron count |
| Arithmetic mean, sample standard deviation | per-droplet and overall summaries | mean e, sd, % error |

---

No machine learning is used anywhere. Every stage above is classical computer vision or
ordinary statistics, which is why each threshold can be justified from the manual or
from a measurement rather than from training data.
