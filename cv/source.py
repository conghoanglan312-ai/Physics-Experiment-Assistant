"""Where frames come from.

One interface, three uses: a recorded file, a webcam, and a capture dongle. Everything
downstream -- `autoROI`, `detectDroplets`, the tracker -- is unchanged by which one is
in play, because they only ever see `(frame, t)`.

`t` is **seconds from a single monotonic clock**, and it is the only thing the
measurement path is allowed to use. Frame indices are kept for seeking, naming and
rendering, never for timing. On a file `t = index / fps` exactly, so the measured fall
times are identical to what frame counting gave. On a camera it is the difference
between a right answer and a wrong one: a device that advertises 30 fps and delivers
27 inflates every fall time by 11%, the error goes straight into the fall speed and
then into q, and nothing downstream would flag it because the numbers stay plausible.

Selection is explicit -- `openSource("cam:0")`, never a guess from the shape of the
argument alone. Picking the wrong source and getting believable numbers is the exact
failure this project keeps running into.

    src = openSource("cam:0")
    bg  = src.learnBackground(seconds = 30)
    for frame, t in src.frames():
        ...
    src.close()
"""

import glob
import os
import sys
import time

import cv2
import numpy as np

CAMERA_PREFIXES = ("cam:", "camera:", "dongle:", "uvc:")
FRAMES_PREFIX = "frames:"


class FrameSource(object):
    """A stream of frames, each with a capture time in seconds."""

    isLive = False
    nominalFps = 0.0        # advisory only -- never used to measure anything
    width = 0
    height = 0
    frameCount = 0          # 0 when unknown, which is always true of a live source
    dropped = 0             # frames the source itself lost, counted where detectable

    def frames(self, first = 0, last = None):
        """Yield (bgrFrame, t). `t` is seconds, monotonic, from one clock."""
        raise NotImplementedError

    def learnBackground(self, samples = 61, seconds = None, first = 0, last = None):
        """Median of `samples` frames -- the still scene behind the droplets."""
        raise NotImplementedError

    def describe(self):
        return "%s %dx%d @ %.2f fps" % (type(self).__name__, self.width,
                                        self.height, self.nominalFps)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FileSource(FrameSource):
    """A recorded clip. Seekable, with an exact and constant frame interval."""

    def __init__(self, path):
        self.path = path
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise IOError("OpenCV could not open %s -- codec or permissions?" % path)
        _fps = cap.get(cv2.CAP_PROP_FPS)
        self.nominalFps = _fps if _fps and _fps > 0 else 30.0
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frameCount = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._cap = cap

    def frames(self, first = 0, last = None):
        cap = self._cap
        last = self.frameCount if last is None else last
        if first:
            cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        n = first
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            n += 1
            if n > last:
                break
            # Exact by construction, and the reason a file run reproduces to the
            # last decimal: t is a linear function of the frame index.
            yield frame, n / self.nominalFps

    def learnBackground(self, samples = 61, seconds = None, first = 0, last = None):
        """Seek to evenly spaced positions and take the median.

        `seconds` is accepted and ignored -- a file is not obliged to wait, and
        spreading the samples across the requested span is strictly better.
        """
        last = self.frameCount if last is None else last
        cap = cv2.VideoCapture(self.path)
        frames = []
        for k in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(first + (last - first) * k / samples))
            ok, frame = cap.read()
            if ok:
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        cap.release()
        if not frames:
            raise IOError("no frames readable from %s" % self.path)
        return np.median(np.stack(frames), axis = 0).astype(np.uint8)

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class LiveSource(FrameSource):
    """A camera or capture dongle. Not seekable, and its frame interval is a promise
    the hardware does not keep.

    A dongle that enumerates as an ordinary UVC device is this class with a different
    index and nothing else changed. Only if one needs a vendor SDK would it deserve a
    class of its own.
    """

    isLive = True

    # The dongle's own default mode is 640x480, which gives 41 px per grid division
    # -- below the 43-93 range the detector was validated on -- and a frame narrow
    # enough that autoROI sees only the nominal 10 divisions, losing the widened
    # observation area. Asking for 1280x720 reproduces the geometry the acceptance
    # baselines were measured at: 61.0 px/division and a 14.9-division ROI, matching
    # clip 8 to a pixel. Measured on the bench, 2026-09-03, across every mode the
    # device offers. A device that cannot do it ignores the request and hands back
    # whatever it has, which is what would have happened anyway.
    DEFAULT_SIZE = (1280, 720)

    def __init__(self, index = 0, width = None, height = None, fps = None,
                 backend = None, warmupS = 1.0):
        if width is None and height is None:
            width, height = self.DEFAULT_SIZE
        if backend is None and sys.platform == "win32":
            backend = cv2.CAP_DSHOW      # the default backend on Windows is slow to open
        cap = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
        if not cap.isOpened():
            raise IOError("no camera at index %s. Run cv/UVC_check.py to see what is "
                          "connected." % index)
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps:
            cap.set(cv2.CAP_PROP_FPS, fps)
        self.index = index
        self._cap = cap
        # Some devices hand back a few malformed frames while exposure settles.
        # Read them now rather than letting autoROI see them.
        #
        # This also waits for the mode change above to take effect.  Asked for a size
        # immediately after release by a previous session, this dongle reports -1x-1
        # and delivers nothing, and the session then died 30 s later inside
        # learnBackground with "returned no frames" -- long after the real failure.
        # Keep reading until a frame actually arrives, and take the dimensions from
        # that frame rather than from cap.get(), which reports the stale handle.
        until = time.perf_counter() + warmupS
        good = None
        while time.perf_counter() < until or good is None:
            ok, frame = cap.read()
            if ok and frame is not None:
                good = frame
            elif time.perf_counter() > until + 5.0:
                cap.release()
                raise IOError("camera %s opened but delivered no frames in %.0f s. "
                              "Another application may still hold it; close it and "
                              "retry." % (index, warmupS + 5.0))
        # `or 30.0` does not catch this: CAP_DSHOW returns -1.0 for a device that
        # will not declare a rate, and -1.0 is truthy.  A negative nominal fps then
        # reaches RollingBackground, where `windowS * fps / samples` goes negative
        # and clamps to 1 -- so the 300 s window becomes 61 consecutive frames and
        # the median rebuilds every frame instead of every 5 s.  Measured on the
        # bench 2026-09-03: 2.51 fps delivered, and the droplets on screen ended up
        # inside their own background.  Advisory only, so a wrong-but-positive value
        # is survivable; a non-positive one is not.
        _fps = cap.get(cv2.CAP_PROP_FPS)
        self.nominalFps = _fps if _fps and _fps > 0 else 30.0
        self.height, self.width = good.shape[:2]
        self._t0 = None

    def _stamp(self):
        """Seconds since the first frame of this session."""
        now = time.perf_counter()
        if self._t0 is None:
            self._t0 = now
        return now - self._t0

    def frames(self, first = 0, last = None):
        """`first` and `last` are frame ordinals, kept only so a live run can be
        bounded the same way a file run is. Nothing seeks."""
        cap = self._cap
        n = 0
        prev = None
        gap = 1.8 / max(1.0, self.nominalFps)   # a miss this big is a dropped frame
        while True:
            # grab() is the capture; retrieve() is the decode.  Stamping between them
            # is as close to the shutter as OpenCV will let us get -- anything later
            # measures our own processing lag instead of the experiment.
            if not cap.grab():
                break
            t = self._stamp()
            ok, frame = cap.retrieve()
            if not ok:
                continue
            n += 1
            if prev is not None and t - prev > gap:
                self.dropped += int(round((t - prev) * self.nominalFps)) - 1
            prev = t
            if n <= first:
                continue
            if last is not None and n > last:
                break
            yield frame, t

    def learnBackground(self, samples = 61, seconds = 30.0, first = 0, last = None):
        """Spread `samples` grabs over `seconds` and take the median.

        The window is long on purpose. A droplet that hovers -- which is exactly what
        a balanced droplet does, and balancing is a required step -- burns into the
        median and is then subtracted away for the rest of the session. Learn this
        before the droplets are introduced, and keep the window wide.
        """
        every = max(0.01, float(seconds) / max(1, samples))
        frames = []
        for k in range(samples):
            due = time.perf_counter() + every
            if self._cap.grab():
                ok, frame = self._cap.retrieve()
                if ok:
                    frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            wait = due - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        if not frames:
            raise IOError("camera %s returned no frames while learning the background"
                          % self.index)
        # Kept so the caller can seed a rolling window with them.  They are clean by
        # construction -- learning happens before any droplets are introduced -- and
        # throwing them away is what forces a live run to start blind.
        self.learnFrames = frames
        return np.median(np.stack(frames), axis = 0).astype(np.uint8)

    def describe(self):
        return "LiveSource(index=%s) %dx%d @ %.2f fps advertised" % (
            self.index, self.width, self.height, self.nominalFps)

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class FramesSource(FrameSource):
    """A directory of lossless per-frame PNGs, with the REAL capture timestamps.

    The reason this exists: an H.264 copy of a session cannot stand in for the
    session.  Re-running detection over a window capture of a known 32 s legal fall
    found zero traverses (2026-09-04), because the codec destroys isolated spatial
    detail and a droplet is nothing but that.  Any threshold tuned on such a copy is
    tuned to the codec.

    `test/tools/raw_capture.py` writes these.  The frames are already ROI-cropped and
    `times.npy` holds the capture clock, so replaying one reproduces a live run
    exactly -- including its irregular frame interval, which is the whole point of
    measuring on timestamps rather than frame counts.

    It reports `isLive = True`: every live-only branch in the pipeline must run, or
    this tests something the bench never does.  Frames are read from disk on demand,
    never all at once -- a 20 minute fixture is 6 GB.
    """

    isLive = True

    def __init__(self, folder):
        self.folder = folder
        self.paths = sorted(glob.glob(os.path.join(folder, "f*.png")))
        if not self.paths:
            raise IOError("no f*.png frames in %s" % folder)
        stamps = os.path.join(folder, "times.npy")
        if not os.path.exists(stamps):
            raise IOError("%s has frames but no times.npy -- the capture clock is "
                          "not optional, timing runs on it" % folder)
        self.times = np.load(stamps)
        if len(self.times) != len(self.paths):
            raise IOError("%s: %d frames but %d timestamps"
                          % (folder, len(self.paths), len(self.times)))
        first = cv2.imread(self.paths[0], cv2.IMREAD_GRAYSCALE)
        if first is None:
            raise IOError("could not read %s" % self.paths[0])
        self.height, self.width = first.shape[:2]
        self.frameCount = len(self.paths)
        span = float(self.times[-1] - self.times[0])
        self.nominalFps = (len(self.times) / span) if span > 0 else 30.0

    def frames(self, first = 0, last = None):
        last = self.frameCount if last is None else last
        for i in range(first, min(last, self.frameCount)):
            g = cv2.imread(self.paths[i], cv2.IMREAD_GRAYSCALE)
            if g is None:
                continue
            yield cv2.cvtColor(g, cv2.COLOR_GRAY2BGR), float(self.times[i])

    def learnBackground(self, samples = 61, seconds = None, first = 0, last = None):
        """The capture already wrote one, from frames taken before any droplets.

        Re-deriving it from the fixture would be wrong: these frames CONTAIN the
        droplets, which is exactly the mistake the live path was built to avoid.
        """
        saved = os.path.join(self.folder, "background.png")
        if os.path.exists(saved):
            g = cv2.imread(saved, cv2.IMREAD_GRAYSCALE)
            if g is not None:
                self.learnFrames = [g]
                return g
        raise IOError("%s has no background.png" % self.folder)

    def describe(self):
        return "FramesSource(%s) %dx%d, %d frames @ %.2f fps measured" % (
            os.path.basename(self.folder), self.width, self.height,
            self.frameCount, self.nominalFps)


def openSource(spec, **kw):
    """Turn a specification into a FrameSource. Explicit, never guessed.

        openSource(src)          an existing FrameSource, returned unchanged
        openSource(0)            camera index 0
        openSource("cam:1")      camera index 1  (also camera:, dongle:, uvc:)
        openSource("clip.mp4")   a file
        openSource("frames:DIR")  a directory of raw PNGs written by raw_capture.py

    A bare integer means a camera because there is no other sensible reading of it.
    Everything else must say so, so that a mistyped path cannot silently open a
    webcam and produce numbers that look fine.
    """
    if isinstance(spec, FrameSource):
        return spec
    if isinstance(spec, int):
        return LiveSource(spec, **kw)
    text = str(spec)
    if text.lower().startswith(FRAMES_PREFIX):
        return FramesSource(text[len(FRAMES_PREFIX):].strip())
    for prefix in CAMERA_PREFIXES:
        if text.lower().startswith(prefix):
            tail = text[len(prefix):].strip()
            return LiveSource(int(tail) if tail.isdigit() else tail, **kw)
    return FileSource(text)


def isCameraSpec(spec):
    """True if `spec` names a camera rather than a file, without opening anything."""
    if isinstance(spec, (FrameSource, int)):
        return isinstance(spec, int) or spec.isLive
    return str(spec).lower().startswith(CAMERA_PREFIXES)
