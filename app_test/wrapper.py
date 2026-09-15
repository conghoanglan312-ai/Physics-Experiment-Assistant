"""Bridge between the UI and the lab-bench check + live pipeline.

pipeline.py and UVC_check.py are UNCHANGED. The live pipeline still opens its
own native "Droplets" window exactly the way it always has -- run
`python -m cv.pipeline live N` directly and click-to-pick still works, same
as before this file existed.

What's new is a background thread that repeatedly screenshots THAT window
(the same trick a screen-share uses -- it is not told anything about what the
window contains) and re-serves those screenshots as an MJPEG stream. The UI
then points a plain <img> tag at that stream. A screenshot has no click
handler, so nothing the UI does can reach back into the pipeline process --
the embed is pure display, exactly as asked.

Used two ways:

  python wrapper.py
      Unchanged CLI behaviour: run the bench check, then run the live
      pipeline in a normal, visible, clickable window and block until the
      operator quits it with `q` or Ctrl+C. No mirroring involved.

  from wrapper import start_live, stop_live
      What the Streamlit UI calls. Runs the bench check, launches the live
      pipeline in the background, waits for its window to appear, and starts
      mirroring it. Returns a handle; stop_live(handle) tears everything down.

Needs pywin32 (for locating and reading the native window) and Pillow (for
the actual screen grab), on top of what pipeline.py already needs:
    pip install pywin32 pillow
This only works on Windows, same as the rest of this lab's toolchain.
"""

import re
import socket
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import ImageGrab
import win32gui

from app_test.stream_server import FrameStreamer

# Must match the title pipeline.py passes to cv2.namedWindow(...) -- it always
# does, so this is a contract with that file, not a change to it.
WINDOW_TITLE = "Droplets"

# A live run learns its background for the first ~30s before its window opens
# (see pipeline.py's own docstring/comments) -- give it real room, or a slow
# machine looks like a launch failure when it's just still warming up.
WINDOW_TIMEOUT_S = 45.0

_COMMAND_RE = re.compile(r"^python -m cv\.pipeline live (\d+)\s*$")


def _bench_check():
    """Run the bench check and return the device index it recommends.

    Raises RuntimeError, with the bench check's own explanation as the
    message, if no device showed the grid -- there's nothing to launch.
    """
    result = subprocess.run(
        [sys.executable, "-m", "cv.UVC_check"],
        capture_output = True, text = True, encoding = "utf-8", errors = "replace",
    )
    output = result.stdout.strip()
    lastLine = output.splitlines()[-1].strip() if output else ""
    match = _COMMAND_RE.match(lastLine)
    if not match:
        raise RuntimeError(
            "Bench check did not find a device showing the grid:\n\n" + output
        )
    return int(match.group(1))


def _find_free_port(start = 8899, tries = 20):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port found for the live feed server")


def _find_window(title, timeout, poll = 0.25):
    """Wait for the native window to appear and return its handle, or None
    if it never does within `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd and win32gui.IsWindowVisible(hwnd):
            return hwnd
        time.sleep(poll)
    return None


def _window_bbox(hwnd):
    """Screen-space bounding box of the window's CLIENT area only -- the
    picture itself, with no title bar or OS chrome around it."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)   # (0, 0, w, h)
    screenLeft, screenTop = win32gui.ClientToScreen(hwnd, (left, top))
    screenRight, screenBottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    return (screenLeft, screenTop, screenRight, screenBottom)


def _mirror_window(hwnd, streamer, stop_event, fps = 12):
    period = 1.0 / fps
    while not stop_event.is_set():
        t0 = time.time()
        if not win32gui.IsWindow(hwnd):
            break            # the pipeline's window closed -- nothing left to mirror
        try:
            bbox = _window_bbox(hwnd)
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                shot = ImageGrab.grab(bbox = bbox)
                frame = np.array(shot)[:, :, ::-1]     # RGB (PIL) -> BGR (cv2/JPEG)
                streamer.push(frame)
        except Exception:
            pass             # a transient grab failure just skips one frame
        time.sleep(max(0.0, period - (time.time() - t0)))


def start_live(stream_port = None, window_timeout = WINDOW_TIMEOUT_S):
    """Run the bench check, launch the live pipeline in the background with
    its normal native window, then start mirroring that window over HTTP.

    Returns a handle dict; pass it to stop_live() when the UI is done.
    """
    device = _bench_check()
    port = stream_port or _find_free_port()
    command = [sys.executable, "-m", "cv.pipeline", "live", str(device)]
    print("Command:", " ".join(command))
    process = subprocess.Popen(command)

    hwnd = _find_window(WINDOW_TITLE, timeout = window_timeout)
    if hwnd is None:
        process.terminate()
        raise RuntimeError(
            "The pipeline started but its '%s' window never appeared within "
            "%.0fs. It prints its own progress to the console this was run "
            "from (including the background-learning wait) -- check there "
            "for what it's stuck on." % (WINDOW_TITLE, window_timeout)
        )

    streamer = FrameStreamer(port).start()
    stop_event = threading.Event()
    mirror_thread = threading.Thread(
        target = _mirror_window, args = (hwnd, streamer, stop_event), daemon = True
    )
    mirror_thread.start()

    return {
        "process": process,
        "streamer": streamer,
        "stop_event": stop_event,
        "mirror_thread": mirror_thread,
        "hwnd": hwnd,
        "port": port,
        "device": device,
        "url": "http://127.0.0.1:%d/stream" % port,
    }


def stop_live(handle):
    """Tear down everything start_live() started. Safe to call twice."""
    if not handle:
        return
    handle["stop_event"].set()
    streamer = handle.get("streamer")
    if streamer is not None:
        streamer.stop()
    proc = handle.get("process")
    if proc is not None and proc.poll() is None:
        proc.terminate()


if __name__ == "__main__":
    device = _bench_check()
    command = [sys.executable, "-m", "cv.pipeline", "live", str(device)]
    print("Command:", " ".join(command))
    subprocess.run(command, check = True)
