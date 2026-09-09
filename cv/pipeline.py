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
import time
import collections
import pandas as pd

import os
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
    hot = profile > floor
    out, start = [], None
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
        if perimeter == 0 or cArea < s["areaMin"] or cArea > s["areaMax"]:
            continue
        circularity = 4 * np.pi * cArea / (perimeter ** 2)
        if circularity < CIRCULARITY_MIN:
            continue
        M = cv2.moments(cnt)
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        if min(abs(cy - r) for r in s["gridRows"]) > s["gridClear"] \
           and min(abs(cx - c) for c in s["gridCols"]) > s["gridClear"] \
           and min(cx, cy, s["roiW"] - cx, s["roiH"] - cy) > s["edgeClear"]:
            centres.append((cx, cy, cArea))
    return centres, levelOtsu, len(contours)


def buildSettings(roi, spacing):
    mag = spacing / 71.5           # px/division on 150V_1, where these were measured
    return {"kernel": cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            "areaMin": 8 * mag ** 2, "areaMax": 100 * mag ** 2,
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
        self.reacquired = 0                 # times this fall was stitched across a gap
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
        if vy <= 0:                                 # only continue things that were falling
            continue
        predY = lastY + vy * elapsed
        slack = max(4.0, REID_Y_SLACK * abs(predY - lastY))
        if not (lastY - 4.0 <= y <= predY + slack):  # must be at or below where it was
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
        wide = t.miss > 0 and len(t.points) >= CONFIRM_POINTS and t.vel[1] > 0
        if wide:
            slack = max(4.0, REID_Y_SLACK * abs(aim[1] - t.pos[1]))
            yLo, yHi = t.pos[1] - 4.0, aim[1] + slack
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


def updateCrossings(t, yTop, yBottom, spacing):
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
    if t.tTop is None:
        if y0 < yTop <= y1:
            t.tTop = t0 + (yTop - y0) / (y1 - y0) * (t1 - t0)
        return None
    if y1 < yTop - 0.5 * spacing:            # drifted back up above `0`; that run is void
        t.tTop = None
        return None
    if t.tBottom is None and y0 < yBottom <= y1:
        t.tBottom = t0 + (yBottom - y0) / (y1 - y0) * (t1 - t0)
        t.seconds = t.tBottom - t.tTop
        t.legal = isLegalFall(FALL_DIVISIONS, t.seconds)
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
    yTop, yBottom = s["gridRows"][LINE_TOP], s["gridRows"][LINE_BOTTOM]
    box = readoutBox(roi)
    maxSpeed = s["maxSpeed"]            # px per second; the per-frame gate is this x dt
    panelEvery = max(1, int(round(fps / 3.0)))      # live: ~3 panel reads a second
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
    if show:
        cv2.namedWindow("Droplets",
                        cv2.WINDOW_NORMAL if blind else cv2.WINDOW_AUTOSIZE)
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
                    finished = updateCrossings(t, yTop, yBottom, spacing)
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
                        t.balanceVoltage = readLatchedVolts(
                            [cell for ct, cell in latched
                             if t.tTop <= ct <= t.tBottom]) \
                            if src.isLive else None
                        falls.append(finished)
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
                if waited <= (MAX_COAST_S if len(t.points) >= CONFIRM_POINTS
                              else probationS):
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
            shadow = [(t.pos, t.predict(now)) for t in live]
            dup2 = s["dupPx"] ** 2
            for j, c in enumerate(centres):
                if j in claimed:
                    continue
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
                for t in live:
                    if t.miss > 0:
                        continue                     # never draw a coasting ghost
                    # Speckle lives for a frame or two; a droplet being measured lives
                    # for the whole traverse.  Anything younger than DRAW_MIN_AGE_S is
                    # not worth the operator's attention and is not drawn.  A track
                    # that has already crossed `0` is always drawn, however young --
                    # that is the one being timed.
                    age = now - t.points[0][0] if t.points else 0.0
                    if age < DRAW_MIN_AGE_S and t.tTop is None:
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
                hud = "tracked %d   falls %d   legal %d" % (
                    nTracked, len(falls), sum(1 for f in falls if f.legal))
                secs = int(now - (firstT or 0))
                clock = "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)

                def drawHud(canvas):
                    cv2.putText(canvas, hud, (10, 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
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
                if show:
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
    for f in falls:
        f.fallDrift, f.fallDirection = fallDrift(f, spacing)
    return falls


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
    pd.DataFrame([{"id": f.id, "startFrame": f.fTop, "startTime": f.tTop,
                   "seconds": f.seconds,
                   "speedGridPerSec": FALL_DIVISIONS / f.seconds,
                   "reacquired": getattr(f, "reacquired", 0),
                   "kinematicLegal": getattr(f, "kinematicLegal", f.legal),
                   "fieldOff": getattr(f, "fieldOff", None),
                   "balanceVoltageV": getattr(f, "balanceVoltage", None),
                   "legal": f.legal}
                  for f in falls]).to_csv(csvPath, index = False)
    print("\nWrote", csvPath)
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
        chargeRows, perDroplet, overall = chargeMath.table(droplets, voltages)
        outDir = outDirFor(videoPath)
        os.makedirs(outDir, exist_ok = True)
        csvPath = artefact(outDir, videoPath, "charges.csv")
        pd.DataFrame([{"droplet": r["label"], "fallId": r["fallId"],
                       "seconds": r["seconds"], "balanceVoltageV": r["balanceVoltage"],
                       "radiusM": r["radiusM"], "chargeC": r["chargeC"],
                       "electrons": r["electrons"], "elementaryC": r["elementaryC"]}
                      for r in chargeRows]).to_csv(csvPath, index = False)
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

DIGIT_SIZE = (24, 22)      # canonical (rows, cols) every glyph is normalised into
_DIGITS = None


def latchedBox(roi):
    """The panel cell holding the latched balance voltage, one band below V:/t:."""
    return (roi["x"] + roi["w"], roi["rows"][1], roi["border"], roi["rows"][2])


def _digitTemplates():
    """Templates for the apparatus's panel font, built from a labelled sweep.

    test/fixtures/voltage_sweep_0907, held at 0123 / 0145 / 0167 / 0189, which between
    them contain every digit.  Fixed font in a fixed place, so this is template
    matching -- no learning, nothing to tune.
    """
    global _DIGITS
    if _DIGITS is None:
        try:
            z = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "panel_digits.npz"))
            _DIGITS = {k[1:]: z[k].astype(np.float32) for k in z.files}
        except Exception:
            _DIGITS = {}
    return _DIGITS


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
    lo, hi = max(runs, key = lambda r: r[1] - r[0])
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


def readLatchedVolts(cells, alignCells = None):
    """Majority-vote the balance voltage over many crops of the latched line.

    `alignCells` are crops of the V:/t: line from the same session.  The two lines are
    vertically aligned -- same x, same font -- and the V: line is the one the comb
    search locks onto reliably (27/27 and 21/21 on the labelled sweep), so the
    alignment is found there and applied here.

    Returns volts as an int, or None.  NEVER RAISES.
    """
    if not cells:
        return None
    try:
        if not _digitTemplates():
            return None
        source = list(alignCells) if alignCells else list(cells)
        shapes = set(c.shape for c in source)
        median = (np.median(np.stack(source), axis = 0).astype(np.uint8)
                  if len(shapes) == 1 else source[len(source) // 2])
        alignment = panelAlignment(median)
        if alignment is None:
            return None
        votes = {}
        for cell in cells:
            text = readPanelDigits(cell, alignment)
            if text and len(text) == 4:
                votes[text] = votes.get(text, 0) + 1
        return int(max(votes, key = votes.get)) if votes else None
    except Exception:
        return None


if __name__ == "__main__":
    main(sys.argv)
