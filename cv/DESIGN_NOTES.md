# Design notes: decisions, dead ends and what they cost

Every approach below was built and measured before it was kept or dropped. Each entry
states the problem, the options that were real at the time, which one won and why, and
what the choice cost. Kept because the findings are real and the rejected options are
the ones a reader will propose again.

---

## 1. The circularity gate was mathematically unreachable

**Problem.** Droplets were being detected in only 77% of frames and never for more than
62 consecutive (2.6 s), so no track survived `PROBATION_S` and falls were never timed.

**Why.** `4πA/P²` is biased low on small blobs: `contourArea` measures the polygon
through pixel centres while `arcLength` measures its perimeter. A *perfect synthetic
disc* scores 0.785 at r = 2 px, 0.674 at r = 3, 0.763 at r = 5, and does not reach 0.80
until r = 8 — larger than any droplet this apparatus produces. The old gate of 0.80
therefore admitted a droplet only when noise happened to inflate its area or shorten
its perimeter: a coin flip per frame.

**Options.** (a) Keep 0.80 and compensate elsewhere. (b) Drop the shape test entirely.
(c) Re-derive the threshold from the measured bias.

**Chose (c).** Swept on 706 raw dongle frames: continuity reaches 100% and stays there
at 0.52 and below. Set to 0.52 — the knee, with margin.

**Cost.** Detections rise from 3.7 to 14.2 a frame. Accepted because that is still far
below the 123/frame the noise floor exists to stop, and because (b) would have removed
the only cue that separates a droplet from a grid-line flare.

---

## 2. An absolute upper area cap measures the focus knob, not the droplet

**Problem.** The pipeline found 1 of 15 hand-timed falls. The droplet was on the
predicted path in essentially every frame; `area > areaMax` was discarding it.

**Why.** At 1 px = 3.28 µm (l = 1.60×10⁻³ m over 8 divisions of 61 px) a 30 s droplet is
**0.41 px across**. Every pixel of every blob is scattering halo, so apparent size is set
by illumination and focus. Measured across one session: three droplets whose radii
differ by 4% by eq. (1-13) appeared with median areas of 62, 132 and 129 px² — a factor
of 2.8 apart, purely from the operator refocusing between droplets.

**Options.** (a) Raise the cap. (b) Remove it. (c) Make it adaptive per session.

**Chose (a), 900·mag² = 655 px².** Sampling 501 frames across a whole session, the cost
of raising it is almost nothing: 5.79 blobs a frame at 100·mag², 9.13 at 300, 9.52 at
900, 9.54 with no cap at all. Past 300 the curve is flat, because what lies above is a
handful of grid-line flares that bloom to 700–3800 px² and are already rejected on
circularity (0.03–0.51, against 0.58 at p1 for droplets) or for sitting on a column.
655 px² is a blob 29 px across — 2.9× the widest droplet ever observed (228 px²), so a
refocus cannot walk out of it. (c) was deferred, not dismissed; it is the right answer
if the apparatus is ever used at a very different magnification.

**Cost, and it is real.** On `one_droplet_slow`, an older cropped fixture, the looser
cap takes the result from 5 of 5 falls to 3 of 5. The extra admitted blobs hijack the
droplet's track — the two lost falls are precisely the two that needed reacquisition.
No intermediate value splits the difference: 250 and 400 lose them exactly as 900 does.
Kept at 900 deliberately, because the alternative is 1 of 15 on the current apparatus.

---

## 3. A focus change cannot be detected directly, so detect its effect

**Problem.** When the operator changes focus the picture's appearance changes but
nothing moves. The rolling background is a median over the last 300 s, so it holds a
blend of two different-looking scenes and disagrees with every frame everywhere. One
small focus nudge left the field covered in false detections for minutes: 68,609 fall
ids in ten minutes, against 2,362 for a healthy session.

**Options.** (a) Detect the change from frame-vs-background disagreement. (b) Detect it
from the reticle's sharpness. (c) Detect the resulting *symptom* instead.

**(a) failed:** disagreement is 0.31% while the operator manipulates the apparatus,
0.39% during a spray and 0.41% when quiet — no separation.
**(b) is impossible:** the reticle is drawn onto the video by the apparatus's own
electronics, so its sharpness, spacing and offset are bit-exact through a focus change.

**Chose (c).** A spray's burst decays as droplets settle — measured peak at 43 s, back
to baseline by 68 s — while a stale background stays elevated for the whole window. A
detection rate still 4× the session's own quiet level after 45 s is therefore not a
spray, whatever it is. Comparison is always against *this session's* quiet rate, which
has ranged from 0.74 to 123 detections a frame; an absolute threshold is meaningless.

**Cost.** A 45 s delay before the rebuild fires, and one earlier version calibrated
itself blind by recomputing the reference after each resync — it refilled during the
still-cluttered period, the threshold rose to match the clutter, and the detector fired
once and then never again while ids climbed to 50,627. The reference is now taken as a
running minimum and only ever revised downwards.

---

## 4. Never sample the background on a whole number of seconds

**Problem.** Two 60 s background windows over the same footage disagreed about how many
traverses existed.

**Why.** The panel carries a `t:` readout and the display has its own refresh beat, both
close to 1 Hz. Sampling in step with them catches the same phase every time, so the
median never averages them out.

**Measured.** A 60 s window at exactly 1.000 s found 13 traverses and 0 legal falls. The
same window at 1.067 s found 37 and the 2 legal falls every other window agrees on.

**Chose:** step the sampling interval away from any whole second, automatically.

---

## 5. Corroboration gating: right diagnosis, wrong cure

**Problem.** The apparatus emits bursts of single-frame speckle whenever the operator
has hands on it, and the tracker was chaining unrelated one-frame blobs into tracks
carrying physically plausible velocities.

**Proposed.** A new track may only start on a detection that also appeared in the
previous frame. Speckle is uncorrelated between frames; a droplet is not.

**The measurement supported it.** Requiring a neighbour within 6 px in the previous
frame removes 87% of detections during the noisy minutes (54.1/frame → 7.1 at minute 4,
48.3 → 5.6 at minute 19), while a real droplet corroborates about 70% of the time — and
70% looked like plenty, because this gates *spawning only*.

**It still failed.** 70% is an average over all detections, and the moments that matter
are the spawn instants. A droplet that fails to corroborate in the one frame where its
track would have started never gets a track at all. On a real session it took the
result from 5 of the operator's 5 timed falls down to 3, while saving one spurious
traverse. **Rejected.**

---

## 6. Timing against capture time, not frame index

**Problem.** A fall's duration is the whole measurement. Computing it as
`frames / fps` silently trusts the camera's advertised rate.

**Measured.** The dongle advertises 30 fps and delivers 22.4–23.6. A device advertising
30 and delivering 27 would inflate every fall by 11% — plausibly, and with no symptom.

**Chose:** interpolate both crossings against the capture timestamp taken between
`grab()` and `retrieve()`, which is as close to the shutter as OpenCV allows. On a file
this is arithmetically identical to interpolating frame indices; on a camera it is the
entire point.

---

## 7. Losing a droplet mid-fall: three gates, each set by what a slow droplet needs

**Problem.** On one clip 388 traverses began and only 157 finished. Droplets blink out
of the footage itself in bursts of ~15 frames, unrelated to the background or the
threshold. Of 190 tracks that died mid-traverse, 187 died with no contour anywhere near
them, and 7 of the 18 still on a legal pace had their droplet reappear 0.9–1.5 s later,
further from the prediction than one frame's step can reach.

**Why it matters that the droplet is slow.** A droplet is invisible while it overlaps a
grid line, and the *slower* it moves the longer that lasts. The legal droplets are the
slowest ones, so every gate is set by what they need.

**Chose three cooperating mechanisms rather than one loose gate:**
- **Coasting** for up to 3.0 s. At 0.5 s both legal falls in the reference clip are
  lost; at 3.0 s both are recovered.
- **A widened re-acquisition gate**, deliberately *asymmetric*: tight in x (a verified
  fall drifts only 5–6 px sideways across a whole traverse) and elapsed-scaled in y. A
  circle wide enough to cover a second of falling would be far too loose sideways, and
  that is exactly how two droplets get filed as one.
- **A morgue**, so a dead track's `tTop` can be inherited rather than thrown away.

**Cost.** Every stitch is counted in `falls.csv` as `reacquired`, because a gate that is
too loose fabricates a fall out of two droplets and that must be visible rather than
trusted.

---

## 8. Duplicate tracks, and why the area test has a floor

**Problem.** Loosening circularity tripled detections, and a noise blob landing beside a
healthy track spawned a twin that then split the droplet's blobs with it. Both
survived, both completed, and one droplet was reported as two or three falls
(34.10 / 34.17 / 34.30 s for a single droplet).

**Measured.** 868 of 2814 spawned tracks — 31% — were born within 12 px of a live track.
Two genuinely different *confirmed* droplets are that close only 0.596% of the time:
their separation is 10.3 px at p0.5 and 18.1 px at p1, so 12 px sits between the two
distributions and costs almost nothing.

**Related.** `AREA_TOLERANCE` is a *relative* test, which is brutal on small blobs: a
droplet measured at 10 px one frame and 18 the next differs by 80%, and that is ordinary
threshold noise on a 5-pixel object. On one clip a droplet's area swung between 10 and
27 px on consecutive frames and the gate rejected the correct blob on 42 of them. Fixed
with a floor in the denominator, not by loosening the tolerance.

---

## 9. One track, five falls — and the bug that hid for weeks

**Problem.** `updateCrossings` cleared `tTop` when a droplet went back above `0` but
never cleared `tBottom`, so once a track had measured one fall the completion test could
never fire again. The overlay held that first duration frozen through all five drops.

**Why it went unnoticed.** With the old area cap the droplet was barely detected, so the
track died during every lift and a *new* track — with clean state — picked up each fall.
Fixing detection is what exposed it. The two bugs had been hiding each other.

**A second, coupled bug.** `falls` held the live `Track` object, so a track completing
five traverses would have appended the same mutable object five times: five rows in
`falls.csv`, all carrying the fifth fall's numbers. Fixed by snapshotting each
completion.

**Unexpected payoff.** Because a measured droplet is the only thing in the field that
gets lifted and re-dropped, "tracks with five legal falls" is exactly the set of
droplets the operator worked on. On the benchmark that separates perfectly — three
tracks with five legal falls each, five strays with one — which makes `binding`
automatic with no operator notes at all. It did **not** reproduce on the next live
session, where nine falls came from seven track ids.

---

## 10. Voiding a run: a direction, not a height

**Problem.** The test for "the droplet went back up, abandon this run" was
`y < yTop − 0.5·spacing` — the droplet had to be *seen* above an absolute height. But
`edgeClear` suppresses every detection within 20·mag px of the ROI edge, leaving only
~30 px in which the test can fire, and a droplet being lifted crosses that in well under
a second.

**Consequence.** A stale `tTop` survived a lift and a fall came out as a 75.78 s
traverse — whose *bottom* crossing was within 0.7 s of the operator's, so the
measurement was right and only its start was stale.

**Chose:** test the direction instead. Half a division of upward travel cannot be noise
— the centroid is steady to about a pixel, and `assign()` will not hand a track a
detection more than 4 px above where it already is.

---

## 11. Reserving a corridor without an area test starves the droplet

**Problem.** A fall one second from completing was lost because the droplet went dark
for 2.5 s on the `16` line — `gridClear` guarantees invisibility exactly where the
measurement completes — and a new track took the blob when it reappeared.

**First fix, wrong.** Refuse to spawn anywhere a mid-measurement track is predicting.
It mirrored `assign()`'s position gates but *not* its area tolerance, so a blob whose
area had drifted failed assignment, was then refused a track of its own, and was
orphaned permanently: never assigned, never spawned. It turned a stitch into a
starvation and lost the two falls on `one_droplet_slow` that needed reacquisition.

**Correct fix.** Reserve only what `assign()` could actually have taken — position *and*
area. Caught by the fixture suite, which is the entire reason it exists.

---

## 12. Reading the panel: four rejected approaches

**Problem.** The balance voltage is burned into the video by the apparatus. Reading it
removes the last piece of operator-supplied data from the pipeline.

- **A rigid comb for the four digits.** They are unevenly spaced — 58, 100, 139, 183,
  gaps of 42/39/44 — so no comb sits on all four, and whichever it misses gets a column
  shaved off. A `9` with two columns clipped reads as `2`.
- **Scoring every x position instead.** Worse: partial glyphs score well and the search
  found chains in empty space. Replaced by segmentation proposing candidate runs, then a
  four-step dynamic program picking the four that read best with plausible spacing.
- **Normalising glyph aspect ratio.** Stretching every glyph to the same width discards
  the strongest cue this font has — a `1` is a third the width of an `8` — and made every
  `1` read as `7`.
- **Aligning from the `V:` line.** It changes during a fall so its median is smeared; it
  produced 609 V and 749 V on a fixture where neither is a possible balance voltage. The
  latched line is constant for a whole fall, so it aligns from itself.

---

## 13. A misread digit is not fixable by a better matcher

**Problem.** The apparatus's `2` read as `7` on 55–88% of frames depending on the
moment, reporting 700 V for a droplet balanced at 200 V — confidently, with no hint it
was a guess.

**Options.** (a) A better matcher. (b) More labelled template data. (c) Refuse to answer
when the evidence disagrees.

**(a) failed three ways.** Zero-mean normalised cross-correlation was already in use; a
common vertical cut and a fully binarised match both still read `0700`.

**Chose (c) first, then (b).** The latched line holds *one* value for a whole fall, so
every frame is a repeat measurement of the same constant and two frames disagreeing can
only mean a glyph was misread. Measured over fifteen falls: the ten correct reads agree
97–100% of the time, seven of them unanimously; the four wrong ones agree 51%, 55%, 62%
and 88%. Nothing sits between 0.88 and 0.97, so the gate is 0.9 — and that separation is
not a coincidence of one session, it is what a constant read repeatedly has to look like.

Then (b): a labelled sweep of the panel through fourteen held voltages. The first sweep
left the counts wildly uneven — 2067 samples of `0` against 136 each of `6` and `8`,
which are the most confusable pair in the set — so a second sweep of ten more values was
cut specifically to reinforce them, taking `6` to 1034 and `8` to 962.

**Cost.** A refusal returns `None` and the operator's notes carry the voltage. On the
benchmark that is 14 correct, 1 silent, **0 confidently wrong** — and the one silent case
was the panel genuinely displaying `+0000V`, which the per-droplet mode then recovered
from the other four falls anyway.

---

## 14. H.264 clips cannot be the gating reference

**Problem.** Every clip recorded before September was captured through OBS, so it is
H.264. Two of them were promoted to gating status because they were the dongle footage.

**Why that was wrong.** Being dongle footage was never the point — being *raw* was.
H.264 blur lets blobs clear a circularity gate that a real droplet cannot, so the two
disagree about the one constant that decides whether a fall is seen at all. At 0.80 the
clip keeps 10 of 10 and a real session yields 3 of 5; at 0.52 the clip keeps 8 of 10 and
the session yields 5 of 5. Both of the clip's losses were verified genuine.

**Chose:** gate on raw per-frame captures replayed through the whole pipeline; keep the
clips as advisory. They stay because they are the only *adversarial* material there is —
bloomed digits, perspective tilt, glare, blackouts — and they have caught changes that
looked correct on clean footage alone.

---

## 15. Results that only exist in the terminal do not exist

**Problem.** `falls.csv` was written once, after the last frame. Three sessions were
lost to that in a single day: two live runs the operator stopped, and a 56,543-frame
replay that ran to completion and then died in `to_csv` because its output folder did
not exist.

**Chose:** write each fall as it completes and flush; create the output folder before
tracking rather than after. The same habit had already cost a capture's timestamps, so
`times.txt` is now flushed per frame alongside `times.npy`. The summary statistics —
mean e, its standard deviation, the % error — were terminal-only for the same reason and
now go into `charges.csv` as columns.

---

## 16. What the charge layer deliberately does *not* do

**Not a dead end — a constraint accepted on purpose.** `q`, `n`, `e` and the balance
voltage in volts are computed inside the CV layer and written to `charges.csv`, and they
never enter any dict crossing to the app or the LLM. Rubric 三.1.4 requires the
student's analysis ability to be exercised rather than replaced, and the LLM has the
manual in retrieval — so `seconds` plus `U_n` is already the whole answer. The allow-list
in `cv/api.py` is enforced at the single point where a prompt is assembled; if a prompt
can be built without passing through it, the seal is decoration.

---

## 17. A display gate can lose a measurement it took correctly

**Problem.** On 2026-09-09 the operator watched a balanced droplet cross `0`, saw no
ring appear on it, concluded it had not been detected, lifted it and dropped it again.
The second attempt was measured; the first was never written. It cost about 20 s and,
worse, it made the operator distrust the overlay for the rest of the run.

The recording says the pipeline had it the whole time. The track crossed `0` at
668.4 s and its fall clock was running; nothing was drawn on it until 673.3 s, 4.9 s
later, and the operator had already lifted it by 676 s.

**Cause.** Two draw rules that were each right on their own. Once a column is pinned
for measurement, tracks outside it are not drawn -- hunt-phase rings glitching through
a measurement was a real complaint. Separately, a track that has crossed `0` is always
drawn however young, because that is the one being timed. The first `continue` came
fifteen lines before the second, so the exemption never ran. `chosenX` was pinned at
x=853; the droplet was at x=691; 162 px is outside the 60 px window, so a fall in
progress was suppressed for being in the wrong column.

**Chose:** add `t.tTop is None` to the column test. Hunt clutter is still suppressed
during a measurement -- that was the point of the gate -- but a traverse already being
timed is always visible, wherever it is.

**Cost:** during a measurement, a stray that crosses `0` in another column will now be
drawn. That is a ring the operator has to ignore, against a fall they would otherwise
throw away. The general lesson is the expensive one: **a suppression rule for the
display is not cosmetic, because the operator acts on what they can see.** Every gate
in the draw path is now a gate on the operator's decisions.

---

## 18. Discarding a droplet does not discard its time

**Problem.** `hunttime.py` is the only evidence for the pipeline saving any time, and
it reported one hunt of 2035 s -- 40% of the baseline mean on its own, and the widest
point of the range. The operator did not believe it and was right not to.

**Cause.** The tool called a cluster a droplet only if it had the five falls IV.4 asks
for. A droplet on 2026-09-08 gave four legal falls between 378 s and 581 s and was
dropped for being one short. Its time did not disappear with it: hunt is measured from
the previous droplet's last fall, so the next droplet's hunt was reported from the
start of tracking, straight through another droplet's entire measurement.

**Chose:** two falls is enough to close the span, since the 5% duration tolerance has
already had to hold between them at the same column. The report still prints the fall
count and marks anything under five as INCOMPLETE, so a four-fall droplet is visible as
what it is rather than silently deleted.

Separately, the first droplet of every session is now excluded from the pooled
statistics and labelled `(from run start)`. Its clock starts when tracking starts, so
it also holds the learn phase and whatever happened before the first spray. It is an
upper bound on a hunt, not a hunt, and pooling it with the others flatters nothing --
it just adds noise to the only number the time claim rests on.

**Cost:** the baseline is now six hunts instead of six droplets, and the honest
headline moved with it. That is the point: a benchmark that reports a number nobody
can defend is worth less than a smaller number that survives questioning.

---

## 19. Dropped frames are a display cost, not a measurement error

**Problem.** Every live run drops frames -- 1888 to 4253 of them, at an effective
22.3 fps against a 30 fps camera. `assign()` was the standing suspect.

**Measured**, on 3000 frames of `bench_0908` with everything else held equal:

```
saveVideo = False    30.3 ms/frame    33.0 fps
saveVideo = True     41.2 ms/frame    24.3 fps
```

The annotated-video writer costs **10.9 ms/frame**, which is the whole difference
between keeping up with a 30 fps camera and not. `assign()` is real but second, at
about 15% of the remaining work; the panel template matcher is comparable at about 22%.

**Chose: change nothing.** Every measurement is taken against capture time, not frame
index (section 6), so a dropped frame costs temporal resolution on a crossing and
nothing else: at 22 fps a boundary is located to about 22 ms on a 20 s fall, which is
0.1%. Against that, the annotated video is the only forensic record a live run leaves
-- the frames themselves are gone -- and it is what identified the fault in section 17
after the fact. Spending 11 ms a frame to keep it is the right trade.

**Worth knowing anyway:** if a future run ever does need the full frame rate, the
writer is the thing to drop, not `assign()`.

---

## 20. The circularity near-misses are noise, and the sweep proves it

**Problem.** The reject census on a live run showed 17,855 contours failing *only* the
shape test while sitting at circularity 0.50 against a 0.52 gate, at area p50 42 --
droplet-sized, and just barely out. That population had been the standing suspect for
every droplet the operator thought was missed, and lowering the gate to 0.45 was
proposed, tried and reverted twice because nobody had measured what it cost over a
whole session.

**Measured.** `bench_0908` -- 56,547 frames, the current apparatus, raw, fifteen falls
the operator timed by hand -- run three times end to end, with only the constant
changed:

```
CIRCULARITY_MIN    contours rejected on shape    traverses    hand falls found / legal
   0.52                  1,153,865                  260              15 / 15
   0.48                    997,526                  261              15 / 15
   0.45                    884,979                  259              15 / 15
```

Admitting 269,000 more contours produced **no additional fall**, identical durations to
the hundredth of a second, and one traverse fewer. The near-miss population is noise
sitting under the gate, not droplets being turned away.

**Chose:** keep 0.52. It is the tighter gate, it does strictly less work, and the
question is now settled by measurement rather than deferred again.

**Worth stating plainly:** the census counted *contours*, not droplets, and a count
that large is persuasive in the wrong direction -- 17,855 near-misses sounds like a
lot of lost droplets and is in fact a lot of lost noise. A rejection census says what
a gate removes; only a run against known falls says whether that mattered.

**A second baseline, corrected the same night.** `one_droplet_slow` had been failing
its traverse bar at 43 against 46. Disabling `panelClear` and `staticMask` together
restored exactly 46, so those two gates account for all three traverses and nothing
else did. The kinematic-legal count is 16 in every configuration and all five
reference falls survive in every configuration, so the three that go are traverses no
droplet could have made. The bar was re-baselined to 43 with that table recorded
beside it -- the 46 predated both gates and had never been re-measured against the
code that ships.

For scale, the same fixture on the pre-session pipeline gives **33 traverses and 7
kinematic-legal**, also keeping 5 of 5. So 43 is not a fall from 46: it is 33 raised to
46 by this session's area cap, ownership and coast work, then trimmed by three
deliberate ones.

**Still open.** Kinematic-legal went from 7 to 16 on that fixture. Those extras are
either other droplets in a real chamber -- the operator only ever timed their target --
or noise stitched into something that lands in the 16-40 s band. The 2026-09-05
artifacts already carried seven such extras, so they predate this session and are not a
new fault, but nothing has confirmed what they are. On `bench_0908` the equivalent six
untimed legal falls all sit in the gaps *between* the operator's droplets, which is
where other droplets would be, and position-and-duration binding excludes them from any
charge. That is reassuring, not conclusive.
