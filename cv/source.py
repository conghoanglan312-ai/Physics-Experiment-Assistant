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
    # Where the time goes, so a run can answer it without a separate bench slot.
    # 2026-09-21: 22.1 of the camera's 30 fps reached the pipeline, and the question
    # "is the camera slow to hand frames over, or are we slow to come back for them"
    # could not be answered from outside -- both look identical from the far end.
    grabS = 0.0             # seconds inside grab()+retrieve()
    workS = 0.0             # seconds the consumer spent before asking for the next

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

    # Switches for the capture path, chosen at the bench without a rebuild:
    #
    #     MILLIKAN_CAPTURE=mjpg          ask the device for MJPG instead of raw
    #     MILLIKAN_CAPTURE=buffer1       keep one frame queued, not the driver's
    #     MILLIKAN_CAPTURE=dshow         DirectShow instead of Media Foundation
    #     MILLIKAN_CAPTURE=mjpg,buffer1  any combination, comma separated
    #
    # The default is Media Foundation because it was measured to be faster here;
    # see __init__ for the numbers. These exist so the next device that behaves
    # differently can be handled at the bench without a rebuild, and so the
    # measurement can be repeated: test/tools/capture_bench.py tries them all.
    #
    # Every session prints what it got -- milliseconds inside the camera against
    # milliseconds inside the pipeline -- so a device that needs a different
    # setting says so rather than quietly costing frames.
    @staticmethod
    def _wanted():
        raw = os.environ.get("MILLIKAN_CAPTURE", "")
        return set(w.strip().lower() for w in raw.split(",") if w.strip())

    # Given a frame, is this the device we want?  Set by the pipeline to "does
    # this show the reticle", which is the only question that actually
    # identifies the dongle -- see accept() below.
    accept = None

    def __init__(self, index = 0, width = None, height = None, fps = None,
                 backend = None, warmupS = 1.0):
        if width is None and height is None:
            width, height = self.DEFAULT_SIZE
        want = self._wanted()
        if backend is None and sys.platform == "win32":
            # MEDIA FOUNDATION, measured on this dongle on 2026-09-21 with
            # test/tools/capture_bench.py, twelve seconds per mode:
            #
            #     (default, DSHOW)   23.6 fps   YUY2
            #     mjpg               23.6 fps   YUY2   <- request ignored
            #     buffer1            23.6 fps   YUY2
            #     mjpg,buffer1       23.6 fps   YUY2
            #     msmf               30.0 fps
            #     msmf,mjpg          30.0 fps
            #
            # DirectShow could not be moved off uncompressed YUY2 -- it accepted
            # the FOURCC and kept the format -- and 1280x720 of it is about
            # 27 MB/s, which this device sustains at 23.6 fps and no more. Media
            # Foundation delivers the full 30.0, opens in 1.2 s, and hands back
            # the same 1280x720 frames.
            #
            # That is a quarter more samples per second for every speed estimate,
            # and it is the frames the tracker was missing: a session delivered
            # 22.1 fps on 2026-09-20, tracks died mid-fall, and the square kept
            # losing droplets the operator could see perfectly well.
            backend = cv2.CAP_DSHOW if "dshow" in want else cv2.CAP_MSMF
        cap = cv2.VideoCapture(index, backend) if backend else cv2.VideoCapture(index)
        if not cap.isOpened():
            raise IOError("no camera at index %s. Run cv/UVC_check.py to see what is "
                          "connected." % index)
        # BEFORE the size: several drivers apply a pixel format only while the mode
        # is being negotiated and ignore it afterwards.
        if "mjpg" in want:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if "buffer1" in want:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if want:
            # Say what the device actually agreed to.  A request that was ignored
            # and one that worked look identical from here otherwise, which is the
            # whole point on a bench where the switch is chosen by trial.
            code = int(cap.get(cv2.CAP_PROP_FOURCC)) or 0
            fourcc = "".join(chr((code >> (8 * k)) & 0xFF) for k in range(4)).strip()
            print("  capture switches %s -> backend %s, format %s"
                  % (",".join(sorted(want)),
                     "MSMF" if backend == cv2.CAP_MSMF else "DSHOW",
                     fourcc or "?"), flush = True)
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
            _t1 = time.perf_counter()
            if not cap.grab():
                break
            t = self._stamp()
            ok, frame = cap.retrieve()
            _t2 = time.perf_counter()
            self.grabS += _t2 - _t1
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
            self.workS += time.perf_counter() - _t2

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
        # times.npy is written during a clean shutdown; times.txt is flushed every
        # frame.  A capture that was killed has only the latter, and losing a whole
        # session to a missing index file is not acceptable -- it happened once.
        stamps = os.path.join(folder, "times.npy")
        log = os.path.join(folder, "times.txt")
        if os.path.exists(stamps):
            self.times = np.load(stamps)
        elif os.path.exists(log):
            self.times = np.array([float(v) for v in open(log) if v.strip()])
            print("  %s: recovered %d timestamps from times.txt (the capture did not "
                  "shut down cleanly)" % (os.path.basename(folder), len(self.times)))
        else:
            raise IOError("%s has frames but no times.npy or times.txt -- the capture "
                          "clock is not optional, timing runs on it" % folder)
        if len(self.times) != len(self.paths):
            # A killed capture can leave one more frame than timestamp, or the
            # reverse.  Trim to the shorter rather than refuse the whole fixture.
            keep = min(len(self.times), len(self.paths))
            if abs(len(self.times) - len(self.paths)) > 5:
                raise IOError("%s: %d frames but %d timestamps -- too far apart to "
                              "be a shutdown race"
                              % (folder, len(self.paths), len(self.times)))
            print("  %s: %d frames, %d timestamps; using %d"
                  % (os.path.basename(folder), len(self.paths), len(self.times), keep))
            self.paths, self.times = self.paths[:keep], self.times[:keep]
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


# How many camera indices to look at when the requested one is not the dongle.
CAMERA_SCAN = 4


def findCamera(index = 0, accept = None, **kw):
    """A LiveSource showing what `accept` wants, starting at `index`.

    THE INDEX IS NOT AN IDENTITY.  DirectShow and Media Foundation enumerate the
    same two devices in opposite orders on this laptop -- measured 2026-09-21:
    the dongle is 0 under DSHOW and 1 under MSMF, with the built-in webcam taking
    the other slot.  So changing the backend silently pointed every "device 0" in
    the project at the webcam, and the session opened, ran, learned a background
    and reported "Auto-ROI failed: no grid in frame" with the operator standing at
    the apparatus wondering what they had unplugged.

    `accept(gray)` answers the only question that identifies the dongle: does
    this frame show the reticle.  The requested index is tried first, so an
    operator who names one still gets it if it is right.
    """
    # ...AND NEITHER IS THE BACKEND.  Media Foundation is the faster path on the
    # machine this was measured on, but OpenCV builds differ: a teammate's logged
    # "VIDEOIO(MSMF): backend is generally available but can't be used to capture
    # by index" for every index, so making MSMF the default took their bench
    # from working to dead (issue #17).  Where it cannot capture, fall back to
    # DirectShow and say so -- a slower session is a session; no camera is not.
    backends = [kw.pop("backend", None)]
    if backends[0] is None and sys.platform == "win32":
        backends = [None, cv2.CAP_DSHOW if "dshow" not in LiveSource._wanted()
                    else cv2.CAP_MSMF]
    tried, found = [], None
    for backend in backends:
        for k in [index] + [i for i in range(CAMERA_SCAN) if i != index]:
            try:
                src = LiveSource(k, backend = backend, **kw)
            except IOError:
                tried.append(k)
                continue
            if accept is None:
                return src
            ok = False
            try:
                for frame, _t in src.frames():
                    ok = bool(accept(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
                    break
            except Exception:
                ok = False
            if ok:
                found = (k, backend)
                break
            src.close()
            tried.append(k)
        if found:
            break
    if not found:
        raise IOError("no camera showing the grid (tried %s, on both Windows "
                      "capture backends). Run cv/UVC_check.py."
                      % ", ".join(str(t) for t in tried))
    k, backend = found
    if k != index or backend is not None:
        print("  camera: index %d%s -- it is the one showing the grid"
              % (k, "" if backend is None else " via DirectShow, because Media "
                 "Foundation would not capture by index here"), flush = True)
    return src


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
        return findCamera(spec, accept = LiveSource.accept, **kw)
    text = str(spec)
    if text.lower().startswith(FRAMES_PREFIX):
        return FramesSource(text[len(FRAMES_PREFIX):].strip())
    for prefix in CAMERA_PREFIXES:
        if text.lower().startswith(prefix):
            tail = text[len(prefix):].strip()
            if tail.isdigit():
                return findCamera(int(tail), accept = LiveSource.accept, **kw)
            return LiveSource(tail, **kw)
    return FileSource(text)


def isCameraSpec(spec):
    """True if `spec` names a camera rather than a file, without opening anything."""
    if isinstance(spec, (FrameSource, int)):
        return isinstance(spec, int) or spec.isLive
    return str(spec).lower().startswith(CAMERA_PREFIXES)