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
import json
import glob
import sys
try:
    from scipy.optimize import linear_sum_assignment
    HAVE_SCIPY = True
except ImportError:                     # greedy fallback, same gates
    HAVE_SCIPY = False

try:
    from .source import openSource, isCameraSpec, FRAMES_PREFIX, LiveSource
    from . import charge as chargeMath
except ImportError:                     # run as a loose script rather than a package
    from source import openSource, isCameraSpec, FRAMES_PREFIX, LiveSource
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
# Two falls taken at different plate voltages are not one measurement: the operator
# turned the knob between them, which is the definition of a different droplet or a
# different attempt.  On 2026-09-16 two falls at 218 V and one at 150 V, ten minutes
# apart, were filed as one droplet and charges.csv reported its charge at "U_n 175 V".
# A volt of tolerance covers the panel's own jitter, nothing more.
SAME_DROPLET_VOLT_TOL = 1.5
# ...and five falls of one droplet arrive minutes apart, not ten.  The same session put
# a spray, a re-tune and 580 s between two falls it then bound together.  Ⅳ.4 re-drops
# the SAME droplet five times; nobody leaves it sitting for ten minutes in between.
SAME_DROPLET_GAP_S = 180.0

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


# How many times a fall may lose its droplet and take it back and still be a
# measurement of ONE droplet.
#
# On 2026-09-21 the aid recorded two legal falls, 32.9 s and 37.4 s, both inside
# the manual's band and both with the field confirmed off. The operator filmed the
# screen at the moment each completed: at the first there was a ring that flashed
# "38.3 s" on something invisible before and after, and at the second the chamber
# was empty. They were stitched out of noise -- and they needed 8 and 13
# re-acquisitions to do it.
#
# The five reference falls in the gate need 0 or 1. The fixture's own two junk
# traverses need 14 and 17. The separation is not marginal, and nothing else in
# the fall's numbers distinguishes them: duration, speed and field state all look
# like a droplet, because a chain of blobs down one column is arithmetically
# indistinguishable from a droplet unless you ask how it was assembled.
REACQUIRE_MAX_FOR_LEGAL = 5


def isLegalFall(divisions, seconds, reacquired = 0):
    if seconds <= 0:
        return False
    if reacquired > REACQUIRE_MAX_FOR_LEGAL:
        return False                    # see REACQUIRE_MAX_FOR_LEGAL
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


# Publishing frames to whoever launched us, instead of being screen-captured.
#
# The app used to mirror this window with PrintWindow, which costs about 55 ms a frame
# against the 4.3 ms it costs to JPEG-encode the same picture -- so the browser was
# capped near 17 fps while the window ran at 23, and the difference was pure Windows
# re-rendering a window we had already drawn.
#
# The socket is OUTBOUND: the app listens, we connect.  A measurement process should
# not be binding ports, waiting on firewall prompts or handling a port conflict
# mid-session.  One slot, newest wins: if the consumer is slow the browser loses a
# frame, which costs nothing, rather than the tracker being held up, which costs a
# measurement.
PUBLISH_QUALITY = 80
PUBLISH_EVERY = 2               # JPEG costs 3.3 ms; the browser does not need 30 fps
_publishSlot = [None]
_publishWake = threading.Event()

LIVE_STATUS_S = 0.4             # how often the live status file is rewritten.  The
                                # app shows this as its own banner, so at 1 s it read
                                # visibly behind the window it mirrors.
_liveStatusAt = [0.0]


DARK_EARLY_S = 2.0              # ...and how long its prediction is worth trusting
DARK_LOOK_PX = 30.0             # how near the prediction a detection still counts
                                # as "the droplet is right there" while a track is
                                # dark.  See darkTally, printed when a measuring
                                # track is given up on.

CONTENT_DARK = 16               # a pixel this dark carries no picture...
CONTENT_SHARE = 0.005           # ...and a line with fewer lit than this is a black bar.
                                # Deliberately timid: the apparatus's `V:` panel is
                                # sparse bright text on black and sits OUTSIDE the ROI,
                                # so anything less cautious would crop the reading the
                                # operator balances against.


def contentBox(img, roi):
    """The apparatus picture inside the captured frame, as (x, y, w, h).

    The dongle delivers the microscope's 4:3 picture inside a 16:9 frame, so every
    frame arrives with a black bar down each side.  In the pipeline's own window
    that costs nothing, but the app draws the same frame into a panel beside the
    text, where the bars are most of the width and the operator called it dead
    space.  This is the picture with the bars cut off -- the WHOLE picture, grid
    and `V:` panel included, not the measured ROI.

    The ROI is always inside the result: a dim apparatus must never crop away the
    region the measurement is made in.
    """
    # A SHARE of the line, not its brightest pixel: one hot pixel or a speck of
    # sensor noise in the bar would otherwise keep the whole bar.
    grey = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lit = grey > CONTENT_DARK
    cols = np.where(lit.mean(axis = 0) > CONTENT_SHARE)[0]
    rows = np.where(lit.mean(axis = 1) > CONTENT_SHARE)[0]
    x, y, w, h = roi
    if len(cols) == 0 or len(rows) == 0:
        return (0, 0, int(grey.shape[1]), int(grey.shape[0]))
    x0, x1 = min(int(cols[0]), x), max(int(cols[-1]) + 1, x + w)
    y0, y1 = min(int(rows[0]), y), max(int(rows[-1]) + 1, y + h)
    return (x0, y0, x1 - x0, y1 - y0)


def publishFrame(display):
    """Hand the drawn frame to the publisher thread, if one is running."""
    if _publishPort[0] is None:
        return
    _publishSlot[0] = display            # newest wins; a dropped frame is not a fault
    _publishWake.set()


_publishPort = [None]


def startPublisher(port):
    """Connect back to the launcher and stream JPEG frames, length-prefixed."""
    import socket as _socket
    _publishPort[0] = int(port)

    def run():
        try:
            sock = _socket.create_connection(("127.0.0.1", int(port)), timeout = 5.0)
        except OSError as exc:
            print("  frame publishing off: %s" % exc, flush = True)
            _publishPort[0] = None
            return
        print("  publishing frames to 127.0.0.1:%s" % port, flush = True)
        try:
            while True:
                _publishWake.wait(1.0)
                _publishWake.clear()
                frame = _publishSlot[0]
                _publishSlot[0] = None
                if frame is None:
                    continue
                ok, buf = cv2.imencode(".jpg", frame,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), PUBLISH_QUALITY])
                if not ok:
                    continue
                payload = buf.tobytes()
                sock.sendall(len(payload).to_bytes(4, "big") + payload)
        except Exception:
            pass                          # the viewer went away; the session has not
        finally:
            _publishPort[0] = None
            try:
                sock.close()
            except Exception:
                pass

    threading.Thread(target = run, daemon = True).start()


def liveStatusPath():
    """Where a running session publishes what it is doing, for the app to read.

    A file, not a socket or a pipe: it survives the app restarting, it needs no
    handshake, and a reader that arrives late still gets the current state.  The app
    launches the pipeline as a subprocess and mirrors its window, so until now the
    only thing it knew was whether that window existed -- which is why a browser
    watching a live session sat on a blank box for the thirty seconds of background
    learning with nothing to show.
    """
    os.makedirs(OUTPUT, exist_ok = True)
    return os.path.join(OUTPUT, "live_status.json")


_liveOwner = [False]            # has THIS process claimed the status file?


def writeLiveStatus(**fields):
    """Publish the current state, at most once a second.  Never raises.

    Only a run that has published a `learning` or `running` state owns the file.
    An offline analysis ends by calling report() like any other run, and its
    "Session finished" would otherwise land on top of a live session that is
    still going -- the app would show the operator's own run as over.
    """
    now = time.time()
    phase = fields.get("phase")
    if phase in ("learning", "running"):
        _liveOwner[0] = True
    elif not _liveOwner[0]:
        return
    if phase != "learning" and now - _liveStatusAt[0] < LIVE_STATUS_S:
        return
    _liveStatusAt[0] = now
    fields["updatedAt"] = now
    try:
        tmp = liveStatusPath() + ".tmp"
        with open(tmp, "w", encoding = "utf-8") as f:
            json.dump(fields, f)
        os.replace(tmp, liveStatusPath())       # readers never see half a file
    except Exception:
        pass                                    # a status file is never load-bearing


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
# A blob where a live track is already pointing carries a prior that no shape gate can
# match.  Small faint droplets -- 6 to 16 px, circularity sitting on the 0.52 gate --
# blink out of the detector for seconds at a time, and a track that dies mid-fall takes
# the measurement with it: two falls were lost that way on 2026-09-14.
#
# Relaxing all three gates at once FAILED the acceptance gate -- two reference falls
# lost, two more shifted -- so each was gated alone on one_droplet_slow:
#
#   area floor x0.6   keeps all 5 reference falls, 50 traverses vs 46, timing identical
#   grid mask off     LOSES 33.47 s                                        -> rejected
#   circularity off   keeps all 5, but traverses FALL to 44 -- a relaxation that finds
#                     fewer falls is not a fix                             -> rejected
#
# So only the area floor relaxes, and only for a contour sitting where a confirmed
# track is already pointing.  Shape and the grid mask are unchanged: a droplet may be
# fainter than the hunt would accept, but it must still look like a droplet and must
# still be off the lines.  The static-mark mask, the area ceiling, the panel margin and
# the frame edge never relax at all.
SECOND_CHANCE_PX = 14.0
SECOND_CHANCE_GROW = 12.0     # px of radius per second a track has been missing
SECOND_CHANCE_MAX_PX = 30.0
SECOND_CHANCE_AREA_FRAC = 0.6
# A track whose CLOCK IS RUNNING carries a measurement nothing else can reproduce, and
# it knows where its droplet is to within a few pixels.  Inside that radius the gates
# are answering a question that has already been answered, so for that track only they
# relax further: the area floor drops again, and the grid mask does not apply -- the
# `16` line IS a grid row, so the one moment a fall must be seen is a moment the mask
# blinds us.  Circularity still applies: a smear that is not round is not the droplet.
#
# Gated one relaxation at a time on one_droplet_slow, for a mid-fall track within
# 15 px:
#
#   area floor x0.35   keeps all 5 reference falls, 50 traverses vs 46   -> shipped
#   grid mask off      LOSES 33.44 s, 53 traverses                       -> rejected
#   both               LOSES 33.44 s                                     -> rejected
#
# So the area floor relaxes and the grid mask does not -- not for any track, by any
# rule tried, however narrow.  A droplet parked on a line stays invisible, and that
# stays an operator rule: park just above `0`, not on it.
MEASURE_RESCUE_PX = 15.0
MEASURE_RESCUE_AREA_FRAC = 0.35
RECOVERED = [0]
# Where this frame's rejected contours were, and why they were rejected.  Cleared every
# frame, so it costs one list per frame and answers the only question that matters when
# a track dies mid-fall: was the droplet INVISIBLE, or was it seen and gated out?  The
# area floor is 5.8 px at the bench magnification and the tracks that die were carrying
# 4 to 16 px, so the area gate cannot be the whole story -- but "nothing was detected
# nearby" and "a contour was there and circularity refused it" need opposite fixes, and
# until now the log could not tell them apart.
REJECT_POINTS = []

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
    if RECOVERED[0]:
        out.append("")
        out.append("kept beside a live track, under the area floor: %d"
                   % RECOVERED[0])
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


def nearestRejected(x, y):
    """(distance, reason, area, circularity) for the rejected contour nearest here."""
    if not REJECT_POINTS:
        return None
    best, bestD = None, None
    for cx, cy, why, area, circ in REJECT_POINTS:
        d = (cx - x) ** 2 + (cy - y) ** 2
        if bestD is None or d < bestD:
            best, bestD = (d ** 0.5, why, area, circ), d
    return best


def _nearTrack(cx, cy, keepNear):
    """Is this contour where a confirmed track is already pointing?"""
    for x, y, r in keepNear:
        if (cx - x) ** 2 + (cy - y) ** 2 <= r * r:
            return True
    return False


def detectDroplets(gray, background, s, allAreas = None, keepNear = None,
                   measuring = None):
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
    del REJECT_POINTS[:]
    centres = []
    for cnt in contours:
        cArea = cv2.contourArea(cnt)
        if allAreas is not None:
            allAreas.append(cArea)
        perimeter = cv2.arcLength(cnt, True)
        M = cv2.moments(cnt)
        if perimeter == 0 or M["m00"] == 0:
            _reject("degenerate")
            continue
        # The centroid is needed before the gates now, because which gates apply
        # depends on whether a track is already pointing here.
        cx, cy = M["m10"] / M["m00"], M["m01"] / M["m00"]
        near = bool(keepNear) and _nearTrack(cx, cy, keepNear)
        timing = bool(measuring) and _nearTrack(cx, cy, measuring)
        areaFloor = s["areaMin"]
        if timing:
            areaFloor *= MEASURE_RESCUE_AREA_FRAC
        elif near:
            areaFloor *= SECOND_CHANCE_AREA_FRAC
        circularity = 4 * np.pi * cArea / (perimeter ** 2)
        if cArea < areaFloor:
            _reject("area<min")
            REJECT_POINTS.append((cx, cy, "area<min", cArea, circularity))
            continue
        if cArea > s["areaMax"]:
            _reject("area>max")
            REJECT_POINTS.append((cx, cy, "area>max", cArea, circularity))
            continue
        if circularity < CIRCULARITY_MIN:
            _reject("circularity", cArea, circularity)
            REJECT_POINTS.append((cx, cy, "circularity", cArea, circularity))
            continue
        if near and cArea < s["areaMin"]:
            RECOVERED[0] += 1
        mask = s.get("staticMask")
        if mask is not None:
            iy, ix = int(round(cy)), int(round(cx))
            if 0 <= iy < mask.shape[0] and 0 <= ix < mask.shape[1] and mask[iy, ix]:
                _reject("static mark")
                REJECT_POINTS.append((cx, cy, "static mark", cArea, circularity))
                continue                # sitting on something the apparatus drew
        onGrid = (min(abs(cy - r) for r in s["gridRows"]) <= s["gridClear"]
                  or (s["gridCols"]
                      and min(abs(cx - c) for c in s["gridCols"]) <= s["gridClear"]))
        inPicture = (cx < s["roiW"] - s["panelClear"]
                     and min(cx, cy, s["roiW"] - cx, s["roiH"] - cy) > s["edgeClear"])
        if inPicture and not onGrid:
            if timing and cArea < s["areaMin"]:
                RECOVERED[0] += 1
            centres.append((cx, cy, cArea))
        else:
            if onGrid and min(abs(cy - r) for r in s["gridRows"]) <= s["gridClear"]:
                why = "grid row"
            elif s["gridCols"] and min(abs(cx - c) for c in s["gridCols"]) <= s["gridClear"]:
                why = "grid col"
            elif cx >= s["roiW"] - s["panelClear"]:
                why = "panel margin"
            else:
                why = "frame edge"
            _reject(why)
            REJECT_POINTS.append((cx, cy, why, cArea, circularity))
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
# Raised from 6.0 on 2026-09-14.  A droplet whose circularity sits on the gate blinks
# out for seconds at a time -- one fall that session lost its track six times and was
# never recorded -- and a track that is mid-fall has nothing to gain from dying early:
# it is reserved its corridor while it coasts, and the x-gate that takes the droplet
# back is 14 px, so a longer wait cannot pick up a neighbour.
MEASURING_COAST_S = 12.0
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
# ...except for a track whose CLOCK IS RUNNING and which has gone dark.  Measured on
# one_droplet_slow: of every frame where a detection the pipeline had already accepted
# sat within 30 px of such a track's prediction and was not given to it, 74% were
# refused by the area test alone -- against 10% taken by another track.  The test is
# self-defeating there: a droplet goes dark BECAUSE it dimmed or bloomed, and then has
# to come back the size it was before it did.  Everything else still applies: the x
# window, the y window along the axis of travel, and the measuring track's priority.
MEASURING_AREA_TOLERANCE = 2.5
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
        self.voltageFrom = None     # where its balance voltage was read from
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
        return chargeFromBuckets(self.byVolts)

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
        # DROP THE FRAMES WHERE A GRID LINE IS UNDER THE DROPLET.  The reticle is
        # drawn into the video, so while a droplet overlaps a line the bright line
        # is inside its contour and drags the centroid: the operator noticed that
        # slow droplets' readings surge as they cross, and on 2026-09-21 one such
        # surge to 0.21 grid/s was enough to call a droplet that had been falling
        # at about 0.15 USABLE. The bias is an artefact of the picture, not motion.
        #
        # Only if enough is left to fit: near the `0` line a balanced droplet sits
        # ON a line for minutes, and refusing to measure it at all would be worse
        # than measuring it with the bias.
        rows = getattr(self, "gridRows", None)
        if rows:
            clear = [p for p in pts
                     if min(abs(float(p[1]) - r) for r in rows) > GRID_BIAS_PX]
            if len(clear) >= 8:
                pts = clear
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
            tol = (MEASURING_AREA_TOLERANCE
                   if (t.miss > 0 and t.tTop is not None and t.tBottom is None)
                   else AREA_TOLERANCE)
            if areaDiff > tol:
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
# PHANTOM DETECTIONS remain open, and not for want of trying. An operator watched
# seventeen rings with running fall clocks sitting on empty background on
# 2026-09-20 while the real specks beside them carried none. Two attempts to
# require a track to move like a droplet before its clock counts -- one tested at
# the `0` crossing, one a second after it -- both failed the gate outright: the
# first took the fixture from 46 traverses to 2, the second lost four of the five
# reference falls. The reason is the same both times: a legal fall here is slow,
# 0.2-0.3 grid/s, so a droplet covers ~15 px in the second being examined and
# detector noise dominates the fit. A straight-line test cannot separate them at
# that scale, and no threshold on it will.
#
# What would: a detector that scores a blob's SHAPE against a droplet's point
# spread rather than its area and roundness. That is a replacement for the
# decision rule, not a tuning of it -- see cv/DESIGN_NOTES.md.

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

# ...and the same question asked as a RATIO, which is what actually works.
#
# The difference test has a dead band: below 0.10 grid/s of separation and above
# 0.02 it answers "unclear", and on 2026-09-14 twenty-six droplets out of twenty-six
# landed there.  An operator watched one droplet ignore every voltage change for two
# thirds of a session and it was never crossed out.  The reason is that the difference
# between two noisy speeds is noisier than either, while their RATIO is not: a droplet
# still falling at three quarters of its free-fall speed with the plates at 250 V is
# not being held, whatever the difference happens to measure that second.
CHARGE_HELD_FRACTION = 0.75        # still this fast at the higher voltage = not held

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


MEASURED_FREE = {}          # column key -> descent rates from completed legal falls


def noteMeasuredFree(x, seconds):
    """A legal fall IS the free-fall speed: 8 divisions in a measured time, field off.

    Everything on the screen is a fraction of v_free -- the balance percentage, the
    Brownian floor, "barely held" -- and until now v_free was a median of smoothed
    per-frame velocities pooled over the column.  That estimate ran low, and a low
    v_free inflates every percentage computed from it: an operator was told a droplet
    sitting at 163 V was "4.1% out, the Brownian limit over 45 s" when the floor for a
    droplet falling 0.47 gr/s over 45 s is nearer 0.9%, and was told another was
    "barely held" while it was almost stationary.

    A completed traverse needs no fitting and no pooling: it is the manual's own
    measurement, 8 divisions against a clock.
    """
    if not seconds or seconds <= 0:
        return
    key = int(round(float(x) / CHARGE_COLUMN_PX))
    rates = MEASURED_FREE.setdefault(key, [])
    rates.append(FALL_DIVISIONS / float(seconds))
    if len(rates) > 8:
        del rates[0]


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
    key = int(round(float(x) / CHARGE_COLUMN_PX))
    # A measured fall outranks any fit.  Neighbouring columns too: a droplet drifts
    # sideways across a traverse and the pin follows the group, not the pixel.
    measured = []
    for k in (key - 1, key, key + 1):
        measured += MEASURED_FREE.get(k, [])
    if measured:
        return float(np.median(measured))
    if columns is None:
        return None
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
    # See reachableBalance: unknown means the top of the band, not its middle.
    free = MAX_SPEED if vFree is None else float(vFree)
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

# How well the droplet has to be balanced before its falls are worth trusting.
#
# BALANCED_MAX is the "it has stopped moving" test, and it is far too generous to
# MEASURE against.  A residual drift v at plate voltage U means the true balance is
# U / (1 - v/v_free), so the charge comes out wrong by about v/v_free: 0.02 grid/s on
# a droplet that free-falls at 0.30 is a 7% error -- the size of the spread between
# our own eleven droplets, and invisible to the eye (a third of a division in 30 s).
#
# So do not tighten BALANCED_MAX, which would stop the aid ever saying "settled".
# Measure the residual drift instead and report it as the percentage it will cost.
BALANCE_TARGET_PCT = 2.0        # implied error in q to aim under before timing
BALANCE_FIT_S = 45.0            # the drift is fitted over at most this much of the hold
BOLTZMANN = 1.380649e-23
LAB_TEMPERATURE_K = 298.0       # 25 C, what the room reads; only the Brownian floor
                                # uses it, never the charge
BALANCE_MIN_FIT_S = 8.0         # ...and a shorter hold than this cannot say anything
BALANCE_CHECK_S = 0.5           # how often it is recomputed, per droplet
BALANCE_SHOW_S = 2.0            # ...and how often the screen is allowed to change
SETTLE_AFTER_V_S = 2.0          # no verdict this soon after the voltage moved
PANEL_LAG_S = 3.0               # how long after a reading changes the switch may have
                                # moved first -- OCR of a display that redraws slowly
HOLD_SLACK_DIV = 0.35           # a held droplet may wander this far and still be held
# Past this, "off balance by N%" is not a useful number -- 571% was shown on a droplet
# in free fall.  Beyond it the droplet is not near balance at all, so give the
# instruction and drop the arithmetic.
BALANCE_FAR_PCT = 50.0
# The implied balance voltage is U / (1 - v/v_free), which near balance amplifies every
# wobble in v: an operator at 250 V was told to LOWER to 220, tuned to 224, was told to
# LOWER to 200, then to RAISE to 250, then at 247 to LOWER to 220 again.  Each answer
# was arithmetically right about a different second of Brownian motion.  So take the
# median of the last few seconds of estimates, and say nothing when the move being
# suggested is smaller than the noise on it.
BALANCE_TARGET_MEMORY_S = 12.0
# Past this far out, U / (1 - v/v_free) is still exact arithmetic and no longer advice:
# v is then most of the free-fall speed, so the answer swings hundreds of volts between
# frames.  An operator tuning a droplet that balanced at 169 V was sent to 43 V, 93 V,
# 301 V and 364 V on consecutive seconds.  Beyond this, say which way and stop there.
BALANCE_ADVICE_MAX_PCT = 40.0
# A Brownian floor larger than this is not a floor, it is a window too short to fit:
# the aid called 28.8% "as good as it gets" because a 2 s window makes the floor huge.
BALANCE_FLOOR_MAX_PCT = 4.0
# How far out a droplet already called balanced must drift before it is called off
# balance again, as a multiple of the target.
BALANCE_HYSTERESIS = 1.8
# ...and how much settled hold the Brownian-limit claim needs.  Over a 9 s window the
# floor lands near its cap, so "that is the Brownian limit, tuning cannot do better"
# was said about a droplet sitting 4.8% out.  Over 20 s the floor is a real floor.
FLOOR_CLAIM_MIN_S = 20.0
# 提升 sits far above the balance band, so a reading up here is a lift whatever the
# latch thinks.  Balance means nothing while the operator is carrying the droplet up.
LIFT_VOLTS = 300.0
# The fall-speed verdict is taken on a median over this long, and must cross the gate
# by this much to flip.  Raw frame-to-frame speed sat on the 0.2 gr/s gate and the
# banner alternated USABLE / DISCARD every two seconds on one droplet.
SPEED_MEDIAN_S = 5.0
SPEED_GATE_SLACK = 0.15
SPEED_UNSTABLE_GRID_S = 0.20    # a speed swinging wider than this is not a reading
SPEED_FIRST_VERDICT_S = 1.5     # ...and one measured over less time than this is not
                                # a reading either.  A droplet cut to 0 V was called
                                # "0.02 gr/s -- too slow, DISCARD" a second after the
                                # switch, off a window that still held the seconds it
                                # spent balanced.  Reversing a verdict already needed
                                # this long; making the FIRST one is no cheaper.
STILL_GRID_S = 0.05             # slower than this, with the field off, is not falling
                                # -- the pair that produced DISCARD then USABLE two
                                # seconds apart spanned 0.24
# How long a droplet must go on being unbalanceable before it is crossed out.
#
# "Uncharged" needs the droplet seen at TWO voltages, and a droplet falling at full
# speed leaves the picture before the operator can toggle: across a live session the
# test reached two states on one track out of dozens, so four droplets that plainly
# ignored the field went unmarked.  One state is enough for the weaker but sufficient
# statement -- with this voltage on the plates it is still falling at free-fall pace,
# so no voltage the supply can reach will hold it -- and that is the thing the operator
# actually needs: do not chase this one.
# OFF, by the operator's decision of 2026-09-21: it was drawn on a droplet that
# had plainly answered the field and was being balanced at 195 V, and a cross that
# fires wrongly is worse than no cross at all in a demonstration.  The verdict
# behind it stays -- only the mark on the picture is withheld.
SHOW_WONT_BALANCE = False
UNREACHABLE_SHOW_S = 3.0
CHOSEN_IDLE_S = 90.0            # pinned column with nothing measured: let it go
# ...and how long a REAL measurement holds the banner with no new fall arriving.  An
# operator finished five falls at 7:34, moved to a second droplet, measured three more
# falls on it -- and the banner still read "DONE -- 5 of 5" at 14:36, seven minutes
# later, about a droplet they had left.  A count nobody is adding to is not news.
MEASURE_IDLE_S = 180.0

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
    # When the droplet's own free-fall speed is not yet known, assume the TOP of
    # Ⅳ.2's band rather than its middle.  U_b = U/(1 - v/v_free) is very sensitive to
    # this, and the two errors are not symmetric: too high a v_free suggests a droplet
    # that turns out not to balance, which costs one toggle to find out, while too low
    # a one hides a perfectly good droplet and the operator never learns it was there.
    # An operator working at 150 V reported that nothing was ever suggested; at the
    # midpoint a droplet drifting 0.25 grid/s there computes as needing 525 V and is
    # passed over, while at the top of the band it needs 300 V and is reachable.
    #
    # This is the permissive corner of a range, which is how the FIRST version of this
    # gate went wrong -- the difference is that this is a fallback for an unknown, not
    # a fixed threshold, and it is replaced by a measurement the moment the droplet is
    # seen with the field off.
    free = MAX_SPEED if vFree is None else float(vFree)
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

# How long after a voltage change before descent rates mean anything again.
#
# The rate recorded is the track's smoothed velocity, and both it and the droplet need
# a moment to answer a new field.  Samples taken across the change therefore describe
# neither state, and they land in the NEW voltage's bucket -- which is how a bucket
# comes to hold two field states at once, widen, and be thrown out by the spread guard.
# The result was that a droplet the operator had plainly proved inert was never marked,
# because the only evidence about it had been poisoned at the moment it was gathered.
#
# The velocity filter settles in about four frames and the droplet itself in far less,
# so half a second is generous.
CHARGE_SETTLE_S = 0.5

# How far back to look for the working voltage a fall was released from.
#
# The balance voltage is read from the LATCHED line, which holds the last voltage
# before the field was cut.  On 2026-09-10 that line read `+0000V` for 26 of 36
# refusals -- more than a third of the night's legal falls came back with no voltage
# at all, and a fall without its voltage yields no charge.
#
# But the `V:` line is already read ten times a second, so the working voltage is
# known without the latch: it is the last steady non-zero reading before the droplet
# was released.  That is the same quantity the latched line is supposed to show,
# arrived at from data already in hand.  The window bounds how stale a reading may be
# and still describe the fall that followed it.
WORKING_VOLTS_MEMORY_S = 120.0
# How old the V: line's fallback may be when it is WRITTEN INTO A FALL.  The
# operator sets the voltage and then cuts to 0 within a second or two, so a
# working reading older than this belongs to something else.
WORKING_VOLTS_RECORD_S = 20.0

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
        # A lift is 提升: +210 V on top of the working voltage, so it always ends
        # up ABOVE LIFT_VOLTS.  Without that half of the test, any jump of 150 V
        # counted -- including the jump from a panel reading that flickered to 0 and
        # back.  On 2026-09-21 the aid announced "lifting; balance is not measured
        # during a lift" at 250 V, repeatedly, while the operator was balancing.
        if not self.lifting and step >= LIFT_STEP_V and level > LIFT_VOLTS:
            self.lifting, self.since, self.base = True, now, was
        elif self.lifting and step <= -LIFT_STEP_V:
            self.lifting, self.since, self.base = False, now, None
        elif self.lifting and self.base is not None \
                and level <= self.base + LIFT_TOLERANCE_V:
            # Released without a clean step -- readings dropped through the change.
            self.lifting, self.since, self.base = False, now, None
        elif self.lifting and level <= LIFT_VOLTS:
            # A LIFT IS A PLACE, NOT JUST A LATCH.  提升 adds 210 V on top of the
            # working voltage, so while it is held the plates sit above LIFT_VOLTS.
            # Both releases above can miss it: the step test needs the whole drop
            # inside half a second, which a hand on the knob never manages, and the
            # base test needs the level back where the lift departed from -- which
            # was 0 V whenever the lift began from a panel reading of zero, so
            # nothing short of cutting the field could clear it.
            #
            # On 2026-09-21 that left "lifting; balance is not measured during a
            # lift" on screen with the panel showing 150 V.  A lift suspends the
            # balance fit, so the droplet the operator was holding was not being
            # measured at all while it said so.  If the plates are not above
            # LIFT_VOLTS, no lift is being held, whatever the latch remembers.
            self.lifting, self.since, self.base = False, now, None
        return self.lifting

    def quiet(self, now):
        """True when the aid should say nothing: a lift held longer than the dwell."""
        return (self.lifting and self.since is not None
                and now - self.since >= LIFT_DWELL_S)


# Kept because the lift latch compares a released voltage against its starting point,
# and because DESIGN_NOTES cites it.
LIFT_TOLERANCE_V = 15.0
# How long the panel must keep reading zero before the aid acts on it.  See the
# use site: the reader's failure mode is 0000, and everything keys off this.
ZERO_HOLD_S = 1.0
# ...and how long a reading stays worth believing once the panel stops reading.
VOLTS_STALE_S = 4.0



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
# A live session delivered 19.5 fps against a 30 fps camera on 2026-09-20 and the
# driver dropped 13,467 frames, which costs measurements twice over: a third fewer
# samples per second makes every speed estimate noisier, and "moving too unevenly
# to judge" was the single most common thing the aid said all session.
#
# Profiled at 24 ms a frame before the window is even drawn, plus 5.6 ms for
# imshow+waitKey and 3.3 ms to JPEG a frame for the browser. Neither the operator
# nor the browser needs 30 updates a second -- a droplet crosses half a grid line
# in that time -- so both get every second frame and the tracker keeps the rest.
# The RECORDING still gets every frame: that is the measurement's evidence.
SHOW_EVERY = 2

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

    # `droplet` is the session's droplet NUMBER, decided once, at the moment the
    # fall completed, and never revisited.  The tally used to be re-derived every
    # frame from the falls lying near the live droplet's column, which is a
    # different question with a different answer each time the droplet drifted:
    # on 2026-09-21 it flickered between 1 and 2 for half a minute, dropped from
    # 4 of 5 to 1 of 5 in the middle of a fall and stayed there, and read
    # "MEASURING 1 of 5" about a freshly picked droplet that had never fallen --
    # counting the previous droplet's fall, which lay in the same column.
    FIELDS = ("id", "droplet", "fTop", "fBottom", "tTop", "tBottom", "seconds",
              "legal", "kinematicLegal", "fieldOff", "balanceVoltage", "reacquired",
              "adopted", "voltageFrom", "balanceDriftPct", "balanceImpliedV")

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
            # Ⅴ.5 advises a balance voltage of 150-250 V, and a suggestion that lands
            # outside it is worth flagging rather than following: an operator was told
            # to LOWER to 87-94 V, which is a balance the apparatus can reach and the
            # manual does not recommend.  pickedVerdict already said so; this did not.
            mid = (est[0] + est[1]) / 2.0
            if not ADVISORY_MIN_V <= mid <= ADVISORY_MAX_V:
                tail += " (outside %g-%g V)" % (ADVISORY_MIN_V, ADVISORY_MAX_V)
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



def electronsOff(seconds, volts):
    """(q/e, distance from the nearest whole number), or None.

    Layer 2 only.  This is the same computation cv/charge.py does and it stays on this
    side of cv/api.py: it reaches the operator through the overlay, which is where the
    balance voltage is already displayed, and never through a dict crossing to the app
    or the LLM.  Do not pass the result outward.
    """
    if not seconds or not volts or seconds <= 0 or volts <= 0:
        return None
    try:
        from cv import charge as _charge
        ratio = _charge.perFall(float(seconds), float(volts))["chargeC"] \
            / _charge.ELEMENTARY_CHARGE
    except Exception:
        return None
    return ratio, abs(ratio - round(ratio))


def _spreadOf(rates):
    """Interquartile spread of one voltage bucket's descent rates."""
    if len(rates) < 4:
        return 0.0
    return float(np.percentile(rates, 75) - np.percentile(rates, 25))


# A spray fills the chamber with debris, and some of it falls 8 divisions in a legal
# time with the field off.  Counting any legal fall near the picked column made the
# banner announce "MEASURING -- 1 of 5 falls" about a speck that had fallen forty
# seconds earlier, in a session where no droplet was ever balanced.  A measurement
# begins when the operator commits -- clicks, or holds a droplet on `0` -- so a fall
# from before that belongs to nobody.  The grace is one fall long: a droplet dropped
# and THEN clicked is still theirs.
COMMIT_GRACE_S = MAX_FALL_S


def dropletFalls(falls, nearX, since = None):
    """The legal falls that belong to one droplet, grouped as `binding` groups them.

    The banner used to count falls within SAME_DROPLET_X_PX of a PINNED column, while
    binding groups them around a RUNNING MEDIAN.  Those are not the same rule, so the
    display and the data disagreed by construction: a droplet whose five falls drifted
    648 -> 572 px binds as one droplet and was shown as "3 of 5", because the last two
    sat more than 60 px from where the first one happened to be.

    Pinning was introduced for a real reason -- following the LIVE droplet slides the
    window and makes the tally go DOWN as it drifts -- but the median of the falls
    already counted does not chase the droplet, it tracks the group.  This is the same
    two-stage rule binding uses: cluster on column, then require the durations to agree,
    so a stray that falls down the same column is not absorbed.
    """
    # Column FIRST and duration SECOND was still two rules, and it broke the moment
    # two droplets were measured in the same column: on 2026-09-14 one operator's five
    # 26 s falls and another droplet's three 38 s falls clustered together, the median
    # duration landed at 32 s between the two, and the 10% filter then threw away
    # EVERYTHING -- the tally read "DONE 5 of 5" and then "MEASURING 4 of 5" on the
    # same screen, and np.median() of the empty list filled the terminal with "Mean of
    # empty slice".
    #
    # dropletGroups() grows each group on both tests at once, so two droplets sharing a
    # column stay two groups.  It is what charges.csv already binds on, so the number
    # on the screen and the number in the file now come from one rule instead of two
    # that agree by coincidence.
    if since is not None:
        # The fall must have been IN PROGRESS when the operator committed, or later.
        # Measuring from its start with a fall-length grace was too generous: a speck
        # that fell forty seconds before the click still qualified, which is the very
        # case this exists to exclude.  A droplet dropped and then clicked ends after
        # the click; a stray that already landed does not.
        falls = [f for f in falls
                 if (f.tTop or 0.0) + float(f.seconds) >= float(since) - 2.0]
    groups = dropletGroups(falls)
    if not groups:
        return []
    centres = [float(np.median([q.xPx for q in g])) for g in groups]
    i = min(range(len(groups)), key = lambda k: abs(centres[k] - nearX))
    if abs(centres[i] - nearX) > 2 * SAME_DROPLET_X_PX:
        return []
    rows = groups[i]
    return rows


def chargeFunnel(tracks):
    """Where the charge test stops, counted across the live tracks.

    Four droplets that plainly ignored the field went unmarked all session and no
    number on the screen said why.  The test has four gates before it can answer --
    the track must be confirmed, it must have been seen at some voltage, at two of
    them with enough samples each, and those two must be far enough apart -- so count
    the survivors of each and the answer falls out.
    """
    confirmed = [t for t in tracks if len(t.points) >= CONFIRM_POINTS]
    seen = [t for t in confirmed if getattr(t, "byVolts", None)]
    twoStates, apart = [], []
    for t in seen:
        full = sorted(v for v, r in t.byVolts.items() if len(r) >= CHARGE_MIN_SAMPLES)
        if len(full) >= 2:
            twoStates.append(t)
            if full[-1] - full[0] >= CHARGE_MIN_STEP_V:
                apart.append(t)
    yes = sum(1 for t in apart if t.chargeState() is True)
    no = sum(1 for t in apart if t.chargeState() is False)
    crossed = sum(1 for t in confirmed if getattr(t, "unreachableSince", None))
    return ("%d confirmed, %d seen at some voltage, %d at two, %d far enough apart "
            "-> charged %d, uncharged %d, unclear %d; %d marked won't-balance"
            % (len(confirmed), len(seen), len(twoStates), len(apart),
               yes, no, len(apart) - yes - no, crossed))


def chargeFromBuckets(byVolts):
    """True / False / None from {plate voltage: [descent rates]}.

    Split out of Track so the same test can be applied to a history that is NOT the
    track's.  A lift ends the track -- that is what a lift does -- and the replacement
    starts with an empty byVolts, so a droplet the operator had just proved charged
    read as untested again.  The operator watched a droplet answer 提升 plainly and
    the banner say nothing had been seen.  The PICK survives the lift, so the pick
    keeps the history.
    """
    usable = [(v, rates) for v, rates in byVolts.items()
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
    #
    # THREE answers, not two.  One threshold cannot both confirm and condemn: at a
    # single 0.10 grid/s cut, a droplet that went from falling 0.05 to rising 0.03 --
    # a separation of 0.08, and unmistakably a response -- was reported as PROVEN
    # uncharged and crossed out on screen.  An operator watched it move and the aid
    # deny it.
    #
    # So: a separation at or above CHARGE_MIN_SEPARATION is a confident yes; one no
    # larger than BALANCED_MAX, the speed this project already treats as "not moving",
    # is a confident no; and everything between is a response too small to call either
    # way, which is None and stays a candidate.
    # A bucket is supposed to hold one field state.  When the panel misreads -- and
    # in the run that exposed this, three of four falls could not be read at all -- the
    # balanced samples (~0 grid/s) and the free-fall samples (~0.28) land in the SAME
    # bucket, their median falls between the two, the separation collapses and a
    # plainly charged droplet reads as uncharged.  A bucket spread that wide is not
    # one state, so it cannot answer the question.
    if max(_spreadOf(loRates), _spreadOf(hiRates)) > CHARGE_MIN_SEPARATION:
        return None
    diff = float(np.median(loRates)) - float(np.median(hiRates))
    if diff >= CHARGE_MIN_SEPARATION:
        return True
    if diff <= BALANCED_MAX:
        return False
    # The ratio test, for everything the difference cannot call.  Only meaningful when
    # the droplet was actually moving at the lower voltage -- a droplet that was not
    # falling there says nothing about what the field does to it.
    lo = float(np.median(loRates))
    hi = float(np.median(hiRates))
    if lo >= MIN_SPEED * 0.5 and hi >= CHARGE_HELD_FRACTION * lo:
        return False
    return None


def answersField(t, now, speed, vFree):
    """Sticky: the field is visibly holding this droplet, so it has a charge.

    The test used to read a one-second speed, which spikes, so a droplet being calmly
    balanced was interrupted twelve times in one session with "no response seen yet,
    toggle again to be sure".  Held is held: take the median of a couple of seconds,
    and once a droplet has been seen held, stop asking.
    """
    if getattr(t, "answeredField", False):
        return True
    hist = getattr(t, "heldHistory", None)
    if hist is None:
        hist = t.heldHistory = []
    hist.append((float(now), abs(float(speed))))
    while hist and now - hist[0][0] > 2.5:
        hist.pop(0)
    if (hist[-1][0] - hist[0][0] >= 1.0
            and float(np.median([v for _ts, v in hist])) <= 0.25 * float(vFree)):
        t.answeredField = True
    return bool(getattr(t, "answeredField", False))


def steadySpeed(t, now, speed, sinceChange = None):
    """This droplet's fall speed over the last few seconds, not this frame's.

    Samples from before the last voltage change are dropped.  A droplet released from
    balance was held still a moment ago, so a window spanning the cut averages "not
    moving" with "falling" and reports a speed far too slow -- which is why a droplet
    was called TOO SLOW for a few seconds after every crossing of `0`.
    """
    hist = getattr(t, "speedHistory", None)
    if hist is None:
        hist = t.speedHistory = []
    hist.append((float(now), float(speed)))
    cut = now - SPEED_MEDIAN_S
    if sinceChange:
        cut = max(cut, now - float(sinceChange))
    while len(hist) > 1 and hist[0][0] < cut:
        hist.pop(0)
    return float(np.median([v for _ts, v in hist]))


def usableSpeed(t, now, speed, sinceChange = None):
    """(speed to report, is it in the legal band) -- with hysteresis on the band.

    A droplet drifting at the 0.2 gr/s gate crosses it constantly, and each crossing
    used to rewrite the banner: an operator watched one droplet go DISCARD, USABLE,
    DISCARD, USABLE over ten seconds.  Once a verdict is given it takes a clear
    crossing to reverse it.
    """
    v = steadySpeed(t, now, speed, sinceChange)
    hist = t.speedHistory
    # A verdict needs a speed that means something.  "too slow for 0.2-0.5, DISCARD"
    # and "0.20 gr/s -- USABLE" arrived two seconds apart on one id whose speed was
    # swinging from -0.04 to 0.20: nothing that unstable should be judged at all.
    if len(hist) >= 4:
        spread = max(x for _ts, x in hist) - min(x for _ts, x in hist)
        if spread > SPEED_UNSTABLE_GRID_S:
            return v, None
    span = hist[-1][0] - hist[0][0] if len(hist) > 1 else 0.0
    was = getattr(t, "speedVerdict", None)
    if was is None and span < SPEED_FIRST_VERDICT_S:
        return v, None              # too little of it to judge -- see the constant
    lo, hi = MIN_SPEED, MAX_SPEED
    if was:
        lo, hi = lo * (1.0 - SPEED_GATE_SLACK), hi * (1.0 + SPEED_GATE_SLACK)
    ok = lo <= v <= hi
    if was is not None and span < 1.5:
        ok = was            # a median of one second cannot reverse a verdict either
    t.speedVerdict = ok
    return v, ok


_shown = {}


def brownianFloorPct(vFree, seconds):
    """The best a drift measurement can do on this droplet in this window, in percent.

    Brownian motion carries the droplet sqrt(2 D t) in time t, so a straight-line fit
    over a window of t sees an apparent drift of about sqrt(2 D / t) however perfectly
    the droplet is balanced.  Below that number there is nothing left to tune, and an
    operator asked exactly the right question: how do you reach 2% when the thing
    jiggles?  On a typical droplet, 20 s of hold cannot resolve better than ~2.5%,
    45 s reaches ~1.7%, and 90 s ~1.2%.  Telling them when they are at the floor is
    what stops them chasing it.
    """
    if not vFree or seconds < BALANCE_MIN_FIT_S:
        return None
    v = float(vFree) * chargeMath.FALL_DISTANCE / FALL_DIVISIONS      # grid/s -> m/s
    if v <= 0:
        return None
    a = np.sqrt(9.0 * chargeMath.AIR_VISCOSITY * v
                / (2.0 * chargeMath.OIL_DENSITY * chargeMath.GRAVITY))
    D = (BOLTZMANN * LAB_TEMPERATURE_K
         / (6.0 * np.pi * chargeMath.AIR_VISCOSITY * a))
    floor = float(100.0 * np.sqrt(2.0 * D / seconds) / v)
    return floor if floor <= BALANCE_FLOOR_MAX_PCT else None


# Lines that report the aid's own state rather than telling the operator anything.
# Counted on 2026-09-20 over two and a half minutes of one droplet being balanced:
# of 31 lines the banner showed, 4 carried a number to act on and 25 were these --
# six "watching it", six "settling after the voltage change", three "moving too
# unevenly to judge", and so on. Each one REPLACED the instruction before it, so an
# operator who looked up a second late saw a status word instead of the advice.
#
# They are not wrong and they are not useless: after a voltage change the aid really
# does know nothing. But a line saying so is worth less than the last real
# instruction, which is still roughly true, so the instruction stays until something
# better replaces it or it goes stale.
FILLER_LINES = ("watching it", "settling after the voltage change",
                "moving too unevenly to judge", "timing its fall",
                "still calculating")
FILLER_HOLD_S = 12.0            # ...after which even a stale instruction is dropped
_lastReal = {}                  # pick id -> (text, colour, when)


def keepInstruction(pickId, text, colour, now):
    """Prefer the last real instruction over a status word, for a little while."""
    if pickId is None or not text:
        return text, colour
    isFiller = any(f in text for f in FILLER_LINES)
    if not isFiller:
        _lastReal[pickId] = (text, colour, now)
        return text, colour
    held = _lastReal.get(pickId)
    if held and now - held[2] <= FILLER_HOLD_S:
        return held[0], held[1]
    return text, colour


def forgetInstruction():
    """Drop every held instruction, because the field they were about has changed.

    keepInstruction holds the last real line for FILLER_HOLD_S so a number stays
    readable.  That also kept verdicts alive across the very change that refuted
    them: with the panel misreading zero for one frame, "falls -0.08 gr/s -- too
    slow, DISCARD" was computed against no field and then shown for twelve seconds
    with 150 V on the panel beside it.
    """
    _lastReal.clear()


def forgetHeld(key):
    """Drop a held line at once, because what it described is gone.

    The hold keeps a number still long enough to read.  It also kept "picked id 22438
    -- USABLE" on screen for two seconds after the pick had been dropped, while the
    drawing had already switched from the operator's square to the aid's own ring: the
    picture said one thing and the words another.
    """
    _shown.pop(key, None)


def holdText(key, now, text, colour, seconds = BALANCE_SHOW_S):
    """Keep a changing number on the screen for `seconds` before letting it change.

    Recomputed every frame, a drift percentage and a target voltage step through a
    dozen values a second, and an operator reported the result as unreadable -- "more
    of a nuisance than a help".  The calculation still runs every frame; only what is
    DISPLAYED is held.
    """
    was = _shown.get(key)
    if was is not None and now - was[2] < seconds:
        return was[0], was[1]
    _shown[key] = (text, colour, now)
    return text, colour


def fieldLevel(volts):
    """Which of the three states the plates are in: off, working, or lifting."""
    if volts is None:
        return None
    if float(volts) <= 0:
        return "off"
    return "lift" if float(volts) > LIFT_VOLTS else "work"


DRIFT_HISTORY = {}          # column key -> [(t, drift)], survives the track
TARGET_HISTORY = {}         # column key -> [(t, implied balance voltage)]
# How tightly the last few seconds of implied balance voltages must agree before one
# of them is put on the screen as an instruction, and how much of the way to it the
# operator is asked to move while they do not.
TARGET_STABLE_FRAC = 0.12
TARGET_MIN_SAMPLES = 6
TARGET_HALF_STEP = 0.5
TARGET_GIVE_UP_S = 25.0         # ...after this long unsettled, stop promising a number
# Smaller than this and the number is inside its own error, and inside the knob's
# resolution.  Say the direction instead.
MIN_NUDGE_V = 5.0


def steadyTarget(t, now, ub, volts):
    """(target, is it trustworthy, how far to move meanwhile).

    U_b = U / (1 - v/v_free) is exact and, far from balance, useless as an
    instruction: v is a drifting droplet's speed, so the answer swings either side of
    the truth and only settles as the droplet slows.  The operator put it exactly --
    the suggestion becomes correct only about half way to balance, and until then it
    is noise being handed out as a number.

    So while the estimates disagree, say so, and ask for a move that is deliberately
    SHORT of the estimate: half way slows the droplet, which tightens the next
    estimate, which is the thing that actually has to happen.  Overshooting does the
    opposite.
    """
    if ub is None or not volts:
        return None, False, None
    key = int(round(float(t.pos[0]) / CHARGE_COLUMN_PX))
    hist = TARGET_HISTORY.setdefault(key, [])
    hist.append((float(now), float(ub)))
    while len(hist) > 1 and now - hist[0][0] > BALANCE_TARGET_MEMORY_S:
        hist.pop(0)
    values = [u for _ts, u in hist]
    target = float(np.median(values))
    # Estimates from the other side of balance describe a drift that is gone.  A
    # falling droplet's own estimate is always above the panel and a rising one's
    # below it, so a median on the far side of the reading from THIS estimate can
    # only be old samples -- and it comes out as advice that argues with itself:
    # "RAISE to about 206 V" with the panel at 214, seconds after the same droplet
    # had been told to LOWER.  Seen twice on 2026-09-20, on both the picked-droplet
    # line and the MEASURING line, which is why the test lives here and not in
    # either caller.
    if (target - float(volts)) * (float(ub) - float(volts)) < 0:
        del hist[:-1]
        values = [float(ub)]
        target = float(ub)
    step = TARGET_HALF_STEP * (target - float(volts))
    # The median can sit far from what THIS estimate says while it is still settling,
    # and the operator is asked to act on the difference: "LOWER by about 35 V" went
    # out about a droplet 2.8% off balance -- some 5 V at 197 V -- three seconds
    # before the same droplet was called balanced.  A move no larger than the newest
    # estimate's own gap keeps the instruction inside the physics.
    cap = max(5.0, abs(float(ub) - float(volts)))
    step = float(np.clip(step, -cap, cap))
    step = float(np.sign(step) * max(5.0, round(abs(step) / 5.0) * 5.0))
    if len(values) < TARGET_MIN_SAMPLES or hist[-1][0] - hist[0][0] < 3.0:
        return target, False, step
    lo, hi = np.percentile(values, [25, 75])
    spread = (hi - lo) / max(abs(target), 1.0)
    return target, spread <= TARGET_STABLE_FRAC, step


def steadyDrift(t, now, drift, sinceChange = None):
    """This droplet's drift over the last few seconds, not this frame's.

    Smoothing the TARGET voltage and leaving the PERCENTAGE raw produced a screen that
    argued with itself: "balanced to 6.0% ... within the noise, leave it", because the
    6.0% was one frame's fit and the target was a twelve-second median.  Both now come
    off the same smoothed drift, so the number and the instruction cannot disagree.

    The memory is cut at the last voltage change.  After the operator turns the knob
    the old samples describe a field that is gone, and averaging across the change is
    how an aid lags behind the person using it.
    """
    # Keyed by column, not by track.  Five falls of one droplet carried four track
    # ids, and a history kept on the track object started empty each time -- so the
    # "smoothed" percentage was one raw sample and swung from 2% to 36% between
    # seconds.  The column is what survives a lift, which is why binding uses it.
    key = int(round(float(t.pos[0]) / CHARGE_COLUMN_PX))
    hist = DRIFT_HISTORY.setdefault(key, [])
    hist.append((float(now), float(drift)))
    cut = now - BALANCE_TARGET_MEMORY_S
    if sinceChange:
        cut = max(cut, now - float(sinceChange))
    while len(hist) > 1 and hist[0][0] < cut:
        hist.pop(0)
    return float(np.median([d for _ts, d in hist]))


def balanceError(t, spacing, now, volts, vFree, seconds, sinceChange = None):
    """What the residual drift at this voltage will cost the charge, in percent.

    Returns (percent, drift grid/s, implied balance voltage, assumed) or None while
    there is too little of the hold to fit.  `assumed` is True when the droplet's own
    free-fall speed was never measured and the fallback had to be used, in which case
    the percentage is an estimate of an estimate and is printed with a `~`.

    Uses motion and the plate voltage only -- no charge, no electron count, no
    accepted value.  From v = v_free (1 - U/U_b), a droplet still drifting at v is off
    balance by v/v_free, and q is wrong by the same fraction.
    """
    if not volts or spacing <= 0:
        return None
    # A long fit over a steady hold is the accurate answer; the short-window speed is
    # the one available while the operator is still turning the knob, which is exactly
    # when they need to be told which way.  Marked `coarse` so the text can say `~`.
    windows = ([seconds] if seconds >= BALANCE_MIN_FIT_S else []) + [BALANCE_MIN_FIT_S, 5.0]
    # ...but never across a voltage change.  The fit runs over the track's own points,
    # and those points remember the field that was applied when they were recorded: for
    # up to 45 s after a toggle the window mixed two states.  That is how a droplet
    # balancing at 171 V was reported "barely held, 67% of free fall", and why the
    # percentage swung 27% -> 3% -> 18% inside seven seconds.  Below FIT_MIN_S of
    # settled field there is nothing honest to say, so say nothing.
    if sinceChange is not None:
        windows = [w for w in windows if w <= float(sinceChange)]
        if not windows:
            return None
    fit, used = None, None
    for w in windows:
        fit = t.windowFit(spacing, now, w)
        if fit is not None:
            used = w
            break
    drift = fit[0] if fit is not None else t.recentSpeed(spacing)
    if drift is None:
        return None
    drift = steadyDrift(t, now, drift, seconds)
    free = vFree if vFree else SLOW_DRIFT_FREE
    if free <= 0:
        return None
    return (100.0 * abs(drift) / free, drift,
            reachableBalance(volts, drift, vFree), vFree is None,
            fit is None or used < BALANCE_MIN_FIT_S)


def updateHold(t, yTop, spacing, now, volts = None, columns = None,
               sinceChange = None, lifting = False):
    """Track how long this droplet has sat balanced on the top line.

    Ⅳ.4's own first step, and the one thing on the screen that cannot happen by
    accident: a droplet held still at `0` is one somebody balanced deliberately.
    """
    v = t.recentSpeed(spacing)
    onLine = float(t.pos[1]) <= yTop + MEASURE_HOLD_DIV * spacing
    # Brownian motion defeats an instantaneous speed test.  A well balanced droplet
    # crosses 0.02 grid/s constantly -- that is what the jiggle IS -- and every crossing
    # used to reset the hold, so the fifteen seconds it must sit still were almost never
    # reached: an operator reported the "hold it" line appearing about one try in ten.
    # What matters is whether it has actually gone anywhere, so end the hold on
    # DISPLACEMENT and let it jiggle in place.
    if t.heldSince is None:
        if v is not None and abs(v) <= BALANCED_MAX and onLine:
            t.heldSince, t.heldAtY = float(now), float(t.pos[1])
    elif not onLine or abs(float(t.pos[1]) - getattr(t, "heldAtY", float(t.pos[1]))) \
            > HOLD_SLACK_DIV * spacing:
        t.heldSince, t.heldAtY = None, None
    # The hold IS the balance check: while the droplet sits on the line with the field
    # on, fit its residual drift and keep the percentage it will cost.  Throttled,
    # because freeFallSpeed reads the column history.  The value stays on the track
    # after the operator cuts to 0 V, which is how the fall that follows carries it.
    # The window to fit over is how long the VOLTAGE has been still, not how long the
    # droplet has sat on the top line.  An operator left the knob untouched at 282 V
    # for a minute with the droplet mid-screen, and nothing was ever measured or
    # recorded, because a hold that never started gave a window of zero.
    window = min(BALANCE_FIT_S, float(sinceChange)) if sinceChange else 0.0
    if t.heldSince is not None:
        window = max(window, min(BALANCE_FIT_S, float(now) - t.heldSince))
    # Never while the droplet is being lifted.  A lift is 210 V above the working
    # voltage and the droplet climbs at its free-fall pace, so the arithmetic returns
    # about 100% -- and three of five falls carried that instead of the balance the
    # operator had actually set.
    if (volts and columns is not None and not lifting
            and window >= BALANCE_MIN_FIT_S
            and float(now) - getattr(t, "lastBalanceCheck", 0.0) >= BALANCE_CHECK_S):
        t.lastBalanceCheck = float(now)
        got = balanceError(t, spacing, now, volts, freeFallSpeed(columns, t.pos[0]),
                           window, sinceChange)
        if got is not None:
            pct, drift, impliedV, _assumed, coarse = got
            # A droplet nowhere near balance has no balance quality to record: the
            # number belongs to the voltage the operator settled on, not to whatever
            # the droplet was doing on the way there.
            if not coarse and pct <= BALANCE_FAR_PCT:
                t.balanceDriftPct, t.balanceDrift, t.balanceImpliedV = pct, drift, impliedV


def heldLongEnough(t, now):
    since = getattr(t, "heldSince", None)
    return since is not None and float(now) - since >= MEASURE_HOLD_S


# Slower than this on live plates means the field is holding it.  Free fall is
# 0.2-0.5 grid/s, so a fifth of the slow end leaves no room for confusion.
HELD_BY_FIELD = 0.10            # grid/s


def pickedVerdict(t, spacing, now, volts, charged, vFree, ownCharge = None,
                  sinceChange = None, lifting = False, measured = False,
                  lastWorkingVolts = None):
    """What to tell the operator about the droplet THEY chose.  (text, colour, ok)

    Reports on it whether or not it passes the candidate gates, because the operator
    asked about this one -- and "this will not work, discard it" is the most useful
    thing the aid can say once they have committed time to a droplet.
    """
    speed = t.recentSpeed(spacing)
    if speed is None:
        return "picked id %d -- watching it" % t.id, (0, 220, 255), None
    # Before anything else.  This test used to sit below the field-off branch, so a
    # droplet cut to 0V was judged TOO SLOW on a speed still averaging the second it
    # spent balanced.
    if sinceChange is not None and sinceChange < SETTLE_AFTER_V_S:
        return ("picked id %d -- settling after the voltage change" % t.id,
                (0, 220, 255), None)
    if volts is None:
        # Not the same as zero.  `if not volts` treated an unreadable panel as a field
        # that is off and judged the droplet's speed against the falling band, which
        # is only the right test when the field really is off.
        return "picked id %d -- panel unreadable, cannot judge it" % t.id, (0, 220, 255), None
    if (not volts) and lastWorkingVolts and speed < STILL_GRID_S:
        # The panel says 0 V, the droplet is NOT FALLING, and a working voltage was
        # on the plates seconds ago.  With the field off there is only gravity, so
        # a droplet that hovers or climbs is being held up by something and the
        # reading is wrong -- which is how -0.08 and -0.03 grid/s came to be
        # reported as "too slow for 0.2-0.5, DISCARD" at 150 V.
        #
        # The test is the DIRECTION, not the magnitude.  It was briefly "slower
        # than the 0.2-0.5 band", which is a different claim and a false one: that
        # band is the band of droplets worth measuring, not a law about falling.
        # A droplet too heavy for it falls at 0.15 grid/s quite legitimately, and
        # treating that as a misread meant the aid never said "too slow" at all --
        # it judged a genuinely slow droplet against a field that was not there and
        # reported "the field is not holding it yet" for eighteen seconds.
        #
        # on the plates seconds ago. `volts` is OCR of a display that misreads --
        # it voted 1759 V on a panel showing 0175 on 2026-09-21 -- and when it
        # reads 0 by mistake, a BALANCED droplet looks like a lost square: the
        # operator spent minutes being told "if you have cut to 0 V the square is
        # probably not on your droplet" while they were balancing it. A droplet
        # sitting still is evidence of a field, so judge it against the voltage
        # they actually set.
        volts = float(lastWorkingVolts)

    if not volts:
        # FIELD OFF, so the only question is whether it falls at a usable pace, and the
        # charge verdict is not merely unnecessary here -- it is meaningless.  With no
        # field there is nothing for the droplet to answer, so "does not answer the
        # field" describes every droplet in the chamber.
        #
        # This test used to sit BELOW the charge check, which therefore pre-empted it:
        # an operator watched a droplet fall on 0 V and be told it did not react, and a
        # droplet falling too slowly be told the same instead of that it was too slow.
        # Both were the wrong question answered first.
        shown, ok = usableSpeed(t, now, speed, sinceChange)
        hist = getattr(t, "speedHistory", None) or []
        span = (hist[-1][0] - hist[0][0]) if len(hist) > 1 else 0.0
        if ok is None:
            # Two different silences.  A window too short to mean anything is not the
            # same as a droplet whose speed will not hold still, and saying "let it
            # settle" about the first one told the operator to wait for something
            # that had already happened.
            if span < SPEED_FIRST_VERDICT_S:
                return ("picked id %d -- timing its fall" % t.id, (0, 220, 255), None)
            return ("picked id %d -- moving too unevenly to judge; let it settle"
                    % t.id, (0, 220, 255), None)
        if abs(shown) <= STILL_GRID_S:
            # Not falling at all with no field to hold it.  That is not a slow
            # droplet, it is a speck stuck to the window or a stale track: on
            # 2026-09-20 "falls -0.00 gr/s -- too slow for 0.2-0.5, DISCARD" was
            # written about one, which reads as a verdict on the operator's droplet.
            #
            # ...unless the field is not off at all.  `volts` is OCR of a display
            # that redraws in its own time and sometimes does not redraw at all: at
            # 11:44 the same session the panel read 0 while the apparatus sat at
            # 164 V, and the aid told the operator their square was on the wrong
            # droplet while the field was holding it perfectly still.  A droplet
            # that does not move when the field is supposedly off is itself the
            # evidence that the reading is stale.
            return ("picked id %d is not moving -- if you have cut to 0 V the square "
                    "is probably not on your droplet; if the field is still on, it "
                    "is holding it" % t.id, (0, 140, 255), None)
        if ok:
            return ("picked id %d falls %.2f gr/s -- USABLE, balance it on `0`"
                    % (t.id, shown), (0, 255, 0), True)
        if shown <= -MIN_SPEED / 2.0 and not lifting:
            # With the plates at 0 V nothing rises.  At the end of the 2026-09-20
            # session the square sat on empty field by the panel while the operator
            # watched their droplet mid-frame, and the aid reported that stray as
            # "falls -0.25 gr/s -- too slow for 0.2-0.5, DISCARD" over and over: a
            # verdict, on the wrong object, in words that hid what had happened.
            # Rising at 0 V is not a slow droplet.  But which of the two reasons it
            # is cannot be decided from here, and the first version picked one and
            # was wrong twice in one session: it told the operator the square was on
            # the wrong droplet while their own was genuinely being carried up, the
            # plates at +210 V and the panel still reading 0.  `volts` is OCR of a
            # display that redraws in its own time, and LiftWatch is fed those same
            # readings, so neither of them knows any better.  Say both, and let the
            # operator -- who knows which switch they just pressed -- decide.
            return ("picked id %d is RISING %.2f gr/s while the panel reads 0 V -- "
                    "either the field is still on, or the square is not on your "
                    "droplet" % (t.id, abs(shown)), (0, 140, 255), None)
        why = "too fast" if shown > MAX_SPEED else "too slow"
        return ("picked id %d falls %.2f gr/s -- %s for %g-%g, DISCARD"
                % (t.id, shown, why, MIN_SPEED, MAX_SPEED), (0, 0, 255), False)
    # Field on from here, so "did it answer" is a question that means something --
    # unless the droplet is answering it in plain sight.  A droplet sitting almost
    # still at 282 V, which would fall at its free-fall speed with the field off, is
    # being HELD by the field; that IS the response.  The column verdict can belong to
    # a previous droplet in the same column, and an operator who had left a balanced
    # droplet alone for a minute was told "no response seen yet, toggle again".
    if (volts and vFree and speed is not None
            and answersField(t, now, speed, vFree)):
        charged = True
    # ...and the same thing said without needing vFree.  answersField compares the
    # droplet against its own free-fall speed, which is only known once it has FALLEN
    # in that column -- so on a droplet the operator balanced before ever dropping it,
    # the one case where the answer is obvious, there was nothing to compare with.
    # On 2026-09-21 a droplet sitting at -0.06 grid/s on 250 V plates was told it
    # "does NOT answer the field -- DISCARD, spray again", and then that it would not
    # balance, while the operator was holding it balanced and could see it.
    #
    # Gravity is not optional: with the field on, an uncharged droplet falls at its
    # full free-fall pace, 0.2-0.5 grid/s by the manual's own band.  Anything drifting
    # slower than HELD_BY_FIELD is being held up by the field, and being held up by
    # the field is what "it answers the field" means.
    if (volts and not lifting and speed is not None
            and abs(speed) <= HELD_BY_FIELD):
        charged = True
    if charged is False and measured:
        # It has completed legal falls under this operator's hand.  Whatever the speed
        # buckets say, a droplet that was balanced and dropped has answered the field,
        # and "no response seen yet, toggle again" is nonsense addressed to somebody one
        # fall into a measurement.
        charged = True
    if charged is False:
        # Telling the operator to throw away a droplet they have spent minutes on is
        # the most expensive thing this can say, so it is said only on the droplet's
        # OWN evidence.  A pooled column verdict is enough to rank candidates and not
        # enough to condemn one.
        if ownCharge is False:
            return ("picked id %d does NOT answer the field -- DISCARD, spray again"
                    % t.id, (0, 0, 255), False)
        return ("picked id %d -- no response seen yet, toggle again to be sure"
                % t.id, (0, 220, 255), None)
    if charged is None:
        return ("picked id %d -- toggle the field to see if it answers"
                % t.id, (0, 220, 255), None)
    # Nothing useful can be said in the first moments after the knob moves: the speed
    # estimate still holds the motion from BEFORE the change.  An operator toggled back
    # to 250 V and was told "no reachable balance, DISCARD" about a droplet that was
    # simply still falling at its free-fall pace, which is what U_b = U/(1 - v/v_free)
    # returns when v is momentarily v_free.
    # A droplet being carried up by 提升 is moving because the operator is moving it.
    # Balance is not a question during a lift, and answering it anyway produced
    # "balance ~1106.9%" and a target hundreds of volts away, on the screen of an
    # operator who was simply holding the button.  The latch misses a ramped press, so
    # the voltage itself is the backstop.
    if lifting or (volts and float(volts) > LIFT_VOLTS):
        # ...but a droplet that keeps falling at its free-fall pace while the plates
        # are at 360 V is not being carried by anything.  That is the clearest charge
        # test the apparatus offers, and it does not need five samples in two buckets:
        # an operator toggled to 提升 three times on one droplet, watched it drift
        # down unchanged each time, and was told only that a lift was in progress.
        if vFree and speed is not None and abs(speed - float(vFree)) <= 0.15 * float(vFree):
            return ("picked id %d does NOT answer 提升 -- no charge, spray again"
                    % t.id, (0, 0, 255), False)
        return ("picked id %d -- lifting; balance is not measured during a lift"
                % t.id, (0, 220, 255), None)
    # How well it is balanced, as the percentage it will cost the charge.  One answer
    # whether it is drifting or settled, because "not moving" and "balanced well enough
    # to measure" are different questions and the eye cannot tell them apart: at
    # 0.02 grid/s a droplet looks perfectly still and its charge is already 7% out.
    held = getattr(t, "heldSince", None)
    steady = held is not None and float(now) - held >= SETTLED_S
    quality = balanceError(t, spacing, now, volts, vFree,
                           min(BALANCE_FIT_S, float(now) - held)
                           if held is not None else 0.0, sinceChange)
    if quality is None:
        return "picked id %d -- watching it" % t.id, (0, 220, 255), None
    pct, drift, ub, assumed, coarse = quality
    about = "~" if assumed or coarse else ""
    # The target is 2%, but no measurement over a short window can resolve better than
    # Brownian motion allows -- 2.5% over 15 s, 1.8% over 30 s.  Telling the operator
    # to tune below that is telling them to chase noise, which is exactly what they
    # were doing when they asked why the aid kept saying tune at 2-5%.
    window = min(BALANCE_FIT_S, float(now) - held) if held is not None else 0.0
    if sinceChange:
        window = max(window, min(BALANCE_FIT_S, float(sinceChange)))
    # Once a droplet has been called balanced it takes a clear excursion to un-call it.
    # At the 2% line Brownian motion crosses back and forth, and the banner alternated
    # BALANCED / off balance / BALANCED / off balance on readings of 1.3, 1.7, 4.1,
    # 1.5, 3.1 and 0.3 percent -- all of them the same droplet, sitting still.
    wasBalanced = bool(getattr(t, "balancedVerdict", False))
    limit = BALANCE_TARGET_PCT * (BALANCE_HYSTERESIS if wasBalanced else 1.0)
    t.balancedVerdict = pct <= limit
    floor = brownianFloorPct(vFree, window)
    # `coarse` means the fit fell back to a short window, where the floor is not a
    # floor and the percentage is a guess.  No green cue is given on a guess.
    # Hysteresis, like the balanced verdict above it.  Told "1.0% -- as good as it
    # gets", then "2.0% -- still calculating, RAISE by about 5 V" seconds later, an
    # operator has been handed two contradictory instructions about a droplet that
    # did not change: the floor itself moves with the window it is measured over.
    wasFloor = bool(getattr(t, "atFloorVerdict", False))
    atFloor = (floor is not None and not coarse
               and pct <= floor * (1.7 if wasFloor else 1.3)
               and window >= FLOOR_CLAIM_MIN_S)
    t.atFloorVerdict = atFloor
    if pct <= limit or atFloor:
        outside = not ADVISORY_MIN_V <= volts <= ADVISORY_MAX_V
        farOutside = (volts < ADVISORY_MIN_V - ADVISORY_MARGIN_V
                      or volts > ADVISORY_MAX_V + ADVISORY_MARGIN_V
                      or volts < ADVISORY_FLOOR_V)
        if farOutside:
            return ("picked id %d balances at %d V, well outside the manual's %g-%g V "
                    "-- worth swapping for another droplet"
                    % (t.id, volts, ADVISORY_MIN_V, ADVISORY_MAX_V),
                    (0, 140, 255), False)
        note = ("  (%d V, just outside %g-%g)" % (volts, ADVISORY_MIN_V, ADVISORY_MAX_V)
                if outside else "")
        if coarse:
            return ("picked id %d looks balanced (~%.1f%%) -- hold it still a few "
                    "seconds to confirm%s" % (t.id, pct, note), (0, 220, 255), None)
        if atFloor and pct > BALANCE_TARGET_PCT:
            return ("picked id %d BALANCED to %.1f%% at %d V -- that is the Brownian "
                    "limit over %.0f s, tuning cannot do better. Cut to 0V and time it"
                    % (t.id, pct, volts, window), (0, 255, 0), True)
        if steady:
            # Ⅳ.3: a droplet this well balanced has reached mechanical equilibrium.
            # Say it plainly, because the instinct is to keep tuning and that is how
            # the droplet gets lost.
            return ("picked id %d BALANCED to %s%.1f%% at %d V -- STOP ADJUSTING, the "
                    "jiggle is Brownian. Cut to 0V and time it%s"
                    % (t.id, about, pct, volts, note), (0, 255, 0), True)
        return ("picked id %d balanced to %s%.1f%% at %d V -- hold it, do not tune%s"
                % (t.id, about, pct, volts, note), (0, 255, 0), True)
    way = "RAISE" if drift > 0 else "LOWER"
    if drift > 0 and abs(drift) >= 0.9 * (vFree if vFree else SLOW_DRIFT_FREE):
        # Falling at the pace it would with no field at all: the field is not holding
        # it.  A state, not a verdict -- and more use than any percentage.
        return ("picked id %d is falling at free-fall pace -- the field is not holding "
                "it yet" % t.id, (0, 220, 255), None)
    if drift > 0 and pct > BALANCE_ADVICE_MAX_PCT:
        # Only the FALLING case is ill-conditioned: U / (1 - v/v_free) has a
        # denominator going to zero, which is how a droplet that balanced at 169 V was
        # sent to 364 V.  A droplet rising is the damped case -- denominator above one,
        # the answer bounded and worth printing -- so it falls through to the ordinary
        # advice below.  What is true here is not "turn it a lot": it is that this
        # droplet needs more field than the apparatus band has.
        return ("picked id %d is barely held (%s%.0f%% of free fall) -- RAISE it; if it "
                "still falls at %g V it is too heavy, spray again"
                % (t.id, about, pct, ADVISORY_MAX_V), (0, 140, 255), None)
    if ub is not None and floor is not None and pct <= 2.0 * BALANCE_TARGET_PCT:
        # A move smaller than the measurement's own noise is not worth making -- but
        # only a droplet that is ALREADY near balance can be "within the noise".  This
        # test is on the voltage step alone, and a step can be small while the drift is
        # not: it told an operator to leave a droplet sitting 6.0% out.
        if abs(ub - float(volts)) <= float(volts) * floor / 100.0:
            return ("picked id %d balanced to %s%.1f%% at %d V -- within the noise, "
                    "leave it" % (t.id, about, pct, volts), (0, 255, 0), True)
    if ub is None:
        free = vFree if vFree else SLOW_DRIFT_FREE
        if abs(drift) >= 0.9 * free:
            # Falling as fast as it would with no field at all: the field is not
            # holding it yet.  That is a state, not a verdict, and calling it DISCARD
            # threw away a droplet the operator was in the middle of catching.
            return ("picked id %d is falling at free-fall pace -- the field is not "
                    "holding it yet" % t.id, (0, 220, 255), None)
        return ("picked id %d drifts %+.3f gr/s -- no reachable balance, DISCARD"
                % (t.id, drift), (0, 0, 255), False)
    target, steady, step = steadyTarget(t, now, ub, volts)
    if not steady:
        # ...and if it never settles, say that instead of asking forever.  An operator
        # tuned one droplet from 2:19 to the end of the session and never got a usable
        # number out of the aid, because the estimates never agreed for long enough.
        hist = TARGET_HISTORY.get(int(round(float(t.pos[0]) / CHARGE_COLUMN_PX)), [])
        if hist and now - hist[0][0] >= TARGET_GIVE_UP_S:
            return ("picked id %d will not hold still enough to measure -- %s to about "
                    "%d V, or spray again" % (t.id, way, round(target)),
                    (0, 140, 255), None)
        return ("picked id %d off balance by %s%.1f%% -- still calculating; %s by "
                "about %d V to slow it down" % (t.id, about, pct, way, abs(step)),
                (0, 140, 255), None)
    warn = "" if ADVISORY_MIN_V <= target <= ADVISORY_MAX_V else "  (outside %g-%g V)" % (
        ADVISORY_MIN_V, ADVISORY_MAX_V)
    return ("picked id %d off balance by %s%.1f%% -- %s to about %d V%s"
            % (t.id, about, pct, way, round(target), warn), (0, 140, 255), None)


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
# ...and how far outside that band a droplet may balance and still be worth
# measuring.  Ⅳ.2's 150-250 V is a recommendation for choosing a droplet, not a
# limit on the apparatus: the lab's own instructor sets the condition at U > 100 V.
# Refusing to help at 148 V -- which the aid did on 2026-09-20, twice, on a droplet
# the operator had balanced to within 4% -- throws away a perfectly good
# measurement over 2 V. Outside the band is said; outside the band by a MARGIN is
# where the advice stops.
PANEL_MAX_V = 600.0             # the supply's ceiling; anything above it is a misread
ADVISORY_MARGIN_V = 20.0
ADVISORY_FLOOR_V = 100.0        # below this nothing is worth balancing

# What it takes to believe the operator has CHOSEN a droplet, rather than one merely
# happening to satisfy a gate.
#
# Earlier versions asked whether a fall clock was running, then whether the droplet had
# seen two voltages.  Both were guesses at intent and both fired on droplets the
# operator had never looked at, which silenced the hunt aid mid-spray.  The operator
# supplied the real test: they balance the droplet on the top line and leave it there
# while they check it.  Nothing accidental sits still at the line for fifteen seconds.
# How long a picked droplet must sit still before the aid calls it balanced and asks
# the operator to STOP adjusting.
#
# The last few volts are where a newcomer loses the droplet: it jiggles -- Ⅳ.3 says
# plainly that Brownian motion above and below the line IS mechanical equilibrium -- and
# they read the jiggle as "not balanced yet" and keep turning until it drifts off.  The
# aid can see the difference between jiggle and drift and they cannot, so it should say
# so rather than leave them guessing.
SETTLED_S = 6.0

MEASURE_HOLD_S = 15.0
MEASURE_HOLD_DIV = 1.0        # how far below `0` still counts as "on the line"

# Clicking a droplet.  With several plausible droplets on screen the ring moves between
# them, and no automatic rule can know which one the operator has decided to spend the
# next ten minutes on -- only they know that.  So let them say.
# After this long with no click and nothing being timed, the aid picks the droplet
# nearest the middle itself.  Suggesting one in the banner was not enough: the operator
# has both hands on the apparatus, and a message naming a droplet they then have to
# find and click is a message they ignore.  A pick is reversible in one click, costs
# nothing if wrong, and is the difference between the aid reporting on the droplet they
# care about and reporting on nothing at all.
IDLE_PICK_S = 30.0
AUTO_PICK_SHOW_S = 6.0        # how long to say it happened

PICK_RADIUS_PX = 40.0         # how near a click has to land to select a droplet
# Nothing may be picked, auto-picked or handed the pick unless it has been on
# screen this long. Detection still admits blobs that are not droplets -- ids ran
# from 14467 to 19801 in forty seconds of an almost empty chamber on 2026-09-21,
# and the square kept landing on them -- and while fixing that in the detector
# failed the gate twice, the PICK is a different matter: a droplet the operator is
# measuring has always been visible for seconds, and noise never is. This costs
# nothing in the gate, which makes no picks at all.
# How near a grid line a point has to be before its centroid is distrusted. The
# lines are about 2 px wide in the dongle's picture and a droplet is 5-9 px
# across, so this is "the line is inside the blob".
GRID_BIAS_PX = 5.0

PICK_MIN_AGE_S = 2.0


# How long a droplet stays "the one we were just looking at" after the square
# loses it.  A column, not a point: a droplet drifts a little and falls a long way,
# but it does not change column, which is the same identity dropletGroups uses to
# group falls into droplets.
SAME_PICK_MEMORY_S = 60.0


def samePickedDroplet(spot, t, now):
    """Is this track the droplet the last pick was on?  (x, y, t) or None."""
    if spot is None or t is None:
        return False
    return (now - spot[2] <= SAME_PICK_MEMORY_S
            and abs(float(t.pos[0]) - spot[0]) <= SAME_DROPLET_X_PX)


# ...and longer again when the aid is choosing for itself.  A click is the
# operator saying "this one" and is honoured at once; an automatic pick is a
# guess, and on 2026-09-21 it guessed a phantom in the first twenty seconds of a
# session and then described it to them for thirteen.  Noise does not last six
# seconds.
AUTO_PICK_MIN_AGE_S = 6.0


def oldEnoughToPick(t, now, minAge = None):
    """Has this track been on screen long enough to be somebody's droplet?"""
    pts = getattr(t, "points", None)
    want = PICK_MIN_AGE_S if minAge is None else minAge
    return bool(pts) and (now - pts[0][0]) >= want \
        and len(pts) >= CONFIRM_POINTS


PICK_FOLLOW_PX = 30.0         # how far from where it was predicted a droplet may be
                              # and still be the same one.  SMALLER than it looks: the
                              # search starts from the PREDICTED position, not the last
                              # one, so this is error in the prediction and not travel.
                              # At 45 px it silently jumped to neighbouring droplets --
                              # a crowded frame had two 30 px apart.
PICK_LIFT_PX = 90.0           # how far from the prediction to look DURING a lift.
                              # A lift accelerates the droplet through a 210 V step, so
                              # a constant-velocity prediction lags badly and the usual
                              # window loses it -- which an operator reported. Wider is
                              # safe here because a lift is also when the chamber is
                              # least ambiguous: everything charged is moving the same
                              # way and the speed test still has to agree.
PICK_SPEED_TOL = 0.40         # grid/s a re-acquired droplet may differ by, at range
PICK_PREDICT_S = 3.0          # how far ahead a dead track's velocity is still trusted
PICK_REACH_GROW = 6.0         # px added to the search radius per second missing
PICK_REACH_MAX_PX = 150.0
PICK_LOST_S = 15.0            # dropped after this long with nothing where it should be.
                              # Fifteen seconds, because that is how long the operator
                              # says recovery takes: past it the droplet has reached the
                              # bottom of the chamber and the field cannot reach it, so
                              # holding the square on a memory only misleads.
# How far q/e may sit from a whole number before the balance voltage is suspect.
#
# The charge comes out as q = k/U_n, so an error in the balance voltage lands straight
# on q -- and the one thing known in advance about q is that it must be a whole number
# of electrons.  Distance from that whole number is therefore a check on U_n that needs
# no reference measurement and can be run the moment a fall completes.
#
# Measured over every droplet the project has: the five sound ones sit 0.03, 0.08, 0.12,
# 0.16 and 0.16 off an integer, and the one whose balance voltage was 140 V -- below
# Ⅴ.5's advisory band -- sits 0.29 off and reports e nearly 10% low.  0.20 separates
# them, and an operator who sees it before writing the voltage down can re-balance
# instead of discovering it afterwards.
INTEGER_WARN = 0.20

# A chamber is "calm" once this few droplets are left that could still be the target.
# Below it the operator can see everything at once and so can the aid, and a suggestion
# is worth making even about a droplet no gate has passed.
CALM_TRACKS = 5

# How long the detection count must stay high before it is believed to be a spray.
#
# `noisy` is computed from the raw count in the current frame, so when the count sits
# near its threshold it flickers frame to frame -- and a flickering `noisy` cleared and
# re-set the measurement column on alternate frames.  The banner alternated between
# MEASURING and the picked droplet's verdict at the frame rate, and the selection
# square, which takes the banner's colour, strobed with it.  An operator described the
# box as doing disco.
#
# A spray fills the chamber for many seconds; nothing real is one frame long.
NOISY_HOLD_S = 1.0

# How far apart two speeds must be before the pick is presumed to have been stolen by
# a droplet passing through.  A traverse holds terminal velocity to a few percent, so a
# change of this size across a single re-acquisition is a different droplet, not the
# same one behaving differently.
STEAL_SPEED_GAP = 0.15        # grid/s
STEAL_SHOW_S = 6.0

# What the picked droplet is recognised BY when its track ends and several droplets are
# in reach.  Nearest-to-prediction alone picked the interloper when the operator's
# droplet crossed a grid line and another was passing.
#
# Two things about a droplet are stable across a lift and differ between droplets: how
# fast it falls with the field off, and how big its blob is.  Both are already
# maintained -- the free-fall rate for the charge test, the area for assignment -- so
# this weighs what is there rather than measuring anything new.  Position still decides
# when the other two cannot.
REACQUIRE_SPEED_W = 220.0     # px of positional error one grid/s of mismatch is worth
REACQUIRE_AREA_W = 60.0       # ...and one whole area-fraction of mismatch

PICK_HELD_SEARCH_S = 8.0      # ...but a droplet the field is HOLDING may blink for
                              # this long before it counts as lost.  A balanced droplet
                              # is motionless, small and dim -- the three things that
                              # make it hard to see -- and it goes dark for a second or
                              # two routinely.  At 2 s the aid announced "lost your
                              # droplet" three to twelve times a MINUTE through a
                              # 10-minute session on 2026-09-20, which is noise where a
                              # warning should be, and it was the only thing the live
                              # tutor ever had to talk about.
PICK_SEARCH_S = 2.0           # unmatched this long and the operator is told the aid
                              # is searching, rather than left watching a square sit on
                              # nothing for PICK_LOST_S.  A droplet moving fast leaves
                              # the prediction window at once and there is no reason to
                              # keep that quiet.
PICK_GONE_SHOW_S = 8.0        # how long the "it left, here is how to get it back" line
                              # stays up after the pick is dropped

# Fraction of the fall the adopting track must cover.
#
# 0.5 was a guess and it was wrong in the direction that matters: the fall this feature
# exists to recover -- 2026-09-10, `0` at 973.5 s and `1.6` at 998.8 s, track broken
# from 985.6 to 989.3 -- leaves a surviving segment of 9.5 s, 38% of the traverse. The
# guard would have rejected the very case it was built for, and the fixture could never
# have shown that because nothing on it needs adopting at all.
#
# 0.35 clears that case with margin while still demanding a third of the descent. The
# residual test does the rest of the work: a track must lie on the line joining the two
# crossings, which a droplet appearing near `1.6` does not.
# A track that dies just ABOVE `0` on its way down was about to start a fall, and the
# one place it is guaranteed to die is the line itself: grid rows are masked so nothing
# is detected within gridClear of them, which is where the operator is told to park.
# On 2026-09-17 a droplet was released from just above `0`, its track starved at the
# line, it reappeared 20 px below as a new track with no `0` of its own, and a complete
# fall went unrecorded.
#
# Rather than relax the mask -- tried twice, both times it lost a reference fall -- the
# crossing the dying track was ABOUT to make is recorded from its own velocity.  It
# becomes an unclaimed crossing like any other, and the track that reaches `1.6` adopts
# it through adoptZeroCrossing, which still has to prove it fell along the line joining
# the two and covered a third of the descent.  Nothing is claimed here; only offered.
PREDICT_ZERO_BAND = 2.0        # how many gridClear widths above `0` still counts
PREDICT_ZERO_MAX_S = 3.0       # ...and how far ahead its velocity may be believed

ADOPT_MIN_SPAN = 0.35
ADOPT_MAX_RESIDUAL_PX = 6.0   # furthest any of its points may sit off the line


LINE_CREDIT_DIV = 0.25          # how close to `16` counts as having reached it
# How long the droplet may be invisible across the frames the `16` crossing is
# interpolated between.  Two seconds is about a fifth of a division at the slow end
# of the usable band -- small beside a 30 s fall, and nothing like the seven the
# 2026-09-21 session guessed across.
CROSS_GAP_MAX_S = 2.0


def _creditAtLine(t, yBottom, spacing):
    """Finish a fall whose track died on the line, or None.  See the call site."""
    if spacing <= 0 or not t.points:
        return None
    lastT, lastY = float(t.points[-1][0]), float(t.points[-1][1])
    short = float(yBottom) - lastY
    if not 0.0 <= short <= LINE_CREDIT_DIV * spacing:
        return None
    down = [(ts, y) for ts, y in t.points if ts >= lastT - 2.0]
    if len(down) < 2 or down[-1][1] <= down[0][1]:
        return None                     # it was not still going down
    speed = (down[-1][1] - down[0][1]) / (down[-1][0] - down[0][0])
    if speed <= 0:
        return None
    t.tBottom = lastT + short / speed
    t.seconds = t.tBottom - t.tTop
    t.crossGap = 0.0                    # it was SEEN on the line; nothing is guessed
    t.legal = isLegalFall(FALL_DIVISIONS, t.seconds, getattr(t, "reacquired", 0))
    return t


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
                t.legal = isLegalFall(FALL_DIVISIONS, t.seconds,
                              getattr(t, "reacquired", 0))
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
        t.crossGap = None
        t.reacquired = 0
        t.adopted = False
        return None
    if t.tBottom is None and y0 < yBottom <= y1:
        t.tBottom = t0 + (yBottom - y0) / (y1 - y0) * (t1 - t0)
        t.seconds = t.tBottom - t.tTop
        # WAS THE DROPLET THERE WHEN IT CROSSED?  This interpolates between the last
        # frame above `16` and the first below it, which is exact across one frame and
        # a guess across a gap.  On 2026-09-21 a fall was filed at 27.91 s at the
        # moment the banner read "lost your droplet 7s ago": the crossing was
        # interpolated across that darkness, and the droplet had been falling at
        # 0.31-0.32 grid/s, which is 25 s for the eight divisions, not 28.  The
        # operator called the number statistically wrong before anyone looked at the
        # frames.
        #
        # Keeping the measurement means keeping the moment it ends.  A crossing
        # nobody saw is not one.
        t.crossGap = float(t1 - t0)
        t.legal = (isLegalFall(FALL_DIVISIONS, t.seconds,
                               getattr(t, "reacquired", 0))
                   and t.crossGap <= CROSS_GAP_MAX_S)
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
    # Which camera is the dongle is decided by the picture, not by its index:
    # the two Windows backends enumerate the same devices in opposite orders.
    # autoROI IS the test -- it is what fails, loudly and thirty seconds late,
    # when the wrong device is opened.
    LiveSource.accept = lambda gray: autoROI(gray) is not None
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
        writeLiveStatus(phase = "learning", learnSeconds = float(LIVE_LEARN_S),
                        startedAt = time.time(),
                        message = "Learning the background for %.0f s -- do not spray "
                                  "yet" % LIVE_LEARN_S)
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
    lastClickAt = 0.0               # ...and when they last clicked anything
    pickedAuto = -1e9               # when the aid last picked one for them
    clicks = []                     # filled by the mouse callback, drained in the loop
    remoteClicks = []               # ...and by a parent process, as fractions of the
                                    # picture the app shows -- see contentBox
    webBox = None                   # that picture: the frame without its black bars

    def onMouse(event, mx, my, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((mx, my))

    def readRemoteClicks():
        """Take clicks from whoever launched us, one JSON object per line.

        The browser app mirrors this window as pixels, so the operator could watch a
        session through it but not run one: picking a droplet is the whole
        interaction and the click had nowhere to go.

        stdin rather than a file or a socket, because a click is an EVENT.  A pipe
        gives ordering and delivered-once for nothing, it closes when this process
        dies so a stale click cannot arrive from a session that ended, and it adds no
        port to the process that must not fall over mid-measurement.  (Status is
        state, which is why that goes to a file.)

        Fractions of the frame, not pixels: the sender is scaling the picture to
        whatever size its window happens to be, and fractions survive that.
        """
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                got = json.loads(line)
                remoteClicks.append((float(got["x"]), float(got["y"])))
            except Exception:
                continue            # a malformed line is not worth a session

    if show and not sys.stdin.isatty():
        # Only when stdin is a pipe.  Run from a terminal it would eat the operator's
        # keystrokes and block on a read that never returns.
        threading.Thread(target = readRemoteClicks, daemon = True).start()

    if show:
        # Declare the process DPI-aware BEFORE the window exists.  Python is not, by
        # default, so on a laptop at 175% display scaling Windows took the 1280x720
        # frame as logical pixels and bitmap-stretched it to 2240x1260 -- a window
        # "significantly larger than its original size", and blurry with it (issue
        # #14).  Aware, the window is the frame at one screen pixel per image pixel,
        # sharp, and the mouse callback still receives image coordinates because the
        # window and the image are now the same size.  Windows only; a no-op elsewhere.
        if sys.platform == "win32":
            try:
                import ctypes
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per monitor
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass                    # an old Windows; scaling stays as it was
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
    zeroSince = None                # when the vote first read 0; see ZERO_HOLD_S
    lastVoltsAt = None              # when the panel last actually read
    lastFieldLevel = None           # off/work/lift, to drop held lines on a change
    panelAlign = None               # digit positions, found once and reused
    alignCells = collections.deque(maxlen = 8)      # median to align and read from
    # Short windows on purpose.  These lag the knob: the median is taken over the
    # last few samples and the mode over the last second, so a long window shows a
    # voltage the operator has already turned away from.  At 10 panel reads a second
    # this settles in well under a second while still outvoting a stray misread.
    # A SECOND OF READINGS, not half of one.  The panel is read ten times a
    # second and the mode of five spans 0.5 s, which is too short a vote when
    # single-frame reads are wrong a tenth of the time and wrong in scattered
    # ways: on 2026-09-22 the display alternated 104 and 184 while the knob sat
    # at 184, because five samples can split 3-2 on a bad digit.  Eleven cannot
    # split that way without most of them agreeing, and the knob takes longer
    # than a second to move anywhere the aid would act on differently.
    voltHistory = collections.deque(maxlen = 11)
    bestNow = None                  # the candidate the operator is being told about
    bestAt, bestSeen, bannerAt = None, 0.0, 0.0   # bestAt: WHERE, not which id
    ranked = []                     # last computed candidate list, reused between frames
    columnVolts = {}                # column -> {plate voltage: [descent rates]}
    columnVerdicts = {}             # column -> (charged?, when) -- outlives the rates
    liftWatch = LiftWatch()         # follows the panel and latches on a 提升 press
    pickedSeen = 0.0                # when the picked droplet was last seen...
    pickedAt = (0.0, 0.0)           # ...and where, so a dead track can be re-found
    pickedVolts = {}                # the PICK's own {volts: [rates]}, kept across the
                                    # track changes every lift causes
    pickedGone = None               # (when, which edge) after it left, to prompt recovery
    pickedDoubt = None              # (when, before, after) when a hand-over looked wrong
    pickedLevel = None              # the field state when the pick was last SEEN
    lastPickedId = None             # ...and which track it was, to drop stale banners
    committedAt = None              # when the operator took charge of this droplet
    lastPickSpot = None             # where the last pick was, and when
    pickSerial = 0                  # ...and which droplet of the session that is.  A
                                    # track id changes every time the tracker loses
                                    # the droplet for a moment, and the app announced
                                    # a new droplet each time: DROPLET 12, 13, 14
                                    # inside a minute, all of them the same droplet.
    noisySince = None               # when the detection count started running high
    lastVoltBucket = None           # the plate voltage last seen, bucketed to 10 V...
    voltsChangedAt = -1e9           # ...and when it changed, for CHARGE_SETTLE_S
    workingVolts = collections.deque(maxlen = 4000)   # (t, volts) whenever the field
                                    # is ON, so a fall can be given its voltage even
                                    # when the latched line refuses to hold it
    zeroCrossings = []              # every `0` crossing, so a broken track can still
                                    # be paired with its own start -- see adoptZeroCrossing
    voltsFailedAt = None            # when the `V:` line first refused to read
    chosenX = None                  # the column the operator has settled on
    chosenAt = None                 # ...and when it was pinned, to let a dead pin go
    chosenSeen = 0.0                # when a track was last alive in it
    banner, bannerColour = None, (0, 220, 255)
    detections = 0
    detSeries = []                  # (t, count) per frame, for spraySpans
    detRates = []                   # detections/frame, for the quiet level
    quietLevel = None               # frozen once, from the first clean stretch
    staleSince = None               # when the rate first went and stayed high
    resyncCooldown = 0.0            # no second rebuild while the first is settling
    resyncs = 0
    lastFunnel = 0.0                # when the charge-test funnel was last printed
    implausible = 0                 # panel readings above PANEL_MAX_V, i.e. misreads
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
            if webBox is None:              # the picture the app is shown; see contentBox
                webBox = contentBox(frame, (X, Y, W, H))
                print("  app view %dx%d at (%d,%d) of a %dx%d frame"
                      % (webBox[2], webBox[3], webBox[0], webBox[1],
                         frame.shape[1], frame.shape[0]), flush = True)
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
            # `or show` covers playback.  The panel reader was live-only, so a
            # recorded clip never read the plate voltage at all and the app's status
            # reported it as null for the whole session -- which a teammate reasonably
            # read as "the API returns nothing at 0 V".  Headless analysis is untouched:
            # it takes the voltage per fall by seeking back to the frames.
            if (src.isLive or show) and n % panelEvery == 0:
                # The same crop the field-off check uses, read once a frame instead of
                # once a fall.  A droplet's response to the field is the FIRST half of
                # Ⅳ.2's selection and nothing automated it before.
                vCell = cv2.cvtColor(frame[box[1]:box[3], box[0]:box[2]],
                                     cv2.COLOR_BGR2GRAY)
                # A READ THAT FAILS IS NOT A READING.  `volts` was never cleared
                # here, so whenever the median would not read -- which happens
                # whenever the knob is moving, and for a whole session on
                # 2026-09-21 when the alignment kept landing on 1111 -- the last
                # good value was handed on as though it had just been measured.
                # It was voted into steadyVolts, fed to the lift watch, and
                # appended to workingVolts with a FRESH timestamp, which is how
                # four legal falls came to be recorded at 144 V while the operator
                # balanced at 155 V and the panel said so on screen.  A wrong
                # balance voltage is a wrong charge; that is the whole measurement.
                volts = None
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
                        if got is not None and 0 <= got <= PANEL_MAX_V:
                            volts = got
                        elif got is not None:
                            # Keep the crop that produced it: a live run records only
                            # the ROI, so without this there is nothing to look at
                            # afterwards and the diagnosis is guesswork.
                            implausible += 1
                            if implausible == 1:
                                print("  the `V:` line read %d V, which the supply "
                                      "cannot make -- ignoring these; crop saved"
                                      % got, flush = True)
                                try:
                                    cv2.imwrite(os.path.join(outDir,
                                                             "panel_implausible.png"), med)
                                except Exception:
                                    pass
                if volts is not None:
                    lastVoltsAt = now
                elif lastVoltsAt is not None and now - lastVoltsAt > VOLTS_STALE_S:
                    # Nothing has been read for a while, so the aid does not know
                    # the voltage.  Saying so is what `None` is for: pickedVerdict
                    # answers "panel unreadable, cannot judge it" rather than
                    # judging against a number from a minute ago.
                    steadyVolts = None
                    voltHistory.clear()
                    zeroSince = None
                    # ...and no lift is being held either.  LiftWatch returns early
                    # on a reading of None, so once the panel stopped reading, a
                    # latched lift could never be released: the aid went on saying
                    # "lifting" -- which suspends the balance fit -- for as long as
                    # the reader was out.  Not knowing the voltage is not evidence
                    # of a lift.
                    if liftWatch.lifting:
                        liftWatch.lifting = False
                        liftWatch.since, liftWatch.base = now, None
                if volts is not None:
                    voltHistory.append(volts)
                    # The mode over the last few reads, so one misread cannot move it.
                    # The RAW value is what feeds observeVoltage: the charge test has
                    # to see a 0.5 s toggle, which smoothing would erase.
                    mode = collections.Counter(voltHistory).most_common(1)[0][0]
                    # ...and a ZERO has to last before it is believed.  Every verdict
                    # on the screen hangs off this one number -- which branch of
                    # pickedVerdict runs, whether the field reads off/work/lift,
                    # whether a speed is recorded as free fall and therefore whether
                    # the droplet is still called suitable -- and a dim glyph matches
                    # the `0` template better than anything else, so the panel's
                    # failure mode is to read 0000.
                    #
                    # On 2026-09-21 that cost, repeatedly and in one run: "falls
                    # -0.08 gr/s -- too slow, DISCARD" at 150 V; FIELD reading off at
                    # 147 V; and "DROPLET 2 is no longer suitable, currently falls at
                    # -0.03 grid/s" about a droplet the operator was holding balanced,
                    # which the aid itself had called suitable a minute earlier.
                    #
                    # Cutting to 0 V is a deliberate act and the panel then reads zero
                    # for the whole fall -- twenty seconds and more -- so waiting a
                    # second before believing it costs nothing.  Legality is not
                    # affected either way: a fall's field-off check votes over its own
                    # panel crops, not over this.
                    if mode == 0:
                        zeroSince = now if zeroSince is None else zeroSince
                    else:
                        zeroSince = None
                    if not (mode == 0 and steadyVolts
                            and now - zeroSince < ZERO_HOLD_S):
                        steadyVolts = mode
                # The RAW reading, not the mode: the lift is recognised by how fast
                # it moves, and voltHistory's mode would smear a one-reading step
                # across half a second, which is the very edge being measured.
                if fieldLevel(steadyVolts) != lastFieldLevel:
                    # See forgetInstruction: what the held line described is gone.
                    lastFieldLevel = fieldLevel(steadyVolts)
                    forgetInstruction()
                liftWatch.update(volts, now)
                # A clock running while the field is lifting is measuring nothing.
                #
                # The rise void below has to SEE the droplet climb, and during a lift
                # a track usually goes dark -- so on 2026-09-20 the operator raised a
                # droplet clean above `0` and its timer kept counting, through the
                # lift and out the other side. The field says what happened without
                # needing to see it: a fall timed at lift voltage can never be legal,
                # and neither can one past MAX_FALL_S, so both are voided here rather
                # than left running and rejected later.
                lifted = liftWatch.lifting or (volts and float(volts) > LIFT_VOLTS)
                for t in live:
                    if t.tTop is None or t.tBottom is not None:
                        continue
                    if lifted or now - t.tTop > MAX_FALL_S:
                        t.tTop = t.seconds = t.legal = None
                        t.fTop = t.yDeepest = None
                        t.reacquired, t.adopted = 0, False
                # When the plate voltage last MOVED, so the charge test can ignore the
                # samples taken while everything was still catching up.
                bucketNow = (int(round(volts / 10.0)) * 10
                             if volts is not None else None)
                # A panel that could not be READ is not a voltage that CHANGED.  This
                # compared the raw reading, so every dropped read flipped the bucket to
                # None and back and stamped voltsChangedAt twice.  With reads failing
                # now and then, the settle window never elapsed: an operator tuning at a
                # steady 175 V was told "settling after the voltage change" for
                # twenty-two seconds straight, got no balance line for three minutes,
                # and every window measured from voltsChangedAt was clamped to nothing.
                if bucketNow is not None and bucketNow != lastVoltBucket:
                    lastVoltBucket, voltsChangedAt = bucketNow, now
                # NOT while the 提升 button is held.  The lift adds 210 V, so the
                # last non-zero reading before a droplet is released can be the LIFT
                # voltage rather than the balance voltage -- a fall was recorded at
                # 460 V on 2026-09-14, which would make its charge nearly half what it
                # should be.  The fallback exists to recover the working voltage; the
                # lift is not it.
                if volts and not liftWatch.lifting:
                    workingVolts.append((now, int(volts)))

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
            # Where the tracker is already looking, and how sure it still is.
            keepNear, measuring = [], []
            for t in live:
                if len(t.points) < CONFIRM_POINTS:
                    continue                  # a probationary track proves nothing
                px, py = t.predict(now)
                gap = max(0.0, float(now) - float(t.lastT))
                keepNear.append((float(px), float(py),
                                 min(SECOND_CHANCE_MAX_PX,
                                     SECOND_CHANCE_PX + SECOND_CHANCE_GROW * gap)))
                if t.tTop is not None and t.tBottom is None:
                    measuring.append((float(px), float(py), MEASURE_RESCUE_PX))
            centres, _, _ = detectDroplets(gray, background, s, keepNear = keepNear,
                                           measuring = measuring)
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

            if now - lastFunnel >= 30.0:
                lastFunnel = now
                print("  charge test: %s" % chargeFunnel(live), flush = True)
            def completeFall(t, finished):
                """Everything a finished fall needs: field, voltage, CSV."""
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
                # A reading the apparatus cannot produce is a MISREAD, not
                # a measurement. On 2026-09-21 the digit alignment slipped one
                # place on a panel showing 0175 and voted 1759 V; the lift
                # guard below then binned it, and both legal falls of the
                # session came out with no voltage at all -- so no charge, and
                # charges.csv could never say 5 of 5. Discarding the READING
                # is right; discarding the FALL's voltage with it is not, when
                # the V: line still knows what the knob was set to.
                if (t.balanceVoltage is not None
                        and float(t.balanceVoltage) > PANEL_MAX_V):
                    panelReport["why"] = ("latched vote %.0f V is impossible; "
                                          "used the V: line instead"
                                          % float(t.balanceVoltage))
                    t.balanceVoltage = None
                t.voltageFrom = "latched" if t.balanceVoltage else None
                if src.isLive and not t.balanceVoltage:
                    # The latch did not hold.  Use the last steady non-zero
                    # reading before the droplet was released -- which is what
                    # the latched line is meant to be showing.
                    # ...and recently.  This window was the two minutes the
                    # banner uses, which is far more than the operator needs
                    # between setting the voltage and cutting to 0 -- and long
                    # enough to hand a fall the voltage of the droplet before it.
                    # No number is better than a wrong one: the charge is
                    # computed from this.
                    got = next((v for ts, v in reversed(workingVolts)
                                if ts <= t.tTop
                                and t.tTop - ts <= WORKING_VOLTS_RECORD_S),
                               None)
                    if got:
                        t.balanceVoltage = got
                        t.voltageFrom = "V: line"
                # A live run records only the ROI, so when the panel
                # cannot be read there is otherwise nothing left to
                # look at: four of nine legal falls came back with no
                # voltage on 2026-09-08 and the diagnosis had to be
                # guesswork.  Keep the crop and the vote spread.
                # Nothing balances above LIFT_VOLTS.  On 2026-09-20 a legal
                # fall was filed at 407 V, because the droplet had been lifted
                # shortly before it was released and that is what the panel
                # was showing.  dropletGroups already refuses to group such a
                # fall; the CSV kept the number anyway, which is a wrong
                # balance voltage in the data the team works from.  No number
                # is better than that one.
                if (t.balanceVoltage is not None
                        and float(t.balanceVoltage) > LIFT_VOLTS):
                    t.balanceVoltage, t.voltageFrom = None, "lift, ignored"
                if (src.isLive and t.balanceVoltage is None
                        and t.kinematicLegal):
                    saveRefusedPanel(outDir, t.tTop, panelReport)
                t.completedFalls = getattr(t, "completedFalls", 0) + 1
                # WHOSE FALL THIS IS, settled here and not asked again.  The
                # operator's droplet if this track IS the pick, or if it fell in
                # the pick's column: the square loses a droplet mid-fall often
                # enough that demanding identity would throw away measurements
                # the operator watched happen.
                t.droplet = None
                if picked is not None:
                    near = abs(float(finished.pos[0]) - float(picked.pos[0]))
                    if finished is picked or near <= SAME_DROPLET_X_PX:
                        t.droplet = pickSerial
                elif lastPickSpot is not None:
                    # The square had lost it, which is not the droplet's fault.
                    gone = now - lastPickSpot[2]
                    off = abs(float(finished.pos[0]) - lastPickSpot[0])
                    if gone <= SAME_PICK_MEMORY_S and off <= SAME_DROPLET_X_PX:
                        t.droplet = pickSerial
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
                         ("crossing guessed across %.1f s dark - rejected"
                          % getattr(t, "crossGap", 0.0)
                          if getattr(t, "crossGap", 0.0) > CROSS_GAP_MAX_S else
                          "field ON - rejected" if t.kinematicLegal
                          and t.fieldOff is False else
                          "panel UNREADABLE - rejected" if t.kinematicLegal
                          else "rejected")),
                      flush = True)

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
                    if now - voltsChangedAt >= CHARGE_SETTLE_S:
                        t.observeVoltage(volts, spacing)
                        noteColumnVoltage(columnVolts, t, volts, spacing)
                    t.gridRows = s["gridRows"]     # see recentSpeed
                    updateDivision(t, s["gridRows"])
                    updateHold(t, yTop, spacing, now, volts, columnVolts,
                               now - voltsChangedAt if voltsChangedAt else None,
                               liftWatch.lifting)
                    finished = updateCrossings(t, yTop, yBottom, spacing,
                                               zeroCrossings)
                    if finished is not None and finished.legal:
                        noteMeasuredFree(finished.xs[-1] if getattr(finished, "xs", None)
                                         else finished.pos[0], finished.seconds)
                    if finished is not None:
                        completeFall(t, finished)
                else:
                    t.miss += 1
                    # What the DETECTOR saw where this track is pointing, every frame
                    # it is dark -- not just at the moment it is given up on.  The
                    # question behind every lost measurement is whether the droplet
                    # was still in the picture, and answering it from outside the
                    # pipeline failed twice: a blob detector of my own could not tell
                    # the droplet from the specks around it, scoring the same in a
                    # control column 150 px away.  The pipeline's own gates already
                    # make that distinction, so let them answer.
                    if t.tTop is not None and t.tBottom is None \
                            and len(t.points) >= CONFIRM_POINTS:
                        waitedDark = now - float(t.lastT)
                        aimX, aimY = t.predict(now)
                        seen = min((float(np.hypot(c[0] - aimX, c[1] - aimY))
                                    for c in centres), default = None)
                        gated = nearestRejected(aimX, aimY)
                        tally = getattr(t, "darkTally", None)
                        if tally is None:
                            tally = t.darkTally = {"frames": 0, "accepted": 0,
                                                   "rejected": 0, "gates": {},
                                                   "early": 0, "earlyAcc": 0,
                                                   "earlyRej": 0, "why": {}}
                        tally["frames"] += 1
                        # ...and when something WAS there and we did not take it,
                        # which of the gates said no.  "Seen but not assigned" is
                        # the half of these losses that is ours to fix, and the
                        # fix depends entirely on which test refused it.
                        if seen is not None and seen <= DARK_LOOK_PX:
                            j = min(range(len(centres)),
                                    key = lambda k: np.hypot(centres[k][0] - aimX,
                                                             centres[k][1] - aimY))
                            c = centres[j]
                            # Gates first, "claimed" last: a blob this track was
                            # never allowed to take is not one another track stole.
                            if abs(float(c[2]) - t.area) / max(t.area,
                                                               s["areaFloor"])                                     > AREA_TOLERANCE:
                                why = "area too different"
                            elif abs(c[0] - t.pos[0]) > s["reidX"]:
                                why = "outside the x window"
                            elif j in set(pairs.values()):
                                why = "allowed, but another track took it"
                            else:
                                why = "outside the y window or step"
                            tally["why"][why] = tally["why"].get(why, 0) + 1
                        # The first couple of seconds separately.  After that the aim
                        # is extrapolated velocity and has wandered, so "nothing
                        # there" says as much about the prediction as the picture.
                        early = waitedDark <= DARK_EARLY_S
                        tally["early"] += int(early)
                        if seen is not None and seen <= DARK_LOOK_PX:
                            tally["accepted"] += 1
                            tally["earlyAcc"] += int(early)
                        elif gated is not None and gated[0] <= DARK_LOOK_PX:
                            tally["rejected"] += 1
                            tally["earlyRej"] += int(early)
                            tally["gates"][gated[1]] = tally["gates"].get(gated[1], 0) + 1
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
                    # About to cross `0` and dying on the line: leave the crossing it
                    # was going to make, for whatever comes out below to adopt.
                    if (t.tTop is None and float(t.vel[1]) > 0
                            and t.pos[1] < yTop
                            and yTop - float(t.pos[1])
                            <= PREDICT_ZERO_BAND * s["gridClear"]):
                        gap = (yTop - float(t.pos[1])) / float(t.vel[1])
                        if 0 < gap <= PREDICT_ZERO_MAX_S:
                            noteZeroCrossing(zeroCrossings, float(t.lastT) + gap,
                                             float(t.xs[-1]) if getattr(t, "xs", None)
                                             else float(t.pos[0]))
                    if measuring:
                        # A track dying mid-fall costs a measurement, and the fix
                        # depends on WHY it died: a droplet that was genuinely
                        # invisible for longer than the coast is a detection problem,
                        # one with a blob sitting right beside it is an assignment
                        # problem.  Guessing between them cost a failed gate on
                        # 2026-09-14, so the live run is asked instead.
                        near = min((float(np.hypot(c[0] - t.pos[0], c[1] - t.pos[1])),
                                    float(c[2])) for c in centres) if centres else None
                        # ROI coordinates: REJECT_POINTS is recorded in the same frame
                        # of reference the tracks live in.
                        gated = nearestRejected(t.pos[0], t.pos[1])
                        # ...and WHEN, so the raw footage at that moment can be looked
                        # at afterwards.  Without it these lines could only be counted,
                        # not checked: the frame is what says whether the droplet was
                        # really invisible or merely unassigned.
                        tally = getattr(t, "darkTally", None) or {}
                        gates = ", ".join("%s %d" % kv for kv in
                                          sorted(tally.get("gates", {}).items(),
                                                 key = lambda kv: -kv[1])[:3])
                        print("  mid-fall track %d died at %.1f s (frame %d) x=%.0f "
                              "y=%.0f after %.1f s dark (limit %.1f); own area %.0f; "
                              "nearest ACCEPTED %s; nearest REJECTED %s"
                              % (t.id, now - (firstT or 0.0), n,
                                 t.pos[0], t.pos[1], waited, allowed, t.area,
                                 "none" if near is None else "%.0f px (area %.0f)"
                                 % (near[0], near[1]),
                                 "none" if gated is None else
                                 "%.0f px (%s, area %.0f, circ %.2f)"
                                 % (gated[0], gated[1], gated[2], gated[3])),
                              flush = True)
                        if tally.get("frames"):
                            print("      while dark: %d frames, ACCEPTED within "
                                  "%.0f px on %d, REJECTED on %d%s | first %.0fs: "
                                  "%d frames, %d accepted, %d rejected"
                                  % (tally["frames"], DARK_LOOK_PX, tally["accepted"],
                                     tally["rejected"],
                                     " (%s)" % gates if gates else "",
                                     DARK_EARLY_S, tally["early"], tally["earlyAcc"],
                                     tally["earlyRej"]),
                                  flush = True)
                            if tally.get("why"):
                                print("      not taken because: %s"
                                      % ", ".join("%s %d" % kv for kv in
                                                  sorted(tally["why"].items(),
                                                         key = lambda kv: -kv[1])),
                                      flush = True)
                    # A droplet seen ON the finish line has finished.  The
                    # crossing test needs a frame BELOW `16` to interpolate between,
                    # and the last thing a falling droplet does is dim out as it
                    # reaches the bottom of the lit field -- so the one frame it needs
                    # is the one it never gets.  On 2026-09-21 the operator watched a
                    # 23.7 s fall reach the line with the clock running, the track
                    # died there, and nothing was recorded: their second fall of the
                    # session, gone, and the aid told them it had "lost your droplet".
                    #
                    # Credit it only where there is nothing left to doubt: the clock
                    # was running, the droplet was last seen within a quarter division
                    # of the line, it was still going down, and the remaining sliver
                    # is crossed at the speed it was doing.  That last step is the
                    # same arithmetic updateCrossings does between two frames, over a
                    # shorter distance.
                    if (t.tTop is not None and t.tBottom is None
                            and len(t.points) >= CONFIRM_POINTS):
                        gone = _creditAtLine(t, yBottom, spacing)
                        if gone is not None:
                            completeFall(t, gone)
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
                    fresh = Track(c, now)
                    # Inherit what was learned about the droplet that was just here.
                    #
                    # A track ends on every lift, and the replacement starts with no
                    # voltage history, so chargeState() answers "unknown" and a droplet
                    # the operator has plainly proved inert is never marked. Reading the
                    # COLUMN instead pools everything within 90 px and mis-marked
                    # charged droplets, so neither answer worked on its own.
                    #
                    # The history is not the track's, it is the DROPLET's. A track that
                    # died in this column moments ago was watching the same droplet, so
                    # its observations carry over -- gated tightly, because inheriting
                    # the wrong droplet's history is how a charged one gets crossed out.
                    for dead in morgue:
                        if (now - dead.points[-1][0] <= CHARGE_WINDOW_S
                                and abs(dead.xs[-1] - c[0]) <= SAME_DROPLET_X_PX
                                and getattr(dead, "byVolts", None)):
                            fresh.byVolts = {v: list(r) for v, r in dead.byVolts.items()}
                            break
                    live.append(fresh)
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
                    busy = (quietLevel is not None
                            and len(centres) > max(3.0, STALE_FACTOR * quietLevel))
                    if not busy:
                        noisySince = None
                    elif noisySince is None:
                        noisySince = now
                    # Believed only once it has HELD.  See NOISY_HOLD_S.
                    noisy = (noisySince is not None
                             and now - noisySince >= NOISY_HOLD_S)

                    lifting = liftWatch.quiet(now)
                    # A click selects the nearest droplet; clicking bare field
                    # clears the selection.  The window shows the whole frame with the
                    # ROI pasted in at (X, Y), so a window coordinate is an ROI
                    # coordinate minus that origin.
                    # A remote click arrives as a fraction of the frame; the mouse
                    # callback reports pixels of the same frame, because the window is
                    # AUTOSIZE and shows it 1:1.  Convert and they are the same event.
                    while remoteClicks:
                        fx, fy = remoteClicks.pop()
                        # Fractions of the picture the app is SHOWING -- the frame
                        # with its black bars cut off, see contentBox.  The window
                        # shows the whole frame, so the click is put back where a
                        # mouse click on the same droplet would have landed.
                        bx, by, bw, bh = webBox or (0, 0, frame.shape[1], frame.shape[0])
                        clicks.append((int(round(fx * bw)) + bx,
                                       int(round(fy * bh)) + by))
                    while clicks:
                        mx, my = clicks.pop()
                        rx, ry = mx - X, my - Y
                        near = [t for t in live
                                if not t.miss and oldEnoughToPick(t, now)
                                and np.hypot(t.pos[0] - rx, t.pos[1] - ry)
                                <= PICK_RADIUS_PX]
                        was = picked
                        picked = (min(near, key = lambda t: np.hypot(t.pos[0] - rx,
                                                                    t.pos[1] - ry))
                                  if near else None)
                        # Clicking a DIFFERENT droplet starts a different measurement.
                        # committedAt was set on the session's first pick and cleared
                        # only when nothing was picked AND no column was pinned, so it
                        # survived every re-pick: the banner read "MEASURING -- 1 of 5
                        # falls" about a droplet being balanced at 214 V, counting a
                        # 250 V fall that had ended four minutes earlier.
                        #
                        # "Different" is measured against the COLUMN BEING MEASURED,
                        # not against the previous pick.  Against the pick it reset on
                        # almost every re-click, because the pick is what wandered off
                        # to a stray in the first place -- which is how five legal
                        # falls of one droplet were shown as "1 of 5".
                        # ...and against the FALLS ALREADY COUNTED as well as the
                        # pinned column, because the pin is released whenever the
                        # column goes quiet. On 2026-09-20 an operator's track died,
                        # they clicked their droplet again -- the same droplet, in the
                        # same place -- and the measurement started over: "all our
                        # MEASURING falls were gone". Falls that have been counted are
                        # a better record of where the droplet is than a pin that has
                        # already been let go.
                        mineNow = dropletFalls(falls, float(picked.pos[0]),
                                               committedAt) if picked is not None else []
                        nearOwnFalls = bool(mineNow)
                        # ...and against the droplet that was picked a moment ago.
                        # Nothing is pinned until a measurement starts, so with only
                        # the pin and the falls to go on, every re-click of the same
                        # droplet counted as a new one: an operator clicking theirs a
                        # few times between 3:49 and 3:55 on 2026-09-21 watched it go
                        # from DROPLET 4 to DROPLET 8. Losing the track is the reason
                        # they are clicking again; it does not make the droplet new.
                        samePlace = samePickedDroplet(lastPickSpot, picked, now)
                        if picked is not None and not nearOwnFalls and not samePlace and (
                                chosenX is None
                                or abs(float(picked.pos[0]) - chosenX)
                                > SAME_DROPLET_X_PX):
                            committedAt = now
                            pickSerial += 1
                        if picked is not None:
                            lastPickSpot = (float(picked.pos[0]), float(picked.pos[1]), now)
                        pickedSeen = now
                        pickedLevel = fieldLevel(steadyVolts)
                        lastClickAt = now
                        pickedAt = ((float(picked.pos[0]), float(picked.pos[1]))
                                    if picked is not None else (0.0, 0.0))
                    # Nobody has clicked for a while, nothing is being timed, and there
                    # are few enough droplets that the operator is choosing rather than
                    # waiting: choose for them.  Nearest the middle, because near either
                    # line one wrong toggle carries the droplet off the picture before
                    # it can be balanced.  Never while a fall clock is running -- that
                    # droplet is the measurement and attention must not move off it.
                    if (picked is None and now - lastClickAt >= IDLE_PICK_S
                            and now - pickedAuto >= IDLE_PICK_S
                            and not any(t.tTop is not None and t.tBottom is None
                                        for t in live)):
                        room = [t for t in live
                                if not t.miss
                                and oldEnoughToPick(t, now, AUTO_PICK_MIN_AGE_S)
                                and columnChargeState(columnVolts, t.pos[0], now,
                                                      columnVerdicts) is not False]
                        if 0 < len(room) <= CALM_TRACKS:
                            mid = (yTop + yBottom) / 2.0
                            picked = min(room, key = lambda t: abs(t.pos[1] - mid))
                            pickedSeen = pickedAuto = now
                            pickedAt = (float(picked.pos[0]), float(picked.pos[1]))
                            # The same droplet, found again, is not droplet number
                            # two.  This is the path that runs when the square has
                            # lost its droplet and the aid picks one itself, which is
                            # exactly when it is most likely to land back on the one
                            # the operator is working on: on 2026-09-21 the square
                            # dropped their droplet, re-found it fifteen seconds
                            # later, and the panel renumbered it DROPLET 2.
                            if not samePickedDroplet(lastPickSpot, picked, now):
                                committedAt = now  # a new droplet, a new measurement
                                pickSerial += 1
                            lastPickSpot = (float(picked.pos[0]), float(picked.pos[1]), now)
                    # HOLD the track while it lives.  Re-selecting the nearest track
                    # every frame is not following a droplet, it is following a
                    # neighbourhood, and in a crowded frame it changes its mind: two
                    # droplets 30 px apart handed the square back and forth.
                    if picked is not None:
                        # A coasting pick is still aimed at its droplet, but it has not
                        # SEEN it, and "lost your droplet" is written off pickedSeen:
                        # an operator watched the square sit on their droplet for seven
                        # seconds while the banner insisted it was lost.  The track's
                        # own last sighting is the honest answer to "when was it seen".
                        if picked in live and not picked.miss:
                            pickedSeen = max(pickedSeen, float(picked.lastT))
                        if picked in live and picked.miss and (
                                now - float(picked.lastT)) > PICK_SEARCH_S:
                            # Coasting long enough to be worth a second look: a
                            # confirmed track sitting where the droplet should be IS
                            # the droplet, and taking it back keeps the square with it
                            # during a lift, which is when tracks break most.
                            aim = picked.predict(now)
                            reach = PICK_LIFT_PX if liftWatch.lifting else PICK_FOLLOW_PX
                            # Moving like the droplet, not merely sitting near it.
                            # During a lift the reach is 90 px and grows with the gap,
                            # and a speck above `0` was close enough to qualify: the
                            # square jumped to it mid-lift and stayed until the
                            # operator clicked their droplet again.  A droplet being
                            # carried up is going UP, and its speed does not jump.
                            was = picked.recentSpeed(spacing)
                            def sameMotion(q):
                                v = q.recentSpeed(spacing)
                                if was is None or v is None:
                                    return True       # nothing to contradict
                                if liftWatch.lifting and v >= 0:
                                    return False      # not rising during a lift
                                if abs(was) > BALANCED_MAX and abs(v) > BALANCED_MAX \
                                        and (v > 0) != (was > 0):
                                    # One is going up and the other down.  At 249 V an
                                    # operator's rising droplet was handed to a falling
                                    # one, which no speed tolerance should ever allow:
                                    # direction is not a matter of degree.
                                    return False
                                return abs(v - was) <= STEAL_SPEED_GAP * 2.0
                            # NO AGE GATE HERE.  oldEnoughToPick keeps the square
                            # off two-frame noise when it is CHOOSING a droplet; this
                            # is the square taking back the droplet it was already on,
                            # and the evidence is the position and the motion, which
                            # are tested below.  Requiring two seconds of history meant
                            # a droplet that reappeared as a fresh track could not be
                            # claimed until the coast had already expired: on
                            # 2026-09-21 the operator watched detection come back and
                            # had to click their droplet again by hand.
                            near = [q for q in live
                                    if q is not picked and not q.miss
                                    and abs(q.pos[0] - aim[0]) <= s["reidX"]
                                    and np.hypot(q.pos[0] - aim[0],
                                                 q.pos[1] - aim[1]) <= reach
                                    and sameMotion(q)]
                            if near:
                                picked = min(near, key = lambda q: np.hypot(
                                    q.pos[0] - aim[0], q.pos[1] - aim[1]))
                                pickedAt = (float(picked.pos[0]), float(picked.pos[1]))
                                pickedSeen = now
                                pickedLevel = fieldLevel(steadyVolts)
                        if picked in live:
                            # A COASTING track (miss > 0) is still live and still aimed
                            # at its droplet.  Treating it as lost and re-selecting the
                            # nearest track every frame is what made the square flicker
                            # between neighbours whenever the droplet blinked -- which
                            # is most often exactly when it is balanced and still.
                            if not picked.miss:
                                pickedAt = (float(picked.pos[0]), float(picked.pos[1]))
                                pickedSeen = now
                                pickedLevel = fieldLevel(steadyVolts)
                        else:
                            # Its track died -- which happens on every lift -- so look
                            # where the droplet should have GOT to, not where the track
                            # was last seen.  Without the prediction a lifted droplet is
                            # always out of range by the time anything is looking.
                            gap = now - pickedSeen
                            # A velocity extrapolated over ten seconds is not a
                            # prediction.  A droplet went dark on a grid line at 1:58,
                            # came back at 2:11 within a few pixels of where it left,
                            # and the aim had walked far past it: nothing was within
                            # reach, so the pick was dropped as "left the view".
                            # Predict only as long as the velocity means anything, then
                            # stop moving the aim and widen the search instead.
                            fly = min(gap, PICK_PREDICT_S)
                            aim = (pickedAt[0] + float(picked.vel[0]) * fly,
                                   pickedAt[1] + float(picked.vel[1]) * fly)
                            reach = (PICK_LIFT_PX if liftWatch.lifting
                                     else PICK_FOLLOW_PX)
                            reach = min(PICK_REACH_MAX_PX, reach + PICK_REACH_GROW * gap)
                            # ...and the same here: this is the pick being handed
                            # back to where its droplet was predicted to be.
                            near = [t for t in live
                                    if not t.miss
                                    and np.hypot(t.pos[0] - aim[0], t.pos[1] - aim[1])
                                    <= reach]
                            # A droplet falls and rises; it does not travel sideways.
                            # The radius grows to 150 px while a track is missing, and
                            # in x that is a quarter of the picture: on 2026-09-20 the
                            # square kept jumping to droplets halfway across the frame
                            # while the operator's own was merely invisible for a
                            # second.  Keep the generous vertical reach, bound x to
                            # the same window everything else calls one droplet.
                            near = [t for t in near
                                    if abs(t.pos[0] - aim[0]) <= SAME_DROPLET_X_PX]
                            if not near:
                                # One confirmed droplet in an otherwise empty picture
                                # can only be the one that was picked -- as long as it
                                # is where that droplet could be.  Without the test
                                # this rule had no distance limit at all, and handed
                                # the pick to whatever single track happened to exist.
                                alone = [t for t in live if not t.miss
                                         and len(t.points) >= CONFIRM_POINTS
                                         and abs(t.pos[0] - aim[0]) <= SAME_DROPLET_X_PX]
                                if len(alone) == 1:
                                    near = alone
                            wasSpeed = picked.recentSpeed(spacing)
                            wasArea = float(getattr(picked, "area", 0.0) or 0.0)
                            if near and wasSpeed is not None:
                                # Widening the search made recovery possible and swaps
                                # easy: the square jumped from a droplet rising at
                                # -0.75 grid/s to one falling at 0.19, and again from
                                # 2.55 to -0.75.  At arm's length the speed has to
                                # match; only right where the droplet was may it differ.
                                near = [c for c in near
                                        if np.hypot(c.pos[0] - aim[0], c.pos[1] - aim[1])
                                        <= PICK_FOLLOW_PX
                                        or (c.recentSpeed(spacing) is not None
                                            and abs(c.recentSpeed(spacing) - wasSpeed)
                                            <= PICK_SPEED_TOL)]
                            if near:
                                # Score every droplet in reach on where it should be,
                                # how fast it falls and how big it is -- not distance
                                # alone.  A droplet passing through is usually the
                                # nearest thing and almost never the right speed.
                                def _cost(t):
                                    c = np.hypot(t.pos[0] - aim[0], t.pos[1] - aim[1])
                                    sp = t.recentSpeed(spacing)
                                    if sp is not None and wasSpeed is not None:
                                        c += REACQUIRE_SPEED_W * abs(sp - wasSpeed)
                                    ar = float(getattr(t, "area", 0.0) or 0.0)
                                    if ar > 0 and wasArea > 0:
                                        c += REACQUIRE_AREA_W * abs(ar - wasArea)                                             / max(ar, wasArea)
                                    return c
                                picked = min(near, key = _cost)
                                # A traverse holds terminal velocity to a few percent,
                                # so a speed that jumps across the hand-over is a
                                # different droplet passing through -- which is what
                                # happened when the operator's droplet crossed a grid
                                # line and the square stayed on the interloper.  Say so
                                # rather than quietly measuring the wrong thing.
                                # Say so after EVERY hand-over, not only a doubtful
                                # one: the operator asked to be told whenever the
                                # selection changed hands, so a wrong guess can be
                                # corrected with one click instead of being discovered
                                # five falls later.
                                nowSpeed = picked.recentSpeed(spacing)
                                # ...unless the FIELD changed while the track was
                                # missing, in which case the droplet is supposed to be
                                # moving differently and a speed gap says nothing about
                                # its identity.  Toggling 0V/工作 every fall, an
                                # operator was warned fifteen times in one session that
                                # their droplet "may be a DIFFERENT droplet".
                                # Forgiven only when the field STATE changed across
                                # the gap -- 0V to working, working to lift.  "The
                                # voltage moved" was too broad: a lift IS a voltage
                                # move, so the one hand-over that mattered went
                                # unreported.  Two droplets rose side by side at
                                # 0.17 and 0.33 grid/s, the square took the slower one,
                                # and the aid said nothing.  Inside ONE state a speed
                                # that jumps is still a different droplet.
                                # The panel READING lags the switch -- it is OCR of a
                                # display that redraws in its own time -- so at the
                                # moment of a toggle both sides of the gap can read the
                                # same state while the field has already changed.  An
                                # operator saw "may be a DIFFERENT droplet" on a droplet
                                # that had simply stopped falling because they had just
                                # pressed 提升.  A change anywhere near the gap counts.
                                nowLevel = fieldLevel(steadyVolts)
                                fieldMoved = (pickedLevel is not None
                                              and nowLevel is not None
                                              and nowLevel != pickedLevel)
                                if voltsChangedAt and now - voltsChangedAt <= PANEL_LAG_S:
                                    fieldMoved = True
                                pickedDoubt = (now, wasSpeed, nowSpeed,
                                               wasSpeed is not None
                                               and nowSpeed is not None
                                               and not fieldMoved
                                               and abs(nowSpeed - wasSpeed)
                                               > STEAL_SPEED_GAP)
                                pickedAt = (float(picked.pos[0]), float(picked.pos[1]))
                                pickedSeen = now
                                pickedLevel = fieldLevel(steadyVolts)
                            elif gap > PICK_LOST_S:
                                # Say which way it went, because the recovery differs:
                                # off the top means the field is still lifting it, off
                                # the bottom means it is falling away.
                                pickedGone = (now, "top" if pickedAt[1] < yTop
                                              else "bottom" if pickedAt[1] > yBottom
                                              else "edge")
                                picked, pickedVolts = None, {}
                    # Recorded against the PICK, so it survives the track ending --
                    # which a lift always does, and a lift is the very thing being
                    # tested for.
                    if picked is not None and steadyVolts is not None and spacing > 0:
                        pv = picked.recentSpeed(spacing)
                        if pv is not None:
                            bucket = pickedVolts.setdefault(
                                int(round(steadyVolts / 10.0)) * 10, [])
                            bucket.append(float(pv))
                            if len(bucket) > 200:
                                del bucket[0]
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
                        # Clicking says "this is my droplet", NOT "I am measuring".
                        # Treating it as the latter pinned the column the instant the
                        # operator picked something, and MEASURING then outranked the
                        # verdict about the very droplet they had just clicked -- before
                        # anything was balanced, which is when that verdict matters
                        # most.  A measurement starts when the droplet is held on the
                        # line, whether or not it was picked by hand.
                        # `completedFalls > 0` used to sit here and it is the same
                        # mistake this function has now made four times: a completed
                        # legal fall proves a droplet FELL legally, not that anyone
                        # chose it. The operator sprays with the field off, a stray
                        # falls freely at 0.29 grid/s, and the column pins to it --
                        # which empties the candidate list and suppresses every ring
                        # outside that column, so the hunt aid goes quiet, nothing gets
                        # marked uncharged, and the banner reads MEASURING 1 of 5 with
                        # nothing balanced. One cause, three symptoms.
                        #
                        # Only the operator's own commitment starts a measurement: a
                        # droplet held on the top line, or one they clicked.
                        if heldLongEnough(t, now):
                            return True
                        # ...or the droplet the operator PICKED, once one of its own
                        # falls is in the book.  The hold test asks for IV.4's first
                        # step -- balanced on the top line for fifteen seconds -- and
                        # an operator who balances mid-screen instead never satisfies
                        # it.  On 2026-09-21 that meant a legal fall was timed,
                        # recorded and announced, and the tally never appeared at all:
                        # no MEASURING 1 of 5, nothing to say the session had started.
                        #
                        # This is not the old "a legal fall pins the column" mistake,
                        # which fired on strays nobody had chosen.  It fires only on
                        # the track the operator themselves picked.
                        return (picked is not None and t is picked
                                and bool(dropletFalls(falls, float(t.pos[0]),
                                                      committedAt)))


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
                            chosenX, chosenAt = float(settledTrack.pos[0]), now
                        chosenSeen = now
                    elif balanced is not None:
                        if chosenX is None:
                            chosenX, chosenAt = balanced["pos"][0], now
                        chosenSeen = now
                    elif chosenX is not None and any(
                            abs(float(t.pos[0]) - chosenX) <= SAME_DROPLET_X_PX
                            for t in live if not t.miss):
                        chosenSeen = now           # still occupied, between drops
                    # The operator's click is the subject, and the pin has to agree
                    # with it.  Pinned on a column that was then sprayed empty, the pin
                    # kept the banner for its full 90 s idle life while the operator
                    # tuned a droplet 200 px away: MEASURING -- 0 of 5 falls, no balance
                    # reading, and the occasional number computed on whatever dust
                    # drifted through the dead column.  A pin with nothing measured in
                    # it has no claim on anything.
                    #
                    # It MOVES a pin, it does not create one: a click is a question
                    # about a droplet, not a decision to measure it, and pinning on
                    # every click would clear the candidate rings the operator is
                    # still hunting with.
                    # The click decides, full stop.  The old rule refused to move a
                    # pin off a column that had any fall in it -- meant to protect a
                    # measurement in progress, it instead protected a single stray fall
                    # from earlier in the session: the pin sat 100 px from the droplet
                    # the operator was measuring, MEASURING could not own the banner
                    # because the pick was outside the pinned column, and five falls
                    # were reported as USABLE over and over.  Releases still happen on
                    # spray, on loss and on idle, so nothing is lost by trusting it.
                    if (picked is not None and chosenX is not None
                            and not liftWatch.lifting
                            and abs(float(picked.pos[0]) - chosenX) > SAME_DROPLET_X_PX):
                        chosenX, chosenAt, chosenSeen = float(picked.pos[0]), now, now
                    # An empty column with nothing measured in it is released at once,
                    # not after the idle timer: there is nothing to protect.
                    if (chosenX is not None and not dropletFalls(falls, chosenX)
                            and not any(abs(float(t.pos[0]) - chosenX)
                                        <= SAME_DROPLET_X_PX
                                        for t in live if not t.miss)):
                        chosenX, chosenAt = None, None
                    # ...and so is a FINISHED one nobody is adding to.  Five falls
                    # were counted at 7:34 and the banner still said so at 14:36, while
                    # the operator was three falls into a different droplet.
                    if chosenX is not None:
                        done = dropletFalls(falls, chosenX)
                        if (len(done) >= TARGET_FALLS_PER_DROPLET
                                and now - max((f.tTop or 0.0) for f in done)
                                > MEASURE_IDLE_S):
                            chosenX, chosenAt = None, None
                    if noisy:
                        # A spray puts new droplets in every column, so what was
                        # established about the old ones no longer describes what is
                        # being ringed.  The verdict outlives its samples but not this.
                        columnVerdicts.clear()
                    # A column that has measured nothing is not a measurement.  Pinned
                    # once, it stayed pinned as long as ANY droplet loitered in it, and
                    # an operator watched "MEASURING -- 0 of 5 falls" sit on the screen
                    # for three minutes while the aid said nothing useful about the
                    # droplet they were actually tuning.
                    if (chosenX is not None and chosenAt is not None
                            and now - chosenAt > CHOSEN_IDLE_S
                            and not dropletFalls(falls, chosenX)):
                        chosenX, chosenAt = None, None
                    if chosenX is not None and (noisy or now - chosenSeen > CHOSEN_RELEASE_S):
                        chosenX, chosenAt = None, None   # sprayed, or the column emptied
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
                    # Held by WHERE it is, not by which track id it currently has.
                    # Matching on id looks stable and is not: with a dozen droplets in
                    # the chamber the ids churn constantly, the held one vanishes every
                    # second or so, and the banner reads as though it cannot make up
                    # its mind.  An operator described it as jumping hysterically.  The
                    # droplet had not moved; only its number had.
                    held = None
                    if bestAt is not None:
                        me = [c for c in confirmed
                              if abs(c["pos"][0] - bestAt[0]) <= SAME_DROPLET_X_PX]
                        if me:
                            held = min(me, key = lambda c: abs(c["pos"][0] - bestAt[0]))
                    if held is not None:
                        bestNow, bestSeen, bestAt = held, now, held["pos"]
                    elif bestNow is None or now - bestSeen > CANDIDATE_STICKY_S:
                        # Only now is the held droplet really gone.  The grace has to cover
                        # a dropout while OTHER candidates still qualify -- that is the
                        # common case, and applying it only when the list went empty left
                        # the banner alternating between two droplets every couple of
                        # seconds.
                        if confirmed:
                            bestNow, bestSeen = confirmed[0], now
                            bestAt = confirmed[0]["pos"]
                        else:
                            bestNow, bestAt = None, None
                    # An explicit pick outranks every automatic choice, and is
                    # reported whether or not it passes the candidate gates: the
                    # operator asked about THIS droplet, and "this one will not work"
                    # is the most useful thing to say once they have spent time on it.
                    urgent = None
                    measuringBanner = None
                    measuringAdvice = False
                    reacquired = False
                    nowPicked = None if picked is None else int(picked.id)
                    if nowPicked != lastPickedId:
                        forgetHeld("picked")          # it is about somebody else now
                        lastPickedId = nowPicked
                    if picked is not None and committedAt is None:
                        committedAt = now             # the measurement starts here
                    elif picked is None and chosenX is None:
                        committedAt = None
                    # Held still by the field, or falling?  A droplet being held is
                    # expected to vanish now and then; one in motion is not.
                    # `picked` is None for most of a session -- every frame before the
                    # first click -- and reading its speed here crashed the pipeline on
                    # its first frame, which looked from the outside like the window
                    # opening and vanishing.  The gate never caught it: it makes no
                    # picks, and this line lives in the branch that draws for an
                    # operator.
                    pickedSpeed = (picked.recentSpeed(spacing)
                                   if picked is not None else None)
                    heldStill = (picked is not None
                                 and fieldLevel(steadyVolts) in ("work", "lift")
                                 and (pickedSpeed is None
                                      or abs(pickedSpeed) <= BALANCED_MAX * 4))
                    lostAfter = PICK_HELD_SEARCH_S if heldStill else PICK_SEARCH_S
                    if (picked is not None and pickedSeen
                            and now - pickedSeen >= lostAfter):
                        urgent = (("lost your droplet %.0fs ago -- it may be moving too "
                                   "fast to follow. Click it again"
                                   % (now - pickedSeen)), (0, 140, 255))
                    if pickedDoubt is not None and now - pickedDoubt[0] <= STEAL_SHOW_S:
                        _w, _b, _a, odd = pickedDoubt
                        if odd:
                            urgent = (("re-acquired, but it now falls %.2f gr/s and was "
                                       "%.2f -- may be a DIFFERENT droplet, click yours "
                                       "again" % (_a, _b)), (0, 140, 255))
                            banner, bannerColour = urgent
                            bannerAt = now
                        else:
                            # A benign hand-over ANNOTATES, it does not take over.
                            # Replacing the verdict for STEAL_SHOW_S meant that with
                            # tracks breaking often the operator never saw what their
                            # droplet was actually doing -- they picked one falling
                            # outside the legal band and the aid never said so.
                            reacquired = True
                    elif picked is None and pickedGone is not None \
                            and now - pickedGone[0] <= PICK_GONE_SHOW_S:
                        # It left the picture.  Which way it went decides the recovery,
                        # and the operator cannot always tell: off the top means the
                        # field is still carrying it up, off the bottom means it is
                        # falling away and has seconds before the field cannot reach it.
                        way = {"top": "cut to 0V to bring it back down",
                               "bottom": "press \u63d0\u5347 (LIFT) to raise it",
                               "edge": "reposition it into view"}[pickedGone[1]]
                        urgent = (("the droplet you picked has left the view -- %s, "
                                   "then click it again" % way), (0, 140, 255))
                        banner, bannerColour = urgent
                        bannerAt = now
                    elif picked is not None:
                        # The droplet's OWN history first, the column only as a
                        # fallback.  The column pools everything within +-1 of it --
                        # 90 px -- which is what makes it survive a lift, and also what
                        # makes it wrong in a crowded chamber: with a dozen droplets on
                        # screen it mixes several droplets' descent rates and can
                        # produce a confident "not charged" about a droplet that plainly
                        # answers the field.  An operator watched it say DISCARD to one.
                        # For a droplet being actively watched, its own track is both
                        # available and specific.
                        own = chargeFromBuckets(pickedVolts)
                        pv = own
                        if pv is None:
                            pv = columnChargeState(columnVolts, picked.pos[0], now,
                                                   columnVerdicts)
                        if now - pickedAuto <= AUTO_PICK_SHOW_S:
                            bannerAt = now
                        banner, bannerColour, _ok = pickedVerdict(
                            picked, spacing, now, steadyVolts, pv,
                            freeFallSpeed(columnVolts, picked.pos[0]), own,
                            now - voltsChangedAt if voltsChangedAt else None,
                            liftWatch.lifting,
                            bool(dropletFalls(falls, float(picked.pos[0]))),
                            next((v for ts, v in reversed(workingVolts)
                                  if now - ts <= WORKING_VOLTS_MEMORY_S), None))
                        banner, bannerColour = keepInstruction(
                            int(picked.id), banner, bannerColour, now)
                        banner, bannerColour = holdText("picked", now, banner,
                                                        bannerColour)
                        if reacquired:
                            banner += "   (re-acquired -- click again if wrong)"
                        bannerAt = now
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
                        # The operator's droplet, not the pinned coordinate.  A pin
                        # lands where a droplet settled; the operator then moves on,
                        # and everything keyed to that coordinate goes on describing a
                        # droplet nobody is looking at.  On 2026-09-17 the banner read
                        # "1 of 5 falls" and "still the same droplet?" for ninety
                        # seconds about a droplet abandoned at 1:50, while the one in
                        # the square was boxed, balanced and about to be dropped.
                        # COUNT WHAT WAS BOUND, never re-derive it.  See
                        # Fall.FIELDS: each fall was assigned to a droplet number
                        # when it completed, so the tally cannot move because the
                        # droplet drifted, or because somebody else's older fall
                        # came nearer the column than one of this droplet's.
                        mine = [f for f in falls if f.legal
                                and getattr(f, "droplet", None) == pickSerial]
                        # NO FALLBACK.  A column count was what this replaced, and
                        # leaving it in for "before the first fall" let it back in by
                        # the side door: on 2026-09-22 an auto-picked droplet opened
                        # at 3 of 5, counting falls from earlier sprays that happened
                        # to land in its column.  A droplet with nothing bound to it
                        # has had no falls, and saying 0 is the truth.
                        # Follow the group, not the coordinate.  chosenX was pinned
                        # where the droplet happened to be when it settled, and the
                        # count is "the falls nearest that x" -- so when this droplet's
                        # falls landed 40 px away and an older droplet's fall sat
                        # nearer the pin, the tally read 1, then 0, then 2 on the same
                        # droplet.  Re-centre the pin on the falls it is counting;
                        # that is the median binding already groups on, and it does
                        # not chase the live droplet, which is what made the tally go
                        # DOWN when it was tried before.
                        if mine:
                            chosenX = float(np.median([f.xPx for f in mine]))
                        done = len(mine)
                        lastFall = max((f.tTop or 0.0) for f in mine) if mine else None
                        # A click is the operator saying "yes, this one".  The
                        # prompt asked exactly that and then ignored the answer.
                        since = max(lastFall or 0.0, lastClickAt or 0.0)
                        # ...and never ask about falls that are not the picked
                        # droplet's.  Asking "still the same droplet?" while the
                        # operator is mid-balance on a different one is noise they
                        # cannot answer.
                        stale = (lastFall is not None
                                 and now - since > MEASURE_IDLE_S
                                 and (picked is None
                                      or abs(float(picked.pos[0]) - chosenX)
                                      <= SAME_DROPLET_X_PX))
                        # q/e USED TO BE SHOWN HERE and is deliberately not any more.
                        # It is the student's own result, and putting it on the screen
                        # mid-measurement invites tuning the voltage until q/e lands on
                        # a whole number -- selecting data by the answer, which is the
                        # very habit this project criticises.  Rubric 三.1.4 wants the
                        # student's analysis exercised, not performed for them.  The
                        # balance percentage below says the same thing about the
                        # voltage without revealing anything about the charge.
                        if done >= TARGET_FALLS_PER_DROPLET:
                            mText = ("DONE -- %d of %d falls. Spray again for the next "
                                     "droplet" % (done, TARGET_FALLS_PER_DROPLET))
                            mColour = (0, 255, 0)
                        elif stale:
                            # Half-measured and untouched for minutes.  It may be the
                            # same droplet and the operator stepped away, or it may be
                            # long gone -- the aid cannot tell, so it asks instead of
                            # asserting.
                            mText = ("%d of %d falls -- still the same droplet? click "
                                     "it to keep the count, or spray again to start "
                                     "over" % (done, TARGET_FALLS_PER_DROPLET))
                            mColour = (0, 140, 255)
                        else:
                            mText = "MEASURING -- %d of %d falls" % (
                                done, TARGET_FALLS_PER_DROPLET)
                            mColour = (200, 200, 200)
                        # How well the droplet in this column is balanced, live.
                        #
                        # MEASURING used to be the whole line, and on 2026-09-14 an
                        # operator tuning a drifting droplet was told "MEASURING -- 0 of
                        # 5" and nothing else: the one thing the aid could usefully say
                        # -- which way to turn the knob -- was hidden by a progress
                        # count.  So while nothing is actually falling, the balance
                        # instruction outranks the count.
                        # ...and the balance line describes the droplet in the
                        # square, not the nearest track to the pin.  Those disagreed by
                        # a factor of eight in the same second: the pick verdict said
                        # "BALANCED to 1.9% at 180 V" while MEASURING said "balance
                        # 14.9%, RAISE to about 214 V", then "95.0%".
                        if picked is not None and not picked.miss:
                            chosen = [picked]
                        else:
                            chosen = [t for t in live
                                      if not t.miss and len(t.points) >= CONFIRM_POINTS
                                      and abs(float(t.pos[0]) - chosenX)
                                      <= SAME_DROPLET_X_PX]
                        timing = any(t.tTop is not None and t.tBottom is None
                                     for t in live)
                        q = None
                        hasAdvice = False
                        if done >= TARGET_FALLS_PER_DROPLET:
                            # DONE means stop.  For four minutes after a droplet was
                            # finished the banner went on asking for 265 V, 272 V,
                            # 242 V, 236 V about a droplet the operator had left.
                            pass
                        elif chosen and (liftWatch.lifting
                                       or (volts and float(volts) > LIFT_VOLTS)
                                       or (voltsChangedAt is not None
                                           and now - voltsChangedAt
                                           < SETTLE_AFTER_V_S)):
                            # Mid-lift the droplet is meant to be moving.  "far from
                            # balance -- LOWER to about 219 V" while the operator holds
                            # 提升 is advice about a state they created on purpose.
                            pass
                        elif chosen:
                            me = min(chosen, key = lambda t: abs(float(t.pos[0]) - chosenX))
                            hold = getattr(me, "heldSince", None)
                            window = (min(BALANCE_FIT_S, now - hold)
                                      if hold is not None else 0.0)
                            if voltsChangedAt:
                                window = max(window, min(BALANCE_FIT_S,
                                                         now - voltsChangedAt))
                            q = balanceError(me, spacing, now, volts,
                                             freeFallSpeed(columnVolts, me.pos[0]),
                                             window,
                                             now - voltsChangedAt if voltsChangedAt
                                             else None)
                        if q is not None:
                            pct, drift, ub, assumed, coarse = q
                            mark = "~" if assumed or coarse else ""
                            way = "RAISE" if drift > 0 else "LOWER"
                            floor = brownianFloorPct(
                                freeFallSpeed(columnVolts, me.pos[0]), window)
                            if floor and not coarse and pct <= floor * 1.3:
                                # The green cue an operator never sees while MEASURING
                                # owns the banner: this is as good as this droplet gets.
                                add = ("   balance %s%.1f%% -- as good as it gets, cut "
                                       "to 0V and time it" % (mark, pct))
                                addColour = (0, 255, 0)
                            elif pct <= BALANCE_TARGET_PCT:
                                add, addColour = "   balance %s%.1f%%" % (mark, pct), None
                            elif ub is None:
                                add = ("   balance %s%.1f%% -- retune before the next "
                                       "fall" % (mark, pct))
                                addColour = (0, 140, 255)
                            elif drift > 0 and pct > BALANCE_ADVICE_MAX_PCT:
                                # The falling case is the one where the implied voltage
                                # blows up; a rising droplet's target is bounded and
                                # keeps its number, further down.
                                add = ("   barely held -- RAISE it, spray again if it "
                                       "still falls at %g V" % ADVISORY_MAX_V)
                                addColour = (0, 140, 255)
                            else:
                                target, steady, step = steadyTarget(me, now, ub, volts)
                                if not steady:
                                    # A number the operator cannot act on is noise
                                    # dressed as an instruction.  While the estimate
                                    # is still moving, a "RAISE by about 3 V" is
                                    # inside its own uncertainty and inside what a
                                    # hand on that knob can do: say which way, and
                                    # leave the number out until it means something.
                                    add = ("   balance %s%.1f%% -- still calculating; "
                                           "%s by about %d V" % (mark, pct, way,
                                                                 abs(step))
                                           if abs(step) >= MIN_NUDGE_V else
                                           "   balance %s%.1f%% -- still calculating; "
                                           "%s slowly" % (mark, pct, way))
                                elif timing:
                                    # "Retune" on its own is not an instruction.  Which
                                    # way, and to what, is the whole content of it.
                                    add = ("   balance %s%.1f%% -- %s to about %d V "
                                           "before the next fall"
                                           % (mark, pct, way, round(target)))
                                else:
                                    add = (" -- off balance by %s%.1f%%, %s to about "
                                           "%d V" % (mark, pct, way, round(target)))
                                addColour = (0, 140, 255)
                            add, addColour = holdText("balance", now, add,
                                                      addColour or mColour)
                            mText += add
                            hasAdvice = bool(add)
                            if addColour is not None and addColour != mColour:
                                mColour = addColour
                        # Who the line belongs to.  A progress count outranks hunting
                        # chatter only while it describes the droplet the operator has
                        # in hand: with falls to its name and their pick inside the
                        # column.  Otherwise their own droplet has the line back --
                        # which is how "USABLE" came to be missing for ninety seconds
                        # while a dead column reported 0 of 5.
                        inColumn = (picked is not None
                                    and abs(float(picked.pos[0]) - chosenX)
                                    <= SAME_DROPLET_X_PX)
                        if picked is None or (done and inColumn):
                            banner, bannerColour = mText, mColour
                            measuringBanner = (mText, mColour)
                            measuringAdvice = hasAdvice
                        bestNow, bestAt = None, None
                    # Nothing picked, nothing qualifying, and few enough droplets left
                    # that the operator is choosing rather than waiting.  The one with
                    # the most room to tune is the one nearest the middle: near either
                    # line a wrong toggle carries it off the picture before it can be
                    # balanced.  A suggestion only -- a click overrides it, and the
                    # droplet need not have passed any gate, because the one that
                    # mattered on 2026-09-10 was rising too fast to be a candidate and
                    # was still the droplet the operator measured.
                    if picked is None and bestNow is None and banner is None:
                        room = [t for t in live
                                if not t.miss and len(t.points) >= CONFIRM_POINTS
                                and columnChargeState(columnVolts, t.pos[0], now,
                                                      columnVerdicts) is not False]
                        if 0 < len(room) <= CALM_TRACKS:
                            mid = (yTop + yBottom) / 2.0
                            pickMe = min(room, key = lambda t: abs(t.pos[1] - mid))
                            # An id is not a way to point at something on a screen:
                            # the operator sees dots, not numbers, and "id 16391" told
                            # them nothing about which one.  Say where it is.
                            banner = ("quiet now -- click the droplet you want, or "
                                      "wait and the aid picks the one nearest the "
                                      "middle for you")
                            bannerColour = (0, 220, 255)
                            bannerAt = now
                    # A droplet with falls to its name outranks every hunting message.
                    # Mid-measurement an operator was shown "USABLE, balance it on `0`"
                    # about the very droplet they were four falls into, because the
                    # pick verdict is written after MEASURING and the pin had been let
                    # go for a moment.  Urgent messages still win -- those are about
                    # this droplet too.
                    if measuringBanner is not None:
                        mt, mc = measuringBanner
                        # THE COUNT DOES NOT REPLACE THE ADVICE.  Where the balance
                        # fit produced a line, the count already carries it; where
                        # it did not -- the droplet is drifting, the window is too
                        # short, the field just changed -- the pick's own verdict is
                        # the only useful thing on offer, and MEASURING was written
                        # over the top of it.  An operator balancing a droplet three
                        # falls in was shown a progress count and nothing else for
                        # forty seconds on 2026-09-21.
                        # ...and not when it would say the same thing twice.  The
                        # count carries its own balance line, and pickedVerdict
                        # computes one of its own from the same numbers: appending
                        # that put "BALANCED to 1.4% at 180 V" on the screen
                        # directly after "balance 1.4%", which reads as a fault in
                        # the aid rather than agreement.
                        if (not measuringAdvice and banner
                                and "balance" not in banner.lower()
                                and not any(f in banner for f in FILLER_LINES)):
                            said = banner.split("--", 1)
                            mt = mt + "   " + (said[1].strip() if len(said) > 1
                                               else banner)
                        banner, bannerColour = mt, mc
                    # Last word to the urgent ones.  MEASURING is written after the
                    # pick messages and was overwriting them, so "your droplet left the
                    # view" and "re-acquired, this may be a different droplet" were
                    # computed and then silently discarded -- exactly while a
                    # measurement was running, which is the only time they matter.
                    if urgent is not None:
                        banner, bannerColour = urgent
                        bannerAt = now
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
                    # A droplet PROVEN not to answer the field can never be
                    # balanced, so it can never be measured.  Drawing it like any other
                    # candidate wastes the operator's attention on the one thing that
                    # certainly will not work -- they reported rings landing on these
                    # repeatedly.  Cross it out and say so instead.
                    # Its OWN history, not the column's.  The column pools everything
                    # within +-1 of it, 90 px, which is what lets a verdict survive a
                    # lift and also what makes it wrong in a crowded chamber: a charged
                    # droplet sharing a column with uncharged ones inherited their
                    # answer and got crossed out.  Crossing a droplet out says it can
                    # never be measured, which is the same weight as DISCARD, so it
                    # takes the same evidence: the droplet's own.
                    # ...and never while it is falling.  Mid-traverse the field is
                    # off by definition, so there is nothing for a charge test to
                    # compare, and the droplet is being MEASURED -- crossing it out
                    # then is both unfounded and in the way.
                    # ...nor while the operator is lifting.  A lift is a 210 V step
                    # and the droplet is accelerating through it, so the samples on
                    # either side belong to different states and the verdict drawn from
                    # them is not about this droplet's charge.  An operator watched
                    # droplets get crossed out mid-lift.
                    # Cannot be balanced from where the supply is, sustained.  Weaker
                    # than "uncharged" and far more often available, so it is said in
                    # weaker words.
                    vNow = t.recentSpeed(spacing)     # `v` is not set until below
                    if (volts and vNow is not None and vNow > 0 and t.tTop is None
                            and now - getattr(t, "lastReachCheck", 0.0) >= 0.5):
                        t.lastReachCheck = now
                        if reachableBalance(volts, vNow,
                                            freeFallSpeed(columnVolts, t.pos[0])) is None:
                            if getattr(t, "unreachableSince", None) is None:
                                t.unreachableSince = now
                        else:
                            t.unreachableSince = None
                    elif not volts or t.tTop is not None:
                        t.unreachableSince = None
                    # Crossing a droplet out is the most expensive thing the aid
                    # says -- the operator stops chasing it -- so it has to HOLD.  The
                    # verdict is recomputed every frame off noisy speeds, and an
                    # operator watched a droplet that was plainly answering the field
                    # get crossed out for a moment.  One second of noise must not cost
                    # a usable droplet.
                    if t.chargeState() is False:
                        if getattr(t, "notHeldSince", None) is None:
                            t.notHeldSince = now
                    else:
                        t.notHeldSince = None
                    # A droplet moving under 提升 is answering the field by moving,
                    # and one that has ever been held by the field has a charge.  The
                    # cross appeared on a picked droplet at 460 V, and again at 458 V on
                    # one that had already given four legal falls.  The lift latch needs
                    # a 150 V step inside half a second and a slow switch never gives it
                    # one, so the voltage itself is the backstop.
                    lifting = liftWatch.lifting or (volts and float(volts) > LIFT_VOLTS)
                    if (getattr(t, "notHeldSince", None) is not None
                            and now - t.notHeldSince >= UNREACHABLE_SHOW_S
                            and SHOW_WONT_BALANCE
                            and not lifting
                            and not getattr(t, "answeredField", False)
                            and not (t.tTop is not None and t.tBottom is None)):
                        cv2.line(marked, (p[0] - 7, p[1] - 7), (p[0] + 7, p[1] + 7),
                                 (90, 90, 90), 1)
                        cv2.line(marked, (p[0] - 7, p[1] + 7), (p[0] + 7, p[1] - 7),
                                 (90, 90, 90), 1)
                        cv2.circle(marked, p, 9, (90, 90, 90), 1)
                        cv2.putText(marked, "won't balance", (p[0] + 14, p[1] + 4),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (90, 90, 90), 1)
                        continue
                    v = t.recentSpeed(spacing)
                    plausible = v is not None and SCREEN_MIN <= v <= SCREEN_MAX
                    if (SHOW_WONT_BALANCE
                            and getattr(t, "unreachableSince", None) is not None
                            and now - t.unreachableSince >= UNREACHABLE_SHOW_S
                            and not lifting
                            and not getattr(t, "answeredField", False)):
                        cv2.line(marked, (p[0] - 7, p[1] - 7), (p[0] + 7, p[1] + 7),
                                 (90, 90, 90), 1)
                        cv2.line(marked, (p[0] - 7, p[1] + 7), (p[0] + 7, p[1] - 7),
                                 (90, 90, 90), 1)
                        cv2.circle(marked, p, 9, (90, 90, 90), 1)
                        cv2.putText(marked, "won't balance", (p[0] + 14, p[1] + 4),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (90, 90, 90), 1)
                        continue
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
                        # Past the legal maximum the fall cannot count however it
                        # ends, so it stops looking like a measurement in progress.
                        # On 2026-09-20 a clock ran to 94 s in plain sight -- more
                        # than twice MAX_FALL_S -- with nothing to say it was already
                        # spent, which is an operator waiting for nothing.
                        late = (now - t.tTop) > MAX_FALL_S
                        clockColour = (150, 150, 150) if late else (0, 255, 255)
                        cv2.circle(marked, p, 13, clockColour, 1)
                        # Clear of the pick square, which is 18 px to the side: at
                        # +16 the first digit sat under its edge and "42.8 s over"
                        # read as "2.8 s over" on the operator's screen.
                        cv2.putText(marked, "%.1f s%s" % (now - t.tTop,
                                                          " over" if late else ""),
                                    (p[0] + 24, p[1] + 13),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, clockColour, 1)
                nTracked = sum(1 for t in live
                               if t.miss == 0 and len(t.points) >= CONFIRM_POINTS)
                # The same numbers the HUD draws, published for the app.  The operator
                # decided on 2026-09-16 that the balance voltage may cross to the APP:
                # it is the apparatus's own panel reading, which they can see with
                # their eyes, and showing it is advice rather than the student's
                # arithmetic.  It still does NOT enter an LLM prompt -- q = K(t)*d/U_n
                # and the tutor has the manual, so `seconds` plus `U_n` together would
                # let it produce the answer the student is meant to produce.  See
                # LLM_KEYS in cv/api.py, which is unchanged.
                picked_ = None
                if picked is not None:
                    v_ = picked.recentSpeed(spacing)
                    # FALL TIME is the number drawn beside the droplet: running while
                    # it falls, frozen once it crosses `1.6`.  It was read off the pick
                    # alone and only while running, so on 2026-09-19 the app's panel
                    # showed a dash beside a droplet that plainly had a clock on it --
                    # blank the moment a fall finished, and blank whenever the clock
                    # sat on a track that had re-found the droplet while the pick was
                    # still coasting.  Read it from whichever track carries the ring.
                    aim_ = picked.predict(now) if picked.miss else picked.pos
                    clocked = [picked] if picked.tTop is not None else sorted(
                        (q for q in live
                         if q.tTop is not None and not q.miss
                         and len(q.points) >= CONFIRM_POINTS
                         and np.hypot(q.pos[0] - aim_[0], q.pos[1] - aim_[1])
                         <= PICK_FOLLOW_PX),
                        key = lambda q: np.hypot(q.pos[0] - aim_[0], q.pos[1] - aim_[1]))
                    clock_ = clocked[0] if clocked else None
                    running_ = clock_ is not None and clock_.tBottom is None
                    # Past MAX_FALL_S the clock cannot become a legal fall however it
                    # ends, so it is not a fall time and the app must not print one.
                    # On 2026-09-20 the panel read FALL TIME 52.5 s, then 62.2 s, on a
                    # droplet sitting still with the field off -- a number that looked
                    # like a measurement, climbing, about nothing at all.
                    if running_ and now - clock_.tTop > MAX_FALL_S:
                        clock_, running_ = None, False
                    picked_ = {
                        # The DROPLET, not the track: see pickSerial.  The track id
                        # is still published, because a diagnostic that cannot be
                        # matched to the banner is no diagnostic.
                        "id": int(pickSerial),
                        "trackId": int(picked.id),
                        "speedGridPerSec": None if v_ is None else round(float(v_), 3),
                        "fallSeconds": (None if clock_ is None
                                        else round(now - clock_.tTop, 2) if running_
                                        else round(float(clock_.seconds), 2)),
                        "fallRunning": running_,
                        # HOW LONG THIS TRACK HAS EXISTED.  A droplet somebody is
                        # balancing has been on the screen for a while; detection
                        # noise has not.  On 2026-09-21 the square sat on a phantom
                        # for thirteen seconds and the panel announced it as
                        # "Balanceable at 250 V", which is the aid vouching for
                        # something that was never there.
                        "trackAgeS": (round(float(now - picked.points[0][0]), 1)
                                      if getattr(picked, "points", None) else 0.0),
                        "balancePercent": (round(float(picked.balanceDriftPct), 1)
                                           if getattr(picked, "balanceDriftPct", None)
                                           is not None else None),
                    }
                # A session the operator is watching.  An offline analysis of a clip
                # writes the same file, and one running in the background overwrote a
                # LIVE session's status: the app's panel would have been fed another
                # run's droplets mid-measurement.  Only a run with a window -- live or
                # playback, which is what the app starts -- says what it is doing.
                if show:
                    writeLiveStatus(
                        phase = "running",
                        elapsedSeconds = round(float(now - (firstT or 0)), 1),
                        tracked = int(nTracked),
                        falls = len(falls),
                        legalFalls = sum(1 for f in falls if f.legal),
                        voltageV = None if steadyVolts is None else int(steadyVolts),
                        fieldState = fieldLevel(steadyVolts),
                        message = banner or "",
                        picked = picked_,
                        frameWidth = int(webBox[2]),    # the picture the app is
                        frameHeight = int(webBox[3]),   # shown, which clicks map to
                        dropletsCompleted = len(dropletGroups(falls)),
                        targetFallsPerDroplet = TARGET_FALLS_PER_DROPLET,
                    )
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
                    # The app gets the same picture, minus the black bars the dongle
                    # pads the frame with -- in a browser panel those were most of its
                    # width.  Everything the operator looks at stays: the grid, the
                    # droplets, the apparatus's own `V:` readout.  Published BEFORE
                    # the HUD, which the app draws itself and does not want burnt in.
                    bx, by, bw, bh = webBox
                    if n % PUBLISH_EVERY == 0:
                        publishFrame(display[by:by + bh, bx:bx + bw].copy())
                    if not blind:
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
                       # WHERE the missing frames went.  See source.py: `grab` is
                       # the camera handing a frame over, `work` is everything this
                       # pipeline does before it asks for the next one.  Whichever
                       # is the larger is the one worth fixing, and until these
                       # existed the answer was guesswork.
                       "grabMsPerFrame": round(1000.0 * getattr(src, "grabS", 0.0)
                                               / max(1, n), 1),
                       "workMsPerFrame": round(1000.0 * getattr(src, "workS", 0.0)
                                               / max(1, n), 1),
                       "backgroundResyncs": resyncs,
                       "spraySpans": spraySpans([t for t, _c in detSeries],
                                                [c for _t, c in detSeries])})
    # Printed, not just returned: a live session's health dict goes nowhere the
    # operator can see, and the frame rate is the one number that explains a run
    # where tracks kept dying.
    if src.isLive and n:
        # Unconditionally, because the live path passes no health dict and this is
        # the number that explains a session where tracks kept dying.
        print("  capture: %.1f fps delivered of %.0f nominal, %d dropped; "
              "%.1f ms in the camera, %.1f ms in the pipeline, per frame"
              % (measuredFps or 0.0, fps, src.dropped,
                 1000.0 * getattr(src, "grabS", 0.0) / max(1, n),
                 1000.0 * getattr(src, "workS", 0.0) / max(1, n)), flush = True)
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
                "balanceVoltageV", "voltageFrom", "balanceDriftPct",
                "balanceImpliedV", "legal"]


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
            # "latched" is the apparatus's own hold; "V: line" means the latch was
            # empty and the voltage was recovered from the live readout instead.  Same
            # quantity, different route, and a reader should be able to tell.
            "voltageFrom": getattr(f, "voltageFrom", None),
            # How well the droplet was actually balanced before this fall, measured
            # from its residual drift while it was held on the line, as the percentage
            # it costs the charge.  Independent of q, n and e: it is a property of the
            # balancing, not of the answer, so it can be used to judge a fall without
            # judging it by its result.
            "balanceDriftPct": (None if getattr(f, "balanceDriftPct", None) is None
                                else round(f.balanceDriftPct, 2)),
            "balanceImpliedV": (None if getattr(f, "balanceImpliedV", None) is None
                                else round(f.balanceImpliedV)),
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


CHARGE_MIN_FALLS = 2            # fewer than two falls is not a droplet


def dropletGroups(falls):
    """This session's legal falls, grouped into droplets the way binding groups them.

    Same two-stage rule as `dropletFalls`: the same column, and durations that agree.
    A droplet's five falls run within a few percent of each other, while a stray that
    happens to fall down the same column does not.
    """
    # A fall whose panel reading is a LIFT voltage cannot be a balance measurement --
    # nothing balances at 360 V.  One such stray pinned the banner at "1 of 5 falls"
    # for three minutes and then asked the operator whether they were still measuring
    # a droplet that was never theirs.
    #
    # But throwing the FALL away to be rid of the READING cost a real measurement.  On
    # 2026-09-20 an operator lifted their droplet just before releasing it, so the
    # panel still showed 407 V when the fall was timed; the fall itself was legal, in
    # the same column as the four that followed, and within 0.5 s of their time.  It
    # was dropped anyway, charges.csv said "INCOMPLETE, 4 of 5 falls" about a droplet
    # that had done five, and the tutor repeated it.
    #
    # So the reading is discarded and the fall is not: it may JOIN a droplet, on the
    # same column and duration evidence as everything else, but it may never START
    # one.  A stray has no siblings to vouch for it and still forms no group; a fall
    # whose droplet is on the screen is taken back, and writeCharges() gets its
    # voltage from the median of its siblings, which is what the vote is for.
    lifted = [f for f in falls
              if f.legal and getattr(f, "xPx", None) is not None
              and getattr(f, "balanceVoltage", None)
              and float(f.balanceVoltage) > LIFT_VOLTS]
    for f in lifted:
        f.balanceVoltage = None
        f.voltageFrom = "lift, ignored"
    legal = sorted((f for f in falls
                    if f.legal and getattr(f, "xPx", None) is not None
                    and f not in lifted),
                   key = lambda f: f.tTop or 0.0)
    # Distance to the NEAREST fall already in the group, not to the group's running
    # median.  A droplet drifts sideways as it is measured -- five falls on 2026-09-16
    # walked 523 -> 494 -> 459 -> 433 -> 436 px -- and by the fourth the median had
    # only reached 494, so |433 - 494| = 61 px missed a 60 px window by one pixel and
    # the droplet was filed as two.  The fifth then rejoined, and charges.csv reported
    # "INCOMPLETE, 4 of 5" about a droplet that had done all five.
    #
    # Nearest-fall linkage follows the drift for nothing, because consecutive falls of
    # one droplet are always close even when the first and last are not.  What stops it
    # chaining across unrelated droplets is the duration test below, which is unchanged:
    # a stray in the same column has to fall at the same speed to be absorbed.
    groups = []
    for f in legal:
        for g in groups:
            mean = sum(q.seconds for q in g["rows"]) / len(g["rows"])
            gap = min(abs(f.xPx - q.xPx) for q in g["rows"])
            # Same voltage, and soon after the last fall of the group.  Both are
            # physical: the knob did not move, and the operator did not wander off.
            volts = [float(q.balanceVoltage) for q in g["rows"]
                     if getattr(q, "balanceVoltage", None)]
            mine = getattr(f, "balanceVoltage", None)
            sameVolts = (not volts or mine is None
                         or abs(float(mine) - float(np.median(volts)))
                         <= SAME_DROPLET_VOLT_TOL)
            since = (f.tTop or 0.0) - max((q.tTop or 0.0) for q in g["rows"])
            if (gap <= SAME_DROPLET_X_PX and sameVolts
                    and since <= SAME_DROPLET_GAP_S
                    and abs(f.seconds - mean) <= DROPLET_TIME_TOL * mean):
                g["rows"].append(f)
                g["x"] = float(np.median([q.xPx for q in g["rows"]]))
                break
        else:
            groups.append({"x": float(f.xPx), "rows": [f]})
    # ...and now the falls that may only join. A droplet's own fall sits in its
    # column and matches its pace whether it comes first or last, so this pass runs
    # over the finished groups rather than in time order, and the gap is measured to
    # the NEAREST fall in the group, not the latest: the lifted fall was the first
    # of its droplet's five.
    for f in lifted:
        for g in groups:
            mean = sum(q.seconds for q in g["rows"]) / len(g["rows"])
            gap = min(abs(f.xPx - q.xPx) for q in g["rows"])
            since = min(abs((f.tTop or 0.0) - (q.tTop or 0.0)) for q in g["rows"])
            if (gap <= SAME_DROPLET_X_PX and since <= SAME_DROPLET_GAP_S
                    and abs(f.seconds - mean) <= DROPLET_TIME_TOL * mean):
                g["rows"].append(f)
                g["rows"].sort(key = lambda q: q.tTop or 0.0)
                break
    return [g["rows"] for g in groups]


def writeCharges(falls, outDir, path):
    """q, n and e for every droplet the session completed, written beside falls.csv.

    A live run has no video to re-analyse afterwards -- the frames are gone the moment
    they are tracked -- so `analyze()` cannot produce this, and the physics lived in a
    separate tool that is not even shipped with the repo.  An operator finished five
    falls at the bench and had no result: they had to ask someone else to run something
    they did not have.  The binding is the pipeline's own (same column, durations
    agreeing) and the voltage is the one it read off the panel, so nothing here needs
    the operator's notes.

    Layer 2 only: this goes to a file.  q, n and e never enter a dict that crosses to
    the app or the tutor -- see the allow-list in cv/api.py.
    """
    droplets, voltages = [], []
    for rows in dropletGroups(falls):
        if len(rows) < CHARGE_MIN_FALLS:
            continue
        volts = [float(f.balanceVoltage) for f in rows
                 if getattr(f, "balanceVoltage", None)]
        if not volts:
            continue                    # no voltage was ever read for this droplet
        # Two falls is enough to be a droplet and not enough to be a RESULT.  IV.4
        # asks for five, and a session often ends with a droplet at three -- lost, or
        # the operator stopped -- so the numbers are written rather than thrown away,
        # and the label says what they are.  Same rule hunttime.py uses for a hunt.
        label = "Droplet %d" % (len(droplets) + 1)
        if len(rows) < TARGET_FALLS_PER_DROPLET:
            label += " (INCOMPLETE, %d of %d falls)" % (len(rows),
                                                        TARGET_FALLS_PER_DROPLET)
        droplets.append({"label": label,
                         "falls": [{"id": f.id, "seconds": f.seconds} for f in rows]})
        voltages.append(float(np.median(volts)))
    if not droplets:
        return None
    chargeRows, perDroplet, overall = chargeMath.csvRows(droplets, voltages)
    csvPath = (artefact(outDir, path, "charges.csv") if outDir and path
               else os.path.join(outDir or ".", "charges.csv"))
    pd.DataFrame(chargeRows,
                 columns = chargeMath.CSV_COLUMNS).to_csv(csvPath, index = False)
    print("\nWrote", csvPath)
    for d in perDroplet:
        print("  %-12s U_n %5.0f V  %d falls  mean e %.4e C  sd %.1f%%  error %+.2f%%"
              % (d["label"], d["balanceVoltage"], d["falls"], d["meanElementaryC"],
                 d["relativeSdPercent"], d["errorPercent"]), flush = True)
    if overall and len(perDroplet) > 1:
        print("  %-12s %11s  %d falls  mean e %.4e C  sd %.1f%%  error %+.2f%%"
              % ("ALL", "", overall["falls"], overall["meanElementaryC"],
                 overall["relativeSdPercent"], overall["errorPercent"]), flush = True)
    return csvPath


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
    try:
        writeLiveStatus(phase = "finished", legalFalls = sum(1 for f in falls if f.legal),
                        falls = len(falls), message = "Session finished")
    except Exception:
        pass
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
    # The physics, without a second tool and without the operator's notes.  It belongs
    # here and not in one caller because a live session ENDS here: the frames are gone
    # the moment they are tracked, so nothing can re-analyse them afterwards.  Wired
    # into the batch runner only, an operator finished five falls at the bench and got
    # falls.csv and no charge at all.
    try:
        writeCharges(falls, outDir, path)
    except Exception as exc:                # never lose falls.csv to the charge maths
        print("  charges.csv could not be written: %s" % exc, flush = True)
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


def publishTo(argv):
    """--publish PORT: stream frames back to the launcher instead of being captured."""
    for i, a in enumerate(argv):
        if a == "--publish" and i + 1 < len(argv):
            startPublisher(argv[i + 1])
            return
        if a.startswith("--publish="):
            startPublisher(a.split("=", 1)[1])
            return


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
        publishTo(argv)
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
    if len(argv) > 1 and argv[1] == "play":
        # A recorded clip through the LIVE window: same "Droplets" window, same
        # click-to-pick, paced to real time by the block that already handles files.
        # The browser app mirrors that window, so the whole integration -- server,
        # stream, chat, start/stop -- can be exercised with no apparatus and no
        # dongle, which is the difference between testing it in a lab slot and
        # testing it at a desk.
        publishTo(argv)
        path = next((a for a in argv[2:] if not a.startswith("-")), VIDEO)
        outDir = outDirFor(path)
        print("Playback -> %s" % outDir)
        print("Stop with q in the window, or Ctrl+C")
        # MILLIKAN_RECORD=1 keeps the annotated video, so a playback session can be
        # reviewed afterwards the way a live one is.  Off by default: it costs about
        # 15 MB a minute, which a demo run has no use for.
        record = bool(os.environ.get("MILLIKAN_RECORD"))
        report(trackAll(path, show = True, saveVideo = record, outDir = outDir),
               outDir, path)
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
# The supply cannot make more than about 459 V -- the 210 V lift step on top of the
# working voltage -- so a reading above this is a misread, not a measurement.
#
# 2026-09-14, live: the `V:` line read 1111 V for most of a session.  It was shown on
# the HUD, written into fourteen rows of falls.csv, and -- worst -- it bucketed every
# charge-test sample at the same impossible voltage, so no droplet could ever be shown
# to answer the field and nothing was marked uncharged all session.  One misread, three
# symptoms, exactly like the stale-background case.
PANEL_MAX_V = 600.0

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


# How many rows to add above and below the text run before cutting glyphs from
# it.  The panel's digits are about 17 px tall and the run found by threshold can
# be several rows short of that at either end.
BAND_PAD_PX = 6
# A crop no taller than this is a single line of text, not a cell holding two.
SINGLE_LINE_ROWS = 40
# Which fraction of the readout cell the `V:` line occupies.  See voltageLine.
V_LINE_TOP, V_LINE_BOTTOM = 0.15, 0.62


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
    # NORMALISE THE GAIN FIRST.  The two panel lines are not equally bright -- the
    # latched line sits in a dimmer band and the dongle's own gain drifts between
    # sessions -- and everything below thresholds at a fixed floor of 140.  On
    # 2026-09-21 the latched line peaked at 164, so that floor kept only the
    # brightest few percent of each glyph: the digits arrived as scattered dots,
    # matched no template, and every fall of the session was written with no
    # voltage.  Scaling the line so its brightest ink lands near 220 leaves a bright
    # line untouched and lets a dim one be read with the same numbers.
    if line.shape[0] <= SINGLE_LINE_ROWS:
        # Already ONE LINE and nothing else: voltageLine cut it that way, so
        # there are no rules to exclude and no second line to choose between.
        # Everything below is about finding a line inside a cell, and running it
        # anyway threw the crop away whenever the ink was too faint to make four
        # bright rows -- eight of thirty-nine on the 2026-09-21 sweep, all of
        # them legible.
        band = line
        return band, band > max(120, float(np.percentile(band, 88)))
    # A WHOLE READOUT CELL, which is what the live path hands over: it reads the
    # panel by calling panelAlignment(med) and readPanelVolts(med, ...) with the
    # cell itself, so a fix that lived in voltageLine was never once used by a
    # session. The cell holds `V:` above `t:`, and the `V:` line is a fixed
    # fraction of it -- see voltageLine for why a fraction and not a threshold.
    # Which line is wanted is already known -- usingTemplates() says so, because
    # the two lines have their own template sets. Asking the picture instead
    # ("does this cell hold two runs of text?") gets a different answer as the
    # exposure moves, and answered "one" on a cell plainly holding V: and t:.
    if _ACTIVE_TEMPLATES == "v":
        h = line.shape[0]
        band = line[int(V_LINE_TOP * h):int(V_LINE_BOTTOM * h), :]
        return band, band > max(120, float(np.percentile(band, 88)))
    top = float(np.percentile(inner, 99.5))
    if top > 20.0:
        inner = np.clip(inner.astype(np.float32) * (220.0 / top),
                        0.0, 255.0).astype(np.uint8)
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
    # ...THEN GIVE THE GLYPHS ROOM.  Those rows are where the ink was bright
    # enough to pass a threshold, which is not the same as where the digits are:
    # on a dim frame the lower strokes fall below it and the band ends at the
    # glyphs' waist.  Every digit is then matched on its top half alone, where
    # 0, 2 and 5 are the same arch -- which is why a line showing 0250 read as
    # 0000, and one showing 0457 as 1111.
    #
    # Measured on the sweep the operator recorded on 2026-09-21, 39 readings
    # against what the panel plainly showed: 4 right before, 33 after. The
    # padding is what did it; re-cutting the templates alone changed nothing.
    if line.shape[0] <= SINGLE_LINE_ROWS:
        # Already one line and nothing else -- voltageLine cut it that way. Re-
        # trimming it here is the same mistake twice, and the second one cannot
        # be undone by padding because the rows are gone.
        band = line
    else:
        lo = max(0, int(lo) - BAND_PAD_PX)
        hi = min(inner.shape[0] - 1, int(hi) + BAND_PAD_PX)
        band = inner[lo:hi + 1, :]
    return band, band > max(120, float(np.percentile(band, 88)))


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
        gap = a0 - joined[-1][1] if joined else None
        if gap is not None and (gap < 6 or (gap < GLYPH_JOIN_PX
                                            and b0 - joined[-1][0] <= GLYPH_MAX_PX)):
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


GLYPH_JOIN_PX = 16              # see _candidates: a `0`'s hole, not a gap between
GLYPH_MAX_PX = 28               # digits


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
        gap = a - merged[-1][1] if merged else None
        # A HOLLOW glyph's interior is wider than the gaps inside a solid one.  The
        # panel's `0` is two vertical strokes about 15 px apart, and a 6 px merge rule
        # left them as separate 3 px and 2 px fragments, both below the 4 px floor --
        # so the `0` was not a candidate at all.
        #
        # On 2026-09-21 that cost an entire session's voltages.  With the leading `0`
        # missing, the four runs the aligner had to choose from were `1`, `8`, `9` and
        # the trailing `V`, so it read 0189 V as 1899, 0175 V as 1759, and refused the
        # latched line on every fall.  Four legal falls came out with no voltage, which
        # means no charge for any of them.
        #
        # The glyph pitch on this panel is ~42 px and a glyph is ~20 px wide, so the
        # gap BETWEEN two digits is ~22 px.  Joining under 16 px, and only while the
        # joined run stays about one glyph wide, cannot weld two digits together.
        if gap is not None and (gap < 6 or (gap < GLYPH_JOIN_PX
                                            and b - merged[-1][0] <= GLYPH_MAX_PX)):
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
    # A PANEL LINE READS `:+ D D D D V`.  When segmentation finds exactly those
    # six, the digits are the middle four and there is nothing to search for.
    # The search that follows is for the cases where it does not -- and left to
    # itself it chose a chain STARTING AT THE SIGN on 2026-09-21, because a `+`
    # correlates well enough with a `0` to pass the leading-zero rule, and read
    # 1946 V off a line that showed nothing of the sort.
    # `[:] + D D D D V` -- the colon is there on the `V:` line and not on the
    # latched one, and it comes and goes with the exposure, so COUNT FROM THE
    # RIGHT: the digits are the four runs before the trailing V, whatever is to
    # their left.  Counting from the left read 0250 as 9979 the moment the colon
    # appeared, because the chain then started on the `+`.
    if len(runs) >= 6:
        return [(a, b) for a, b in runs[-5:-1]]
    # ...and when it does not, the sign and the V are still not digits.  The
    # search below chose a chain starting at the `+` and read 1946 V off a line
    # showing 0269; dropping the outermost runs from what it may choose leaves
    # it searching only where digits can be.  Five runs means one of the two
    # merged with its neighbour, so only the leading sign is certain.
    if len(runs) >= 6:
        runs = runs[1:-1]
    elif len(runs) == 5:
        runs = runs[1:]
    scored = [(a, b) + _scoreRun(band, hotBand, a, b) for a, b in runs]
    # The supply tops out near 459 V -- the 210 V lift step on top of the working
    # voltage -- so on a four-digit display the first digit is ALWAYS `0`.  That is
    # not a heuristic about this session, it is what the apparatus can produce, and
    # it is the one fact that tells a chain starting at the real leading zero from a
    # chain that starts one glyph late and swallows the trailing V.  Both were
    # available on 2026-09-21 and the scores preferred the wrong one.
    #
    # Preferred, not required: if no chain starts with a readable `0`, the old
    # unconstrained search still runs, and the impossible-value guards downstream
    # still catch what it returns.
    for mustBeZero in (True, False):
        chain = _digitChain(scored, gapRange, mustBeZero)
        if chain is not None:
            return chain
    return None


def _digitChain(scored, gapRange, firstMustBeZero = False):
    """The four runs that read best with a plausible pitch between them."""
    lo, hi = gapRange
    n = len(scored)
    # best[k][i] = best total score for a chain of k+1 digits ending at candidate i
    best = [[-9.0] * n for _ in range(4)]
    back = [[-1] * n for _ in range(4)]
    for i, (_a, _b, d, sc) in enumerate(scored):
        if d is None or (firstMustBeZero and d != "0"):
            continue
        best[0][i] = sc
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
    # A slot is a PLACE on the line, not a box around a glyph.  The alignment is cut
    # once, from whatever digits the panel happened to be showing, and digits are not
    # the same width: `1` is 7 px on this font and `2` is 17.  Reading later frames
    # inside those exact boxes means reading a 17 px `2` through the 7 px window a `1`
    # left behind, which scores as nothing at all -- and one unreadable slot refuses
    # the whole line.  On 2026-09-21 that refused the latched line on all four legal
    # falls of the session, so none of them got a voltage.
    #
    # So: keep the CENTRES, and look for the glyph nearest each one in a window a
    # little narrower than the pitch, which is what _readAt already does for the
    # comb.  The centres are what the alignment actually knows.
    # A PANEL LINE READS `:+ D D D D V`.  When segmentation finds exactly that --
    # the sign, four digits and the V, once fragments are joined -- there is
    # nothing to search for: the digits are the middle four, whole, each cut at
    # its own edges.  Measured against the operator's sweep of 2026-09-21, 39
    # readings with the panel's value plainly legible in the crop: this reads 33
    # of them, where finding each digit inside a window centred on the alignment
    # reads 23.  A window has to guess where one glyph ends, and a `7` whose
    # stem segments separately is guessed wrong.
    #
    # The alignment path stays for every other case -- a line where the V merged
    # with a digit, or the sign was lost in noise -- which is what it was built
    # for.
    runs = _candidates(band, hotBand)
    if len(runs) >= 6:
        runs = runs[-5:-1]           # see panelAlignment: count from the V
        text = ""
        for a, b in runs:
            d, _sc = _scoreRun(band, hotBand, a, b)
            if d is None:
                text = None
                break
            text += d
        if text:
            return text
    centres = [(a + b) / 2.0 for a, b in alignment]
    gaps = sorted(centres[i + 1] - centres[i] for i in range(len(centres) - 1))
    pitch = gaps[len(gaps) // 2] if gaps else 42.0
    half = max(8.0, min(GLYPH_MAX_PX / 2.0, pitch / 2.0 - 8.0))
    text = ""
    for c in centres:
        x = int(round(c - half))
        d, _sc = _readAt(band, hotBand, x, int(round(2 * half)))
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
        # readoutLines is NOT asked any more.  It answers "where is the ink
        # bright enough", and on a faint crop it answers "nowhere" -- which
        # threw away eight of thirty-nine legible readings on the sweep before
        # the band was ever cut.  A cell with no text in it produces no digit
        # runs later, which is the same refusal arrived at honestly.
        # WHERE THE DIGITS ARE, not where the ink passed a threshold.  The run
        # readoutLines finds moves with the exposure -- the same panel gave a
        # 17-row run on one crop and a 10-row run on the next -- and a band cut
        # to the short one holds the tops of the glyphs and nothing else, where
        # 0, 2 and 5 are the same arch.  That is how a line showing 0250 read as
        # 0000, and one showing 0457 as 1111.
        #
        # The cell is a fixed band of the grid and `V:` is the upper of its two
        # rows of text, so a FRACTION of the cell locates the line and does not
        # move with brightness.  Measured on the operator's sweep of 2026-09-21,
        # 39 readings against what the panel plainly showed: 4 right before, 33
        # after -- and unchanged anywhere between 0.13 and 0.16 for the top edge,
        # so it is not a number that wants tuning.
        h = cell.shape[0]
        return cell[int(V_LINE_TOP * h):int(V_LINE_BOTTOM * h), :]
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