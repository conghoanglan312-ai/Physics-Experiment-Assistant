"""Lab bench check for the capture dongle.  Run this BEFORE anything else.

The apparatus outputs composite video over BNC (the socket labelled 视频输出).
Whatever converter chain we use, it has to end up as a USB video device the laptop
recognises without installing a driver.  This script answers, in one run:

  1. Does the dongle show up as a camera at all?
  2. Does the picture actually contain the grid and the V:/t: panel?
  3. What resolution and what frame rate does it REALLY deliver?

Question 2 is the one that decides whether the whole idea works.  If the converter
taps the raw sensor and not the display, we get droplets with no grid, and the
timing lines, the scale in px/division and the legality test all have nothing to
stand on.

Usage:
    python cv/UVC_check.py            # scan devices 0..5
    python cv/UVC_check.py 1          # check one device
"""

import os
import sys
import time

import cv2

# autoROI answers checklist item 1 -- 'is the grid visible' -- better than an
# eye can, and it is the item that gates everything else.  Guarded: if the
# import fails the bench tool must still run and report devices.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from cv.pipeline import autoROI
except Exception:
    autoROI = None

# The size the pipeline will actually request, so this probes the same mode it will.
try:
    from cv.source import LiveSource
except Exception:                       # keep the bench tool runnable regardless
    class LiveSource(object):
        DEFAULT_SIZE = (1280, 720)

# The Windows console defaults to cp1252 and cannot print the label on the
# apparatus socket; without this the script dies before doing anything useful.
try:
    sys.stdout.reconfigure(encoding = "utf-8")
except Exception:
    pass

SECONDS = 5.0            # how long to measure the real frame rate over
SHOTS = "uvc_check"      # sample frames land here


def describe(index, measure = True):
    # Open exactly the way the pipeline does -- CAP_DSHOW, and ask for the same size.
    # This script used to open at the device's native mode, and the answer it gave did
    # not survive: on 2026-09-07 it named device 1 as the one showing the grid, and at
    # 1280x720 the grid was on device 0.  A bench tool whose index the pipeline cannot
    # use is worse than no bench tool, because it is believed.
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, LiveSource.DEFAULT_SIZE[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, LiveSource.DEFAULT_SIZE[1])
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None

    h, w = frame.shape[:2]
    claimed = cap.get(cv2.CAP_PROP_FPS)

    real, n = None, 0
    if measure:
        # Never trust CAP_PROP_FPS.  A device that claims 30 and delivers 27
        # inflates every fall time by 11%, and nothing downstream would notice.
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < SECONDS:
            good, f = cap.read()
            if not good:
                break
            n += 1
            frame = f
        real = n / (time.perf_counter() - t0)

    os.makedirs(SHOTS, exist_ok = True)
    shot = os.path.join(SHOTS, "device_%d.png" % index)
    cv2.imwrite(shot, frame)
    cap.release()
    roi = None
    if autoROI is not None:
        try:
            roi = autoROI(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        except Exception:
            roi = None
    return {"index": index, "w": w, "h": h, "claimed": claimed,
            "real": real, "frames": n, "shot": shot, "roi": roi}


def report(d):
    print("\n  device %d" % d["index"])
    print("    resolution     %d x %d" % (d["w"], d["h"]))
    print("    fps claimed    %.2f" % d["claimed"])
    if d["real"] is not None:
        print("    fps measured   %.2f   (%d frames in %.0f s)"
              % (d["real"], d["frames"], SECONDS))
        if d["claimed"] > 0 and abs(d["real"] - d["claimed"]) / d["claimed"] > 0.05:
            print("    ^^ MISMATCH over 5%. Timing must come from capture")
            print("       timestamps, not from frame counting.")
    print("    sample frame   %s" % d["shot"])

    # The detector's size constants were validated between 43 and 93 pixels per
    # grid division.  The reticle is 11 lines tall and fills most of the frame,
    # so height / 10 is a rough stand-in for the spacing.
    roi = d.get("roi")
    if autoROI is None:
        approx = d["h"] / 10.0
        verdict = "in range" if 43 <= approx <= 93 else "OUTSIDE the tested 43-93 range"
        print("    px/division    ~%.0f if the grid fills the frame  (%s)"
              % (approx, verdict))
    elif roi is None:
        print("    GRID           NOT FOUND -- this device is not showing the reticle")
    else:
        print("    GRID           found: %d rows, %.1f px/division"
              % (len(roi["rows"]), roi["spacing"]))
        print("    ROI            %d,%d,%d,%d   (%.1f divisions wide)"
              % (roi["x"], roi["y"], roi["w"], roi["h"], roi["w"] / roi["spacing"]))


def main(argv):
    print(__doc__.split("Usage:")[0].strip())
    print("\n" + "=" * 68)

    targets = [int(argv[1])] if len(argv) > 1 else range(6)
    found = []
    for i in targets:
        d = describe(i, measure = True)
        if d:
            found.append(d)
            report(d)

    print("\n" + "=" * 68)
    if not found:
        print("NO CAMERA FOUND.")
        print("  - If nothing appears here but Windows Device Manager lists the")
        print("    dongle under Cameras, it is a driver problem, not a wiring one.")
        print("  - If it appears nowhere, it is not UVC and needs the vendor's")
        print("    driver -- which becomes a dependency we must declare.")
        return
    withGrid = [d for d in found if d.get("roi")]
    print("%d device(s) responded to cv2.VideoCapture." % len(found))
    print("    NOTE: a laptop built-in webcam answers too, usually at index 0 and")
    print("    640x480. Responding is NOT the same as being the dongle.")
    if autoROI is not None:
        if not withGrid:
            print("\n*** NO DEVICE IS SHOWING THE RETICLE GRID. ***")
            print("    If the dongle is plugged in, the converter is tapping the wrong")
            print("    source. Nothing downstream can work until this is fixed.")
        else:
            print("\n>>> USE DEVICE %d -- it is the one showing the grid:" % withGrid[0]["index"])
            print("        python -m cv.pipeline live %d" % withGrid[0]["index"])
            if len(withGrid) > 1:
                print("    (%d devices show a grid; check the sample frames.)" % len(withGrid))
    print("""
NOW LOOK AT THE SAVED FRAME for the device named above. The grid and the row
count are checked for you; these are the parts no script can answer.

  [ ] Is the V: / t: readout panel visible? <- needed for the field-off check
  [ ] Does the panel sit clear of the grid, not touching the right-hand column?
  [ ] Is the image steady, not rolling or torn?
  [ ] Is the whole reticle in frame, not cropped at an edge?

If the grid is missing, the converter is tapping the camera instead of the
display. Move the tap to the monitor's own feed and re-run this. There is no
fallback: the camera-on-monitor method is retired, and the geometry constants
are calibrated on the dongle's field now.
""")


if __name__ == "__main__":
    main(sys.argv)
