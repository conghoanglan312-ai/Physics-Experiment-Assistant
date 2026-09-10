"""Millikan oil-drop CV pipeline.

Detects and tracks droplets on the apparatus's CCD feed, times the `0` -> `16`
traverse to sub-frame precision, and judges each fall against the manual's own
criteria (docs/README.md section 8: 8 divisions, 16-40 s, 0.2-0.5 grid/s, and the
field off).

The public contract is `cv/api.py`.  Import from there, not from here.

CLI:
    python -m cv.pipeline <clip>            window, playing a recorded clip
    python -m cv.pipeline live [index]      window, from a camera or capture dongle
    python -m cv.pipeline live [index] --blind
                                            window showing the RAW apparatus picture and
                                            nothing else -- for benchmarking against a
                                            human, who must not see the overlay
    python -m cv.pipeline survey <clip>     headless, writes falls.csv
    python -m cv.pipeline all [pattern]     every clip in MILLIKAN_FOOTAGE
    python -m cv.pipeline preview           ROI preview for every clip

A live run learns its background for the first 30 seconds and shows nothing until it
has.  Do that before any droplets are sprayed: a droplet that sits still burns into
the median and is subtracted away for the rest of the session.
"""

import cv2
import numpy as np
import threading
import time
import collections
import pandas as pd

import os
import csv
import glob
import sys
try:
    from scipy.optimize import linear_sum_assignment
    HAVE_SCIPY = True
except ImportError:                     # greedy fallback, same gates
    HAVE_SCIPY = False

try:
    from .source import openSource, isCameraSpec, FRAMES_PREFIX
    from . import charge as chargeMath
except ImportError:                     # run as a loose script rather than a package
    from source import openSource, isCameraSpec, FRAMES_PREFIX
    import charge as chargeMath

# No machine-specific defaults.  Point these at your own footage with an argument
# or an environment variable.  A missing clip raises: quietly returning an empty
# list is indistinguishable from "this clip had no droplets".
VIDEO = os.environ.get("MILLIKAN_CLIP")             # one clip
FOOTAGE = os.environ.get("MILLIKAN_FOOTAGE")        # a folder of clips
# Artefacts never land inside the package.  They go to the gitignored sandbox when
# it exists (which keeps run logs, CSVs and annotated video out of the repo), and
# otherwise beside wherever you ran from.  MILLIKAN_OUTPUT overrides both.
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sandbox = os.path.join(_repo, "test", "output")
OUTPUT = os.environ.get(
    "MILLIKAN_OUTPUT",
    _sandbox if os.path.isdir(_sandbox) else os.path.join(os.getcwd(), "output"))


def resolveClip(path):
    if not path:
        raise ValueError("No clip given. Pass a path, or set MILLIKAN_CLIP.")
    if isCameraSpec(path):              # "cam:0" never touches the filesystem
        return path
    # "frames:DIR" is a directory of raw per-frame PNGs, not a file.  Still checked:
    # a mistyped fixture name must fail here rather than three minutes into a run.
    if str(path).lower().startswith(FRAMES_PREFIX):
        folder = str(path)[len(FRAMES_PREFIX):].strip()
        if not os.path.isdir(folder):
            raise NotADirectoryError(folder)
        return path
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return path


def resolveFootage(folder=None):
    folder = folder or FOOTAGE
    if not folder:
        raise ValueError("No footage folder given. Pass one, or set MILLIKAN_FOOTAGE.")
    if not os.path.isdir(folder):
        raise NotADirectoryError(folder)
    return folder

# --- what counts as a legal fall (docs/README.md section 8, the manual's own criteria) ---
# The reticle labels `0` and `16` are the 2nd horizontal line from the top and the 2nd
# from the bottom. There are 8 grid intervals between them, spanning 1.6 mm.
LINE_TOP, LINE_BOTTOM = 1, 9           # indices into the 11 detected rows
FALL_DIVISIONS = 8                     # grid intervals from `0` to `16`
MIN_FALL_S, MAX_FALL_S = 16.0, 40.0    # fall time over 1.6 mm
MIN_SPEED, MAX_SPEED = 0.2, 0.5        # grid divisions per second
TARGET_DROPLETS = 3                    # IV.4: at least 3 droplets...
TARGET_FALLS_PER_DROPLET = 5           # ...and at least 5 falls each

# How near in x two traverses must be to be the same droplet.
#
# Measured on bench_0908, across each droplet's own five falls:
#
#     droplet 1   x = 239 248 223 264 250     spread 41 px
#     droplet 2   x = 747 746 768 778 779     spread 33 px
#     droplet 3   x = 647 650 651 654 606     spread 48 px
#
# So a droplet wanders far more between falls than within one -- the 5-6 px figure is
# the drift DURING a traverse, and the operator re-catches it slightly off-centre every
# time.  25 px split droplet 2 into two groups of fewer than five and lost it entirely.
SAME_DROPLET_X_PX = 60.0

# ...but 60 px is wide enough to swallow a stray that happens to fall in the same
# column, and on that same session three did.  Position gets the grouping close;
# DURATION finishes it.  A droplet re-dropped five times repeats itself to 2-4%, while
# the strays sharing its column were 10-18% away:
#
#     droplet 3 + strays   28.09 27.63 27.89 27.75 28.12  |  25.12 22.87 19.96 16.12
#
# Trimming to the falls within 5% of the cluster's median duration separates them
# cleanly and leaves exactly three droplets of five falls each.
SAME_DROPLET_DURATION_TOL = 0.05

# --- knowing when the hunt is over ------------------------------------------------
#
# The banner and the candidate rings exist to pick a droplet out of a crowd.  Once the
# operator has one, they are noise on top of the measurement: they compete with the
# fall clock for attention and can ring some other droplet mid-timing.  The operator
# asked for them to switch off, and it is not something they should have to declare --
# the pipeline can see it happen.
#
# A droplet counts as CHOSEN when any of these is true of a live track:
#   * its clock is running -- it has crossed `0` and not yet reached `16`;
#   * it already has a completed fall to its name;
#   * it has sat inside BALANCED_MAX for CHOSEN_BALANCED_S, which is what balancing
#     one looks like.
#
# What is remembered is the COLUMN, not the track.  Track identity does not survive a
# lift -- two live sessions gave 7 and 9 ids for 9 and 10 falls -- so remembering the
# track would restart the hunt between every drop.  Ⅳ.4 re-drops the same droplet five
# times and the hunt must stay quiet across all five.
CHOSEN_BALANCED_S = 3.0

# How long the column may be empty before the hunt resumes.  Longer than the gap
# between one droplet's falls -- measured at 75-108 s on 2026-09-09, lift included --
# and far shorter than the gap between droplets, which was 515 s in the same session.
# A spray releases it immediately regardless: that is the operator starting over.
CHOSEN_RELEASE_S = 150.0

# How far a fall's duration may sit from its droplet's running mean and still be the
# same droplet.  A droplet's mass does not change between drops, so neither does its
# terminal velocity: the five falls of one droplet in one_droplet_slow ran 33.44 to
# 35.41 s, a standard deviation of 0.92 s on a mean of 34.42 -- 2.7%.  10% is a little
# under four of those, wide enough that ordinary scatter never splits a droplet.
#
# What it CANNOT do is separate two droplets whose fall times happen to be within 10%
# of each other -- 33 s and 36 s would be merged.  So this proposes a grouping; it does
# not certify one.  groupFalls reports the margin at every boundary precisely so a
# marginal split is visible rather than silent.
DROPLET_TIME_TOL = 0.10


def isLegalFall(divisions, seconds):
    if seconds <= 0:
        return False
    return (divisions >= FALL_DIVISIONS
            and MIN_FALL_S <= seconds <= MAX_FALL_S
            and MIN_SPEED <= divisions / seconds <= MAX_SPEED)


# ---------------------------------------------------------------------------
#  Field-off gate.
#
#  Kinematics alone cannot tell a free fall from a driven one: a droplet pushed
#  by a live plate voltage can cross `0`->`16` at a perfectly legal-looking speed.
#  Measured on 08_13 - Aug_25 - 7 frame 2758 -- 18.26 s, 0.438 grid/s, and the
#  panel reading +0235V.
#
#  `+0000V` is the only realistic V: reading whose four digits are the same glyph,
#  so no font knowledge or reference image is needed: compare the digits against
#  each other.  The panel flickers against the camera, so only about a third of
#  frames are readable -- but a live field NEVER produces four matching digits
#  (highest seen 0.06 against 0.92 for field-off), so a handful of clean reads
#  anywhere in the fall settles it.
# ---------------------------------------------------------------------------

FIELD_SAMPLES = 15         # frames sampled across each fall
FIELD_MATCH = 0.5          # pairwise similarity meaning "same glyph"
FIELD_MIN_FRACTION = 0.15  # readable all-alike reads needed to call the field off


def bands(profile, floor):
    out, start = [], None
    hot = profile > floor
    for i, v in enumerate(hot):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i)); start = None
    if start is not None:
        out.append((start, len(hot)))
    return out


def readoutBox(roi):
    """the panel cell holding V: and t:, in full-frame coordinates"""
    return (roi["x"] + roi["w"], roi["rows"][0], roi["border"], roi["rows"][1])


def readoutLines(cell):
    rp = cell.mean(axis = 1).astype(float)
    runs = [r for r in bands(rp, rp.mean()) if r[1] - r[0] >= 0.15 * len(rp)]
    runs.sort(key = lambda r: r[0])
    return runs


# How wide a gap still counts as inside one character, as a fraction of line height.
# 0.40 was measured on the Aug_25 footage, which was a camera pointed at a monitor:
# the glowing digits bloomed into fat filled blobs whose `0` had almost no hollow.
# The Aug_31 dongle capture is sharp and high-contrast, so a `0` is a thin ring whose
# interior gap is WIDER than 0.40 * lineH -- it split into two ~5 px strokes and the
# width test then rejected the window.  Measured on 60 frames: clip 8 read 9/60,
# clip 9 read 1/60, against clip 7's 60/60.
#
# The ladder is a search, not a new constant: each rung is tried only when the
# previous one produced no valid segmentation, and validity is the plausibility test
# allDigitsAlike already applies (6-9 glyphs, four consecutive like-sized ones).
# 0.40 is tried first, so footage that already segmented cleanly is bit-identical --
# clip 7 stays 60/60 and clip 1 keeps all four of its field-off reads.  Raising the
# base value instead was measured and rejected: at a flat 0.55 clip 7 collapses to
# 16/60 because adjacent characters merge.
JOIN_FACTOR = 0.40
JOIN_LADDER = (0.40, 0.55, 0.70, 0.85)


def glyphs(line, joinFactor = JOIN_FACTOR):
    """segment a text line into characters.

    Counts bright pixels per column, so a grid line one or two pixels tall cannot
    pass as a glyph.  A `0` is hollow and splits into two strokes, so runs closer
    than `joinFactor` of the line height are merged back into one character."""
    h = line.shape[0]
    _lvl, bw = cv2.threshold(line, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    cp = (bw > 0).sum(axis = 0).astype(float)
    runs = bands(cp, max(1.0, 0.18 * h))
    if not runs:
        return []
    joinGap = max(3, int(round(joinFactor * h)))
    merged = [list(runs[0])]
    for a, b in runs[1:]:
        if a - merged[-1][1] <= joinGap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    minWide = max(3, int(round(0.25 * h)))
    return [(a, b, line[:, a:b]) for a, b in merged if b - a >= minWide]


def scoreGlyphs(gs):
    """Best score over every window of four consecutive glyphs: how alike are they?

    None means this segmentation is not a readable panel line, which is what lets
    allDigitsAlike try the next rung of the ladder."""
    # `V : + d d d d V` is 8 characters, 7 when `V` and `:` render close enough to
    # merge.  Anything else means the line did not segment properly -- abstain rather
    # than score it, because four identical *fragments* would otherwise read as
    # four identical digits.
    if not 6 <= len(gs) <= 9:
        return None
    widths = [x[1] - x[0] for x in gs]
    median = sorted(widths)[len(widths) // 2]
    imgs = [cv2.resize(x[2], (24, 32)).astype(np.float32) for x in gs]
    best = None
    for k in range(len(imgs) - 3):
        w = widths[k:k + 4]
        if min(w) < 0.6 * median or max(w) > 1.6 * min(w):
            continue                     # not four like-sized characters
        window = imgs[k:k + 4]
        worst = min(float(cv2.matchTemplate(window[i], window[j],
                                            cv2.TM_CCOEFF_NORMED)[0][0])
                    for i in range(4) for j in range(i + 1, 4))
        best = worst if best is None else max(best, worst)
    return best


def allDigitsAlike(cell):
    """True-ish score that the panel read `+0000V` on this frame; None if unreadable.

    Index-free on purpose.  Counting in from either end breaks whenever the font
    renders `V` and `:` close enough to merge, or a `0` splits, which shifts every
    position.  `+0000V` is the only realistic reading with four identical glyphs
    in a row, so where they sit in the line does not matter.
    """
    lines = readoutLines(cell)
    if not lines:
        return None
    a, b = lines[0]
    # BEST across the ladder, not the first rung that returns anything.  Stopping at
    # the first scorable segmentation let a WRONG one block the right one: on the raw
    # dongle feed a `0` splits into two 8 px strokes, and at 0.40 the window
    # [V-fragment, colon, half-zero, half-zero] passes the like-sized test and scores
    # about 0, so the rung that merges the zeros was never reached.  Measured on a
    # panel reading +0000V: 13 of 139 frames scored above 0.5, under the 15% the
    # field-off vote needs, so a field that was plainly off was reported as on.
    # Taking the maximum can only raise a score, and a fragmented segmentation scores
    # near zero while a correct one scores near 1 -- the two do not compete.
    best = None
    for joinFactor in JOIN_LADDER:
        score = scoreGlyphs(glyphs(cell[a:b, :], joinFactor))
        if score is not None:
            best = score if best is None else max(best, score)
    return best


def fieldOffFromCells(cells):
    """True if the panel read zero across these crops of the `V:` readout.

    Split out of fieldWasOff so a live run can hand over the crops it kept while
    the fall was happening.  A camera cannot seek back to them afterwards, and
    without this a live fall could never be called legal at all.
    """
    hits = readable = 0
    for cell in cells:
        score = allDigitsAlike(cell)
        if score is None:
            continue                    # panel unreadable on this frame; not a vote
        readable += 1
        if score > FIELD_MATCH:
            hits += 1
    if readable == 0:
        return None                     # tri-state: never read, so not known
    return hits / float(readable) >= FIELD_MIN_FRACTION


def fieldWasOff(path, box, f0, f1, samples = FIELD_SAMPLES):
    """sample the panel across a fall; True if the field read zero"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    cells = []
    for k in range(samples):
        f = int(f0 + (f1 - f0) * k / max(1, samples - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        if ok:
            cells.append(cv2.cvtColor(frame[box[1]:box[3], box[0]:box[2]],
                                      cv2.COLOR_BGR2GRAY))
    cap.release()
    return fieldOffFromCells(cells)


def sessionStem(path):
    """A filesystem-safe name for whatever produced these frames.

    A live session has no clip to be named after, and on Windows "cam:0" cannot be a
    directory at all, so it gets a timestamp.  Two sessions then never collide, which
    also means a run is never quietly overwritten.
    """
    if isCameraSpec(path):
        return "live_" + time.strftime("%Y%m%d_%H%M%S")
    return os.path.splitext(os.path.basename(path))[0]


def artefact(outDir, path, name):
    """Artefacts are named after the clip unless the folder already carries its name.
    Keeps a one-off run from overwriting a folder's own results."""
    stem = os.path.basename(os.path.normpath(outDir)) if isCameraSpec(path) \
        else os.path.splitext(os.path.basename(path))[0]
    if os.path.basename(os.path.normpath(outDir)) != stem:
        name = stem + "_" + name
    return os.path.join(outDir, name)


def outDirFor(path):
    # every artefact for a clip lands in output/<clip name>/
    d = os.path.join(OUTPUT, sessionStem(path))
    os.makedirs(d, exist_ok = True)
    return d


def buildBackground(path, samples = 61, start = 0, end = None):
    """Median of frames sampled evenly across [start, end).

    With the defaults this is the whole clip, which is what every result before
    2026-08-28 was computed with.  A range makes it the background of a segment --
    the same thing a rolling window does, without the sequential machinery."""
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if end is None:
        end = total
    span = max(1, end - start)
    frames = []
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(start + k * span / samples))
        ok, frame = cap.read()
        if ok:
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    return np.median(np.stack(frames), axis = 0).astype(np.uint8)


# --- rolling background ------------------------------------------------------
#
#  One median over a whole clip averages every scene it contains.  Over 30 minutes
#  the plates get bumped and re-sprayed repeatedly, so that median matches none of
#  them well -- and which falls get found then depends on how much footage was fed
#  in.  Measured: clip 1's last five minutes analysed alone found a legal fall the
#  full-clip run missed, and missed one the full run found.  Same code, same bytes,
#  deterministic; only the sample span differed.
#
#  A window fixes that, and it is also the only thing a live source can do -- it
#  cannot seek, so it can only average what has already gone past.  Same algorithm
#  both ways.
#
#  Cost control: the median of 61 crops is far too expensive to recompute per frame,
#  so it is recomputed every RECOMPUTE_S seconds and reused in between.  The scene
#  changes on the scale of minutes, so this loses nothing.

BG_SAMPLES = 61            # same count the whole-clip version used
RECOMPUTE_S = 5.0          # how often the median is rebuilt
# How long a live source spends learning its first background.  A file spreads the
# same 61 samples across the whole clip in a fraction of a second; a camera has to
# wait them out in real time, and the operator sees nothing until it finishes.
LIVE_LEARN_S = 30.0
BG_WINDOW_S = 300.0        # rolling window length.  None restores the old
                           # whole-clip median.
                           #
                           # 300 s, not shorter: on SPARSE footage a lone slow
                           # droplet appears in most samples of a short window and
                           # is absorbed into its own background.  Clip 7 (5
                           # traverses) at 120 s lost a fall that frame-stepping
                           # confirms is real -- x held 467-472 for the whole
                           # traverse.  At 300 s that fall comes back AND two more
                           # legal ones appear that the whole-clip median never
                           # found: 1 legal -> 3.
                           #
                           # Not longer: 600 s on clip 7 collapses back to the
                           # whole-clip result, 1 legal.
                           #
                           # Known miss: clip 1's 17.83 s fall (a real droplet,
                           # x 66-72 the whole way down) is found by the whole-clip
                           # median and not by any window.  Neither background is
                           # complete; their union is larger than either.


# When the operator changes the FOCUS, the picture's appearance changes but nothing
# moves.  The rolling background is a median over the last BG_WINDOW_S seconds, so it
# then holds a blend of two different-looking scenes and disagrees with every frame
# everywhere -- detections flood the whole ROI and stay there until the last
# pre-change frame ages out.  Observed live on 2026-09-07: a small focus nudge left
# the field covered in false detections for several minutes, and the session reached
# 68,609 fall ids in ten minutes against 2,362 for a healthy one.
#
# Detecting the change directly did not work.  The frame-vs-background disagreement
# does not separate it from a spray (0.31% while manipulating, 0.39% during a spray,
# 0.41% quiet), and the reticle cannot help either -- it is drawn onto the video by
# the apparatus's electronics, so its sharpness and spacing are bit-exact through a
# focus change.
#
# So detect the EFFECT instead, which is unambiguous: a spray's burst decays as the
# droplets settle -- measured on spray_labelled_0907, peak at 43 s and back to
# baseline by 68 s -- while a stale background stays elevated for the whole window.
# A rate still far above this session's own quiet level after STALE_S seconds is
# therefore not a spray, whatever it is, and the background is worth rebuilding.
STALE_FACTOR = 4.0         # times the session's quiet detection rate
STALE_S = 45.0             # sustained for this long: longer than any spray settles


class RollingBackground:
    """Median of the frames sampled over the last `windowS` seconds."""

    def __init__(self, windowS, fps, samples = BG_SAMPLES):
        # Belt and braces.  A non-positive fps silently collapses every interval
        # below to 1, which is not a degraded background but a different algorithm:
        # the window stops spanning `windowS` and the median rebuilds every frame.
        # The source is supposed to guarantee this; assert it rather than clamp.
        if not fps or fps <= 0:
            raise ValueError("RollingBackground needs a positive fps, got %r. "
                             "A live source must supply a sane nominalFps." % (fps,))
        # Belt and braces.  A non-positive fps silently collapses every interval
        # below to 1, which is not a degraded background but a different algorithm:
        # the window stops spanning `windowS` and the median rebuilds every frame.
        # The source guarantees this; assert it rather than quietly clamp.
        if not fps or fps <= 0:
            raise ValueError("RollingBackground needs a positive fps, got %r. "
                             "A live source must supply a sane nominalFps." % (fps,))
        self.every = max(1, int(round(windowS * fps / samples)))
        # Never sample on a whole number of seconds.  The panel carries a `t:`
        # readout and the display has its own refresh beat, both close to 1 Hz;
        # sampling in step with them catches the same phase every time and the
        # median never averages them out.  Measured on clip 1: a 60 s window at
        # exactly 1.000 s found 13 traverses and 0 legal falls, while the same
        # window at 1.067 s found 37 and the same 2 legal falls every other
        # window agrees on.
        def onWholeSecond(e):
            p = e / float(fps)
            return p >= 0.9 and abs(p - round(p)) < 0.005 * round(p)
        if onWholeSecond(self.every):          # step away, nearest value that is not
            for d in range(1, int(fps)):
                if not onWholeSecond(self.every + d):
                    self.every += d; break
                if self.every - d >= 1 and not onWholeSecond(self.every - d):
                    self.every -= d; break
        self.recompute = max(1, int(round(RECOMPUTE_S * fps)))
        # Kept so a resync can sample fast until the window refills, then restore.
        self.normalEvery = self.every
        self.fastEvery = max(1, int(round(0.25 * fps)))
        self.fastUntilFull = False
        self.buf = []
        self.samples = samples
        self.bg = None
        self.lastBuilt = None

    def prime(self, path, box, first, last):
        """Fill the window before tracking starts, so there is no blind warm-up.

        A file can seek, so the first window is built the way the whole-clip median
        was.  A live source cannot do this and has to wait out its first window."""
        Y, H, X, W = box
        cap = cv2.VideoCapture(path)
        top = min(last, first + self.every * self.samples)
        for k in range(self.samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(first + k * (top - first) / self.samples))
            ok, frame = cap.read()
            if ok:
                self.buf.append(cv2.cvtColor(frame[Y:Y+H, X:X+W], cv2.COLOR_BGR2GRAY))
        cap.release()

    def push(self, grayCrop, n):
        if n % self.every:
            return
        self.buf.append(grayCrop.copy())
        if len(self.buf) > self.samples:
            del self.buf[0]
        # A resync samples fast to refill quickly; once full, go back to spanning the
        # real window.  The fast frames then age out normally and the window widens
        # back to BG_WINDOW_S on its own.
        if self.fastUntilFull and len(self.buf) >= self.samples:
            self.every = self.normalEvery
            self.fastUntilFull = False

    def resync(self, grayCrop):
        """Throw the window away and rebuild from now.

        Called when the background has been judged stale.  Waiting for a
        BG_WINDOW_S window to flush itself takes five minutes; this takes about
        fifteen seconds, because the buffer refills at `fastEvery` until it is full.
        """
        self.buf = [grayCrop.copy()]
        self.bg = None
        self.lastBuilt = None
        self.every = self.fastEvery
        self.fastUntilFull = True

    def get(self, n):
        """Rebuild on ABSOLUTE frame boundaries, never relative to where the run
        started.  Anchoring the schedule to the run's own first frame makes the
        background depend on where you began reading, which is the exact
        start-dependence this class exists to remove."""
        if not self.buf:
            return self.bg          # nothing sampled yet; caller keeps the old one
        due = n - (n % self.recompute)
        if self.bg is None or self.lastBuilt != due:
            self.bg = np.median(np.stack(self.buf), axis = 0).astype(np.uint8)
            self.lastBuilt = due
        return self.bg


def autoROI(fullBackground):
    h, w = float(fullBackground.shape[0]), float(fullBackground.shape[1])
    scale = h / 1080.0            # every pixel size below was measured on 1080p footage
    minSpacing, maxSpacing = 55 * scale, 95 * scale
    minSep = max(5, int(round(20 * scale)))
    minStep = max(10, int(round(40 * scale)))
    minRun = max(50, int(round(200 * scale)))
    maxGap = max(5, int(round(25 * scale)))
    halfStrip = max(1, int(round(2 * scale)))

    def findPeaks(prof, minsep = None):
        minsep = minSep if minsep is None else minsep
        out = []
        for i in range(minsep, len(prof) - minsep):
            if not out or i - out[-1] > minsep:
                out.append(i)
            elif prof[i] > prof[out[-1]]:
                out[-1] = i
        return out

    def strongest(prof, cand, n = 20):
        return sorted(sorted(cand, key = lambda i: -prof[i])[:n])

    def walk(cand, start, step, tol):
        run = [start]
        while True:
            nxt = run[-1] + step
            near = [c for c in cand if abs(c - nxt) <= tol * abs(step)]
            if not near:
                return run
            run.append(min(near, key = lambda c: abs(c - nxt)))
            step = 0.5 * step + 0.5 * (run[-1] - run[-2])   # follow the tilt

    def score(run):
        # a real grid's gaps shrink smoothly with perspective; junk runs jump about
        if len(run) < 3:
            return (len(run), 0.0)
        gaps = np.diff(run).astype(float)
        return (len(run), -float(np.maximum(0, np.diff(gaps)).sum() / np.median(gaps)))

    def rhythm(cand, tol = 0.12):
        best = []
        for a in range(len(cand)):
            for b in range(a + 1, len(cand)):
                step = cand[b] - cand[a]
                if step < minStep:
                    continue
                run = walk(cand, cand[a], -step, tol)[::-1][:-1] \
                    + walk(cand, cand[a], step, tol)
                if score(run) > score(best):
                    best = run
        return best

    def findLines(prof, n = 20):
        return rhythm(strongest(prof, findPeaks(prof), n))

    def findRightBorder(topHat, rows):
        ends = []
        for y in rows:
            strip = topHat[max(0, y - halfStrip):y + halfStrip + 1, :].max(axis = 0)
            bright = np.where(strip > 0.35 * np.percentile(strip, 99.5))[0]
            if len(bright) < minRun:
                continue
            runs = np.split(bright, np.where(np.diff(bright) > maxGap)[0] + 1)
            ends.append(max(runs, key = len)[-1])
        return int(np.median(ends)) if len(ends) >= 5 else None

    lineFilter = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(21 * scale))))
    topHat = cv2.morphologyEx(fullBackground, cv2.MORPH_TOPHAT,
                              lineFilter).astype(np.float32)
    rowProfile = topHat[:, int(w * 0.25):int(w * 0.75)].mean(axis = 1)
    rows = findLines(rowProfile)
    spacing = float(np.median(np.diff(rows))) if len(rows) > 1 else 0.0
    rightBorder = findRightBorder(topHat, rows) if len(rows) >= 8 else None

    if len(rows) < 8 or not minSpacing < spacing < maxSpacing or rightBorder is None:
        return None

    TEXT_COL_DIV = 4.5
    GRID_DIV = 10
    margin = int(round(0.3 * spacing))
    x = max(0, int(round(rightBorder - (TEXT_COL_DIV + GRID_DIV) * spacing)))
    y = max(0, rows[0] - margin)
    textStart = int(round(rightBorder - TEXT_COL_DIV * spacing))

    # Vertical grid lines: the same top-hat trick as the rows, with a horizontal
    # kernel.  GRID_DIV = 10 was measured on the Aug_25 footage, a camera pointed at
    # a monitor, whose observation area was 9.5 divisions -- so a fixed 10 covered
    # all of it and the assumption was never tested.  The dongle delivers a wider
    # horizontal field: clip 8 shows 14.6 divisions, and anchoring 10 of them to the
    # right border threw away the leftmost 4.6 -- 75% of the first column, holding
    # 28% of the droplet candidates inside the measured `0`..`16` band.
    #
    # This only ever WIDENS.  Narrowing is never right, and the detector is not
    # reliable everywhere -- on clip 1 it finds a single line, which as an anchor
    # would collapse the ROI to nothing.  Fewer than three lines, or a leftmost line
    # that is already inside the nominal box, leaves the old geometry untouched.
    colFilter = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(21 * scale)), 1))
    colHat = cv2.morphologyEx(fullBackground, cv2.MORPH_TOPHAT,
                              colFilter).astype(np.float32)
    colProfile = colHat[rows[0]:rows[-1], :].mean(axis = 0)
    vLines, floor = [], 0.45 * float(colProfile.max())
    for i in range(minSep, len(colProfile) - minSep):
        if colProfile[i] <= floor \
           or colProfile[i] < colProfile[i - 1] \
           or colProfile[i] < colProfile[i + 1]:
            continue
        if not vLines or i - vLines[-1] > minSep:
            vLines.append(i)
        elif colProfile[i] > colProfile[vLines[-1]]:
            vLines[-1] = i
    vLines = [c for c in vLines if c <= textStart + 3]

    widened = len(vLines) >= 3 and min(vLines) - margin < x
    if widened:
        x = max(0, min(vLines) - margin)

    return {"x": x,
            "y": y,
            "w": textStart - x,
            "h": min(int(h), rows[-1] + margin) - y,
            "rows": rows,
            "spacing": spacing,
            "border": rightBorder,
            # Interior verticals to mask, in full-frame coords.  These used to be
            # returned only when the ROI had been widened, to leave the Aug_25 results
            # untouched -- but that left the production path masking a line that does
            # not exist.  Measured on one_droplet_slow (2026-09-05): the real verticals
            # are at x = 18, 392 and 809, while buildSettings' fallback of
            # `w - 5 * spacing` puts its single mask at x = 603, which is empty space.
            # So all three lines went unprotected and 21.9% of all detections landed
            # within 6 px of one, strung down the verticals exactly as the operator
            # described.  Detected lines are now used whenever there are at least two;
            # the heuristic survives only for footage where detection finds nothing.
            "cols": vLines if len(vLines) >= 2 else None}


def drawROI(fullBackground, roi, name):
    canvas = cv2.cvtColor(cv2.convertScaleAbs(fullBackground, alpha = 2.2),
                          cv2.COLOR_GRAY2BGR)
    if roi is None:
        cv2.putText(canvas, name + "  --  REJECTED, no grid in frame",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        return canvas
    x, y, ww, hh = roi["x"], roi["y"], roi["w"], roi["h"]
    cv2.line(canvas, (roi["border"], 0), (roi["border"], canvas.shape[0]),
             (0, 255, 0), 1)                                  # the anchor
    for r in roi["rows"]:
        cv2.line(canvas, (x, r), (x + ww, r), (0, 200, 255), 1)
    for c in [int(round(ww - 5 * roi["spacing"]))]:
        cv2.line(canvas, (x + c, y), (x + c, y + hh), (255, 160, 0), 1)
    cv2.rectangle(canvas, (x, y), (x + ww, y + hh), (0, 0, 255), 2)
    cv2.putText(canvas, "%s   %d rows   %.1f px/div   ROI %d,%d,%d,%d"
                % (name, len(roi["rows"]), roi["spacing"], x, y, ww, hh),
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    return canvas


# Otsu splits whatever histogram it is handed, including one with no bright class in
# it at all.  On the recorded clips the difference image is bimodal -- near-zero noise,
# droplets far above -- and Otsu lands around 38.  The raw dongle feed carries bursts
# of single-frame impulse speckle (measured 2026-09-03: no pixel is bright in more than
# one of fifteen consecutive frames), which fills the low range, and Otsu then splits
# INSIDE the noise at 3.1 -- yielding 221 detections a frame against clip 8's 6.7, and
# thousands of phantom tracks.
#
# Pre-filtering cannot fix this and was measured doing the opposite: a 3x3 median took
# it to 327/frame, a 5x5 to 376, a 3-frame temporal median to 237.  Cleaning the input
# lowers the variance, Otsu drops its split to match, and the same blobs come back.
# The threshold itself has to have a floor.
#
# The floor is the frame's own robust noise scale, so it costs nothing where Otsu is
# already sane.  6 is the SMALLEST multiple that removes the speckle, and it was chosen
# by sweeping against the reference clips rather than picked: at 6 the live feed goes
# 221.5 -> 0.1 detections a frame while clip 8 (6.7), clip 7 (3.0) and clip 1 (8.9) are
# unchanged to the decimal.  At 9 clip 7 starts to lose detections, so this is also the
# largest value that leaves every reference untouched.  Validated on live frames with
# droplets actually present: a burst frame went 163 -> 3 while a real droplet, which
# reaches 100-191 against a floor near 13, survived.
NOISE_FLOOR_MAD = 6.0

# A droplet cannot pass a 0.80 circularity gate at any size it actually has.
# `contourArea` measures the polygon through pixel CENTRES while `arcLength`
# measures its perimeter, so 4*pi*A/P^2 is biased low on small blobs: a perfect
# synthetic disc scores 0.785 at r=2 px, 0.674 at r=3, 0.763 at r=5, and does not
# reach 0.80 until r=8 -- larger than any droplet in this apparatus.  So the old
# 0.80 admitted a droplet only when noise happened to inflate its area or shorten
# its perimeter, which is a coin flip per frame.  Measured on 706 raw dongle frames
# (2026-09-04): at 0.80 a droplet was present in 77% of frames and never for more
# than 62 consecutive (2.6 s); no track survives PROBATION_S = 0.2 s on that, so
# falls never became confirmed tracks and were never timed.  Swept: continuity
# reaches 100% and stays there at 0.52 and below, so 0.50 is the knee with margin.
# Cost is 3.7 -> 14.2 detections a frame, still far under the 123/frame that the
# noise floor exists to stop.
#
# Sixth constant to be calibrated on one capture path and silently mis-fit another
# (see NEXT_SESSION.md rule 6). H.264 blurs blobs rounder and larger, which is why
# the recorded clips never showed this.
#
# HELD AT 0.80 ANYWAY, 2026-09-04.  Loosening it does fix detection -- continuity
# went to 100% and the overlay stayed on droplets that used to blink out -- but it
# raises detections from 3.7 to 14.2 a frame, and the extra blobs spawn DUPLICATE
# tracks that steal the blob from the real one.  That turns a missing measurement
# into a WRONG one: the live run at 0.50 reported two traverses of 4.6 s where the
# operator had dropped a single droplet once, and clip 8 came back with 34.10,
# 34.17 and 34.30 where one fall exists.  A benchmark can survive a missed fall; it
# cannot survive a confident wrong number.  The gate is not the real fix -- the
# duplicate-track suppression is -- so this stays at 0.80 until that is built.
# Everything measured above still stands and is why 0.80 is a stopgap, not correct.
CIRCULARITY_MIN = 0.52

# The upper size gate, in px^2 at spacing 71.5, scaled by mag^2 in buildSettings.
#
# It was 100 -- 72.8 px^2 at spacing 61 -- until 2026-09-08, and on that morning it
# threw away FOURTEEN of the fifteen falls the operator timed by hand.  Not a tracking
# failure and not a threshold failure: the droplet is on the path in every sampled
# frame of all fifteen, round and obvious, and "area>max" is the gate that discards it.
# Median apparent area, per droplet, over the five falls each:
#
#     droplet 1   62- 73 px^2        droplet 2  113-148        droplet 3  115-175
#
# Those three fall in 30.6 s, 29.2 s and 28.0 s, so by (1-13) their radii are 0.677,
# 0.690 and 0.705 um -- 4% apart, while their apparent areas are a factor of 2.8
# apart.  The apparent size is not the droplet.  At 1 px = 3.28 um (l = 1.60e-3 m
# across 8 divisions of 61 px) a 30 s droplet is 0.41 px wide: every pixel of every
# blob here is scattering halo, and its extent is set by the illumination and by where
# the operator left the focus -- which moves between droplets and during them.  An
# absolute cap in px^2 measures the focus knob, and the only fall that survived was
# the one whose halo happened to fragment under it often enough to hold a track.
#
# So put the gate where it can only catch a gross bloom.  Sampling 501 frames across
# the whole session, past every other gate, the cost of raising it is almost nothing:
# 5.79 blobs a frame at 100, 9.13 at 300, 9.52 at 900 and 9.54 with no cap at all --
# past 300 the curve is flat, because what lies above is a handful of grid-line flares
# that bloom to 700-3800 px^2 and are already rejected on circularity (0.03-0.51,
# against 0.58 at p1 for the droplets) or for sitting on a column.  900 * mag^2 is
# 655 px^2, a blob 29 px across: 2.9x the widest droplet in the fifteen (228 px^2),
# so a refocus cannot walk out of it, and still an order of magnitude below a bloom.
AREA_MAX_PX = 900


REJECTS = {}                # gate -> count, for the run-end census
NEAR_MISSES = []            # (area, circularity) of contours that failed ONLY the
                            # shape test, which is where a flickering droplet lives


def _reject(why, area = None, circ = None):
    REJECTS[why] = REJECTS.get(why, 0) + 1
    if why == "circularity" and len(NEAR_MISSES) < 20000:
        NEAR_MISSES.append((float(area), float(circ)))


def rejectCensus():
    """What the gates threw away this run, commonest first."""
    if not REJECTS:
        return "no contours were rejected"
    total = sum(REJECTS.values())
    out = ["%-16s %8s %7s" % ("gate", "count", "share")]
    for why, count in sorted(REJECTS.items(), key = lambda kv: -kv[1]):
        out.append("%-16s %8d %6.1f%%" % (why, count, 100.0 * count / total))
    if NEAR_MISSES:
        import numpy as _np
        a = _np.array(NEAR_MISSES)
        near = a[a[:, 1] >= CIRCULARITY_MIN - 0.10]
        out.append("")
        out.append("failed ONLY on circularity: %d, of which %d were within 0.10 of "
                   "the %.2f gate" % (len(a), len(near), CIRCULARITY_MIN))
        if len(near):
            out.append("  those near-misses: area p50 %.0f p90 %.0f, circularity "
                       "p50 %.2f p90 %.2f"
                       % (_np.percentile(near[:, 0], 50), _np.percentile(near[:, 0], 90),
                          _np.percentile(near[:, 1], 50), _np.percentile(near[:, 1], 90)))
    return "\n".join(out)


def detectDroplets(gray, background, s, allAreas = None):
    diff = cv2.absdiff(gray, background)
    blurred = cv2.GaussianBlur(diff, (5, 5), sigmaX = 0)
    levelOtsu, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # Subsampled: a median over every 16th pixel estimates the same scale and keeps
    # this off the per-frame budget, which matters on a live source.
    coarse = blurred[::4, ::4].astype(np.float32)
    med = float(np.median(coarse))
    mad = float(np.median(np.abs(coarse - med)))
    level = max(float(levelOtsu), med + NOISE_FLOOR_MAD * max(mad, 1.0))
    _, thresh = cv2.threshold(blurred, level, 255, cv2.THRESH_BINARY)
    levelOtsu = level                    # report what was actually used
    opened = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, s["kernel"])
    cleaned = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, s["kernel"])
    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    centres = []
    for cnt in contours:
        cArea = cv2.contourArea(cnt)
        if allAreas is not None:
            allAreas.append(cArea)
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            _reject("degenerate")
            continue
        if cArea < s["areaMin"]:
            _reject("area<min")
            continue
        if cArea > s["areaMax"]:
            _reject("area>max")
            continue
        circularity = 4 * np.pi * cArea / (perimeter ** 2)
        if circularity < CIRCULARITY_MIN:
            _reject("circularity", cArea, circularity)
            continue
        M = cv2.moments(cnt)
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        mask = s.get("staticMask")
        if mask is not None:
            iy, ix = int(round(cy)), int(round(cx))
            if 0 <= iy < mask.shape[0] and 0 <= ix < mask.shape[1] and mask[iy, ix]:
                _reject("static mark")
                continue                # sitting on something the apparatus drew
        if min(abs(cy - r) for r in s["gridRows"]) > s["gridClear"] \
           and min(abs(cx - c) for c in s["gridCols"]) > s["gridClear"] \
           and cx < s["roiW"] - s["panelClear"] \
           and min(cx, cy, s["roiW"] - cx, s["roiH"] - cy) > s["edgeClear"]:
            centres.append((cx, cy, cArea))
        else:
            if min(abs(cy - r) for r in s["gridRows"]) <= s["gridClear"]:
                _reject("grid row")
            elif s["gridCols"] and min(abs(cx - c) for c in s["gridCols"]) <= s["gridClear"]:
                _reject("grid col")
            elif cx >= s["roiW"] - s["panelClear"]:
                _reject("panel margin")
            else:
                _reject("frame edge")
    return centres, levelOtsu, len(contours)


# What counts as "permanently bright" in the learned background, as a percentile of
# it, and how far that is grown to cover the blur around each mark.
#
# Otsu was the first choice and it is wrong here: on a frame that is mostly empty field
# it lands at 92 of 255 and masks 17% of the picture, swallowing droplets.  The reticle
# is far brighter than that -- the background reads mean 37, p99 189, max 211 -- so the
# furniture is the top percent or so, not the top forty.
# The apparatus draws marks into the video that are not droplets and never move: the
# `0` and `1.6` reticle labels, the `V:`/`t:` panel text.  They are permanently in the
# picture, so they are in the learned background -- and a droplet is by definition the
# thing that is not.  Noise on their high-contrast edges still produces detections, and
# an operator watched rings latch onto the `0` glyph and onto `t:`.
#
# Masking everything bright was tried and is far too blunt: Otsu lands at 92 of 255 and
# masks 17% of the picture, a 99th-percentile cut misses the labels entirely because
# the glyphs are thin, and masking static EDGES costs 20% because the grid lines are
# long.  All three swallow droplets to catch two small marks.
#
# The lines already have gridClear.  What is left is compact: a glyph fits inside a
# grid division, a rule does not.  Selecting bright components smaller than a division
# in both axes masks 0.41% of the frame and covers every label on the picture.
STATIC_MASK_MIN = 60.0       # grey level, floored against the field's own p90
STATIC_MASK_MAX_DIV = 1.2    # a mark wider or taller than this is a rule, not a glyph
STATIC_MASK_GROW = 7         # a blob can overlap a mark its centre clears


def staticMask(background, spacing):
    """Pixels holding a mark the apparatus drew, which no droplet can be."""
    level = max(STATIC_MASK_MIN, float(np.percentile(background, 90)))
    hot = (background > level).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(hot, 8)
    compact = np.zeros_like(hot)
    limit = STATIC_MASK_MAX_DIV * spacing
    for i in range(1, count):
        _x, _y, w, h, _area = stats[i]
        if w < limit and h < limit:
            compact[labels == i] = 1
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (STATIC_MASK_GROW, STATIC_MASK_GROW))
    return cv2.dilate(compact, k) > 0



def buildSettings(roi, spacing):
    mag = spacing / 71.5           # px/division on 150V_1, where these were measured
    return {"kernel": cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            "areaMin": 8 * mag ** 2, "areaMax": AREA_MAX_PX * mag ** 2,
            # AREA_TOLERANCE is a RELATIVE test, which is brutal on small blobs: a
            # droplet measured at 10 px one frame and 18 the next differs by 80%,
            # and that is ordinary threshold noise on a 5-pixel object, not a
            # different droplet.  Measured on clip 1 f52313-52341: one droplet's
            # area swung between 10 and 27 px on consecutive frames, and the gate
            # rejected the correct blob on 42 of them.  Dividing by at least this
            # floor keeps the test meaningful on big blobs while letting normal
            # noise through on small ones.
            "areaFloor": 20 * mag ** 2,
            "gridClear": 4 * mag, "edgeClear": 20 * mag,
            # The panel's leftmost characters -- the `V:` and `t:` labels -- fall
            # INSIDE the ROI, about 21 px from its right edge, which is just
            # outside edgeClear's 17.  They are bright, round and permanent, so
            # they pass every shape gate and never move; one was ringed as a
            # balanced droplet and announced as "holding at 250 V".
            #
            # The charge test cannot save us and in fact endorses them: the panel
            # DIGITS change when the operator turns the knob, so a track sitting
            # on the readout sees different motion at different voltages, which is
            # precisely the signature of a charged droplet.
            #
            # 40 * mag = 34 px at spacing 61, past the labels and still well right
            # of the last grid column at x = 809.
            "panelClear": 40 * mag,
            "dupPx": DUPLICATE_PX * mag,
            "maxStep": 12 * mag,        # px between consecutive frames at 30 fps
            # The same limit as a SPEED, which is what it was always trying to say.
            # A gate in px-per-frame silently means something different on a camera
            # delivering 27 fps than on one delivering 30; px-per-second does not.
            "maxSpeed": 12 * mag * 30.0,
            # Sideways tolerance for continuing a track across a gap.  A distance,
            # so it must not scale with how long the gap was -- a falling droplet
            # drifts 5-6 px sideways across a whole traverse however long it takes.
            "reidX": REID_X_PX * mag,
            "gridRows": [r - roi["y"] for r in roi["rows"]],
            "gridCols": [c - roi["x"] for c in roi["cols"]
                         if roi["x"] < c < roi["x"] + roi["w"]]
                        if roi.get("cols") else [int(round(roi["w"] - 5 * spacing))],
            "roiW": roi["w"], "roiH": roi["h"]}


def previewAll(folder = None, outDir = None):
    if outDir is None:
        outDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roi_check")
    os.makedirs(outDir, exist_ok = True)
    for path in sorted(glob.glob(os.path.join(folder, "*.mp4"))):
        name = os.path.splitext(os.path.basename(path))[0]
        fullBackground = buildBackground(path)
        roi = autoROI(fullBackground)
        cv2.imwrite(os.path.join(outDir, name + ".png"),
                    drawROI(fullBackground, roi, name))
        if roi is None:
            print("%-26s REJECTED" % name, flush = True)
        else:
            print("%-26s %2d rows  %5.1f px/div  ROI %4d,%4d,%4d,%4d"
                  % (name, len(roi["rows"]), roi["spacing"],
                     roi["x"], roi["y"], roi["w"], roi["h"]), flush = True)
    print("Wrote previews to", outDir)



# ==========================================================================
#  Multi-droplet tracking.  No click, no chosen target: every droplet is
#  tracked, and a droplet earns its numbers by completing `0` -> `16`.
# ==========================================================================

# A droplet is invisible while it overlaps a grid line, and the SLOWER it moves the
# longer that lasts -- the legal droplets are the slowest ones, so both gates below are
# set by what they need, not by what a fast droplet needs.  Measured on
# VID_20260825_100038: at 0.5 s / 0.4 both legal falls are lost; at 3.0 s / 0.7 both
# are recovered.  Runaway predictions are held back by the velocity clamp in
# Track.update and by global assignment, not by these.
MAX_COAST_S = 3.0          # how long a CONFIRMED track may be predicted through a blackout

# ...and how long one whose CLOCK IS RUNNING may be, which is a different question.
# A track that has crossed `0` and not yet reached `16` carries a measurement no other
# track can replace: lose it and the fall is gone, while losing an ordinary track costs
# a duplicate at worst.  It also fails in a predictable place -- gridClear blanks every
# detection within 4 * mag px of a row, and `16` IS a row, so the droplet is guaranteed
# invisible exactly where the measurement completes.
#
# Two live sessions lost a fall this way: 2026-09-08 at 16.2 s of a 17.2 s fall, and
# 2026-09-09 at 28.2 s of a 33.5 s one, 5.3 s from the bottom.  Both were legal, both
# were verified by the operator's own stopwatch afterwards.
#
# The risk of coasting longer is a wrong re-acquisition, and it is bounded: the
# crossing is interpolated between the last point above the line and the first below,
# so a longer gap costs accuracy only if the droplet changed speed while invisible --
# and a droplet in free fall at terminal velocity does not.
MEASURING_COAST_S = 6.0
# How close a new detection may be to a track that already exists before we refuse
# to spawn on it.  Measured on clip 8 (2026-09-05) with the gate loosened to 0.52:
# 868 of 2814 spawned tracks -- 31% -- were born within 12 px of a live track, which
# is where the duplicate falls came from (34.10 / 34.17 / 34.30 for one droplet).
# Two genuinely different CONFIRMED droplets are that close only 0.596% of the time:
# their separation is 10.3 px at p0.5 and 18.1 px at p1, so 12 px sits between the
# two and costs almost nothing.  Expressed pre-magnification: 14 * mag = 11.9 px at
# spacing 61.
DUPLICATE_PX = 14

# TRIED 2026-09-05 AND REJECTED -- kept because the finding behind it is real and
# someone will otherwise propose it again.
#
# The idea: a new track may only start on a detection that ALSO appeared in the
# previous frame.
# The apparatus emits bursts of single-frame speckle whenever the operator has hands
# on it -- zoom, focus, voltage -- and the tracker was chaining those unrelated one-
# frame blobs into tracks that carried physically plausible velocities.  Stepping
# through three consecutive frames at 4:18 of one_droplet_slow shows it plainly: every
# circle is somewhere new, nothing persists, and the HUD still read `tracked 33`.
#
# Speckle is uncorrelated between frames; a droplet is not.  Measured over that whole
# session: requiring a neighbour within 6 px in the previous frame removes 87% of the
# detections during the noisy minutes (54.1/frame -> 7.1 at minute 4, 48.3 -> 5.6 at
# minute 19) while a real droplet corroborates about 70% of the time -- and 70% is
# plenty, because this gates SPAWNING ONLY.  An established track keeps being fed by
# assign() whether or not this frame's blob was corroborated, so a fall in progress
# is untouched.  7 * mag = 6.0 px at spacing 61, the radius the measurement used.
#
# It does not work.  On one_droplet_slow at gate 0.52 it took the result from 5 of the
# operator's 5 timed falls down to 3, while saving only one spurious traverse.  The
# 70% corroboration rate is an average over ALL detections, and the moments that
# actually matter are the spawn instants -- a droplet that fails to corroborate in the
# one frame where its track would have started never gets a track at all, and the
# whole fall is lost.  Averages do not gate; instants do.
#
# The underlying observation stands and still needs a fix: the apparatus emits bursts
# of single-frame speckle whenever the operator has hands on it, and those get chained
# into tracks carrying plausible velocities.  Whatever solves it must not depend on
# any single frame's detection surviving.
CORROBORATE_PX = 7              # unused; see above

# --- finding the sprays -----------------------------------------------------------
#
# The operator's rule, 2026-09-05, and it is what makes this implementable at all:
# once a suitable droplet has been chosen there is NO spraying between its falls.  So
# a spray is never noise inside a droplet's sequence -- it is the boundary between one
# droplet and the next.  Fewer than five falls before it means the droplet was
# abandoned; five means they moved on deliberately.
#
# That turns spray detection into something with a definition instead of a threshold
# pulled out of the air: a sustained jump in detections per frame, far above what this
# session normally runs at, lasting longer than a flicker.
#
# `factor` is relative to a trailing median rather than absolute, because the quiet
# rate is a property of the apparatus and the session -- 5.8 detections a frame on
# clip 8, 4 on one_droplet_slow, 123 on the noisy 3 Sep unit.  An absolute number
# calibrated on one of those is meaningless on the others; that mistake has been made
# five times in this project already.
# The quiet level is a LOW PERCENTILE of the whole session, not a trailing median.
# A trailing window cannot see a spray in the first seconds of a run -- its history is
# the burst itself -- and that is not hypothetical: on the 2026-09-07 labelled capture
# every trailing-window setting found the spray at 43 s and missed the one at 1 s.
# spraySpans runs once at the end of a run with the entire series in hand, so it can
# use a global estimate, and a low percentile is the right one because sprays and
# their settling tails are always the minority of a session.
SPRAY_QUIET_PCT = 25       # percentile of detections/frame taken as "quiet"
SPRAY_MIN_S = 1.5          # shorter than this is a flicker, not a spray
SPRAY_GAP_S = 3.0          # two bursts closer than this are one spray
# Measured on test/fixtures/spray_labelled_0907: two sprays, at 1 s and 43 s, with
# nothing else done to the apparatus, on a unit whose empty-chamber noise is 0.74
# detections a frame.  Quiet ran 2-4/frame (p25 = 3), the sprays peaked at 19 and 41.
# factor 3 puts the line at 9, which catches both and nothing between them.
SPRAY_FACTOR = 3.0
SPRAY_MIN_ABS = 6.0        # floor, so a very quiet session cannot make 3x trivial


def spraySpans(times, counts, factor = None, minAbs = None, minSeconds = None,
               gapSeconds = None, quietPct = None):
    """Spans of (start, end) seconds where the detection rate says somebody sprayed.

    Pure function over a recorded series, so it can be swept offline against a capture
    whose spray moments the operator wrote down, and so the same code decides it for a
    file and for a live session.

    A frame counts as spray-like when its detection count exceeds both an absolute
    floor and `factor` times this session's quiet level, taken as its `quietPct`
    percentile.  Relative rather than absolute because the quiet rate is a property of
    the apparatus: 5.8 detections a frame on clip 8, 4 on one_droplet_slow, 0.74 on an
    empty chamber of the 7 Sep unit, 123 on the bad 3 Sep one.  A number calibrated on
    any one of those means nothing on the others.

    Runs shorter than `minSeconds` are discarded, and runs separated by less than
    `gapSeconds` are merged, because a spray is several puffs.

    THIS FINDS BURSTS.  IT CANNOT SAY WHAT CAUSED ONE.  Do not use it to mark droplet
    boundaries.  The operator re-focuses mid-fall when the droplet being timed drifts,
    so a burst is NOT evidence that no droplet is being measured -- treating one as a
    boundary would cut a fall in half.

    Measured on zoom_labelled_0907 (2026-09-07), which contains a spray, field changes
    and several zoom touches in one series.  Five signals were tried and every one of
    them puts a spray and a hand on the apparatus in the same place:

        detections/frame     spray 9.1     manipulating 15.1 and 10.0
        burst shape          neither shows the settling decay a spray was expected to
        near a grid line     spray 40%     manipulating 40% and 47%
        vertical spread      spray 112 px  manipulating 151 and 147 -- spray LOWEST
        new confirmed tracks spray 16.6/s  manipulating 19.6/s and 5.3/s

    A sixth, the grid itself, is also dead: the reticle is drawn onto the video by the
    apparatus's electronics, so its spacing and offset are bit-exact through a zoom
    change (61.0 and 54.0, sd 0.00).  Issue #7 wanted this to derive `binding`
    automatically. It cannot, on this evidence. `binding` comes from the operator's
    notes, checked against groupFalls().
    """
    factor = SPRAY_FACTOR if factor is None else factor
    minAbs = SPRAY_MIN_ABS if minAbs is None else minAbs
    minSeconds = SPRAY_MIN_S if minSeconds is None else minSeconds
    gapSeconds = SPRAY_GAP_S if gapSeconds is None else gapSeconds
    quietPct = SPRAY_QUIET_PCT if quietPct is None else quietPct
    times, counts = np.asarray(times, float), np.asarray(counts, float)
    if len(times) < 2 or len(times) != len(counts):
        return []
    quiet = float(np.percentile(counts, quietPct))
    hot = counts > max(minAbs, factor * quiet)
    spans, start = [], None
    for i, h in enumerate(hot):
        if h and start is None:
            start = i
        elif not h and start is not None:
            spans.append((times[start], times[i - 1]))
            start = None
    if start is not None:
        spans.append((times[start], times[-1]))
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= gapSeconds:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(round(a, 2), round(b, 2)) for a, b in merged
            if b - a >= minSeconds]

# How far a droplet must travel back UP before the run in progress is abandoned.
# In grid divisions; see updateCrossings for why this is a direction and not a height.
RISE_VOID_DIVISIONS = 0.5

CONFIRM_POINTS = 6         # seen this many times before a track is believed;
                           # 8 loses the 17.83 s legal fall, 6 keeps both and
                           # cuts peak live tracks from 604 to 52
PROBATION_S = 0.2          # an unconfirmed track dies this fast; kills one-frame strays
AREA_TOLERANCE = 0.7       # a re-found blob may differ this much in area
SCREEN_MIN, SCREEN_MAX = 0.15, 0.60     # provisional band, deliberately wider than legal

# How long a track must have survived before the overlay draws it.  DISPLAY ONLY --
# detection, tracking and legality are untouched, and the annotated recording keeps
# everything.  This exists because the apparatus emits bursts of one-frame speckle
# that no measurement can separate from a droplet (six signals tried, all failed), so
# the screen fills with markers on things that are not there.  A real droplet lives
# for the whole traverse -- 16 s at the very least -- so a second and a half costs
# nothing and removes almost all of it.
DRAW_MIN_AGE_S = 1.5

# ...and it must have BEEN somewhere.  Age alone only removes speckle that dies fast;
# a speck that persists and sits still is drawn, and the screen fills with markers on
# things that are not there.  Six signals were measured to separate spray from
# droplets and none of them worked, but "has it moved in its whole life" is not a
# separation of spray from droplets -- it is the difference between a thing and a
# nothing, and it costs no measurement because DISPLAY is all it touches.
#
# A fifth of a division.  Small enough that a droplet caught and held still is drawn
# on the strength of the fall that brought it there, large enough that jitter on a
# fixed speck never reaches it.  Started at a third and loosened on the operator's
# call, to keep the gate well clear of anything they might actually want to see --
# it costs nothing to be generous here, because this only decides what is DRAWN.
DRAW_MIN_TRAVEL_DIV = 0.2

# --- re-acquiring a droplet after the track dies ----------------------------
#
#  A track only dies after MAX_COAST_S with no match, so by then the droplet has
#  been unseen for 3 s.  When it reappears a NEW track is created, starting with
#  fTop unset -- so a droplet that crossed `0`, went dark, and came back finishes
#  its fall with no record of ever having started one.  Measured on clip 1: 388
#  traverses began, only 157 finished.
#
#  The information is not lost, it is just thrown away: the dying track knows
#  where it crossed `0`.  So keep the corpse for a while and let a new detection
#  inherit from it.
#
#  The gate is deliberately asymmetric.  A falling droplet's x barely moves -- 6 px
#  over a whole 8-division traverse on the falls we frame-stepped -- while y is
#  predictable from its speed but with growing error.  A circular gate wide enough
#  to cover 3 s of falling would be far too loose sideways, which is exactly how
#  two droplets get filed as one.
REID_MAX_S = 6.0           # how long a dead track waits to be claimed
REID_X_PX = 14             # sideways tolerance, scaled by magnification
REID_Y_SLACK = 0.6         # fraction of the predicted drop allowed as error


class Track:
    nextId = 0

    def __init__(self, centre, t):
        Track.nextId += 1
        self.id = Track.nextId
        self.pos = np.array(centre[:2], dtype = float)
        self.vel = np.zeros(2)              # px per SECOND, not per frame
        self.area = centre[2]
        self.miss = 0
        self.lastT = float(t)               # capture time of the last real match
        self.points = [(float(t), float(centre[1]))]
        self.xs = [float(centre[0])]        # parallel to points; only fallDrift reads it
        self.reacquired = 0
        self.adopted = False        # this fall's `0` came from another track's crossing
        self.heldSince = None       # when it settled on the top line, for MEASURE_HOLD_S                 # times this fall was stitched across a gap
        self.byVolts = {}                   # bucketed plate voltage -> descent rates
        self.alerted = False                # the operator has been told about this one
        self.lastRow = None                 # (time, index) of the last grid line crossed
        self.divisionSeconds = None         # ...and how long the division before it took
        self.yDeepest = None                # lowest point reached since tTop
        self.tTop = None                    # capture time at the `0` crossing
        self.tBottom = None
        self.fTop = None                    # frame index of the same, files only
        self.fBottom = None
        self.seconds = None
        self.legal = None
        self.colour = tuple(int(v) for v in np.random.randint(90, 256, 3))

    def predict(self, now):
        """Where the droplet should be at time `now`, given where it was going."""
        return self.pos + self.vel * max(0.0, float(now) - self.lastT)

    def update(self, centre, t, maxSpeed):
        p = np.array(centre[:2], dtype = float)
        dt = float(t) - self.lastT
        if dt > 0:
            self.vel = 0.75 * self.vel + 0.25 * (p - self.pos) / dt
            fast = np.hypot(self.vel[0], self.vel[1])
            if fast > maxSpeed:             # never let a bad match fling the prediction
                self.vel *= maxSpeed / fast
        self.pos, self.area, self.miss = p, centre[2], 0
        self.lastT = float(t)
        self.points.append((float(t), float(centre[1])))
        self.xs.append(float(centre[0]))

    def observeVoltage(self, volts, spacing):
        """Record how fast this droplet is descending, at the plate voltage given.

        Keyed on the VOLTAGE, not on whether the field is on.  The operator's low-end
        method toggles 平衡/提升, which is field-on at both ends and 210 V apart, so a
        field on/off flag never changes during it and a charge test built on that flag
        can never fire.  A voltage is the thing that actually changed.

        Bucketed to 10 V so the supply wandering by a volt does not scatter one hold
        across a dozen keys.  Cheap on purpose: update() already maintains the
        velocity, so this is a dict append.
        """
        if volts is None or len(self.points) < CONFIRM_POINTS or spacing <= 0:
            return
        key = int(round(volts / 10.0)) * 10
        bucket = self.byVolts.setdefault(key, [])
        bucket.append(float(self.vel[1]) / spacing)
        if len(bucket) > 200:               # a couple of minutes is plenty of history
            del bucket[0]

    def chargeState(self):
        """True if this droplet answers to the field, False if not, None if unknown.

        False covers two physical cases the operator does not need told apart: a
        neutral droplet that ignores the field, and one the field pushes DOWN.  Neither
        can be balanced, so neither is worth chasing.

        Answered off the two furthest-apart voltages this droplet has been seen at,
        which makes it work for either toggle -- 0V/工作 or 平衡/提升 -- without
        knowing or caring which the operator used.
        """
        usable = [(v, rates) for v, rates in self.byVolts.items()
                  if len(rates) >= CHARGE_MIN_SAMPLES]
        if len(usable) < 2:
            return None
        usable.sort()
        loV, loRates = usable[0]
        hiV, hiRates = usable[-1]
        if hiV - loV < CHARGE_MIN_STEP_V:
            return None
        # More volts must mean less falling.  A droplet that descends just as fast at
        # 360 V as at 150 V is not charged, whatever else it is doing.
        return (float(np.median(loRates))
                - float(np.median(hiRates))) >= CHARGE_MIN_SEPARATION

    def balanceVoltageEstimate(self):
        """(low, high) volts that would balance this droplet, or None.

        From two plate voltages it has already been seen at.  With
        v = v_free (1 - U/U_b) at each,

            U_b = (v1 U2 - v2 U1) / (v1 - v2)

        and v_free cancels, which is what makes this usable: the operator's low-end
        method toggles 平衡/提升 and never visits 0 V, so anything needing a free-fall
        reading would never fire during it.  Any two voltages far enough apart will do.

        The band comes from the spread of the readings themselves -- the estimate is
        computed at each combination of the two buckets' quartiles and the extremes
        kept -- rather than from a made-up tolerance.
        """
        usable = [(v, rates) for v, rates in self.byVolts.items()
                  if len(rates) >= CHARGE_MIN_SAMPLES]
        if len(usable) < 2:
            return None
        usable.sort()
        (u1, r1), (u2, r2) = usable[0], usable[-1]
        if u2 - u1 < CHARGE_MIN_STEP_V:
            return None
        out = []
        for a in np.percentile(r1, [25, 75]):
            for b in np.percentile(r2, [25, 75]):
                if abs(a - b) < 1e-6:
                    continue                # no response: uncharged, not balanceable
                ub = (a * u2 - b * u1) / (a - b)
                if SUGGEST_MIN_V <= ub <= SUGGEST_MAX_V:
                    out.append(ub)
        if len(out) < 2:
            return None
        return min(out), max(out)

    def windowFit(self, spacing, now, seconds = None):
        """(grid/s, rms residual px, divisions fallen) over the last few seconds.

        Returns None until there is enough of the window to fit.  The residual is the
        part recentSpeed() never reported and the part that actually separates a fall
        from a coincidence.
        """
        seconds = CANDIDATE_FIT_S if seconds is None else seconds
        pts = [p for p in self.points if now - p[0] <= seconds]
        if len(pts) < 20 or spacing <= 0:
            return None
        ts = np.array([p[0] for p in pts], dtype = float)
        ys = np.array([p[1] for p in pts], dtype = float)
        if ts[-1] - ts[0] < seconds * 0.6:
            return None
        m, c = np.polyfit(ts, ys, 1)
        resid = float(np.sqrt(np.mean((ys - (m * ts + c)) ** 2)))
        return float(m) / spacing, resid, float(ys[-1] - ys[0]) / spacing

    def recentSpeed(self, spacing, window = 30):
        # provisional speed in grid divisions per second, from the last ~1 s of points.
        # Fitting against capture time means no fps appears here either.
        pts = self.points[-window:]
        if len(pts) < 8:
            return None
        ts = np.array([p[0] for p in pts], dtype = float)
        y = np.array([p[1] for p in pts], dtype = float)
        if ts[-1] == ts[0]:
            return None
        return float(np.polyfit(ts, y, 1)[0]) / spacing


def reacquire(dead, centre, now, s):
    """Find the dead track this detection most likely continues, or None.

    Only confirmed tracks are considered -- letting a one-frame stray hand its
    history to something else is how a fabricated fall gets built.
    """
    x, y, area = float(centre[0]), float(centre[1]), centre[2]
    xGate = s["reidX"]
    best, bestCost = None, None
    for t in dead:
        lastT, lastY = t.points[-1]
        elapsed = float(now) - lastT               # seconds, from the capture clock
        if elapsed <= 0 or elapsed > REID_MAX_S:
            continue
        if abs(x - t.xs[-1]) > xGate:              # a falling droplet does not sidestep
            continue
        vy = t.vel[1]
        if vy == 0:                                 # only continue things that were moving
            continue
        predY = lastY + vy * elapsed
        slack = max(4.0, REID_Y_SLACK * abs(predY - lastY))
        # Must be where it was going, which for a lifted droplet is UPWARD.  This read
        # `vy <= 0: continue` and so refused to revive anything rising, which is half
        # of why 提升 ends a track -- the other half was the same assumption in
        # assign().  Reviving a riser cannot resurrect a stale fall clock: rising
        # RISE_VOID_DIVISIONS clears tTop before this can matter.
        if vy > 0:
            if not (lastY - 4.0 <= y <= predY + slack):
                continue
        elif not (predY - slack <= y <= lastY + 4.0):
            continue
        if abs(area - t.area) / max(t.area, 1.0) > AREA_TOLERANCE:
            continue
        cost = abs(y - predY) + 3.0 * abs(x - t.xs[-1])
        if bestCost is None or cost < bestCost:
            best, bestCost = t, cost
    return best


def assign(tracks, centres, maxStep, areaFloor, now, xGate):
    """Match live tracks to this frame's detections.

    `maxStep` is how far a droplet may move in THIS frame interval, in px: a speed
    multiplied by the actual gap between these two timestamps.  A dropped frame
    therefore widens the gate by exactly the right amount on its own, instead of
    orphaning the track.  `now` is this frame's capture time and `xGate` the sideways
    tolerance, which is a fixed distance and must NOT grow with the interval.
    """
    # one global assignment, so two tracks can never claim the same blob
    if not tracks or not centres:
        return {}
    # ...but not all tracks are equal.  A track whose clock is running holds a
    # measurement nothing else can reproduce, and it competes here against strays that
    # cost a duplicate at worst.  On 2026-09-09 a bright fast blob crossed near a
    # droplet 5 s from finishing its fall and the fall was lost.
    #
    # So resolve the measuring tracks against every blob FIRST, take those blobs off
    # the table, and let everything else compete for what is left.  Two optimal
    # assignments over disjoint sets, not one over the union -- which is exactly the
    # priority the physics asks for and costs one extra solve of a much smaller matrix.
    measuring = [i for i, t in enumerate(tracks)
                 if t.tTop is not None and t.tBottom is None]
    if measuring and len(measuring) < len(tracks):
        first = assign([tracks[i] for i in measuring], centres, maxStep, areaFloor,
                       now, xGate)
        pairs = {measuring[r]: c for r, c in first.items()}
        taken = set(pairs.values())
        restI = [i for i in range(len(tracks)) if i not in set(measuring)]
        restC = [j for j in range(len(centres)) if j not in taken]
        if restI and restC:
            second = assign([tracks[i] for i in restI], [centres[j] for j in restC],
                            maxStep, areaFloor, now, xGate)
            for r, c in second.items():
                pairs[restI[r]] = restC[c]
        return pairs
    BIG = 1e6
    cost = np.full((len(tracks), len(centres)), BIG)
    for i, t in enumerate(tracks):
        aim = t.predict(now)
        # A COASTING track needs a wider gate than a tracked one.
        #
        # The droplets blink out of the footage itself, in bursts of roughly 15
        # frames, all of them at once -- nothing to do with the background or the
        # threshold, which are unchanged across the collapse.  Measured on clip 1:
        # of 190 tracks that died mid-traverse, 187 died with no contour anywhere
        # near them, and for 7 of the 18 that were still on a legal pace the
        # droplet came back 0.9-1.5 s later, further from the prediction than one
        # frame's step can reach.  So the track could never take its own droplet
        # back, and a new track -- with no fTop -- claimed it instead.
        #
        # The gate is the same asymmetric shape reacquire() uses, for the same
        # reason: tight in x, because a verified fall drifts only 5-6 px sideways
        # across a whole traverse, and elapsed-scaled in y, because that is the
        # only axis the droplet is actually moving along.  A circle wide enough to
        # cover a second of falling would be far too loose sideways, and that is
        # exactly how two droplets get filed as one.
        # `t.vel[1] > 0` here meant FALLING ONLY, and that is why a lift ends a
        # track.  A droplet the operator raises is moving up, so it failed this test,
        # got no re-acquisition window at all, and any blink -- a grid line, a dim
        # frame -- ended it.  One droplet's three falls carried three different ids on
        # 2026-09-10, and two earlier sessions produced 7 ids for 9 falls and 9 for 10.
        # That is also why binding had to be moved onto position in the first place.
        #
        # The window still opens along the axis of travel only, which is the reason it
        # was asymmetric to begin with -- it is simply mirrored when travel is upward,
        # rather than being absent.
        wide = t.miss > 0 and len(t.points) >= CONFIRM_POINTS and t.vel[1] != 0
        if wide:
            slack = max(4.0, REID_Y_SLACK * abs(aim[1] - t.pos[1]))
            if t.vel[1] > 0:
                yLo, yHi = t.pos[1] - 4.0, aim[1] + slack
            else:
                yLo, yHi = aim[1] - slack, t.pos[1] + 4.0
        for j, c in enumerate(centres):
            d = float(np.hypot(c[0] - aim[0], c[1] - aim[1]))
            if d > maxStep:
                if not wide:
                    continue
                if abs(c[0] - t.pos[0]) > xGate or not yLo <= c[1] <= yHi:
                    continue
            areaDiff = abs(c[2] - t.area) / max(t.area, areaFloor)
            if areaDiff > AREA_TOLERANCE:
                continue
            cost[i, j] = d + areaDiff * maxStep      # appearance weighs as much as position
    pairs = {}
    if HAVE_SCIPY:
        rows, cols = linear_sum_assignment(cost)
        for r, c in zip(rows, cols):
            if cost[r, c] < BIG:
                pairs[int(r)] = int(c)
    else:
        order = np.argsort(cost, axis = None)
        takenT, takenC = set(), set()
        for flat in order:
            r, c = int(flat // cost.shape[1]), int(flat % cost.shape[1])
            if cost[r, c] >= BIG:
                break
            if r in takenT or c in takenC:
                continue
            takenT.add(r); takenC.add(c); pairs[r] = c
    return pairs


# --------------------------------------------------------------------------------
#  THE HUNT
#
#  Ⅳ.2 tells the operator to "select the one with a falling speed of 0.2~0.5 grid/s"
#  by eye, on a crowded screen, seconds after a spray.  That is the step this section
#  mechanises, and it is where the operator's time actually goes: on 2026-09-08 the
#  fifteen timed falls were 7.5 minutes of a 40-minute session.  Timing was never the
#  expensive part.  Hunting was.
#
#  Nothing here decides anything.  It draws on the screen and it beeps; legality,
#  timing and charge are untouched by every line of it.
# --------------------------------------------------------------------------------

# Seconds of track the candidate fit runs over.  A line fit's slope error falls as
# about T^-1.5, so 1 s -> 3 s cuts it roughly fivefold, and a fall is straight over 3 s
# so nothing is lost to curvature.  The cost is latency, and there is room for it: from
# mid-screen to the edge is ~4 divisions, which at 0.2-0.5 grid/s is 8-20 s of warning.
CANDIDATE_FIT_S = 3.0

# Straightness, as the rms departure from that fitted line.  This is the discriminator
# that speed alone lacks: junk which happens to average into the legal band scatters,
# while a real fall is a line.  Measured over the alert trial on bench_0908, the six
# tracks that went on to complete a legal traverse fitted at 0.49-2.99 px.
CANDIDATE_MAX_RESIDUAL_PX = 3.0

# ...and it must have actually gone somewhere.  A track wobbling in place can fit a
# slope; one that has descended most of a division has not.
CANDIDATE_MIN_DIVISIONS = 0.8
CANDIDATE_MIN_AGE_S = 3.0          # never judge a track younger than its own fit window

# How near the bottom of the picture a candidate must be before the operator is told to
# catch it.  In seconds of remaining travel, not pixels: a droplet at 0.2 grid/s has
# five times the grace of one at 0.5.
EDGE_WARN_S = 8.0

# Deciding whether a droplet is charged at all -- Ⅳ.2's FIRST stage, which nothing has
# automated until now.  An uncharged droplet passes the speed test and is useless: it
# cannot be balanced, so it can never yield a voltage.  Measured on bench_0908 over
# tracks seen in both field states, a charged droplet reads about -0.3 grid/s with the
# field on and +0.2 with it off, while an uncharged one reads the same either way
# (0.300 against 0.277, or 0.003 against 0.024).  A tenth of a division per second sits
# far below that gap and far above the noise.
#
# The sign matters as much as the size.  A droplet the field pushes DOWN is charged and
# equally unusable, and an earlier version of this test called it "no response" because
# it only looked at the magnitude.  What the operator needs is "can this be balanced",
# which means the field must slow it or lift it.
CHARGE_MIN_SEPARATION = 0.10       # grid/s between field-off and field-on descent

# How many observations in EACH field state before the question is answerable.  This
# was 12, which assumed the operator would hold each side of the toggle for a couple of
# seconds.  They do not, and cannot: at 150-250 V most of a fresh spray leaves the
# field of view within a second, so the real toggle interval is 0.5-1 s.  At ~23 fps
# that is 12-23 frames per side, and demanding 12 made the test unanswerable exactly
# when it was being used properly.
CHARGE_MIN_SAMPLES = 5

# How slowly a droplet must drift, with the field ON, to be worth chasing.  Direction
# does not matter -- only that the two forces nearly cancel, because that is what makes
# a droplet balanceable with a modest turn of the knob.
#
# Derived.  At plate voltage U the net force is mg(1 - U/U_b), so the terminal drift is
#
#     |v| = v_free * |U_b - U| / U_b
#
# which for U_b = 200 V gives:
#
#     off balance by     5 V    10 V    30 V    70 V   210 V
#     v_free = 0.2    0.005   0.010   0.030   0.070   0.210
#     v_free = 0.5    0.013   0.025   0.075   0.175   0.525
#
# This was 0.20 at first, from taking the most permissive corner of that table -- the
# widest voltage error AND the fastest free fall.  Read the other way, 0.20 admits a
# slow droplet TWO HUNDRED volts off balance, which is 提升 almost exactly: an operator
# watched it ring droplets that were rising under the lift toggle and call them nearly
# balanced.
#
# 0.05 admits 20 V of error on a fast droplet and 50 V on a slow one, which is the
# range the operator actually closes by hand.  Anything further off is a knob turn
# away from being worth looking at, not a candidate.
SLOW_DRIFT_MAX = 0.10              # grid/s -- superseded by slowDriftMax(), kept
                                   # because DESIGN_NOTES cites it

# Fallback free-fall speed: the midpoint of Ⅳ.2's usable 0.2-0.5 grid/s band, used
# only for a droplet that has never been seen with the field off.  The midpoint and not
# the top, because taking the most permissive corner of a range is how the first version
# of this gate was derived and an operator was right to challenge it.
#
# It is a FALLBACK because a fixed value is wrong at both ends.  v_free varies by more
# than a factor of two across usable droplets, and U_b = U / (1 - v/v_free) is very
# sensitive to it: a droplet descending 0.27 grid/s at 150 V implies 656 V if v_free is
# assumed to be 0.35, and 375 V -- comfortably reachable -- if it is really 0.45.  An
# operator reported that nothing was ever ringed while they worked at the 150 V end,
# and that is exactly this: real, reachable droplets computed as out of reach.
SLOW_DRIFT_FREE = 0.35


def freeFallSpeed(columns, x):
    """This droplet's OWN descent rate with the field off, if it has been seen at 0 V.

    With the field off there is no electric force, so the observed speed is v_free by
    definition -- nothing has to be assumed or fitted.  It is read from the column
    history rather than the track, for the usual reason: a track does not survive a
    lift and a column does.

    Returns None when the droplet has not been seen at 0 V, or when what was seen is
    outside the range a usable droplet can have, in which case the caller falls back
    to SLOW_DRIFT_FREE.
    """
    if columns is None:
        return None
    key = int(round(float(x) / CHARGE_COLUMN_PX))
    rates = []
    for k in (key - 1, key, key + 1):
        rates += [r for _ts, r in columns.get(k, {}).get(0, [])]
    if len(rates) < CHARGE_MIN_SAMPLES:
        return None
    v = float(np.median(rates))
    return v if MIN_SPEED * 0.5 <= v <= MAX_SPEED * 2.0 else None


def slowDriftMax(volts, vFree = None):
    """How fast a droplet may drift and still be worth chasing, at this voltage.

    A fixed number was wrong in both directions, because the answer depends on where
    the voltage already sits.  From v = v_free (1 - U/U_b), a droplet drifting at v
    under U balances at U_b, and it is worth ringing only if U_b is a voltage the
    supply can actually reach:

        v_max = v_free (1 - U / U_b_max)

    At 250 V the old fixed 0.10 grid/s reached only U_b = 350 V, so every droplet
    needing 400-500 V was invisible -- and the supply reaches at least 459 V, which is
    the 提升 step measured on this apparatus.  An operator reported exactly that:
    droplets they knew from experience were balanceable were never ringed.

    At a HIGH working voltage the same formula tightens, which is also right: there is
    little headroom left, so only a very slow drift can still be brought to balance.
    """
    if not volts or volts >= SUGGEST_MAX_V:
        return BALANCED_MAX
    free = SLOW_DRIFT_FREE if vFree is None else float(vFree)
    return max(BALANCED_MAX, free * (1.0 - float(volts) / SUGGEST_MAX_V))

# Below this the droplet is BALANCED, not drifting, and telling the operator to change
# the voltage is wrong.  Same relation as above: |v| / v_free = ΔU / U, so 0.02 grid/s
# against a 0.2-0.5 free fall is a balance good to 4-10%, i.e. within about 10-20 V --
# already inside what the operator can read off the panel.  Brownian motion alone
# measures 0.00x-0.01x grid/s on a balanced droplet, so this sits just above the noise.
#
# It also has to bypass the direction filter.  A balanced droplet wobbles across zero,
# so half its frames drift "the wrong way" and a rule that keys on the sign drops it
# every other frame -- which is exactly what an operator saw: a droplet balanced in
# front of them, never ringed.
BALANCED_MAX = 0.02                # grid/s

# The one-division drop test.  Balancing a droplet does not make it measurable: it
# still has to fall at 0.2-0.5 grid/s with the field off, and the operator's quick
# check for that is to drop it wherever it happens to be -- not at the very bottom --
# and time it across a single grid line.  A full 8-division traverse takes 16-40 s; one
# division takes an eighth of that, so
#
#     1 / MAX_SPEED = 2 s   ...  1 / MIN_SPEED = 5 s
#
# which is exactly the 2-5 s the operator uses by eye.  Nothing new is being decided
# here -- Ⅳ.2's band, divided by eight -- it just says it forty seconds sooner.
DIVISION_MIN_S = 1.0 / MAX_SPEED
DIVISION_MAX_S = 1.0 / MIN_SPEED

# How much of the answer the pipeline hands over.
#
#   "range"      "RAISE 30-50 V"     -- a band from the measured scatter
#   "direction"  "RAISE the voltage" -- which way, nothing more
#   "exact"      "set 183 V"         -- the number itself
#   "off"        no suggestion at all
#
# Set to "range" on 2026-09-09.  Rubric 三.1.4 requires the student's analysis ability
# to be exercised rather than replaced, and where a voltage estimate sits against that
# is a judgement for the team, not for this file -- so it is one constant and the
# demotion to "direction" is a one-word edit.  Nothing downstream reads the estimate:
# it is drawn on the overlay and discarded, exactly like the fall clock.
SUGGEST_MODE = "range"

# An estimate is refused outside this band.  Ⅴ.5 puts a usable balance voltage at
# 150-250 V and the supply reaches about 450; anything outside that is arithmetic
# noise from two nearly-equal drift readings, not a voltage worth showing.
SUGGEST_MIN_V, SUGGEST_MAX_V = 40.0, 500.0

# Which way the operator is working, inferred from the voltage they are sitting at.
# Below the low mark they are hunting slow fallers and will raise the voltage; above
# the high mark, slow risers they will bring down.  Between the two, both populations
# are worth showing, and the banner says which way each one needs to go.
# How long a numeric voltage suggestion is held before it is allowed to change.
#
# The operator reads the suggestion, reaches for the knob, and turns it -- and the
# panel, the reader and the fit all lag the knob by a fraction of a second.  A
# suggestion recomputed every frame therefore keeps answering the voltage they have
# just left, and if they overshoot the balance it reverses, which invites them to
# overshoot back.  Holding it still for a few seconds lets the apparatus and the
# reading settle between instructions.  The RING still follows every frame -- only the
# number is held -- and the urgent messages (CATCH IT, BALANCED) are never held.
SUGGEST_HOLD_S = 4.0
_SUGGEST_HELD = {}                 # track id -> (text, when)


def reachableBalance(volts, speed, vFree = None):
    """The balance voltage this drift implies, or None if the supply cannot get there.

    From v = v_free (1 - U/U_b),

        U_b = U / (1 - v / v_free)

    with v_free taken at the midpoint of Ⅳ.2's 0.2-0.5 band.  A slow FALLER implies a
    balance voltage above the present one, a slow RISER one below it, and neither is
    worth an instruction unless the supply can actually reach it.

    This replaces a pair of fixed thresholds (190 V and 210 V) which asked only where
    the operator was working, not whether the droplet was reachable from there.  Under
    those, a droplet drifting slowly down at 459 V was told to RAISE -- there is no
    voltage above 459 that would have balanced it, and an operator reported exactly
    that instruction.  The same test now scales with the working voltage by
    construction, which is the weighting they asked for.
    """
    if not volts:
        return None
    free = SLOW_DRIFT_FREE if vFree is None else float(vFree)
    if free <= 0:
        return None
    denom = 1.0 - float(speed) / free
    if denom <= 0:
        return None                     # would need an unbounded voltage
    ub = float(volts) / denom
    return ub if SUGGEST_MIN_V <= ub <= SUGGEST_MAX_V else None


HUNT_LOW_V = 190.0
HUNT_HIGH_V = 210.0

# 平衡/提升 adds a fixed step to the working voltage -- 210 V on this apparatus,
# give or take the volt the supply wanders by.  Seeing that step is how the pipeline
# knows a balance/rise toggle happened rather than a knob turn.
RISE_STEP_V = 210.0
RISE_STEP_TOL_V = 6.0

# Two voltage readings must differ by at least this much before the difference in how
# a droplet moves between them means anything.  Both toggles the operator uses clear it
# easily: 0V/工作 changes by the whole working voltage, 平衡/提升 by 210 V.
CHARGE_MIN_STEP_V = 50.0

# The charge history is kept per COLUMN of the picture, not per track.
#
# Answering "does this droplet respond to the field" needs it observed at two voltages,
# and the moment the voltage changes is the moment the track is most likely to die: the
# droplet reverses, the tracker loses it, and the replacement starts with no history at
# the old voltage.  So the verdict stayed None, and None is not a rejection -- an
# operator watched it ring dust that plainly ignored the field.
#
# The same lesson as binding: identity does not survive the manoeuvre, position does.
# A column this wide holds one droplet's wander comfortably (measured at up to 48 px
# across a droplet's own five falls) without merging neighbours, which sat hundreds of
# pixels apart.
CHARGE_COLUMN_PX = 30.0

# ...and only what happened in it RECENTLY.  A column is a 30 px strip of the picture,
# not a droplet: over a session everything in the field drifts through it, and without
# a window the medians being compared come from a mixture of droplets rather than the
# one in front of the operator.  Long enough to span a toggle and let the droplet
# settle, short enough that the last droplet through cannot vote on this one.
CHARGE_WINDOW_S = 30.0

# How long a CHARGE VERDICT survives, as opposed to the samples behind it.
#
# CHARGE_WINDOW_S is a window on raw descent rates, and it has to be short: a droplet
# moves between columns and the rates go stale.  But the ANSWER it produces does not go
# stale on the same clock -- a droplet that ignored a 250 V step is uncharged, and it is
# still uncharged a minute later.  Expiring the verdict with the samples meant an
# operator toggled the field, watched the useless droplets lose their rings, and saw
# every one of them come back thirty seconds later.  They reported the hunt aid
# "still latches onto non-charged droplets" three sessions running.
#
# A spray is what really invalidates it: new droplets arrive, and the column no longer
# holds what was tested.  So the verdict is kept until then, or for five minutes,
# whichever comes first.
CHARGE_VERDICT_S = 300.0

# A droplet whose column produced a legal fall this recently is being MEASURED, not
# hunted, however the tracker happens to have re-numbered it since.
MEASURING_MEMORY_S = 240.0

# 平衡/提升 adds RISE_STEP_V to the working voltage, so seeing both V and V+210 within
# a few minutes means the higher one is a LIFT and not a balance.
#
# The tolerance covers the supply wandering a volt and a panel digit being misread; it
# is NOT a free parameter to widen, because everything inside it is a voltage at which
# the aid says nothing.  At 25 V a genuine balance at 485 V against a 250 V working
# point reads as a lift and gets silenced.  15 V leaves the real step (209-210 V as
# measured on 2026-09-10) comfortably inside and hands back that band.
# Telling a LIFT from a hand on the knob.
#
# 平衡/提升 adds a fixed +210 V the instant it is pressed, and while it is held every
# verdict about balance is about a voltage that is not a balance voltage.  The aid has
# to know.  The first version asked only "is this about 210 V above something seen in
# the last five minutes", which cannot tell a press from a slow climb to the same
# place -- so an operator hand-tuning up towards 460 V had the aid go silent on them,
# which is precisely when they wanted it.
#
# The operator's own suggestion was the answer: a hand cannot move the knob that fast.
# Measured on this apparatus, 2026-09-10, reading the panel every 42 ms:
#
#     fastest hand turn      ~80 V/s        =    3 V per reading
#     toggle press          210 V instantly = 210 V per reading   (12 of 12 presses)
#     single-sample OCR glitch                up to 844 V per reading
#
# Every press produced an identical one-reading step, whether pressed slowly or
# quickly: it is a switch, so how it is pressed cannot change it.
#
# The glitch is LARGER than the signal, so a rate test on the raw reading would fire on
# misreads.  A short median over LIFT_SMOOTH samples removes a single-sample spike and
# still passes a real step in about 0.13 s.  On the smoothed reading, in a window of
# LIFT_STEP_S: a hand covers 40 V, a toggle covers 210 V, so 150 V separates them with
# 3.7x margin below and 1.4x above.
LIFT_STEP_V = 150.0        # a jump this big, this fast, is the button and nothing else
LIFT_STEP_S = 0.5          # ...measured across a window this long
LIFT_SMOOTH = 5            # readings in the median that kills single-sample glitches

# How long the lift must be HELD before the aid falls silent.
#
# Not every press is a reposition.  Operators tap it for half a second to see which
# droplets answer the field at all -- a charge test -- and blanking the display for
# that half second hides the one thing they are watching.  A reposition runs 5-10 s.
#
# The threshold is fixed by what the suppression is FOR, not split between those two.
# A false "BALANCED" needs windowFit to report a near-zero speed, and windowFit will
# not answer without CANDIDATE_FIT_S of history, so a lift shorter than that window
# cannot produce the verdict this suppression exists to prevent.
LIFT_DWELL_S = CANDIDATE_FIT_S


class LiftWatch:
    """Follows the plate voltage and says whether 提升 is currently held.

    A latch, not an inference.  It goes up on a fast step up and comes down on the
    step back, so the aid returns the moment the operator releases instead of waiting
    out a timer.  `since` reports how long the current state has lasted, which is what
    LIFT_DWELL_S needs.
    """

    def __init__(self):
        self.recent = collections.deque(maxlen = LIFT_SMOOTH)   # (t, volts)
        self.lifting = False
        self.since = None
        self.base = None            # the working voltage the lift departed from

    def smoothed(self):
        if not self.recent:
            return None
        return float(np.median([v for _t, v in self.recent]))

    def update(self, volts, now):
        """Feed one panel reading.  Returns True while a lift is held."""
        if volts is None:
            return self.lifting
        was = self.smoothed()
        self.recent.append((now, float(volts)))
        level = self.smoothed()
        if was is None or level is None:
            return self.lifting
        # The step is measured against the oldest reading still inside the window, so
        # a dropped frame widens the window rather than hiding the step.
        old = [v for t, v in self.recent if now - t <= LIFT_STEP_S]
        if len(old) < 2:
            return self.lifting
        step = level - float(np.median(old[:max(1, len(old) // 2)]))
        if not self.lifting and step >= LIFT_STEP_V:
            self.lifting, self.since, self.base = True, now, was
        elif self.lifting and step <= -LIFT_STEP_V:
            self.lifting, self.since, self.base = False, now, None
        elif self.lifting and self.base is not None \
                and level <= self.base + LIFT_TOLERANCE_V:
            # Released without a clean step -- readings dropped through the change.
            self.lifting, self.since, self.base = False, now, None
        return self.lifting

    def quiet(self, now):
        """True when the aid should say nothing: a lift held longer than the dwell."""
        return (self.lifting and self.since is not None
                and now - self.since >= LIFT_DWELL_S)


# Kept because the lift latch compares a released voltage against its starting point,
# and because DESIGN_NOTES cites it.
LIFT_TOLERANCE_V = 15.0



# Update the window this many frames apart.  The picture only has to be legible to a
# human watching droplets move at half a grid line a second; what it must not do is
# spend the frame budget.  cv2.waitKey(1) costs 14.1 ms of a 42.4 ms budget on this
# machine -- a third of it, waiting for nothing -- and the live run of 2026-09-08 lost
# frames it did not need to.
# Set to 3 on 2026-09-09 to claw back the 14.1 ms cv2.waitKey(1) costs of a 42.4 ms
# frame budget, and put straight back to 1 the moment an operator saw it: on Windows
# it is waitKey that pumps the repaint, so a third of the frames repainted means an
# 8 fps picture however fast everything else runs. It looked broken with an EMPTY
# chamber, before anything was even sprayed.
#
# The budget was the wrong thing to optimise. The window is what the operator hunts
# with -- there is one BNC out and it feeds this laptop, so this picture is the only
# view of the droplets that exists. A saving that makes it unusable buys nothing.
# The frame drops it was meant to cure cost track continuity; a choppy display costs
# the operator the experiment.
SHOW_EVERY = 1

# How long the banner sticks to the droplet it is already naming.  Without this it
# re-ranks every frame and the instruction flickers: measured over a ten-minute window
# it changed 1152 times, about twice a second, alternating between two candidates and
# blinking out between them.  Unreadable, and an unreadable alert is worse than none.
#
# So the named candidate keeps its place until it stops qualifying, and the numbers
# beside it refresh at a rate a person can actually read.  The one thing allowed to
# interrupt is a candidate whose charge has been CONFIRMED while the current one is
# still unknown, because that is a strictly better thing for the operator to look at.
CANDIDATE_STICKY_S = 2.0        # grace before a vanished candidate is given up on
CANDIDATE_REFRESH_S = 0.5       # how often the numbers in the banner are redrawn


class Fall:
    """One completed `0` -> `16` traverse, frozen at the moment it completed.

    `falls` used to hold the Track itself, which was sound only while a track could
    complete at most once.  Now that a lifted droplet is measured again on the same
    track, appending the track would leave every row of that track pointing at one
    mutable object -- five rows in falls.csv, all carrying the fifth fall's numbers.
    So copy, and take the drift here too, while `points` still ends at tBottom.

    `id` is the TRACK id and repeats across the falls of one droplet.  That is
    useful rather than a defect: rows sharing an id were the same droplet, which is
    exactly what `binding` is otherwise left to infer from timings alone.
    """

    FIELDS = ("id", "fTop", "fBottom", "tTop", "tBottom", "seconds", "legal",
              "kinematicLegal", "fieldOff", "balanceVoltage", "reacquired", "adopted")

    def __init__(self, t, spacing):
        for name in self.FIELDS:
            setattr(self, name, getattr(t, name, None))
        self.fallDrift, self.fallDirection = fallDrift(t, spacing)
        # Where the droplet was, across the measured traverse.  Track identity does
        # not survive the lift -- two live sessions produced 7 and 9 ids for 9 and 10
        # falls -- but POSITION does: a droplet drifts 5-6 px sideways across a whole
        # traverse and the operator re-drops the same one down the same column, while
        # two different droplets sit hundreds of pixels apart.  This is what binding
        # is grouped on.
        xs = [x for (ts, _y), x in zip(t.points, t.xs)
              if t.tTop is not None and t.tBottom is not None
              and t.tTop <= ts <= t.tBottom]
        self.xPx = float(np.median(xs)) if xs else None


def noteColumnVoltage(columns, t, volts, spacing):
    """Record how fast a droplet in this column descends, at this plate voltage."""
    if volts is None or len(t.points) < CONFIRM_POINTS or spacing <= 0:
        return
    key = int(round(float(t.pos[0]) / CHARGE_COLUMN_PX))
    hist = columns.setdefault(key, {})
    bucket = hist.setdefault(int(round(volts / 10.0)) * 10, [])
    bucket.append((float(t.lastT), float(t.vel[1]) / spacing))
    if len(bucket) > 600:
        del bucket[0]


def columnChargeState(columns, x, now, verdicts = None):
    """True / False / None for the column a droplet is in.  See CHARGE_COLUMN_PX.

    `verdicts` remembers the ANSWER once the samples have produced one, because the
    samples expire on CHARGE_WINDOW_S and the physics does not.  See CHARGE_VERDICT_S.
    """
    key = int(round(float(x) / CHARGE_COLUMN_PX))
    hist = {}
    for k in (key - 1, key, key + 1):          # a droplet may straddle a boundary
        for v, rates in columns.get(k, {}).items():
            hist.setdefault(v, []).extend(rates)
    fresh = {v: [rate for ts, rate in r if now - ts <= CHARGE_WINDOW_S]
             for v, r in hist.items()}
    usable = [(v, r) for v, r in fresh.items() if len(r) >= CHARGE_MIN_SAMPLES]
    if len(usable) < 2:
        return _rememberedCharge(verdicts, key, now)
    usable.sort()
    (loV, loR), (hiV, hiR) = usable[0], usable[-1]
    if hiV - loV < CHARGE_MIN_STEP_V:
        return _rememberedCharge(verdicts, key, now)
    got = (float(np.median(loR)) - float(np.median(hiR))) >= CHARGE_MIN_SEPARATION
    if verdicts is not None:
        for k in (key - 1, key, key + 1):
            verdicts[k] = (got, now)
    return got


def _rememberedCharge(verdicts, key, now):
    """The last verdict for this column, while it is still worth believing."""
    if not verdicts:
        return None
    for k in (key, key - 1, key + 1):
        got = verdicts.get(k)
        if got is not None and now - got[1] <= CHARGE_VERDICT_S:
            return got[0]
    return None


def candidates(live, spacing, now, roiH, volts = None, quiet = False,
               columns = None, verdicts = None, lifting = False):
    """The tracks worth the operator's attention, best first.

    Ⅳ.2 SELECTS IN TWO STAGES AND SO DOES THIS.  Getting that wrong is not a tuning
    error, it inverts the answer:

      field ON (150-250 V)   the target is a droplet the field is HOLDING or lifting
                             gently -- the manual's "temporary target oil droplet".
      field OFF              among those, the target is the one falling at
                             0.2-0.5 grid/s -- the manual's "final target".

    The first version tested the falling band in both states, and an operator watching
    it said every circled droplet was one that ignored the field.  Of course it was:
    with the field on, a droplet descending steadily at 0.2-0.5 grid/s is BY
    DEFINITION one the field is not holding.  The rule was guaranteed to pick exactly
    the droplets that cannot be balanced.

    The remaining filters exist because something without each of them fires on
    nothing: charge (an uncharged droplet passes every kinematic test and can never be
    balanced), straightness (junk that averages into the band scatters; a fall does
    not), displacement (a wobble in place can fit a legal slope), and quiet (nothing is
    worth saying mid-spray, and the operator is not hunting yet).

    Recall matters less than precision here, which is the opposite of the measurement
    path: a missed fall is lost data, but an alert that cries wolf gets ignored and is
    then worse than no alert at all.
    """
    # `quiet` is kept as a parameter and no longer used to silence anything.  It
    # suppressed candidates while detections ran above four times the session's quiet
    # rate, which sounded reasonable and was never measured: the quiet rate is often 0
    # or 1 a frame, so the threshold lands at three detections, and ANY spray clears
    # that for minutes.  The operator hunts in the minute after a spray, so the gate
    # muted the feature precisely when it was wanted.  The shape, straightness,
    # movement-history and charge tests are what reject junk; this only rejected the
    # job.
    if volts is None:
        # Which of Ⅳ.2's two stages we are in is decided by the field, so with the
        # panel unreadable there is no way to know which test to apply.  `if fieldOn:`
        # treated that as "off" and told an operator to balance a droplet that was
        # falling through a live 150 V field.  Say nothing instead.
        return []
    if lifting:
        # 提升 is pressed: the operator is putting a droplet they have ALREADY chosen
        # back at the top, and every verdict here would be about a voltage that is not
        # a balance voltage.  There is nothing to hunt during a lift.
        return []
    fieldOn = volts > 0
    out = []
    for t in live:
        if t.miss or len(t.points) < CONFIRM_POINTS:
            continue
        if now - t.points[0][0] < CANDIDATE_MIN_AGE_S:
            continue
        # A free rejection before an expensive one.  windowFit is a least-squares fit
        # over ~70 points and this runs over every live track; after a spray that is
        # hundreds of them, and doing it every frame took a live session from 23 fps to
        # about 5.  t.vel is already maintained by update(), so this costs nothing and
        # throws out almost everything.  The band is deliberately loose -- half the
        # lower limit to twice the upper -- because the fit, not this, is the test.
        rough = float(t.vel[1]) / spacing
        vFree = freeFallSpeed(columns, t.pos[0])
        if fieldOn:
            if abs(rough) > slowDriftMax(volts, vFree) * 2.0:
                continue
        elif not (MIN_SPEED * 0.5 <= rough <= MAX_SPEED * 2.0):
            continue
        got = t.windowFit(spacing, now)
        if got is None:
            continue
        speed, resid, fell = got
        if resid > CANDIDATE_MAX_RESIDUAL_PX:
            continue
        action = None
        if fieldOn:
            # Nearly balanced, whichever way it happens to be drifting.  Direction is
            # not a filter, it is the instruction: down means raise the voltage, up
            # means lower it.
            if abs(speed) > slowDriftMax(volts, vFree):
                continue
            if abs(speed) <= BALANCED_MAX:
                action = "BALANCED"        # no direction, and no filtering by one
            else:
                # Only a droplet the supply can actually bring to balance FROM
                # HERE.  A slow faller needs more voltage, a slow riser needs less, and
                # at 459 V there is no "more" -- reachableBalance() answers that in one
                # test that scales with the working voltage, where the old pair of
                # fixed thresholds could not.
                if reachableBalance(volts, speed, vFree) is None:
                    continue
                action = "RAISE" if speed > 0 else "LOWER"
            # ...but it must have BEEN somewhere.  Dropping the displacement test here
            # was wrong: "not moving" describes a droplet the field is holding and it
            # equally describes a speckle that never moved in its life, and within a
            # minute of going live the banner was pointing at one of those, ringed
            # green, calling it HELD.
            #
            # A droplet the field holds was falling before the field was applied, so
            # its track carries that history.  An artefact carries none.  The range is
            # taken over the whole life rather than the fit window, because by the time
            # it is being held the recent window is flat by definition.
            ys = [p[1] for p in t.points[::5]]
            if max(ys) - min(ys) < CANDIDATE_MIN_DIVISIONS * spacing:
                continue
        else:
            # Stage two: the measurable band, and it must have actually travelled.
            if not (MIN_SPEED <= speed <= MAX_SPEED):
                continue
            if fell < CANDIDATE_MIN_DIVISIONS:
                continue
        # The column's history first -- it survives the track dying at the toggle,
        # which the track's own history does not.  Fall back to the track for a
        # droplet in a column nothing else has been seen in.
        charged = (columnChargeState(columns, t.pos[0], now, verdicts)
                   if columns is not None else None)
        if charged is None:
            charged = t.chargeState()
        if charged is False:
            continue                        # cannot be balanced; not a target
        vy = float(t.vel[1])
        toEdge = (roiH - t.pos[1]) / vy if vy > 1e-6 else None
        out.append({"track": t, "id": t.id, "speed": speed, "residual": resid,
                    "divisions": fell, "charged": charged, "secondsToEdge": toEdge,
                    "fieldOn": fieldOn, "action": action, "volts": volts,
                    "divisionSeconds": t.divisionSeconds,
                    "balanceEstimate": t.balanceVoltageEstimate(),
                    "pos": (float(t.pos[0]), float(t.pos[1]))})
    # Confirmed-charged first.  Then, with the field on, the slowest drift -- that is
    # the droplet nearest to balancing and so the cheapest to finish.  With the field
    # off the speed is the measurement, so the straightest fit wins instead.
    out.sort(key = lambda c: (c["charged"] is not True,
                              abs(c["speed"]) if c["fieldOn"] else c["residual"]))
    # Once ANY candidate is confirmed charged, the unknowns stop being shown.  An
    # uncharged droplet jiggles, and jiggling is what the drift test rewards -- so with
    # everything unknown the ranking put specks first.  Evidence beats the absence of
    # it: if the operator has toggled and something answered, only that is worth a ring.
    if any(c["charged"] is True for c in out):
        out = [c for c in out if c["charged"] is True]
    return out


def divisionVerdict(seconds):
    """How a one-division drop reads: (text, colour), or None if it has not happened.

    The operator's own shortcut, and the fastest question the pipeline can answer.
    Balancing a droplet does not make it measurable -- it still has to fall at
    0.2-0.5 grid/s with the field off -- so they drop it wherever it sits and watch it
    cross one line.  2-5 s is Ⅳ.2's band divided by eight.
    """
    if not seconds:
        return None
    if seconds < DIVISION_MIN_S:
        return "1 div %.1fs TOO FAST" % seconds, (0, 140, 255)
    if seconds > DIVISION_MAX_S:
        return "1 div %.1fs TOO SLOW" % seconds, (0, 140, 255)
    return "1 div %.1fs USABLE" % seconds, (0, 255, 0)


def candidateBanner(best, volts, untested = 0, now = None):
    """One line of instruction, or None.  What to DO, not what was measured.

    Three regimes, because the operator hunts in three:

      field on, low end    a slow FALLER is nearly balanced from below -> raise
      field on, high end   a slow RISER is nearly balanced from above  -> lower
      field off            the drop test, then the 0.2-0.5 band

    SUGGEST_MODE picks how much of the answer the voltage estimate hands over --
    naming the volts, naming a range, or naming only the direction.  Where that sits
    against rubric 三.1.4 is a judgement for the team, so it is one constant.
    """
    if best is None:
        if untested:
            # Something is drifting slowly, but with the voltage sitting still nobody
            # has tested whether it answers the field at all, and a slow drift on its
            # own is equally a description of dust.  Ask for the test rather than
            # naming a droplet: an instruction the operator can act on, and no claim
            # that has not been earned.
            return ("%d slow droplet%s -- toggle 0V/工作 or 平衡/提升 to see which answer"
                    % (untested, "" if untested == 1 else "s"), (0, 220, 255))
        return None, (0, 220, 255)
    t, sp = best["track"], best["speed"]
    edge = best["secondsToEdge"]
    if edge is not None and edge <= EDGE_WARN_S and t.tTop is None:
        act = "press 提升 (LIFT)" if sp > 0 else "cut to 0V"
        return ("CATCH IT -- id %d leaving in %.0f s, %s" % (best["id"], edge, act),
                (0, 140, 255))
    charged = best["charged"]
    if best["fieldOn"]:
        way = "down" if sp > 0 else "up"
        if best["action"] == "BALANCED":
            return ("BALANCED -- id %d holding at %d V. Cut to 0V and time it"
                    % (best["id"], volts or 0), (0, 255, 0))
        if charged is None:
            return ("id %d drifting %s %.3f gr/s -- TOGGLE to check it is charged"
                    % (best["id"], way, abs(sp)), (0, 220, 255))
        est = best["balanceEstimate"]
        if SUGGEST_MODE != "direction" and est is not None and volts:
            lo, hi = sorted((est[0] - volts, est[1] - volts), key=abs)
            if SUGGEST_MODE == "exact":
                tail = "set about %d V" % round((est[0] + est[1]) / 2.0)
            else:
                tail = "%s %d-%d V" % (best["action"], abs(round(lo)), abs(round(hi)))
            # Held still for SUGGEST_HOLD_S.  The knob, the panel and the fit all lag
            # each other, so a number recomputed every frame answers the voltage the
            # operator has just left and reverses if they overshoot.
            if now is not None:
                got = _SUGGEST_HELD.get(best["id"])
                if got is None or now - got[1] >= SUGGEST_HOLD_S:
                    _SUGGEST_HELD[best["id"]] = (tail, now)
                    if len(_SUGGEST_HELD) > 64:
                        for k in [k for k, v in _SUGGEST_HELD.items()
                                  if now - v[1] > 10 * SUGGEST_HOLD_S]:
                            del _SUGGEST_HELD[k]
                else:
                    tail = got[0]
            return ("id %d charged, drifting %s %.3f gr/s -- %s"
                    % (best["id"], way, abs(sp), tail), (0, 255, 0))
        return ("%s the voltage -- id %d charged, drifting %s %.3f gr/s"
                % (best["action"], best["id"], way, abs(sp)), (0, 255, 0))
    # Field off.  The drop test first: it answers the question the operator actually
    # has, and forty seconds sooner than a full traverse.
    div = divisionVerdict(best["divisionSeconds"])
    if div is not None:
        return ("id %d falling %.2f gr/s -- %s" % (best["id"], sp, div[0]), div[1])
    if charged is None:
        return ("id %d falling %.2f gr/s -- switch to 工作 to see if it holds"
                % (best["id"], sp), (0, 220, 255))
    return ("TARGET id %d -- %.2f gr/s, charged, drop it to time"
            % (best["id"], sp), (0, 255, 0))



def updateHold(t, yTop, spacing, now):
    """Track how long this droplet has sat balanced on the top line.

    Ⅳ.4's own first step, and the one thing on the screen that cannot happen by
    accident: a droplet held still at `0` is one somebody balanced deliberately.
    """
    v = t.recentSpeed(spacing)
    onLine = float(t.pos[1]) <= yTop + MEASURE_HOLD_DIV * spacing
    if v is None or abs(v) > BALANCED_MAX or not onLine:
        t.heldSince = None
    elif t.heldSince is None:
        t.heldSince = float(now)


def heldLongEnough(t, now):
    since = getattr(t, "heldSince", None)
    return since is not None and float(now) - since >= MEASURE_HOLD_S


def pickedVerdict(t, spacing, now, volts, charged, vFree):
    """What to tell the operator about the droplet THEY chose.  (text, colour, ok)

    Reports on it whether or not it passes the candidate gates, because the operator
    asked about this one -- and "this will not work, discard it" is the most useful
    thing the aid can say once they have committed time to a droplet.
    """
    speed = t.recentSpeed(spacing)
    if speed is None:
        return "picked id %d -- watching it" % t.id, (0, 220, 255), None
    if charged is False:
        return ("picked id %d does NOT answer the field -- DISCARD, spray again"
                % t.id, (0, 0, 255), False)
    if not volts:                                   # field off: the timing test
        if MIN_SPEED <= speed <= MAX_SPEED:
            return ("picked id %d falls %.2f gr/s -- USABLE, balance it on `0`"
                    % (t.id, speed), (0, 255, 0), True)
        why = "too fast" if speed > MAX_SPEED else "too slow"
        return ("picked id %d falls %.2f gr/s -- %s for %g-%g, DISCARD"
                % (t.id, speed, why, MIN_SPEED, MAX_SPEED), (0, 0, 255), False)
    if charged is None:
        return ("picked id %d -- toggle the field to see if it answers"
                % t.id, (0, 220, 255), None)
    if abs(speed) <= BALANCED_MAX:
        if ADVISORY_MIN_V <= volts <= ADVISORY_MAX_V:
            return ("picked id %d BALANCED at %d V -- cut to 0V and time it"
                    % (t.id, volts), (0, 255, 0), True)
        return ("picked id %d balances at %d V, outside the manual's %g-%g V"
                % (t.id, volts, ADVISORY_MIN_V, ADVISORY_MAX_V), (0, 140, 255), False)
    ub = reachableBalance(volts, speed, vFree)
    if ub is None:
        return ("picked id %d drifts %.3f gr/s -- no reachable balance, DISCARD"
                % (t.id, speed), (0, 0, 255), False)
    way = "RAISE" if speed > 0 else "LOWER"
    warn = "" if ADVISORY_MIN_V <= ub <= ADVISORY_MAX_V else "  (outside 150-250 V)"
    return ("picked id %d -- %s to about %d V%s" % (t.id, way, round(ub), warn),
            (0, 255, 0) if not warn else (0, 140, 255), None)


def updateDivision(t, gridRows):
    """Time the droplet across ONE grid line, the moment it crosses the next.

    The operator's own shortcut: drop a balanced droplet wherever it sits and watch it
    cross a single line.  2-5 s is Ⅳ.2's 0.2-0.5 grid/s band divided by eight, so this
    answers "is it measurable" in a few seconds instead of a full 16-40 s traverse.

    Only ADJACENT lines count.  A droplet that goes dark and reappears two divisions
    down would otherwise report a legal-looking time for twice the distance.
    """
    if len(t.points) < 2 or not gridRows:
        return
    (t0, y0), (t1, y1) = t.points[-2], t.points[-1]
    if y1 == y0:
        return
    for k, row in enumerate(gridRows):
        if (y0 < row <= y1) or (y1 < row <= y0):
            crossed = t0 + (row - y0) / (y1 - y0) * (t1 - t0)
            was = t.lastRow
            t.lastRow = (crossed, k)
            if was is not None and abs(was[1] - k) == 1:
                gap = abs(crossed - was[0])
                if gap > 0:
                    t.divisionSeconds = gap
            return


# A `0` crossing older than this can no longer belong to a legal fall in progress,
# so it is forgotten.  Half again the longest legal fall, which leaves room for the
# interpolation and for a droplet that is still slowing.
ZERO_KEEP_S = MAX_FALL_S * 1.5

# What an adopting track must prove before it may claim someone else's `0`.
#
# Column, timing and an unclaimed crossing are not enough on their own.  Measured on
# one_droplet_slow, those three alone admitted 31 extra traverses, every one of them
# kinematically legal, on a fixture holding a single droplet -- the chamber is never so
# empty that nothing else crosses both lines in the same column inside 16-40 s.
#
# What separates the real continuation is that it is the SAME FALL: a droplet at
# terminal velocity descends in a straight line, so the adopting track's own points
# must sit on the line joining the `0` crossing to the `1.6` crossing.  A stray that
# merely ends up in the right place at the right time does not.
#
# The span requirement is the other half: a track that appears a division above `1.6`
# has not seen enough of the fall to claim it, however well those few points fit.
# Ⅳ.4 balances the droplet on the top grid line before dropping it, and Ⅴ.5 advises a
# balance voltage of 150-250 V.  Both are the manual's numbers, not ours.
ADVISORY_MIN_V, ADVISORY_MAX_V = 150.0, 250.0

# What it takes to believe the operator has CHOSEN a droplet, rather than one merely
# happening to satisfy a gate.
#
# Earlier versions asked whether a fall clock was running, then whether the droplet had
# seen two voltages.  Both were guesses at intent and both fired on droplets the
# operator had never looked at, which silenced the hunt aid mid-spray.  The operator
# supplied the real test: they balance the droplet on the top line and leave it there
# while they check it.  Nothing accidental sits still at the line for fifteen seconds.
MEASURE_HOLD_S = 15.0
MEASURE_HOLD_DIV = 1.0        # how far below `0` still counts as "on the line"

# Clicking a droplet.  With several plausible droplets on screen the ring moves between
# them, and no automatic rule can know which one the operator has decided to spend the
# next ten minutes on -- only they know that.  So let them say.
PICK_RADIUS_PX = 40.0         # how near a click has to land to select a droplet
PICK_FOLLOW_PX = 45.0         # how far it may move between frames and stay selected
PICK_LOST_S = 20.0            # dropped after this long with nothing where it was

ADOPT_MIN_SPAN = 0.5          # fraction of the fall the adopting track must cover
ADOPT_MAX_RESIDUAL_PX = 6.0   # furthest any of its points may sit off the line


def _fellAlong(t, tTop, yTop, tBottom, yBottom):
    """Do this track's own points lie on the fall the pairing implies?"""
    if tBottom <= tTop:
        return False
    v = (yBottom - yTop) / (tBottom - tTop)
    pts = [(ts, y) for ts, y in t.points if tTop <= ts <= tBottom]
    if len(pts) < CONFIRM_POINTS:
        return False
    if (pts[-1][0] - pts[0][0]) < ADOPT_MIN_SPAN * (tBottom - tTop):
        return False
    return max(abs(y - (yTop + v * (ts - tTop))) for ts, y in pts) \
        <= ADOPT_MAX_RESIDUAL_PX


def noteZeroCrossing(crossings, when, x):
    """Record that SOMETHING crossed `0` here, whoever was tracking it."""
    if crossings is None:
        return
    crossings.append({"t": float(when), "x": float(x), "claimed": False})
    cut = float(when) - ZERO_KEEP_S
    crossings[:] = [c for c in crossings if c["t"] >= cut]


def adoptZeroCrossing(crossings, when, x):
    """An unclaimed `0` crossing in this column that would make this fall legal.

    Track identity does not survive a whole traverse and never reliably has: four
    legal falls were lost across four sessions because a track broke part-way down,
    and the track that reached `1.6` had no `0` of its own to pair with.  Position
    does survive -- it is what binding, the charge verdict and the measuring pin all
    key on -- so pair the two crossings by column instead of by track id.

    Three things keep this from inventing falls.  The crossing must be unclaimed, so
    no `0` can serve two falls.  It must be in the same column, within the tolerance
    binding already uses.  And the implied duration must be inside the kinematic band
    -- outside it the fall would be rejected anyway, so adopting it gains nothing and
    only risks pairing two unrelated droplets.
    """
    if crossings is None:
        return None
    best = None
    for c in crossings:
        if c["claimed"] or abs(c["x"] - float(x)) > SAME_DROPLET_X_PX:
            continue
        secs = float(when) - c["t"]
        if not (MIN_FALL_S <= secs <= MAX_FALL_S):
            continue
        if best is None or c["t"] > best["t"]:
            best = c              # the most recent match: the likeliest same droplet
    return best


def _claim(crossings, tTop):
    """Mark the crossing a completed fall used, so nothing else can use it."""
    if crossings is None or tTop is None:
        return
    for c in crossings:
        if not c["claimed"] and abs(c["t"] - float(tTop)) < 0.05:
            c["claimed"] = True
            return


def updateCrossings(t, yTop, yBottom, spacing, crossings = None):
    """Returns the track when it has just completed a full `0` -> `16` traverse.

    Both crossings are interpolated against capture TIME, so the measured duration
    never passes through a frame rate.  On a file this is arithmetically identical to
    interpolating frame indices and dividing by fps, because t is a linear function of
    the index.  On a camera it is the whole point: a device advertising 30 fps and
    delivering 27 would otherwise inflate every fall by 11%, plausibly and silently.
    """
    if len(t.points) < 2:
        return None
    (t0, y0), (t1, y1) = t.points[-2], t.points[-1]
    here = float(t.xs[-1]) if getattr(t, "xs", None) else float(t.pos[0])
    if t.tTop is None:
        if y0 < yTop <= y1:
            t.tTop = t0 + (yTop - y0) / (y1 - y0) * (t1 - t0)
            t.yDeepest = y1
            noteZeroCrossing(crossings, t.tTop, here)
            return None
        # No `0` of its own.  It may still be the SAME DROPLET whose crossing was
        # recorded under a track id that has since died -- see adoptZeroCrossing.
        if y0 < yBottom <= y1:
            when = t0 + (yBottom - y0) / (y1 - y0) * (t1 - t0)
            got = adoptZeroCrossing(crossings, when, here)
            if got is not None and not _fellAlong(t, got["t"], yTop, when, yBottom):
                got = None
            if got is not None:
                got["claimed"] = True
                t.tTop, t.tBottom = got["t"], when
                t.seconds = t.tBottom - t.tTop
                t.legal = isLegalFall(FALL_DIVISIONS, t.seconds)
                t.adopted = True
                return t
        return None
    t.yDeepest = y1 if t.yDeepest is None else max(t.yDeepest, y1)
    if y1 < t.yDeepest - RISE_VOID_DIVISIONS * spacing:
        # Going back UP, judged against the deepest point this run reached rather
        # than against an absolute height.
        #
        # It used to read `y1 < yTop - 0.5 * spacing`: the droplet had to be SEEN
        # above y = yTop - 30 px.  edgeClear suppresses every detection within
        # 20 * mag px of the ROI edge, which leaves about 30 px of the picture in
        # which that test can fire, and a droplet being lifted crosses it in well
        # under a second.  Miss it and tTop survives the lift.  That is what
        # happened to droplet 3's first fall on 2026-09-08: the clock started at
        # 2093.6 s while the operator was still walking the droplet onto the line,
        # never voided, and the fall came out as a 75.78 s traverse -- whose bottom
        # crossing was within 0.7 s of the operator's, so the measurement was right
        # and only its start was stale.
        #
        # Rising is not a height, it is a direction, so test the direction.  Half a
        # division of upward travel cannot be noise: the centroid of one of these
        # blobs is steady to about a pixel, and assign() will not hand a track a
        # detection more than 4 px above where it already is, even on the widened
        # coasting gate.  A real fall never does it; a lift does it immediately.
        #
        # Back above `0`.  Either the run was void, or -- far more often -- the
        # operator has just done what IV.4 says to: switch to 提升, lift the droplet
        # to the top of the screen, switch back to 平衡, and drop it again.  Five
        # falls, one droplet, and if the track survives the lift, ONE TRACK.
        #
        # Clearing only tTop left tBottom set from the previous fall, so the
        # completion test below could never fire again for the life of that track:
        # it measured the first fall and then silently measured nothing, while the
        # overlay held that first duration frozen through all five.  The operator
        # saw the frozen label on 2026-09-08.  It went unnoticed until then because
        # the track almost never survived the lift -- with the old areaMax the
        # droplet was barely detected at all, so a new track, with clean state,
        # picked up each fall.  Fixing detection is what exposed this.
        #
        # Everything about the finished run is already snapshotted into `falls`, so
        # clear the lot, reacquired included: its docstring says "times THIS FALL
        # was stitched", and gaps belonging to the previous fall or to the lift are
        # not this one's.
        t.tTop = t.tBottom = t.seconds = t.legal = None
        t.fTop = t.fBottom = t.yDeepest = None
        t.reacquired = 0
        t.adopted = False
        return None
    if t.tBottom is None and y0 < yBottom <= y1:
        t.tBottom = t0 + (yBottom - y0) / (y1 - y0) * (t1 - t0)
        t.seconds = t.tBottom - t.tTop
        t.legal = isLegalFall(FALL_DIVISIONS, t.seconds)
        _claim(crossings, t.tTop)
        return t
    return None


# A drift is called directional when the net displacement exceeds this fraction of
# the total path walked. Geometric, not physical -- no document supplies a number, so
# treat it as provisional until there is footage to check it against.
DRIFT_DIRECTIONAL = 0.5


def fallDrift(t, spacing):
    """Sideways travel during the measured fall: (divisions/s, direction).

    Magnitude is unsigned; direction is "left" / "right" / "wander", mirroring the
    hold pair. IV.3 reads a consistent one-way drift as unlevel plates or a draught.
    """
    if t.tTop is None or t.tBottom is None or not t.seconds or spacing <= 0:
        return None, None
    xs = [x for (ts, _y), x in zip(t.points, t.xs) if t.tTop <= ts <= t.tBottom]
    if len(xs) < 3:
        return None, None
    net = xs[-1] - xs[0]
    path = sum(abs(b - a) for a, b in zip(xs, xs[1:]))
    if path <= 0:
        return 0.0, "wander"
    ratio = abs(net) / path
    if ratio < DRIFT_DIRECTIONAL:
        direction = "wander"
    else:
        direction = "right" if net > 0 else "left"
    return abs(net) / spacing / t.seconds, direction


def trackAll(path = VIDEO, show = True, saveVideo = True, outDir = None, health = None,
             fromFrame = 0, toFrame = None, bgWindowS = BG_WINDOW_S,
             bgFromRange = False, videoFrom = None, videoTo = None, blind = False):
    """bgWindowS = None keeps the whole-clip background, i.e. every result produced
    before 2026-08-28.  A number switches to a rolling window of that many seconds."""
    path = resolveClip(path)
    outDir = outDirFor(path) if outDir is None else outDir
    # Before anything is measured, not after.  A missing folder used to surface as
    # an OSError inside report()'s to_csv, i.e. after the entire clip had been
    # tracked, and it took the annotated video with it -- cv2.VideoWriter cannot
    # open a file in a folder that is not there and says nothing about it.
    os.makedirs(outDir, exist_ok = True)
    fallLog = FallLog(outDir, path)
    src = openSource(path)
    fps = src.nominalFps            # advisory: naming, pacing and progress only
    total = src.frameCount
    if toFrame is None:
        toFrame = total or None
    # autoROI keeps the WHOLE-clip median on purpose: the grid is static for the
    # entire recording, so a global median finds it better than any window would.
    if src.isLive:
        # There is no "whole clip" to take a median of, so learn one up front.  This
        # is a blind wait at the start of every session and the operator has to be
        # told, or they will think it has hung.  It also has to happen BEFORE the
        # droplets are introduced: a hovering droplet burns into the median and is
        # then subtracted away for the rest of the session.
        print("  %s\n  learning the background for %.0f s -- no droplets yet, please"
              % (src.describe(), LIVE_LEARN_S), flush = True)
        fullBackground = src.learnBackground(seconds = LIVE_LEARN_S)
    else:
        fullBackground = buildBackground(path)
    roi = autoROI(fullBackground)
    if bgFromRange and not src.isLive:   # what analysing a trimmed file does
        fullBackground = buildBackground(path, start = fromFrame, end = toFrame)
    if roi is None:
        print("Auto-ROI failed: no grid in frame.")
        if health is not None:
            health["roiOk"] = False
        src.close()
        return []
    X, Y, W, H = roi["x"], roi["y"], roi["w"], roi["h"]
    spacing = roi["spacing"]
    s = buildSettings(roi, spacing)
    background = fullBackground[Y:Y+H, X:X+W]
    # Built from the LEARNED background, before any droplet has been sprayed, so it
    # holds only what the apparatus draws.  Rebuilding it from the rolling background
    # later would risk baking in a droplet that sat still.
    s["staticMask"] = staticMask(background, spacing)
    yTop, yBottom = s["gridRows"][LINE_TOP], s["gridRows"][LINE_BOTTOM]
    box = readoutBox(roi)
    maxSpeed = s["maxSpeed"]            # px per second; the per-frame gate is this x dt
    # ~10 panel reads a second, not 3.  The charge test compares how a droplet moves
    # either side of a toggle the operator holds for 0.5-1 s, and at 3 Hz the field
    # state could be stale by 330 ms -- a third of the toggle, smearing the two states
    # into each other.  The read is a threshold over a small crop and profiled at
    # under a millisecond, so the extra rate is nearly free.
    panelEvery = max(1, int(round(fps / 10.0)))
    print("%s\n  ROI %d,%d,%d,%d  spacing %.1f  `0` y=%d  `16` y=%d  %d divisions"
          % (os.path.basename(path) if isinstance(path, str) else src.describe(),
             X, Y, W, H, spacing, yTop, yBottom,
             LINE_BOTTOM - LINE_TOP))

    writer = None
    if saveVideo:
        writer = cv2.VideoWriter(artefact(outDir, path, "annotated_multi.mp4"),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    # The apparatus has ONE video out and it now feeds the dongle, so the lab monitor
    # is dark and this window is the only picture the operator has.  Make it resizable
    # for a blind run: they are watching a droplet cross a line on it.
    picked = None                   # the droplet the operator clicked, if any
    clicks = []                     # filled by the mouse callback, drained in the loop

    def onMouse(event, mx, my, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((mx, my))

    if show:
        cv2.namedWindow("Droplets",
                        cv2.WINDOW_NORMAL if blind else cv2.WINDOW_AUTOSIZE)
        # The operator picks their droplet by clicking it.  No automatic rule can know
        # which of several plausible droplets they have decided to spend ten minutes
        # on; they can say in one click, and the ring stops wandering.
        cv2.setMouseCallback("Droplets", onMouse)
    # In a blind session this line IS the answer -- elapsed seconds and LEGAL/rejected,
    # printed the instant the fall completes.  A terminal in the corner of the operator's
    # eye is not a blind benchmark.  Swallow it; falls.csv still records everything.
    sayFall = (lambda *a, **k: None) if blind else print

    roller = None
    if bgWindowS:
        roller = RollingBackground(bgWindowS, fps)
        if not src.isLive:
            roller.prime(path, (Y, H, X, W), fromFrame, toFrame)
        else:
            # Seed the window with the very frames the background was learned from.
            # They span 30 s rather than 300, so the window starts narrow and widens
            # as the session runs -- but a narrow window of droplet-free frames beats
            # no window at all, which is what a live run had before: for its first
            # five minutes it either ran on a stale background or on a median of
            # three frames that still contained the droplets.
            for g in getattr(src, "learnFrames", [])[-BG_SAMPLES:]:
                roller.buf.append(g[Y:Y+H, X:X+W].copy())
        print("  rolling background: %.0f s window, %d samples, every %d frames%s"
              % (bgWindowS, len(roller.buf), roller.every,
                 "" if not src.isLive else
                 " (filling as the session runs; the learned background stands in)"),
              flush = True)

    live, morgue, falls, n = [], [], [], fromFrame
    volts = None                    # last plate voltage read off the `V:` line
    steadyVolts = None              # ...voted over ~1 s, for deciding the hunt mode
    panelAlign = None               # digit positions, found once and reused
    alignCells = collections.deque(maxlen = 8)      # median to align and read from
    # Short windows on purpose.  These lag the knob: the median is taken over the
    # last few samples and the mode over the last second, so a long window shows a
    # voltage the operator has already turned away from.  At 10 panel reads a second
    # this settles in well under a second while still outvoting a stray misread.
    voltHistory = collections.deque(maxlen = 5)
    bestNow = None                  # the candidate the operator is being told about
    bestId, bestSeen, bannerAt = None, 0.0, 0.0
    ranked = []                     # last computed candidate list, reused between frames
    columnVolts = {}                # column -> {plate voltage: [descent rates]}
    columnVerdicts = {}             # column -> (charged?, when) -- outlives the rates
    liftWatch = LiftWatch()         # follows the panel and latches on a 提升 press
    pickedSeen = 0.0                # when the picked droplet was last seen
    zeroCrossings = []              # every `0` crossing, so a broken track can still
                                    # be paired with its own start -- see adoptZeroCrossing
    voltsFailedAt = None            # when the `V:` line first refused to read
    chosenX = None                  # the column the operator has settled on
    chosenSeen = 0.0                # when a track was last alive in it
    banner, bannerColour = None, (0, 220, 255)
    detections = 0
    detSeries = []                  # (t, count) per frame, for spraySpans
    detRates = []                   # detections/frame, for the quiet level
    quietLevel = None               # frozen once, from the first clean stretch
    staleSince = None               # when the rate first went and stayed high
    resyncCooldown = 0.0            # no second rebuild while the first is settling
    resyncs = 0
    # A live run cannot seek back to the panel once a fall has finished, so keep the
    # readout crops as they go past.  Only the last MAX_FALL_S of them can ever matter.
    panel = collections.deque()
    # ...and crops of the LATCHED line, which keeps showing the balance voltage
    # after the field is cut.  Read once at the end of a fall, never during it:
    # nothing about the measurement depends on this number.
    latched = collections.deque()
    # The box can fall outside the frame -- a fixture recorded as an ROI crop has no
    # panel in it at all, and the box then has zero width.  Crop it against the frame
    # and disable the read rather than letting cvtColor raise on an empty image,
    # which took down a whole session's tracking the first time it happened.
    latchBox = latchedBox(roi)
    frameH, frameW = src.height, src.width
    latchBox = (max(0, min(latchBox[0], frameW)), max(0, min(latchBox[1], frameH)),
                max(0, min(latchBox[2], frameW)), max(0, min(latchBox[3], frameH)))
    if latchBox[2] - latchBox[0] < 20 or latchBox[3] - latchBox[1] < 10:
        print("  no balance-voltage panel in frame; that column stays empty",
              flush = True)
        latchBox = None
    # A file's window is full before the first frame, so it is ready immediately and
    # nothing about the file path changes -- including when prime() came up a sample
    # short.  A live window starts empty, and the median of three frames is not a
    # background at all: it still contains the droplets, which then get subtracted
    # away.  Until it fills, the background learned before any droplets existed is
    # strictly better, so keep using that.
    rollerReady = not src.isLive or (roller is not None
                                     and len(roller.buf) >= roller.samples)
    prevT, firstT, lastT = None, None, None
    start = time.perf_counter()
    # Ctrl+C has to land like `q` does, not kill the process: without this a headless
    # run has no way to stop that still writes falls.csv, and the whole session is lost
    # the way it used to be lost to the report() crash.
    try:
        for frame, now in src.frames(fromFrame, toFrame):
            n += 1
            # dt comes from the timestamps, never from fps.  A dropped frame then widens
            # every gate by exactly the right amount instead of orphaning the track.
            dt = (1.0 / fps) if prevT is None else max(1e-6, now - prevT)
            prevT = now
            if firstT is None:
                firstT = now
            lastT = now
            maxStep = maxSpeed * dt         # how far a droplet may move in THIS interval
            crop = frame[Y:Y+H, X:X+W]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if roller is not None:
                roller.push(gray, n)
                rolled = roller.get(n)
                if rolled is not None and rollerReady:
                    background = rolled
                elif rolled is not None and len(roller.buf) >= roller.samples:
                    background, rollerReady = rolled, True
            if src.isLive and n % panelEvery == 0:
                # The same crop the field-off check uses, read once a frame instead of
                # once a fall.  A droplet's response to the field is the FIRST half of
                # Ⅳ.2's selection and nothing automated it before.
                vCell = cv2.cvtColor(frame[box[1]:box[3], box[0]:box[2]],
                                     cv2.COLOR_BGR2GRAY)
                # Zero is read WITHOUT the digit templates.  `+0000V` is four identical
                # glyphs, so there is nothing to tell the four digit runs from the `+`
                # and the `V`, and an alignment found on it lands on the wrong columns
                # -- which read 0 V back as 101 V.  allDigitsAlike answers "are all four
                # the same" directly and needs no alignment at all, which is exactly the
                # question zero asks.
                allZero = fieldOffFromCells([vCell])
                if allZero:
                    # Zero needs no digit reader.  `+0000V` is four identical glyphs,
                    # so nothing distinguishes the digit runs from the `+` and the `V`
                    # and any alignment found on it is wrong; allDigitsAlike answers
                    # the question directly instead.
                    volts = 0
                else:
                    # Align and read the SAME median, every sample.  A carried-over
                    # alignment belongs to whatever digits produced it -- a `1` is half
                    # the width of a `0` -- so 0130's alignment read 0083 as 183.  And
                    # the window is short so the reading follows the knob instead of
                    # lagging a second behind it.
                    alignCells.append(vCell)
                    if len(alignCells) >= alignCells.maxlen:
                        med = np.median(np.stack(alignCells),
                                        axis = 0).astype(np.uint8)
                        was = usingTemplates("v")
                        panelAlign = panelAlignment(med)
                        got = readPanelVolts(med, panelAlign) if panelAlign else None
                        usingTemplates(was)
                        if got is not None:
                            volts = got
                if volts is not None:
                    voltHistory.append(volts)
                    # The mode over the last few reads, so one misread cannot move it.
                    # The RAW value is what feeds observeVoltage: the charge test has
                    # to see a 0.5 s toggle, which smoothing would erase.
                    steadyVolts = collections.Counter(
                        voltHistory).most_common(1)[0][0]
                # The RAW reading, not the mode: the lift is recognised by how fast
                # it moves, and voltHistory's mode would smear a one-reading step
                # across half a second, which is the very edge being measured.
                liftWatch.update(volts, now)

                # Whatever goes wrong, keep the evidence.  A live run records only
                # the ROI, so a panel that will not read leaves nothing to look at --
                # three restarts were spent guessing at `V ?` from the outside.
                if steadyVolts is None or steadyVolts == 0:
                    if voltsFailedAt is None:
                        voltsFailedAt = now
                    elif now - voltsFailedAt > 20.0:
                        voltsFailedAt = now + 1e9          # once per session
                        try:
                            folder = os.path.join(outDir, "panel_refused")
                            os.makedirs(folder, exist_ok = True)
                            cv2.imwrite(os.path.join(folder, "v_line.png"), vCell)
                            if alignCells:
                                cv2.imwrite(
                                    os.path.join(folder, "v_line_median.png"),
                                    np.median(np.stack(alignCells),
                                              axis = 0).astype(np.uint8))
                            was = usingTemplates("v")
                            text = (readPanelDigits(vCell, panelAlign)
                                    if panelAlign else None)
                            usingTemplates(was)
                            with open(os.path.join(folder, "v_line.txt"), "w") as f:
                                f.write("cell %s\n" % (vCell.shape,))
                                f.write("allDigitsAlike -> %s\n"
                                        % (fieldOffFromCells([vCell]),))
                                f.write("alignment: %s\n" % (panelAlign,))
                                f.write("readPanelDigits: %r\n" % (text,))
                                f.write("align window held %d frames\n"
                                        % len(alignCells))
                            print("  the `V:` line will not read -- crops saved to "
                                  "panel_refused/v_line*.png", flush = True)
                        except Exception as exc:
                            print("  could not save it: %s" % exc, flush = True)
                panel.append((now, cv2.cvtColor(frame[box[1]:box[3], box[0]:box[2]],
                                                cv2.COLOR_BGR2GRAY)))
                if latchBox is not None:
                    latched.append((now, cv2.cvtColor(
                        frame[latchBox[1]:latchBox[3], latchBox[0]:latchBox[2]],
                        cv2.COLOR_BGR2GRAY)))
                while panel and now - panel[0][0] > MAX_FALL_S + 5.0:
                    panel.popleft()
                while latched and now - latched[0][0] > MAX_FALL_S + 5.0:
                    latched.popleft()
            centres, _, _ = detectDroplets(gray, background, s)
            detections += len(centres)
            detSeries.append((now, len(centres)))

            # Is the background stale?  Compared against this session's own quiet
            # level, never an absolute number: the quiet rate is a property of the
            # apparatus and has ranged from 0.74 to 123 detections a frame.
            #
            # The reference comes from the session's FIRST clean stretch and is then
            # frozen.  A rolling window does not work here and the failure is not
            # subtle: on 2026-09-07 the reference was recomputed after each resync,
            # refilled during the still-cluttered period that followed, and the
            # threshold rose to match the clutter.  The detector fired once at 92 s
            # and then never again while ids climbed to 50,627 -- it had calibrated
            # itself blind.  What counts as quiet cannot be learned from a stretch
            # that is not quiet.
            # The estimate only ever goes DOWN.  Taking it once from the opening
            # stretch assumes that stretch is quiet, and it usually is not -- the
            # operator sprays as soon as the background learn ends, and on
            # zoom_labelled_0907 that put "quiet" at 15.00 a frame when the session's
            # real floor is 4.  A running minimum cannot be talked upwards by
            # clutter, so the worst a busy start costs is a late first reading.
            detRates.append(len(centres))
            if len(detRates) >= 600:
                recent = float(np.percentile(detRates, 25))
                if quietLevel is None or recent < quietLevel:
                    quietLevel = recent
                    print("  quiet detection rate: %.2f a frame" % quietLevel,
                          flush = True)
                del detRates[:300]
            if roller is not None and quietLevel is not None and now >= resyncCooldown:
                if len(centres) > max(3.0, STALE_FACTOR * quietLevel):
                    if staleSince is None:
                        staleSince = now
                    elif now - staleSince >= STALE_S:
                        # Keep rollerReady True on purpose.  Clearing it would send
                        # the loop back to the background it was using before, which
                        # is the stale one this resync exists to discard.  A median
                        # over a handful of post-change frames is worse than a full
                        # window and far better than a poisoned one.
                        roller.resync(gray)
                        resyncs += 1
                        staleSince = None
                        # Long enough for the window to refill and settle, so a
                        # rebuild is not judged while it is still happening.
                        resyncCooldown = now + STALE_S
                        print("  background rebuilt at %.0f s -- detections stayed "
                              "%.0fx above %.2f/frame for %.0f s, which no spray does"
                              % (now - (firstT or 0), STALE_FACTOR, quietLevel,
                                 STALE_S), flush = True)
                else:
                    staleSince = None

            pairs = assign(live, centres, maxStep, s["areaFloor"], now, s["reidX"])
            for i, t in enumerate(live):
                if i in pairs:
                    c = centres[pairs[i]]
                    # A match further from the prediction than one frame's step could
                    # reach only happened because the widened gate allowed it, i.e.
                    # this fall was stitched across a gap.  Count it, for the same
                    # reason dead-track re-acquisitions are counted: a gate that is
                    # too loose fabricates a fall out of two droplets, and this makes
                    # that visible in falls.csv instead of trusted.
                    aim = t.predict(now)
                    if t.miss > 0 and np.hypot(c[0] - aim[0], c[1] - aim[1]) > maxStep:
                        t.reacquired += 1
                    t.update(c, now, maxSpeed)
                    t.observeVoltage(volts, spacing)
                    noteColumnVoltage(columnVolts, t, volts, spacing)
                    updateDivision(t, s["gridRows"])
                    updateHold(t, yTop, spacing, now)
                    finished = updateCrossings(t, yTop, yBottom, spacing,
                                               zeroCrossings)
                    if finished is not None:
                        if not src.isLive:      # a frame index to seek back to, files only
                            t.fTop, t.fBottom = t.tTop * fps, t.tBottom * fps
                        t.kinematicLegal = t.legal
                        t.fieldOff = None
                        if t.legal:                  # only speed-legal falls need the check
                            t.fieldOff = fieldOffFromCells(
                                [cell for ct, cell in panel if t.tTop <= ct <= t.tBottom]) \
                                if src.isLive else fieldWasOff(path, box, t.fTop, t.fBottom)
                            t.legal = bool(t.fieldOff)
                        # The balance voltage, majority-voted off the latched panel
                        # line over the whole fall.  RECORDED, NEVER RELIED ON:
                        # legality is already decided above and nothing below reads
                        # this.  None when it could not be read.
                        # Aligned from the latched crops THEMSELVES, not from the V:
                        # line.  The latched value is constant for the whole fall, so
                        # its median is sharp; the V: line changes and its median is
                        # smeared.  Aligning off V: read 609 V and 749 V on a fixture
                        # where neither is a possible balance voltage.
                        panelReport = {}
                        t.balanceVoltage = readLatchedVolts(
                            [cell for ct, cell in latched
                             if t.tTop <= ct <= t.tBottom],
                            report = panelReport) \
                            if src.isLive else None
                        # A live run records only the ROI, so when the panel
                        # cannot be read there is otherwise nothing left to
                        # look at: four of nine legal falls came back with no
                        # voltage on 2026-09-08 and the diagnosis had to be
                        # guesswork.  Keep the crop and the vote spread.
                        if (src.isLive and t.balanceVoltage is None
                                and t.kinematicLegal):
                            saveRefusedPanel(outDir, t.tTop, panelReport)
                        t.completedFalls = getattr(t, "completedFalls", 0) + 1
                        falls.append(Fall(finished, spacing))
                        fallLog.add(falls[-1])
                        sayFall("  frame %6d  id %-5d  %6.2f s  %6.3f grid/s  %s"
                              % (n, t.id, t.seconds, FALL_DIVISIONS / t.seconds,
                                 # fieldOff is TRI-STATE and this line used to flatten it:
                                 # anything kinematically legal printed as "field ON",
                                 # whether the panel was read and showed volts (False) or
                                 # was never readable at all (None).  Those need different
                                 # actions -- one is the operator's, one is ours.
                                 "LEGAL" if t.legal else
                                 ("field ON - rejected" if t.kinematicLegal
                                  and t.fieldOff is False else
                                  "panel UNREADABLE - rejected" if t.kinematicLegal
                                  else "rejected")),
                              flush = True)
                else:
                    t.miss += 1
            keep, buried = [], []
            probationS = max(PROBATION_S, 2 * dt)    # never shorter than two frames
            for t in live:
                waited = now - t.lastT
                measuring = t.tTop is not None and t.tBottom is None
                allowed = (MEASURING_COAST_S if measuring and len(t.points) >= CONFIRM_POINTS
                           else MAX_COAST_S if len(t.points) >= CONFIRM_POINTS
                           else probationS)
                if waited <= allowed:
                    keep.append(t)
                elif len(t.points) >= CONFIRM_POINTS:
                    buried.append(t)                 # only confirmed tracks are worth keeping
            live = keep
            morgue.extend(buried)
            morgue[:] = [t for t in morgue if now - t.points[-1][0] <= REID_MAX_S]

            claimed = set(pairs.values())
            # A coasting track is still live and still aiming at its droplet.  If assign
            # did not match it this frame, its blob is unclaimed -- and creating a new
            # track on that blob puts TWO tracks on one droplet, after which the global
            # assignment feeds only one and starves the other.  Measured on clip 1: a
            # duplicate took the blob on 27 of the 90 frames its original was waiting.
            # So do not spawn where a confirmed live track is already predicting.
            coasting = [(t.predict(now), t) for t in live
                        if t.miss > 0 and len(t.points) >= CONFIRM_POINTS]
            # ...and the same argument applies to a track that IS being fed this frame.
            # The guard above only covered coasters, which was enough while detections
            # were scarce.  Loosen the circularity gate and they triple, and a noise
            # blob landing beside a healthy track spawns a twin that then splits the
            # droplet's blobs with it -- both survive, both complete, and one droplet
            # is reported as two or three falls.  So refuse to spawn anywhere a live
            # track already is or is about to be, confirmed or not.
            # A track that is MID-MEASUREMENT owns the corridor it is falling down.
            #
            # The guard below refuses to spawn within maxStep of a coaster's
            # prediction, which is a circle about 10 px across.  That is the right
            # size for a track that missed one frame and far too small for one that
            # has been dark for two seconds -- and the droplet goes dark for exactly
            # as long as it overlaps a grid line, because gridClear suppresses every
            # detection within 4 * mag px of a row.  The `16` line IS a row.  So the
            # one moment in the whole traverse where the droplet is guaranteed
            # invisible is the moment the measurement completes.
            #
            # Seen on 2026-09-08, live: a fall 16.2 s in, the droplet sitting on
            # `1.6`, went dark for 2.5 s -- inside MAX_COAST_S, so the track was
            # still alive and still aiming at it -- and a NEW track spawned on the
            # blob when it reappeared below the line.  The new one had no tTop, the
            # old one was starved by the global assignment, and a legal fall one
            # second from completing was lost.  The operator timed it at 17.20 s.
            #
            # So while tTop is set and tBottom is not, reserve the whole corridor the
            # droplet can plausibly be in, using the same asymmetric shape assign()
            # already uses to take it back: tight in x, because a fall drifts 5-6 px
            # sideways across a whole traverse, and elapsed-scaled in y, because that
            # is the only axis it is moving along.  This costs nothing anywhere else
            # -- a track not being timed reserves nothing.
            # Reserve only what assign() could actually have taken.  The first
            # version of this guard matched assign()'s wide gate in x and y but NOT
            # its area test, so a blob whose area had drifted failed assign, was
            # then refused a track of its own here, and was orphaned for good --
            # never assigned, never spawned.  That turned a stitch into a
            # starvation: one_droplet_slow lost the two falls that needed
            # reacquisition (reacq 5 and reacq 1) while keeping the three that did
            # not.  Caught by check.py, which is the entire reason it exists.
            measuring = [(t.pos, t.predict(now), t.area) for t in live
                         if t.miss > 0 and len(t.points) >= CONFIRM_POINTS
                         and t.tTop is not None and t.tBottom is None and t.vel[1] > 0]
            shadow = [(t.pos, t.predict(now)) for t in live]
            dup2 = s["dupPx"] ** 2
            for j, c in enumerate(centres):
                if j in claimed:
                    continue
                if any(abs(c[0] - p[0]) <= s["reidX"]
                       and p[1] - 4.0 <= c[1] <= a[1] + max(4.0,
                                                            REID_Y_SLACK * abs(a[1] - p[1]))
                       and abs(c[2] - ar) / max(ar, s["areaFloor"]) <= AREA_TOLERANCE
                       for p, a, ar in measuring):
                    continue                     # a fall in progress owns this blob
                if any((c[0] - aim[0]) ** 2 + (c[1] - aim[1]) ** 2 <= maxStep ** 2
                       for aim, _t in coasting):
                    continue                     # belongs to a coaster; do not duplicate
                if any((c[0] - p[0]) ** 2 + (c[1] - p[1]) ** 2 <= dup2
                       or (c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2 <= dup2
                       for p, a in shadow):
                    continue                     # a live track is already on this
                old = reacquire(morgue, c, now, s)
                if old is None:
                    live.append(Track(c, now))
                else:
                    morgue.remove(old)               # it lives again, with its history
                    old.miss = 0
                    old.reacquired += 1
                    old.update(c, now, maxSpeed)
                    live.append(old)

            if show or writer is not None:
                marked = crop.copy()
                cv2.line(marked, (0, yTop), (W, yTop), (255, 255, 0), 1)
                cv2.line(marked, (0, yBottom), (W, yBottom), (255, 255, 0), 1)
                # Nothing here decides anything, so it runs at the rate the screen
                # updates rather than the rate frames arrive.  A spray floods the field
                # and the operator is not hunting yet, so say nothing then either --
                # same test the background watcher uses, this session's own quiet rate
                # and never an absolute number.
                if n % SHOW_EVERY == 0:
                    noisy = (quietLevel is not None
                             and len(centres) > max(3.0, STALE_FACTOR * quietLevel))

                    lifting = liftWatch.quiet(now)
                    # A click selects the nearest droplet; clicking bare field
                    # clears the selection.  The window shows the whole frame with the
                    # ROI pasted in at (X, Y), so a window coordinate is an ROI
                    # coordinate minus that origin.
                    while clicks:
                        mx, my = clicks.pop()
                        rx, ry = mx - X, my - Y
                        near = [t for t in live
                                if not t.miss and len(t.points) >= CONFIRM_POINTS
                                and np.hypot(t.pos[0] - rx, t.pos[1] - ry)
                                <= PICK_RADIUS_PX]
                        picked = (min(near, key = lambda t: np.hypot(t.pos[0] - rx,
                                                                    t.pos[1] - ry))
                                  if near else None)
                        pickedSeen = now
                    # Follow it by position, because the track id will not survive the
                    # lift and the droplet the operator chose is still the droplet the
                    # operator chose.
                    if picked is not None:
                        px, py = float(picked.pos[0]), float(picked.pos[1])
                        near = [t for t in live if not t.miss
                                and np.hypot(t.pos[0] - px, t.pos[1] - py)
                                <= PICK_FOLLOW_PX]
                        if near:
                            picked = min(near, key = lambda t: np.hypot(t.pos[0] - px,
                                                                       t.pos[1] - py))
                            pickedSeen = now
                        elif now - pickedSeen > PICK_LOST_S:
                            picked = None
                    ranked = candidates(live, spacing, now, H, volts = steadyVolts,
                                        quiet = noisy, columns = columnVolts,
                                        verdicts = columnVerdicts, lifting = lifting)

                    # Has the operator settled on one?  Decided AFTER the candidate
                    # list, so it inherits every test in it.  An earlier version asked
                    # only "has this track sat still for three seconds", which is also
                    # a description of dust: it fired with nothing tracked at all and
                    # replaced the hunt with `MEASURING -- 0 of 5`.
                    def measuring(t):
                        """Has the operator committed to this droplet?

                        Three earlier versions guessed at intent and all three were
                        wrong in the same direction.  "A fall clock is running" fires
                        on any speck that crosses `0`.  "It completed a legal fall"
                        fires on a spray droplet that fell freely while the operator
                        was still hunting.  "It has seen two voltages" fires on
                        everything in the chamber the moment the field is switched.
                        Each one silenced the hunt aid for a droplet nobody had chosen.

                        The operator supplied the test that works, and it is Ⅳ.4's own
                        first step: balance the droplet on the top line, and leave it
                        there while you check it.  Nothing accidental sits still on `0`
                        for MEASURE_HOLD_S -- a droplet that is not being held drifts
                        off the line in a second or two, and a speck never arrives on
                        it at all.  A droplet the operator CLICKED counts too, because
                        that is the same statement made explicitly.

                        Once the column is pinned it stays pinned, so this only has to
                        be true at the moment the measurement begins, not throughout.
                        """
                        if picked is not None and t is picked:
                            return True
                        if getattr(t, "completedFalls", 0) > 0:
                            return True
                        return heldLongEnough(t, now)


                    settledTrack = next(
                        (t for t in live
                         if not t.miss and len(t.points) >= CONFIRM_POINTS
                         and measuring(t)), None)
                    balanced = next((c for c in ranked
                                     if c["action"] == "BALANCED"
                                     and c["charged"] is True), None)
                    # PIN the column when it is first chosen; do not follow the
                    # droplet.  Following it slides the 60 px window sideways as the
                    # droplet drifts, so falls counted a minute ago fall outside it and
                    # the tally goes DOWN -- an operator watched it read 2 of 5 and
                    # then 1 of 5 three seconds later.
                    if settledTrack is not None:
                        if chosenX is None:
                            chosenX = float(settledTrack.pos[0])
                        chosenSeen = now
                    elif balanced is not None:
                        if chosenX is None:
                            chosenX = balanced["pos"][0]
                        chosenSeen = now
                    elif chosenX is not None and any(
                            abs(float(t.pos[0]) - chosenX) <= SAME_DROPLET_X_PX
                            for t in live if not t.miss):
                        chosenSeen = now           # still occupied, between drops
                    if noisy:
                        # A spray puts new droplets in every column, so what was
                        # established about the old ones no longer describes what is
                        # being ringed.  The verdict outlives its samples but not this.
                        columnVerdicts.clear()
                    if chosenX is not None and (noisy or now - chosenSeen > CHOSEN_RELEASE_S):
                        chosenX = None             # sprayed, or the column emptied
                    if chosenX is not None:
                        ranked = []
                # Stick with the droplet already named unless it stops qualifying, or
                # one with a CONFIRMED charge turns up while this one is still unknown.
                    # Only a droplet whose charge has been CONFIRMED may be named.
                    # A slow drift with the field sitting still is equally a
                    # description of dust, and the operator has said three times that
                    # the ring latches onto droplets which turn out to answer nothing.
                    # It was true: the banner named whatever drifted slowest, tested or
                    # not.  Untested droplets still get their thin ring -- the operator
                    # has to see what to test -- but the instruction now asks for the
                    # test instead of asserting a result.
                    confirmed = [c for c in ranked if c["charged"] is True]
                    untested = len(ranked) - len(confirmed)
                    held = next((c for c in confirmed if c["id"] == bestId), None)
                    if held is not None:
                        bestNow, bestSeen = held, now
                    elif bestNow is None or now - bestSeen > CANDIDATE_STICKY_S:
                        # Only now is the held droplet really gone.  The grace has to cover
                        # a dropout while OTHER candidates still qualify -- that is the
                        # common case, and applying it only when the list went empty left
                        # the banner alternating between two droplets every couple of
                        # seconds.
                        if confirmed:
                            bestNow, bestId, bestSeen = confirmed[0], confirmed[0]["id"], now
                        else:
                            bestNow, bestId = None, None
                    # An explicit pick outranks every automatic choice, and is
                    # reported whether or not it passes the candidate gates: the
                    # operator asked about THIS droplet, and "this one will not work"
                    # is the most useful thing to say once they have spent time on it.
                    if picked is not None:
                        pv = columnChargeState(columnVolts, picked.pos[0], now,
                                               columnVerdicts)
                        if pv is None:
                            pv = picked.chargeState()
                        banner, bannerColour, _ok = pickedVerdict(
                            picked, spacing, now, steadyVolts, pv,
                            freeFallSpeed(columnVolts, picked.pos[0]))
                        bannerAt = now
                    elif bestNow is None or now - bannerAt >= CANDIDATE_REFRESH_S:
                        banner, bannerColour = candidateBanner(bestNow, steadyVolts,
                                                               untested, now)
                        bannerAt = now
                    if chosenX is not None:
                        # Measuring, not hunting.  One line of progress, and the fall
                        # clock on the droplet itself is already drawn.
                        # LEGAL falls only.  `falls` holds every completed
                        # traverse, and a droplet dropped too fast still crosses both
                        # lines -- so counting traverses made the tally climb on every
                        # downward move rather than on every measurement.
                        done = sum(1 for f in falls
                                   if f.legal and getattr(f, "xPx", None) is not None
                                   and abs(f.xPx - chosenX) <= SAME_DROPLET_X_PX)
                        banner = "MEASURING -- %d of %d falls" % (
                            done, TARGET_FALLS_PER_DROPLET)
                        bannerColour = (200, 200, 200)
                        bestNow, bestId = None, None
                    if bestNow is not None and not bestNow["track"].alerted:
                        bestNow["track"].alerted = True
                        _beep()
                # Every candidate gets a thin ring; only the best gets the banner.
                # Holding the banner steady costs recall if it is the only signal --
                # measured, naming one droplet at a time took 6 of 6 legal traverses
                # down to 5 of 6, because the second one was never mentioned while the
                # first was being shown.  Ringing them all puts that back without
                # reintroducing the flicker: the operator sees every candidate and is
                # TOLD about one.
                for cand in ranked:
                    cp = (int(round(cand["pos"][0])), int(round(cand["pos"][1])))
                    if bestNow is not None and cand["id"] == bestNow["id"]:
                        continue
                    cv2.circle(marked, cp, 16, (0, 200, 200), 1)
                if picked is not None:
                    # A square, so the operator's own choice is never mistaken for the
                    # pipeline's, and it stays on the droplet they clicked.
                    px, py = int(round(picked.pos[0])), int(round(picked.pos[1]))
                    cv2.rectangle(marked, (px - 18, py - 18), (px + 18, py + 18),
                                  bannerColour, 2)
                elif bestNow is not None:
                    bp = (int(round(bestNow["pos"][0])), int(round(bestNow["pos"][1])))
                    cv2.circle(marked, bp, 20, bannerColour, 2)
                for t in live:
                    if t.miss > 0:
                        continue                     # never draw a coasting ghost
                    if (chosenX is not None and t.tTop is None
                            and abs(float(t.pos[0]) - chosenX) > SAME_DROPLET_X_PX):
                        # Measuring, not hunting.  Everything outside the chosen
                        # column is clutter competing with the fall clock, and the
                        # operator watched hunt-phase rings glitch through their
                        # measurement.
                        #
                        # `t.tTop is None` is not optional.  Without it this suppressed
                        # a track that had ALREADY crossed `0` and was being timed,
                        # purely for being in another column -- and the exemption
                        # fifteen lines below, "a track that has already crossed `0` is
                        # always drawn", never ran, because this `continue` came first.
                        #
                        # It cost a fall on 2026-09-09.  At HUD 11:08 the operator's
                        # droplet crossed `0` at x=691 while chosenX was pinned at
                        # x=853; 162 px is past the 60 px window, so nothing was drawn
                        # for 4.9 s while the clock ran.  Seeing no ring, the operator
                        # concluded it had not been detected, lifted it, and the rise
                        # void cleared tTop -- so a fall the pipeline had measured
                        # correctly was never written, and cost 20 s to redo.
                        continue
                    # Speckle lives for a frame or two; a droplet being measured lives
                    # for the whole traverse.  Anything younger than DRAW_MIN_AGE_S is
                    # not worth the operator's attention and is not drawn.  A track
                    # that has already crossed `0` is always drawn, however young --
                    # that is the one being timed.
                    age = now - t.points[0][0] if t.points else 0.0
                    if age < DRAW_MIN_AGE_S and t.tTop is None:
                        continue
                    if t.tTop is None and len(t.points) > 4:
                        ys = [p[1] for p in t.points[::3]]
                        if max(ys) - min(ys) < DRAW_MIN_TRAVEL_DIV * spacing:
                            continue             # never went anywhere; not a droplet
                        # ...and it must have gone there in a LINE.  Travel alone does
                        # not separate a droplet from chained speckle: the tracker
                        # strings unrelated one-frame blobs together and the result
                        # carries a plausible velocity and a large apparent travel --
                        # an operator counted thirteen of them 43 s into a run.  A
                        # droplet falls straight; noise stitched out of different
                        # specks does not.  Same residual the candidate rule uses.
                        # Fitted over DRAW_MIN_AGE_S, not the candidate window.  The
                        # first version asked windowFit for its default three seconds
                        # while a track needs only 1.5 to be drawn, so everything
                        # younger than three seconds got None back and skipped the test
                        # -- which is exactly where speckle lives.  A track too short
                        # to fit at all is not drawn either: unmeasurable is not a pass.
                        fit = t.windowFit(spacing, now, DRAW_MIN_AGE_S)
                        if fit is None or fit[1] > CANDIDATE_MAX_RESIDUAL_PX:
                            continue
                    p = (int(round(t.pos[0])), int(round(t.pos[1])))
                    if len(t.points) < CONFIRM_POINTS:
                        cv2.circle(marked, p, 2, (120, 120, 120), -1)    # unconfirmed: a dot
                        continue
                    v = t.recentSpeed(spacing)
                    plausible = v is not None and SCREEN_MIN <= v <= SCREEN_MAX
                    if v is None:
                        colour = (160, 160, 160)                 # not enough points yet
                    elif plausible:
                        colour = (0, 255, 0)                     # plausible fall
                    elif v < 0:
                        colour = (255, 120, 0)                   # rising
                    else:
                        colour = (0, 0, 255)                     # too fast
                    cv2.circle(marked, p, 9, colour, 1)
                    if plausible:
                        cv2.putText(marked, "%.2f gr/s" % v, (p[0] + 16, p[1] - 3),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
                    if t.tBottom is not None:                    # measured: freeze it
                        done = (0, 255, 0) if t.legal else (0, 0, 255)
                        cv2.circle(marked, p, 13, done, 1)
                        cv2.putText(marked, "%.2f s" % t.seconds,
                                    (p[0] + 16, p[1] + 13),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, done, 1)
                    elif t.tTop is not None:                     # clock is running
                        cv2.circle(marked, p, 13, (0, 255, 255), 1)
                        cv2.putText(marked, "%.1f s" % (now - t.tTop),
                                    (p[0] + 16, p[1] + 13),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
                nTracked = sum(1 for t in live
                               if t.miss == 0 and len(t.points) >= CONFIRM_POINTS)
                hud = "tracked %d   falls %d   legal %d   V %s" % (
                    nTracked, len(falls), sum(1 for f in falls if f.legal),
                    "?" if steadyVolts is None else "%d" % steadyVolts)
                secs = int(now - (firstT or 0))
                clock = "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)

                def drawHud(canvas):
                    cv2.putText(canvas, hud, (10, 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
                    if banner:
                        h = canvas.shape[0]
                        cv2.rectangle(canvas, (0, h - 34), (canvas.shape[1], h),
                                      (0, 0, 0), -1)
                        cv2.putText(canvas, banner, (12, h - 11),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, bannerColour, 2)
                    (tw, _th), _ = cv2.getTextSize(clock, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
                    cv2.putText(canvas, clock, (canvas.shape[1] - tw - 10, 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)

                # The window shows the WHOLE frame the apparatus sent, with the ROI
                # pasted back in place.  Cropping to the ROI hid the `V:` readout the
                # operator balances against, and left the clock in the middle of the
                # window instead of its corner.  The recording stays ROI-sized: that is
                # the measured region and the shape every artefact so far has.
                plain = marked.copy() if (show and not blind) else None
                drawHud(marked)
                inWindow = (videoFrom is None or videoFrom <= n) and (videoTo is None or n <= videoTo)
                if writer is not None and inWindow:
                    writer.write(marked)      # track over a wide span, record a narrow one
                # cv2.waitKey(1) costs 14.1 ms of a 42.4 ms frame budget on Windows,
                # where its resolution is the system timer rather than the millisecond
                # asked for.  A third of the budget, spent waiting.  Showing every third
                # frame gives the operator ~8 updates a second -- ample for a droplet
                # crossing half a grid line a second -- and hands the rest back.  A file
                # source still shows every frame, because there waitKey IS the pacing.
                if show and (not src.isLive or n % SHOW_EVERY == 0):
                    if blind:
                        # Exactly what the lab monitor would have shown.  The apparatus
                        # burns the grid and the V:/t: panel into the signal itself, so
                        # the untouched frame IS that picture.  Nothing of ours on it.
                        display = frame
                    else:
                        display = frame.copy()
                        display[Y:Y + H, X:X + W] = plain
                        cv2.rectangle(display, (X, Y), (X + W, Y + H), (0, 160, 0), 1)
                        drawHud(display)
                    cv2.imshow("Droplets", display)
                    if src.isLive:
                        delay = 1           # a camera sets its own pace; never wait on it
                    else:
                        targetTime = start + n / fps
                        delay = max(1, int(round((targetTime - time.perf_counter()) * 1000)))
                    if cv2.waitKey(delay) == ord("q"):
                        print("Stopped at frame", n)
                        break
            if not show and n % 3000 == 0:
                if toFrame:
                    print("  frame %d/%d (%.0f%%)  live %d  falls %d"
                          % (n, toFrame,
                             100.0 * (n - fromFrame) / max(1, toFrame - fromFrame),
                             len(live), len(falls)), flush = True)
                else:
                    print("  %.0f s  %d frames  live %d  falls %d"
                          % (now - (firstT or 0), n, len(live), len(falls)), flush = True)

    except KeyboardInterrupt:
        print("\nInterrupted at frame", n)
    src.close()
    if writer is not None:
        writer.release()
    if show:
        cv2.destroyAllWindows()
    fallLog.close()

    elapsed = (lastT - firstT) if (firstT is not None and lastT is not None) else 0
    measuredFps = (n - fromFrame - 1) / elapsed if elapsed > 0 else 0.0
    if src.isLive and measuredFps:
        print("  delivered %.2f fps against %.2f advertised, %d dropped"
              % (measuredFps, fps, src.dropped), flush = True)
    if health is not None:
        health.update({"bgWindowS": bgWindowS,
                       "fromFrame": fromFrame,
                       "toFrame": toFrame,
                       "roiOk": True,
                       "gridSpacingPx": round(spacing, 1),
                       "detectionsPerFrame": round(detections / max(1, n), 2),
                       # The rate actually delivered, not the one advertised.  A
                       # device promising 30 and giving 27 is the failure this whole
                       # change exists to make visible; nothing is measured from it,
                       # but the operator should see it.
                       "fps": round(measuredFps, 2) if measuredFps else fps,
                       "nominalFps": fps,
                       "droppedFrames": src.dropped,
                       "framesRead": n,
                       "backgroundResyncs": resyncs,
                       "spraySpans": spraySpans([t for t, _c in detSeries],
                                                [c for _t, c in detSeries])})
    return falls


def _beep():
    """A short tone, on the platforms that have one, and nothing anywhere else.

    The operator's hands and eyes are on the toggles and the voltage knob while a
    candidate is falling.  A banner they are not looking at is not an alert.
    """
    # On a thread, always.  winsound.Beep BLOCKS for its full duration, and 120 ms
    # inside the frame loop stalls three frames -- which would spend more budget than
    # the waitKey change just saved, to announce that the budget matters.
    def ring():
        try:
            import winsound
            winsound.Beep(880, 120)
        except Exception:
            pass                            # no sound device, or not Windows
    try:
        threading.Thread(target = ring, daemon = True).start()
    except Exception:
        pass


FALL_COLUMNS = ["id", "startFrame", "startTime", "seconds", "speedGridPerSec",
                "xPx", "reacquired", "adopted", "kinematicLegal", "fieldOff",
                "balanceVoltageV", "legal"]


def fallRow(f):
    return {"id": f.id, "startFrame": f.fTop, "startTime": f.tTop,
            "seconds": f.seconds,
            "speedGridPerSec": FALL_DIVISIONS / f.seconds,
            "xPx": getattr(f, "xPx", None),
            "reacquired": getattr(f, "reacquired", 0),
            # True when the `0` crossing was made under a DIFFERENT track id and
            # paired to this one by column.  The measurement is the same; the row
            # says so because a reader should be able to see which ones needed it.
            "adopted": bool(getattr(f, "adopted", False)),
            "kinematicLegal": getattr(f, "kinematicLegal", f.legal),
            "fieldOff": getattr(f, "fieldOff", None),
            "balanceVoltageV": getattr(f, "balanceVoltage", None),
            "legal": f.legal}


class FallLog:
    """falls.csv, written a row at a time as the falls complete.

    It used to be written once, by report(), after the last frame.  Everything a
    session measured therefore lived only in memory until then, and three sessions
    have now been lost that way: two live runs on 2026-09-08 that the operator
    stopped, and a 56,543-frame replay that ran to completion and then died in
    to_csv because outDir did not exist.  Forty minutes of measurement, discarded
    at the write.

    So write as we go and flush every row.  report() rewrites the same file at the
    end from the same fallRow(), so a run that finishes is unchanged; a run that
    does not keeps every fall it had found.
    """

    def __init__(self, outDir, path):
        self.path = artefact(outDir, path, "falls.csv")
        self.file = None

    def add(self, f):
        try:
            if self.file is None:
                self.file = open(self.path, "w", encoding = "utf-8", newline = "")
                self.writer = csv.DictWriter(self.file, fieldnames = FALL_COLUMNS)
                self.writer.writeheader()
            self.writer.writerow(fallRow(f))
            self.file.flush()
        except Exception as exc:                 # never let bookkeeping kill a session
            print("  could not append to %s: %s" % (self.path, exc), flush = True)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def report(falls, outDir = None, path = None):
    legal = [f for f in falls if f.legal]
    kin = [f for f in falls if getattr(f, "kinematicLegal", f.legal)]
    print("\ncomplete `0` -> `16` traverses: %d" % len(falls))
    print("passing speed and time (%d div, %g-%g s, %g-%g grid/s): %d"
          % (FALL_DIVISIONS, MIN_FALL_S, MAX_FALL_S, MIN_SPEED, MAX_SPEED, len(kin)))
    print("of those, field confirmed OFF -- LEGAL: %d" % len(legal))
    if falls:
        print("\n%6s %10s %9s %11s %8s" % ("id", "start", "time(s)", "speed(gr/s)", "legal"))
        for f in sorted(falls, key = lambda f: -f.seconds)[:25]:
            # A live fall has no frame index, and tTop can be absent too, which
            # crashed report() at the end of every live session -- after the run,
            # before falls.csv was written, so the whole session was lost.
            start = ("%.0f" % f.fTop if f.fTop is not None else
                     "%.1fs" % f.tTop if f.tTop is not None else "-")
            print("%6d %10s %9.2f %11.3f %8s"
                  % (f.id, start, f.seconds, FALL_DIVISIONS / f.seconds, f.legal))
    csvPath = artefact(outDir, path, "falls.csv") if outDir and path \
        else (os.path.join(outDir, "falls.csv") if outDir else "falls.csv")
    os.makedirs(os.path.dirname(os.path.abspath(csvPath)), exist_ok = True)
    pd.DataFrame([fallRow(f) for f in falls],
                 columns = FALL_COLUMNS).to_csv(csvPath, index = False)
    print("\nWrote", csvPath)
    try:
        census = rejectCensus()
        print("\nwhat the gates rejected:\n%s" % census)
        with open(os.path.join(os.path.dirname(os.path.abspath(csvPath)),
                               "rejects.txt"), "w") as f:
            f.write(census + "\n")
    except Exception as exc:
        print("  could not write the reject census: %s" % exc)
    return sum(1 for f in falls if f.legal), len(falls)


def orderKey(path):
    # clips are named "<mm_ss> - <Mon_DD> - <n>"; sort by the trailing n
    stem = os.path.splitext(os.path.basename(path))[0]
    tail = stem.rsplit("- ", 1)[-1].strip()
    return (0, int(tail)) if tail.isdigit() else (1, stem)


def runAll(pattern = None):
    clips = sorted(glob.glob(pattern or os.path.join(resolveFootage(), "* - * - *.mp4")),
                   key = orderKey)
    if not clips:
        print("No clips matched."); return
    print("Running %d clips\n" % len(clips))
    summary = []
    for i, path in enumerate(clips, 1):
        name = os.path.splitext(os.path.basename(path))[0]
        print("=" * 72)
        print("[%d/%d] %s" % (i, len(clips), name), flush = True)
        started = time.perf_counter()
        outDir = outDirFor(path)
        falls = trackAll(path, show = False, saveVideo = True, outDir = outDir)
        legal, total = report(falls, outDir, path)
        summary.append((name, total, legal, time.perf_counter() - started))
    print("\n" + "=" * 72)
    print("%-24s %10s %7s %9s" % ("clip", "traverses", "legal", "run(s)"))
    for name, total, legal, secs in summary:
        print("%-24s %10d %7d %9.0f" % (name, total, legal, secs))
    print("%-24s %10d %7d" % ("TOTAL",
                              sum(r[1] for r in summary),
                              sum(r[2] for r in summary)))


# ---------------------------------------------------------------------------
#  The cv/api.py shapes.  Names, nesting and units are fixed by that contract;
#  these two functions exist so the app and LLM layers can call the real pipeline
#  instead of the stub.  Nothing here recomputes anything -- it reformats what
#  trackAll already produced.
# ---------------------------------------------------------------------------

def fallRecord(f):
    return {"id": f.id,
            # startFrame is a convenience for finding the fall in a recording, and is
            # None on a live source, which has no frame to seek back to.  startTime is
            # the real one: seconds on the capture clock, and what seconds is measured
            # from.
            "startFrame": f.fTop,
            "startTime": f.tTop,
            "seconds": f.seconds,
            "speedGridPerSec": FALL_DIVISIONS / f.seconds,
            "kinematicLegal": getattr(f, "kinematicLegal", f.legal),
            "fieldOff": getattr(f, "fieldOff", None),
            "legal": f.legal,
            "fallDrift": getattr(f, "fallDrift", None),
            "fallDirection": getattr(f, "fallDirection", None),
            # times this fall was stitched across a gap in which the droplet was
            # invisible -- either the track died and was rebuilt, or it coasted and
            # took its droplet back through the widened gate.  0 is clean.
            # Anything higher means the app should tell the operator, because a
            # wrong re-acquisition fabricates a fall out of two droplets.
            "reacquired": getattr(f, "reacquired", 0),
            # the hold that preceded this fall.  Needs hold-phase tracking, which
            # needs a live source -- nothing in a single-fall recording to measure.
            "holdDrift": None,
            "holdDirection": None}


def survey(videoPath = VIDEO, health = None, show = False, saveVideo = False,
           fromFrame = 0, toFrame = None, bgWindowS = BG_WINDOW_S,
           bgFromRange = False):
    """Every complete `0` -> `16` traverse, in time order.  Selection aid."""
    videoPath = resolveClip(videoPath)      # fail here, not inside outDirFor
    falls = trackAll(videoPath, show = show, saveVideo = saveVideo,
                     outDir = outDirFor(videoPath), health = health,
                     fromFrame = fromFrame, toFrame = toFrame, bgWindowS = bgWindowS,
                     bgFromRange = bgFromRange)
    return [fallRecord(f) for f in falls]


def groupFalls(rows):
    """Propose which falls belong to which droplet, from the falls alone.

    IV.4 re-drops ONE droplet five times before moving on, so the falls of a droplet
    are consecutive in time and share a duration -- same mass, same terminal velocity.
    A new droplet means a new mass, so a step in duration.  Walk the falls in order and
    start a new droplet when one lands further than DROPLET_TIME_TOL from the running
    mean of the droplet being built.

    `rows` are survey() rows or falls.csv rows: dicts with `id`, `startTime`,
    `seconds`.  Returns (groups, margins), where groups is a list of lists of rows and
    margins is the relative duration step at each boundary -- 0.31 means the first fall
    of that droplet was 31% away from the previous droplet's mean, so the split is
    obvious; 0.12 means it barely cleared the tolerance and deserves a look.

    This exists because the alternative was transcribing ids off paper, and a
    transcription slip silently costs a droplet.  It is a PROPOSAL to check against the
    operator's notes, not a replacement for them: two droplets within 10% of each
    other's fall time will be merged and nothing here can tell.
    """
    ordered = sorted(rows, key = lambda r: r["startTime"])
    groups, margins = [], []
    for r in ordered:
        if groups:
            mean = sum(g["seconds"] for g in groups[-1]) / len(groups[-1])
            if abs(r["seconds"] - mean) <= DROPLET_TIME_TOL * mean:
                groups[-1].append(r)
                continue
            margins.append(abs(r["seconds"] - mean) / mean)
        groups.append([r])
    return groups, margins


def analyze(videoPath = VIDEO, binding = None, voltages = None,
            show = False, saveVideo = False):
    """One measurement session: health, droplets, and each droplet's falls.

    `binding` groups fall ids into droplets, e.g. [[51143, 51890], [60011]].
    Nothing in a recording says which ids belong together -- IV.4 re-drops the same
    droplet five times and the tracker loses it during each motionless hold, so one
    droplet yields several ids.  Only the operator knows.  Guessing it would file
    two droplets as one and the resulting charge would look perfectly normal.
    With no binding, `droplets` comes back empty and you want survey().

    `voltages` is one balance voltage per droplet, in the same order as `binding`.
    Given it, the charge maths runs and `charges.csv` is written beside `falls.csv`.
    q, n and e do NOT enter the returned dict -- they reach the student through that
    file and nowhere else.  See the seal in cv/charge.py.
    """
    health = {"roiOk": False}
    rows = survey(videoPath, health = health, show = show, saveVideo = saveVideo)
    byId = {r["id"]: r for r in rows}

    droplets = []
    for k, group in enumerate(binding or [], 1):
        # An id that is not in the survey is a typo in the operator's notes, not a
        # droplet that produced fewer falls.  Dropping it silently is the worst
        # outcome: `dropletsCompleted` quietly falls short and nothing says why,
        # long after the apparatus is packed up.  Ⅳ.4 binding is hand-transcribed
        # from paper, so this WILL happen.
        unknown = [i for i in group if i not in byId]
        if unknown:
            raise KeyError(
                "droplet %d: fall id(s) %s are not in this recording. Check the "
                "drop-order notes against survey(). Ids present: %s"
                % (k, unknown, sorted(byId)))
        falls = [byId[i] for i in group]
        droplets.append({"id": group[0] if group else None,
                         "label": "Droplet %d" % k,
                         "fallCount": len(falls),
                         "falls": falls})

    # The physics, on disk only.  A CSV is layer-2 output; the returned dict is not.
    if voltages is not None and droplets:
        chargeRows, perDroplet, overall = chargeMath.csvRows(droplets, voltages)
        outDir = outDirFor(videoPath)
        os.makedirs(outDir, exist_ok = True)
        csvPath = artefact(outDir, videoPath, "charges.csv")
        pd.DataFrame(chargeRows,
                     columns = chargeMath.CSV_COLUMNS).to_csv(csvPath, index = False)
        print()
        print("Wrote", csvPath)
        for d in perDroplet:
            print("  %-12s U_n %5.0f V  %d falls  mean e %.4e C  sd %.1f%%  error %+.2f%%"
                  % (d["label"], d["balanceVoltage"], d["falls"],
                     d["meanElementaryC"], d["relativeSdPercent"], d["errorPercent"]))
        if overall:
            print("  %-12s %11s  %d falls  mean e %.4e C  sd %.1f%%  error %+.2f%%"
                  % ("ALL", "", overall["falls"], overall["meanElementaryC"],
                     overall["relativeSdPercent"], overall["errorPercent"]))

    return {"contractVersion": 2,
            "roiOk": health.get("roiOk", False),
            "gridSpacingPx": health.get("gridSpacingPx"),
            "detectionsPerFrame": health.get("detectionsPerFrame"),
            "fps": health.get("fps"),
            "droppedFrames": health.get("droppedFrames", 0),
            "dropletsCompleted": sum(1 for d in droplets
                                     if d["fallCount"] >= TARGET_FALLS_PER_DROPLET),
            "targetDroplets": TARGET_DROPLETS,
            "targetFallsPerDroplet": TARGET_FALLS_PER_DROPLET,
            # a spike in detections means somebody sprayed.  Needs a threshold that
            # no footage in hand can supply -- no clip contains a known spray.
            "sprayFrames": [],
            "droplets": droplets}


def main(argv):
    if len(argv) > 1 and argv[1] == "live":
        # --blind shows the raw frame and nothing else: no reticle lines, no track
        # circles, no elapsed-seconds label, no green/red verdict, and no per-fall line
        # in the terminal.  Against a human with a stopwatch that is not a convenience --
        # an operator who can see the overlay is copying the answer rather than taking an
        # independent one.
        #
        # It cannot just hide the window.  The apparatus has ONE video out; it now feeds
        # the dongle, so the lab monitor is dark and this window is the operator's only
        # picture.  Blind means unannotated, not absent.
        blind = "--blind" in argv[1:]
        rest = [a for a in argv[2:] if not a.startswith("-")]
        spec = "cam:" + (rest[0] if rest else "0")
        outDir = outDirFor(spec)
        print("Live session -> %s" % outDir)
        print("Stop with q in the window, or Ctrl+C")
        if blind:
            print("BLIND: the window is the raw apparatus picture -- maximise it.")
        report(trackAll(spec, show = True, blind = blind, saveVideo = True,
                        outDir = outDir),
               outDir, spec)
        return
    if len(argv) > 1 and argv[1] == "preview":
        previewAll()
        return
    if len(argv) > 1 and argv[1] == "all":
        runAll(argv[2] if len(argv) > 2 else None)
        return
    if len(argv) > 1 and argv[1] == "survey":
        path = argv[2] if len(argv) > 2 else VIDEO
        outDir = argv[3] if len(argv) > 3 else outDirFor(path)
        report(trackAll(path, show = False, saveVideo = True, outDir = outDir),
               outDir, path)
        return
    path = argv[1] if len(argv) > 1 else VIDEO
    outDir = argv[2] if len(argv) > 2 else outDirFor(path)
    report(trackAll(path, outDir = outDir), outDir, path)


# --------------------------------------------------------------------------------
#  Reading the balance voltage off the panel
#
#  When the field is cut, `V:` drops to +0000V but a THIRD line keeps showing the
#  voltage that was balancing the droplet.  That is the right place to read the
#  balance voltage from, and better than `V:` in three ways: it persists for the whole
#  fall, it is a different region so it cannot disturb the field-off check which reads
#  `V:`, and it is latched rather than live, so it does not jitter by a volt the way
#  the live reading does.  The operator pointed this out on 2026-09-07.
#
#  Nothing here is load-bearing.  Detection, tracking and legality never read these
#  numbers; a failure returns None and the operator's notes remain the record.
# --------------------------------------------------------------------------------

# What fraction of the readable frames must agree before a latched voltage is
# believed.  Not a tuning knob: the latched line holds ONE value for the whole fall,
# so every frame of that fall is a repeat measurement of the same constant, and two
# frames disagreeing means a glyph was misread -- there is nothing else it can mean.
# Measured over all fifteen falls of bench_0908, voting across each fall's own
# frames: the ten reads that are right agree 97-100% of the time, seven of them
# unanimously.  The four that are wrong agree 51%, 55%, 62% and 88%.  Nothing sits
# between 0.88 and 0.97, so 0.9 separates them with room on both sides -- and the
# separation is not a coincidence of this session, it is what a constant being read
# repeatedly has to look like.
#
# The apparatus's `2` is what fails, against templates cut from voltage_sweep_0907.
# Fixing that needs more labelled panel footage, not a cleverer matcher: zero-mean
# NCC, a common vertical cut and a binarised match were all tried on 2026-09-08 and
# all three still read 0700.  Until then this returns None, which is the documented
# contract -- the operator's notes carry the balance voltage and nothing downstream
# reads this.
LATCHED_AGREEMENT = 0.9

DIGIT_SIZE = (24, 22)      # canonical (rows, cols) every glyph is normalised into
_DIGITS = None
_DIGITS_V = None
# Which set _digitTemplates() hands back.  The `V:` line and the latched line sit in
# different bands of the panel and do not render identically -- the latched-line set
# read 133 V as 733 on the live line, and an operator watching the HUD spotted it
# before any test did.  Two sets, one switch, chosen by whichever line is being read.
_ACTIVE_TEMPLATES = "latched"


def latchedBox(roi):
    """The panel cell holding the latched balance voltage, one band below V:/t:."""
    return (roi["x"] + roi["w"], roi["rows"][1], roi["border"], roi["rows"][2])


def _templateFile():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "panel_digits_v.npz" if _ACTIVE_TEMPLATES == "v"
                        else "panel_digits.npz")


def _digitTemplates():
    """Templates for the apparatus's panel font, built from a labelled sweep.

    test/fixtures/voltage_sweep_0907, held at 0123 / 0145 / 0167 / 0189, which between
    them contain every digit.  Fixed font in a fixed place, so this is template
    matching -- no learning, nothing to tune.
    """
    global _DIGITS, _DIGITS_V
    cached = _DIGITS_V if _ACTIVE_TEMPLATES == "v" else _DIGITS
    if cached is None:
        try:
            z = np.load(_templateFile())
            cached = {k[1:]: z[k].astype(np.float32) for k in z.files}
        except Exception:
            cached = {}
        if _ACTIVE_TEMPLATES == "v":
            _DIGITS_V = cached
        else:
            _DIGITS = cached
    return cached


def _unit(a):
    c = np.asarray(a, np.float32) - float(np.mean(a))
    k = float(np.linalg.norm(c))
    return c / k if k > 1e-6 else c


def _canonical(tight):
    """Scale a glyph to DIGIT_SIZE by HEIGHT, keeping aspect, centred.

    Never stretched to fill: width is one of the strongest cues this font has -- a `1`
    is a third the width of an `8` -- and normalising it away made every `1` read as a
    `7`.  The templates are cut the same way, so matching compares shape, not framing.
    """
    h, w = tight.shape[:2]
    scale = float(DIGIT_SIZE[0]) / max(1, h)
    nw = max(1, min(DIGIT_SIZE[1], int(round(w * scale))))
    small = cv2.resize(np.asarray(tight, np.float32), (nw, DIGIT_SIZE[0]))
    canvas = np.full(DIGIT_SIZE, float(small.min()), np.float32)
    a = (DIGIT_SIZE[1] - nw) // 2
    canvas[:, a:a + nw] = small
    return canvas


def _glyphBand(line):
    """Rows the glyphs occupy, excluding the panel's own horizontal rules.

    The latched line's crop spans a whole grid band, so it contains the rules above
    and below the text.  Those are bright ACROSS THE FULL WIDTH; a plain "any bright
    pixel" test includes them, and then every glyph is cropped against a band that
    starts at a rule rather than at the digits.
    """
    if line is None or getattr(line, "size", 0) == 0:
        return None
    inner = line[2:-2, :] if line.shape[0] > 6 else line
    hot = inner > max(140, float(np.percentile(inner, 88)))
    keep = (hot.sum(axis = 1) > 0) & (hot.mean(axis = 1) < 0.5)
    rows = np.where(keep)[0]
    if len(rows) < 4:
        return None
    runs, first = [], rows[0]
    for a, b in zip(rows, rows[1:]):
        if b - a > 3:
            runs.append((first, a))
            first = b
    runs.append((first, rows[-1]))
    # The TOPMOST text run, not the biggest.  readoutBox spans a whole grid band and
    # that band carries two lines: `V:` above, `t:` below.  Taking the largest picked
    # whichever happened to be taller, and an idle timer reads 00.00S -- four zeros --
    # so the plate voltage came back as 0 V while the plates were at 129.  It went
    # unnoticed for as long as it did because allDigitsAlike takes readoutLines()[0]
    # and so was always looking at the right line, and everything else only ever asked
    # whether the panel read zero, which both lines do when the field is off.
    #
    # Height still filters: a run shorter than 60% of the tallest is an artefact -- a
    # fragment of a rule, a speck -- not a line of text.  On a single-line crop like
    # the latched readout the first run IS the tallest, so nothing changes there.
    tallest = max(r[1] - r[0] for r in runs)
    lo, hi = next(r for r in runs if (r[1] - r[0]) >= 0.6 * tallest)
    if hi - lo < 4:
        return None
    band = inner[int(lo):int(hi) + 1, :]
    return band, band > max(140, float(np.percentile(band, 88)))


def _readAt(band, hotBand, x, w):
    """Best digit and score for a w-wide window at x, cut the way templates were."""
    # A MARGIN, then the glyph nearest the window centre.  The digits are not evenly
    # spaced -- 58, 100, 139, 183 on this panel, gaps of 42, 39 and 44 -- so a uniform
    # comb cannot sit perfectly on all four and clips an edge column off one of them.
    # A 9 with two columns shaved off its left correlates best with a 2, which is
    # exactly the error this fixes.
    m = 7
    lo, hi = max(0, x - m), min(band.shape[1], x + w + m)
    if lo >= hi:
        return None, -1.0
    mask = hotBand[:, lo:hi]
    colsOn = mask.sum(axis = 0) > 0
    runs, first = [], None
    for i, v in enumerate(colsOn):
        if v and first is None:
            first = i
        elif not v and first is not None:
            runs.append((first, i))
            first = None
    if first is not None:
        runs.append((first, len(colsOn)))
    if not runs:
        return None, -1.0
    # Merge fragments: thin strokes split a glyph -- a 0 breaks into two runs on this
    # font -- and picking the nearest fragment instead of the nearest GLYPH read every
    # 0 as a 3.
    joined = []
    for a0, b0 in runs:
        if joined and a0 - joined[-1][1] < 6:
            joined[-1] = (joined[-1][0], b0)
        else:
            joined.append((a0, b0))
    runs = joined
    centre = (x + w / 2.0) - lo
    a, b = min(runs, key = lambda r: abs((r[0] + r[1]) / 2.0 - centre))
    rows = np.where(mask[:, a:b].sum(axis = 1) > 0)[0]
    if b - a < 2 or len(rows) < 3:
        return None, -1.0
    patch = band[int(rows[0]):int(rows[-1]) + 1, lo + a:lo + b]
    if patch.size == 0:
        return None, -1.0
    u = _unit(_canonical(patch))
    best, bestScore = None, -2.0
    for label, t in _digitTemplates().items():
        score = float((u * _unit(t)).sum())
        if score > bestScore:
            best, bestScore = label, score
    return best, bestScore


def _candidates(band, hotBand):
    """Character-like column runs on a panel line: (start, end) pairs.

    Segmentation is used only to PROPOSE, never to decide.  It is reliable at finding
    where ink is and unreliable at saying which ink is a digit -- the trailing V
    splits into fragments on most frames and the V: line's crop also catches the
    colon, so counting groups from either end picks the wrong four.  Matching decides.
    """
    colsOn = hotBand.sum(axis = 0) > 0
    runs, first = [], None
    for i, v in enumerate(colsOn):
        if v and first is None:
            first = i
        elif not v and first is not None:
            runs.append((first, i))
            first = None
    if first is not None:
        runs.append((first, len(colsOn)))
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] < 6:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= 4]


def _scoreRun(band, hotBand, a, b):
    """Best digit and score for one candidate run, cut the way templates were."""
    rows = np.where(hotBand[:, a:b].sum(axis = 1) > 0)[0]
    if len(rows) < 3:
        return None, -2.0
    patch = band[int(rows[0]):int(rows[-1]) + 1, a:b]
    if patch.size == 0:
        return None, -2.0
    u = _unit(_canonical(patch))
    best, bestScore = None, -2.0
    for label, t in _digitTemplates().items():
        score = float((u * _unit(t)).sum())
        if score > bestScore:
            best, bestScore = label, score
    return best, bestScore


def panelAlignment(line, gapRange = (30, 56)):
    """Which four runs on a panel line are the digits: a list of (start, end), or None.

    The digits are NOT evenly spaced -- 58, 100, 139 and 183 on this panel, gaps of
    42, 39 and 44 -- so a rigid comb cannot sit on all four and whichever one it
    misses gets a column shaved off.  A 9 with two columns clipped reads as a 2, which
    is what a comb produced on every latched-line frame.  Scoring every x position
    instead was worse: partial glyphs score well and the search found chains in empty
    space.

    So: segmentation proposes candidate runs, and a four-step dynamic program picks
    the four that READ BEST with plausible spacing between them.  Each digit keeps its
    own true position, and the sign and the trailing V are simply not chosen.

    Run ONCE per session on a median frame; reused for every frame after.
    """
    got = _glyphBand(line)
    if got is None:
        return None
    band, hotBand = got
    runs = _candidates(band, hotBand)
    if len(runs) < 4:
        return None
    scored = [(a, b) + _scoreRun(band, hotBand, a, b) for a, b in runs]
    lo, hi = gapRange
    n = len(scored)
    # best[k][i] = best total score for a chain of k+1 digits ending at candidate i
    best = [[-9.0] * n for _ in range(4)]
    back = [[-1] * n for _ in range(4)]
    for i, (_a, _b, d, sc) in enumerate(scored):
        best[0][i] = sc if d is not None else -9.0
    for k in range(1, 4):
        for i, (ai, _bi, di, sci) in enumerate(scored):
            if di is None:
                continue
            ci = (ai + _bi) / 2.0
            for j in range(i):
                aj, bj = scored[j][0], scored[j][1]
                cj = (aj + bj) / 2.0
                if best[k - 1][j] <= -8.0 or not (lo <= ci - cj <= hi):
                    continue
                cand = best[k - 1][j] + sci
                if cand > best[k][i]:
                    best[k][i] = cand
                    back[k][i] = j
    endI, endScore = -1, -9.0
    for i in range(n):
        if best[3][i] > endScore:
            endI, endScore = i, best[3][i]
    if endI < 0 or endScore <= -8.0:
        return None
    chain = [endI]
    for k in (3, 2, 1):
        chain.append(back[k][chain[-1]])
    chain = list(reversed(chain))
    return [(scored[i][0], scored[i][1]) for i in chain]


def readPanelDigits(line, alignment):
    """The four digits of a panel line at known run positions, or None."""
    if alignment is None:
        return None
    got = _glyphBand(line)
    if got is None:
        return None
    band, hotBand = got
    text = ""
    for a, b in alignment:
        if b > band.shape[1]:
            return None
        d, _sc = _scoreRun(band, hotBand, a, b)
        if d is None:
            return None
        text += d
    return text


def usingTemplates(which):
    """Read the next panel line with the template set cut from that line."""
    global _ACTIVE_TEMPLATES
    was = _ACTIVE_TEMPLATES
    _ACTIVE_TEMPLATES = which
    return was


def voltageLine(cell):
    """Just the `V:` row of the readout cell, or None.

    readoutBox spans a whole grid band, which holds TWO lines: `V:` on top and `t:`
    below it.  Everything before this only ever asked "are all four digits alike", and
    when the field is off both lines are zeros -- so picking the wrong one still gave
    the right answer, and the bug sat there invisibly.  Reading the VALUE is different:
    an idle timer shows 00.00S, which is four zeros, and the panel reported 0 V while
    the plates were at 129.
    """
    try:
        runs = readoutLines(cell)
        if not runs:
            return None
        top, bottom = runs[0]                # `V:` is the upper line
        return cell[int(top):int(bottom) + 1, :]
    except Exception:
        return None


def readPanelVolts(cell, alignment):
    """The number on the `V:` line, or None.  NEVER RAISES.

    Separate from fieldOffFromCells, which only ever needed to know whether the line
    read zero.  The hunt needs the value: the operator's low-end method toggles
    平衡/提升, which is field-on at both ends and 210 V apart, and a yes/no cannot see
    that at all.
    """
    was = usingTemplates("v")
    try:
        text = readPanelDigits(cell, alignment)
        return int(text) if text and len(text) == 4 else None
    except Exception:
        return None
    finally:
        usingTemplates(was)


def readLatchedVolts(cells, alignCells = None, report = None):
    """Majority-vote the balance voltage over many crops of the latched line.

    `alignCells` are crops of the V:/t: line from the same session.  The two lines are
    vertically aligned -- same x, same font -- and the V: line is the one the comb
    search locks onto reliably (27/27 and 21/21 on the labelled sweep), so the
    alignment is found there and applied here.

    Returns volts as an int, or None.  NEVER RAISES.
    """
    if not cells:
        if report is not None:
            report["why"] = "no panel crops were kept for this fall"
        return None
    try:
        if not _digitTemplates():
            if report is not None:
                report["why"] = "no digit templates are loaded"
            return None
        source = list(alignCells) if alignCells else list(cells)
        shapes = set(c.shape for c in source)
        median = (np.median(np.stack(source), axis = 0).astype(np.uint8)
                  if len(shapes) == 1 else source[len(source) // 2])
        alignment = panelAlignment(median)
        if report is not None:
            report["median"] = median
            report["alignment"] = alignment
        if alignment is None:
            if report is not None:
                report["why"] = "could not find four digit runs on the latched line"
            return None
        votes = {}
        for cell in cells:
            text = readPanelDigits(cell, alignment)
            if text and len(text) == 4:
                votes[text] = votes.get(text, 0) + 1
        total = sum(votes.values())
        if report is not None:
            report["votes"] = dict(votes)
            report["frames"] = total
        if total < 5:                    # too few readable frames to check agreement
            if report is not None:
                report["why"] = "only %d frames produced four digits" % total
            return None
        best = max(votes, key = votes.get)
        if votes[best] < LATCHED_AGREEMENT * total:
            if report is not None:
                report["why"] = ("frames disagree: %s won %d of %d, under %.0f%%"
                                 % (best, votes[best], total, 100 * LATCHED_AGREEMENT))
            return None                  # the frames disagree, so the glyphs are wrong
        volts = int(best)
        if report is not None and volts <= 0:
            report["why"] = "latched line read +0000V, which is not a balance voltage"
        # A droplet cannot be balanced by no field, so a latched +0000V is not a
        # balance voltage that happens to be zero -- it is the latch not holding one.
        # It read that way, unanimously, through droplet 3's third fall on
        # 2026-09-08, and charge() would reject it anyway (it raises on U <= 0).
        return volts if volts > 0 else None
    except Exception as exc:
        if report is not None:
            report["why"] = "raised %s" % (exc,)
        return None


def saveRefusedPanel(outDir, tTop, report):
    """Keep the evidence when a balance voltage could not be read.

    A live run records only the ROI, so on 2026-09-08 four of nine legal falls came
    back with no voltage and there was NOTHING to look at afterwards -- the panel is
    outside the recorded crop and the frames are gone the moment they are tracked.
    The whole diagnosis had to be guesswork about which digits were thin in the
    template set.

    So keep the one crop that matters: the median of the latched line over the fall,
    which is what the matcher actually saw, plus the vote spread that made it refuse.
    A few kB per refusal, written only for falls that passed the kinematic test,
    because those are the ones anyone will ask about.
    """
    try:
        folder = os.path.join(outDir, "panel_refused")
        os.makedirs(folder, exist_ok = True)
        stem = os.path.join(folder, "t%08.1f" % tTop)
        median = report.get("median")
        if median is not None:
            cv2.imwrite(stem + ".png", median)
        with open(stem + ".txt", "w") as f:
            f.write("start %.2f s on the capture clock\n" % tTop)
            f.write("why: %s\n" % report.get("why", "unknown"))
            f.write("alignment: %s\n" % (report.get("alignment"),))
            f.write("frames that produced four digits: %s\n" % report.get("frames"))
            for text, count in sorted(report.get("votes", {}).items(),
                                      key = lambda kv: -kv[1]):
                f.write("   %s  %d\n" % (text, count))
    except Exception as exc:            # diagnostics must never kill a session
        print("  could not save the refused panel crop: %s" % exc, flush = True)


if __name__ == "__main__":
    main(sys.argv)
