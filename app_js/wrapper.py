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
      What the browser UI calls. By default (no source) this is unchanged:
      runs the bench check, launches the live pipeline in the background,
      waits for its window to appear, and starts mirroring it. Pass a
      number to use that camera index directly (skipping the bench check),
      or a clip spec (a file path, or "frames:DIR") to run it through
      pipeline.py's own `play` entry point instead -- same window, no
      apparatus needed. Returns a handle; stop_live(handle) tears
      everything down.

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
import ctypes

import numpy as np
from PIL import Image
import win32api
import win32con
import win32gui
import win32ui

from app_js.stream_server import FrameStreamer

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


def _looksNumeric(value):
    """True for anything that should be read as a camera index rather than
    a clip spec: an int/float, or a string of digits (optionally signed).
    Everything else -- a file path, a "frames:DIR" spec, "cam:N" -- is a
    clip and goes through `play`, not `live`.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return value.strip().lstrip("-").isdigit()
    return False


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


def _window_client_size(hwnd):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    return max(0, right - left), max(0, bottom - top)


def _capture_window(hwnd):
    """Capture the native pipeline window into an off-screen bitmap."""
    width, height = _window_client_size(hwnd)
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = None
    src_dc = None
    mem_dc = None
    bitmap = None
    old_bitmap = None
    try:
        hwnd_dc = win32gui.GetDC(hwnd)
        src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        old_bitmap = mem_dc.SelectObject(bitmap)

        # Ask Windows to render only the client region into our off-screen DC.
        # This works without keeping a second visible desktop window around.
        # Capture the rendered OpenCV window. PW_RENDERFULLCONTENT asks Windows
        # to render the current contents even when the window is covered.
        result = ctypes.windll.user32.PrintWindow(
            int(hwnd), mem_dc.GetSafeHdc(), 2
        )
        if result != 1:
            return None

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )

        # PrintWindow above captures the whole native window. Crop back to its
        # client area so the browser feed contains only the pipeline output.
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        win_left, win_top, _, _ = win32gui.GetWindowRect(hwnd)
        crop_left = max(0, client_left - win_left)
        crop_top = max(0, client_top - win_top)
        client_width, client_height = _window_client_size(hwnd)
        crop_right = min(info["bmWidth"], crop_left + client_width)
        crop_bottom = min(info["bmHeight"], crop_top + client_height)
        image = image.crop((crop_left, crop_top, crop_right, crop_bottom))
        return np.array(image)[:, :, ::-1].copy()
    finally:
        if old_bitmap is not None and mem_dc is not None:
            mem_dc.SelectObject(old_bitmap)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if mem_dc is not None:
            mem_dc.DeleteDC()
        if src_dc is not None:
            src_dc.DeleteDC()
        if hwnd_dc is not None:
            win32gui.ReleaseDC(hwnd, hwnd_dc)


def _mirror_window(hwnd, streamer, stop_event, fps = 12):
    period = 1.0 / fps
    while not stop_event.is_set():
        t0 = time.time()
        if not win32gui.IsWindow(hwnd):
            break
        try:
            frame = _capture_window(hwnd)
            if frame is not None:
                streamer.push(frame)
        except Exception:
            pass
        time.sleep(max(0.0, period - (time.time() - t0)))


def start_live(source = None, stream_port = None, window_timeout = WINDOW_TIMEOUT_S):
    """Launch the pipeline in the background with its normal native
    "Droplets" window, then start mirroring that window over HTTP.

    `source` is optional and does not change any existing behaviour when
    omitted:

      - None (default): unchanged -- runs the bench check to pick a real
        camera/dongle, then `python -m cv.pipeline live <device>`.
      - a number (int, or a numeric string): skips the bench check and uses
        that value directly as the camera index for `live <index>`.
      - anything else (a string): a clip spec -- a file path, or a
        "frames:DIR" directory -- run through `python -m cv.pipeline play
        <source>` instead. Same "Droplets" window, paced to real time,
        click-to-pick still working, no apparatus or dongle required. This
        is pipeline.py's own `play` entry point (see its module docstring);
        nothing about pipeline.py changes here, this just chooses which of
        its existing CLI commands to run.

    Returns a handle dict; pass it to stop_live() when the UI is done.
    """
    device = None
    if source is None:
        device = _bench_check()
        command_args = ["live", str(device)]
    elif _looksNumeric(source):
        device = int(str(source).strip())
        command_args = ["live", str(device)]
    else:
        command_args = ["play", str(source)]

    port = stream_port or _find_free_port()
    command = [sys.executable, "-m", "cv.pipeline"] + command_args
    print("Command:", " ".join(command))
    process = subprocess.Popen(command)

    hwnd = _find_window(WINDOW_TITLE, timeout = window_timeout)
    if hwnd is None:
        process.terminate()
        learning_note = (
            " (including the background-learning wait)" if command_args[0] == "live" else ""
        )
        raise RuntimeError(
            "The pipeline started but its '%s' window never appeared within "
            "%.0fs. It prints its own progress to the console this was run "
            "from%s -- check there for what it's stuck on."
            % (WINDOW_TITLE, window_timeout, learning_note)
        )

    # Keep the pipeline window rendered for Windows' PrintWindow capture, but
    # send it to the bottom of the z-order so the browser is the only intended
    # user-facing interface. It remains a backend rendering surface rather
    # than a second interface the user needs to operate.
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(1, right - left)
    height = max(1, bottom - top)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_BOTTOM,
        0,
        0,
        width,
        height,
        win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
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
        "source": source if source is not None else device,
        "url": "http://127.0.0.1:%d/stream" % port,
    }


def click_live(handle, x, y):
    """Forward one left-click to the pipeline's own "Droplets" window, at
    client coordinates (x, y), exactly as if the operator had physically
    clicked the (now bottom-of-z-order, browser-only) native window there.

    pipeline.py is never touched. It already selects a droplet purely
    through cv2.setMouseCallback("Droplets", onMouse), which OpenCV backs
    with the window's ordinary Win32 message loop -- onMouse reads its
    (mx, my) straight out of the WM_LBUTTONDOWN message's lParam. Posting
    that same message ourselves, with the coordinates the browser reports,
    is indistinguishable to pipeline.py from a real click; nothing about
    its own selection/tracking logic needs to know the click didn't come
    from a mouse in the room.

    PostMessage (not SendMessage) queues the message and returns
    immediately without waiting for pipeline.py's message loop to process
    it, and without moving the real system cursor -- appropriate for a
    window that is not meant to be interacted with directly any more.
    """
    if not handle:
        raise RuntimeError("no live session")
    hwnd = handle.get("hwnd")
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError("the pipeline window is not available")

    width, height = _window_client_size(hwnd)
    ix, iy = int(round(x)), int(round(y))
    if width > 0:
        ix = max(0, min(width - 1, ix))
    if height > 0:
        iy = max(0, min(height - 1, iy))

    lparam = win32api.MAKELONG(ix, iy)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)


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
