"""Millikan oil-drop CV pipeline.

Detects and tracks droplets on the apparatus's CCD feed, times the `0` -> `16`
traverse to sub-frame precision, and judges each fall against the manual's own
criteria (docs/README.md section 8: 8 divisions, 16-40 s, 0.2-0.5 grid/s, and the
field off).

The public contract is `cv/api.py`.  Import from there, not from here.

CLI:
    python -m cv.pipeline <clip>            live window
    python -m cv.pipeline survey <clip>     headless, writes falls.csv
    python -m cv.pipeline all [pattern]     every clip in MILLIKAN_FOOTAGE
    python -m cv.pipeline preview           ROI preview for every clip
"""

import cv2
import numpy as np
import time
import pandas as pd

import os
import glob
import sys
try:
    from scipy.optimize import linear_sum_assignment
    HAVE_SCIPY = True
except ImportError:                     # greedy fallback, same gates
    HAVE_SCIPY = False

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


def glyphs(line):
    """segment a text line into characters.

    Counts bright pixels per column, so a grid line one or two pixels tall cannot
    pass as a glyph.  A `0` is hollow and splits into two strokes, so runs closer
    than a quarter of the line height are merged back into one character."""
    h = line.shape[0]
    _lvl, bw = cv2.threshold(line, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    cp = (bw > 0).sum(axis = 0).astype(float)
    runs = bands(cp, max(1.0, 0.18 * h))
    if not runs:
        return []
    joinGap = max(3, int(round(0.40 * h)))
    merged = [list(runs[0])]
    for a, b in runs[1:]:
        if a - merged[-1][1] <= joinGap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    minWide = max(3, int(round(0.25 * h)))
    return [(a, b, line[:, a:b]) for a, b in merged if b - a >= minWide]


def allDigitsAlike(cell):
    """Best score over every window of four consecutive glyphs: how alike are they?

    Index-free on purpose.  Counting in from either end breaks whenever the font
    renders `V` and `:` close enough to merge, or a `0` splits, which shifts every
    position.  `+0000V` is the only realistic reading with four identical glyphs
    in a row, so where they sit in the line does not matter."""
    lines = readoutLines(cell)
    if not lines:
        return None
    a, b = lines[0]
    gs = glyphs(cell[a:b, :])
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


def fieldWasOff(path, box, f0, f1, samples = FIELD_SAMPLES):
    """sample the panel across a fall; True if the field read zero"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    hits = readable = 0
    for k in range(samples):
        f = int(f0 + (f1 - f0) * k / max(1, samples - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        if not ok:
            continue
        cell = cv2.cvtColor(frame[box[1]:box[3], box[0]:box[2]], cv2.COLOR_BGR2GRAY)
        score = allDigitsAlike(cell)
        if score is None:
            continue
        readable += 1
        if score > FIELD_MATCH:
            hits += 1
    cap.release()
    if readable == 0:
        return None
    return hits / float(readable) >= FIELD_MIN_FRACTION


def artefact(outDir, path, name):
    """Artefacts are named after the clip unless the folder already carries its name.
    Keeps a one-off run from overwriting a folder's own results."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if os.path.basename(os.path.normpath(outDir)) != stem:
        name = stem + "_" + name
    return os.path.join(outDir, name)


def outDirFor(path):
    # every artefact for a clip lands in output/<clip name>/
    d = os.path.join(OUTPUT, os.path.splitext(os.path.basename(path))[0])
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


class RollingBackground:
    """Median of the frames sampled over the last `windowS` seconds."""

    def __init__(self, windowS, fps, samples = BG_SAMPLES):
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
    return {"x": x,
            "y": y,
            "w": int(round(rightBorder - TEXT_COL_DIV * spacing)) - x,
            "h": min(int(h), rows[-1] + margin) - y,
            "rows": rows,
            "spacing": spacing,
            "border": rightBorder}


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


def detectDroplets(gray, background, s, allAreas = None):
    diff = cv2.absdiff(gray, background)
    blurred = cv2.GaussianBlur(diff, (5, 5), sigmaX = 0)
    levelOtsu, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
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
        if circularity < 0.8:
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
            "gridClear": 4 * mag, "edgeClear": 20 * mag,
            "maxStep": 12 * mag,
            "gridRows": [r - roi["y"] for r in roi["rows"]],
            "gridCols": [int(round(roi["w"] - 5 * spacing))],
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
CONFIRM_POINTS = 6         # seen this many times before a track is believed;
                           # 8 loses the 17.83 s legal fall, 6 keeps both and
                           # cuts peak live tracks from 604 to 52
PROBATION_S = 0.2          # an unconfirmed track dies this fast; kills one-frame strays
AREA_TOLERANCE = 0.7       # a re-found blob may differ this much in area
SCREEN_MIN, SCREEN_MAX = 0.15, 0.60     # provisional band, deliberately wider than legal


class Track:
    nextId = 0

    def __init__(self, centre, frame):
        Track.nextId += 1
        self.id = Track.nextId
        self.pos = np.array(centre[:2], dtype = float)
        self.vel = np.zeros(2)
        self.area = centre[2]
        self.miss = 0
        self.points = [(frame, float(centre[1]))]
        self.xs = [float(centre[0])]        # parallel to points; only fallDrift reads it
        self.fTop = None
        self.fBottom = None
        self.seconds = None
        self.legal = None
        self.colour = tuple(int(v) for v in np.random.randint(90, 256, 3))

    def predict(self):
        return self.pos + self.vel * (self.miss + 1)

    def update(self, centre, frame, maxStep):
        p = np.array(centre[:2], dtype = float)
        self.vel = 0.75 * self.vel + 0.25 * (p - self.pos) / (self.miss + 1)
        fast = np.hypot(self.vel[0], self.vel[1])
        if fast > maxStep:                      # never let a bad match fling the prediction
            self.vel *= maxStep / fast
        self.pos, self.area, self.miss = p, centre[2], 0
        self.points.append((frame, float(centre[1])))
        self.xs.append(float(centre[0]))

    def recentSpeed(self, spacing, fps, window = 30):
        # provisional speed in grid divisions per second, from the last ~1 s of points
        pts = self.points[-window:]
        if len(pts) < 8:
            return None
        f = np.array([p[0] for p in pts], dtype = float)
        y = np.array([p[1] for p in pts], dtype = float)
        if f[-1] == f[0]:
            return None
        return float(np.polyfit(f, y, 1)[0]) * fps / spacing


def assign(tracks, centres, maxStep):
    # one global assignment, so two tracks can never claim the same blob
    if not tracks or not centres:
        return {}
    BIG = 1e6
    cost = np.full((len(tracks), len(centres)), BIG)
    for i, t in enumerate(tracks):
        aim = t.predict()
        for j, c in enumerate(centres):
            d = float(np.hypot(c[0] - aim[0], c[1] - aim[1]))
            if d > maxStep:
                continue
            areaDiff = abs(c[2] - t.area) / max(t.area, 1.0)
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


def updateCrossings(t, yTop, yBottom, fps, spacing):
    # returns the track when it has just completed a full `0` -> `16` traverse
    if len(t.points) < 2:
        return None
    (f0, y0), (f1, y1) = t.points[-2], t.points[-1]
    if t.fTop is None:
        if y0 < yTop <= y1:
            t.fTop = f0 + (yTop - y0) / (y1 - y0) * (f1 - f0)
        return None
    if y1 < yTop - 0.5 * spacing:            # drifted back up above `0`; that run is void
        t.fTop = None
        return None
    if t.fBottom is None and y0 < yBottom <= y1:
        t.fBottom = f0 + (yBottom - y0) / (y1 - y0) * (f1 - f0)
        t.seconds = (t.fBottom - t.fTop) / fps
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
    if t.fTop is None or t.fBottom is None or not t.seconds or spacing <= 0:
        return None, None
    xs = [x for (f, _y), x in zip(t.points, t.xs) if t.fTop <= f <= t.fBottom]
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
             bgFromRange = False, videoFrom = None, videoTo = None):
    """bgWindowS = None keeps the whole-clip background, i.e. every result produced
    before 2026-08-28.  A number switches to a rolling window of that many seconds."""
    path = resolveClip(path)
    outDir = outDirFor(path) if outDir is None else outDir
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError("OpenCV could not open %s -- codec or permissions?" % path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if toFrame is None:
        toFrame = total
    # autoROI keeps the WHOLE-clip median on purpose: the grid is static for the
    # entire recording, so a global median finds it better than any window would.
    fullBackground = buildBackground(path)
    roi = autoROI(fullBackground)
    if bgFromRange:            # what analysing a trimmed file does: median of the segment
        fullBackground = buildBackground(path, start = fromFrame, end = toFrame)
    if roi is None:
        print("Auto-ROI failed: no grid in frame.")
        if health is not None:
            health["roiOk"] = False
        cap.release()
        return []
    X, Y, W, H = roi["x"], roi["y"], roi["w"], roi["h"]
    spacing = roi["spacing"]
    s = buildSettings(roi, spacing)
    background = fullBackground[Y:Y+H, X:X+W]
    yTop, yBottom = s["gridRows"][LINE_TOP], s["gridRows"][LINE_BOTTOM]
    box = readoutBox(roi)
    maxStep = s["maxStep"] * (30.0 / fps)
    coast = int(round(MAX_COAST_S * fps))
    probation = max(2, int(round(PROBATION_S * fps)))
    print("%s\n  ROI %d,%d,%d,%d  spacing %.1f  `0` y=%d  `16` y=%d  %d divisions"
          % (os.path.basename(path), X, Y, W, H, spacing, yTop, yBottom,
             LINE_BOTTOM - LINE_TOP))

    writer = None
    if saveVideo:
        writer = cv2.VideoWriter(artefact(outDir, path, "annotated_multi.mp4"),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    if show:
        cv2.namedWindow("Droplets")

    roller = None
    if bgWindowS:
        roller = RollingBackground(bgWindowS, fps)
        roller.prime(path, (Y, H, X, W), fromFrame, toFrame)
        print("  rolling background: %.0f s window, %d samples, every %d frames"
              % (bgWindowS, len(roller.buf), roller.every), flush = True)

    live, falls, n = [], [], fromFrame
    detections = 0
    if fromFrame:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fromFrame)
    start = time.perf_counter()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        if n > toFrame:
            break
        crop = frame[Y:Y+H, X:X+W]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if roller is not None:
            roller.push(gray, n)
            background = roller.get(n)
        centres, _, _ = detectDroplets(gray, background, s)
        detections += len(centres)

        pairs = assign(live, centres, maxStep)
        for i, t in enumerate(live):
            if i in pairs:
                t.update(centres[pairs[i]], n, maxStep)
                finished = updateCrossings(t, yTop, yBottom, fps, spacing)
                if finished is not None:
                    t.kinematicLegal = t.legal
                    t.fieldOff = None
                    if t.legal:                  # only speed-legal falls need the check
                        t.fieldOff = fieldWasOff(path, box, t.fTop, t.fBottom)
                        t.legal = bool(t.fieldOff)
                    falls.append(finished)
                    print("  frame %6d  id %-5d  %6.2f s  %6.3f grid/s  %s"
                          % (n, t.id, t.seconds, FALL_DIVISIONS / t.seconds,
                             "LEGAL" if t.legal else
                             ("field ON - rejected" if t.kinematicLegal else "rejected")),
                          flush = True)
            else:
                t.miss += 1
        live = [t for t in live
                if t.miss <= (coast if len(t.points) >= CONFIRM_POINTS else probation)]
        claimed = set(pairs.values())
        for j, c in enumerate(centres):
            if j not in claimed:
                live.append(Track(c, n))

        if show or writer is not None:
            marked = crop.copy()
            cv2.line(marked, (0, yTop), (W, yTop), (255, 255, 0), 1)
            cv2.line(marked, (0, yBottom), (W, yBottom), (255, 255, 0), 1)
            for t in live:
                if t.miss > 0:
                    continue                     # never draw a coasting ghost
                p = (int(round(t.pos[0])), int(round(t.pos[1])))
                if len(t.points) < CONFIRM_POINTS:
                    cv2.circle(marked, p, 2, (120, 120, 120), -1)    # unconfirmed: a dot
                    continue
                v = t.recentSpeed(spacing, fps)
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
                if t.fBottom is not None:                    # measured: freeze it
                    done = (0, 255, 0) if t.legal else (0, 0, 255)
                    cv2.circle(marked, p, 13, done, 1)
                    cv2.putText(marked, "%.2f s" % t.seconds,
                                (p[0] + 16, p[1] + 13),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, done, 1)
                elif t.fTop is not None:                     # clock is running
                    cv2.circle(marked, p, 13, (0, 255, 255), 1)
                    cv2.putText(marked, "%.1f s" % ((n - t.fTop) / fps),
                                (p[0] + 16, p[1] + 13),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            shown = sum(1 for t in live
                        if t.miss == 0 and len(t.points) >= CONFIRM_POINTS)
            cv2.putText(marked, "tracked %d   falls %d   legal %d"
                        % (shown, len(falls), sum(1 for f in falls if f.legal)),
                        (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            secs = int(n / fps)
            clock = "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)
            (tw, _th), _ = cv2.getTextSize(clock, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.putText(marked, clock, (W - tw - 10, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
            if writer is not None and (videoFrom is None or videoFrom <= n)                     and (videoTo is None or n <= videoTo):
                writer.write(marked)      # track over a wide span, record a narrow one
            if show:
                cv2.imshow("Droplets", marked)
                targetTime = start + n / fps
                delay = max(1, int(round((targetTime - time.perf_counter()) * 1000)))
                if cv2.waitKey(delay) == ord("q"):
                    print("Stopped at frame", n)
                    break
        if not show and n % 3000 == 0:
            print("  frame %d/%d (%.0f%%)  live %d  falls %d"
                  % (n, toFrame, 100.0 * (n - fromFrame) / max(1, toFrame - fromFrame),
                     len(live), len(falls)), flush = True)

    cap.release()
    if writer is not None:
        writer.release()
    if show:
        cv2.destroyAllWindows()

    if health is not None:
        health.update({"bgWindowS": bgWindowS,
                       "fromFrame": fromFrame,
                       "toFrame": toFrame,
                       "roiOk": True,
                       "gridSpacingPx": round(spacing, 1),
                       "detectionsPerFrame": round(detections / max(1, n), 2),
                       "fps": fps,
                       "droppedFrames": 0,          # a file drops nothing; live will
                       "framesRead": n})
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
        print("\n%6s %10s %9s %11s %8s" % ("id", "start_f", "time(s)", "speed(gr/s)", "legal"))
        for f in sorted(falls, key = lambda f: -f.seconds)[:25]:
            print("%6d %10.0f %9.2f %11.3f %8s"
                  % (f.id, f.fTop, f.seconds, FALL_DIVISIONS / f.seconds, f.legal))
    csvPath = artefact(outDir, path, "falls.csv") if outDir and path \
        else (os.path.join(outDir, "falls.csv") if outDir else "falls.csv")
    pd.DataFrame([{"id": f.id, "startFrame": f.fTop, "seconds": f.seconds,
                   "speedGridPerSec": FALL_DIVISIONS / f.seconds,
                   "kinematicLegal": getattr(f, "kinematicLegal", f.legal),
                   "fieldOff": getattr(f, "fieldOff", None),
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
            "startFrame": f.fTop,
            "seconds": f.seconds,
            "speedGridPerSec": FALL_DIVISIONS / f.seconds,
            "kinematicLegal": getattr(f, "kinematicLegal", f.legal),
            "fieldOff": getattr(f, "fieldOff", None),
            "legal": f.legal,
            "fallDrift": getattr(f, "fallDrift", None),
            "fallDirection": getattr(f, "fallDirection", None),
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


def analyze(videoPath = VIDEO, binding = None, show = False, saveVideo = False):
    """One measurement session: health, droplets, and each droplet's falls.

    `binding` groups fall ids into droplets, e.g. [[51143, 51890], [60011]].
    Nothing in a recording says which ids belong together -- IV.4 re-drops the same
    droplet five times and the tracker loses it during each motionless hold, so one
    droplet yields several ids.  Only the operator knows.  Guessing it would file
    two droplets as one and the resulting charge would look perfectly normal.
    With no binding, `droplets` comes back empty and you want survey().
    """
    health = {"roiOk": False}
    rows = survey(videoPath, health = health, show = show, saveVideo = saveVideo)
    byId = {r["id"]: r for r in rows}

    droplets = []
    for k, group in enumerate(binding or [], 1):
        falls = [byId[i] for i in group if i in byId]
        droplets.append({"id": group[0] if group else None,
                         "label": "Droplet %d" % k,
                         "fallCount": len(falls),
                         "falls": falls})

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


if __name__ == "__main__":
    main(sys.argv)
