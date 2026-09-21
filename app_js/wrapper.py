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

import json
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
PUBLISH_WAIT_S = 20.0       # how long to wait for the pipeline's outbound connection

_COMMAND_RE = re.compile(r"^python -m cv\.pipeline live (\d+)\s*$")


# --- Make sure the LLM tutor's vector store exists -------------------------
#
# The chat assistant retrieves from a Chroma collection built from the lab
# manual PDF (llm/src/ingest.py, via llm/src/ingestWrapper.py). It only needs
# building once, and ingestWrapper's own docstring says calling it once at
# startup is fine -- runIngestion() chdirs while it runs, which is
# process-wide, so it should not be called on every live session, just once
# here, whenever this module (and so the app) is loaded.
#
# This module's actual job is the camera/live pipeline, so a problem with the
# unrelated LLM stack -- missing dependency, missing PDF, a failed embed --
# must never stop that from working. Any failure here is reported and
# swallowed, never raised.
try:
    from llm.src.ingestWrapper import IngestionError, runIngestion

    def _ensure_manual_ingested():
        try:
            result = runIngestion()  # force=False: no-ops if already ingested
        except IngestionError as exc:
            print("Manual ingestion check failed (chat tutor may lack context): %s" % exc)
            return
        if result["status"] == "ingested":
            print(
                "Ingested the lab manual (%d documents, %.1fs)."
                % (result["documents"], result["seconds"])
            )
        # "skipped" means it was already ingested -- nothing worth printing.

    _ensure_manual_ingested()
except ImportError as exc:
    print("Skipping manual ingestion check (llm.src.ingestWrapper unavailable): %s" % exc)
# ---------------------------------------------------------------------------


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


# The pipeline delivers about 23 fps to its own window and this mirrored 12, which is
# exactly how it looked: a stream sample measured 73 frames in 6 s.
#
# 23 is not reachable by this method.  Measured on the bench laptop, one mirrored frame
# costs about 59 ms, and the split is not where you would guess: JPEG encoding the
# 1272x689 frame is 4.3 ms, so the other ~55 ms is PrintWindow rendering the window
# into an off-screen DC.  Lowering quality saves 0.1 ms and downscaling saves 1.2 ms --
# neither touches the real cost.  The ceiling is about 17 fps.
#
# So 15: a clear improvement on 12, and it still SLEEPS between frames rather than
# pinning a core on a machine that is also tracking droplets.  The tracker's frames are
# the measurement; the browser's are a convenience.
#
# Going beyond this means not capturing a window at all -- the pipeline already has the
# frame in memory and could publish it for 4 ms instead of 59.  See issue #13.
MIRROR_FPS = 15.0


def _serve_published_frames(listener, streamer, stop_event):
    """Accept the pipeline's outbound connection and forward its frames.

    Length-prefixed JPEGs, newest served to every reader.  The pipeline connects to
    us rather than listening itself, so the process that must not fall over
    mid-measurement never binds a port.
    """
    try:
        listener.settimeout(PUBLISH_WAIT_S)
        conn, _ = listener.accept()
    except OSError:
        return False
    conn.settimeout(5.0)
    try:
        while not stop_event.is_set():
            head = b""
            while len(head) < 4:
                chunk = conn.recv(4 - len(head))
                if not chunk:
                    return True
                head += chunk
            size = int.from_bytes(head, "big")
            if size <= 0 or size > 32 * 1024 * 1024:
                return True                     # framing lost; give up cleanly
            payload = b""
            while len(payload) < size:
                chunk = conn.recv(size - len(payload))
                if not chunk:
                    return True
                payload += chunk
            streamer.push_jpeg(payload)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return True


def _mirror_window(hwnd, streamer, stop_event, fps = MIRROR_FPS):
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
    """Run the bench check and launch the live pipeline in the background with its native
    "Droplets" window, then start mirroring that window over HTTP.

    `source` is optional and skips the bench check:
      - None (default): runs the bench check to pick a real camera/dongle, then
        `python -m cv.pipeline live <device>`.
      - a number (int or numeric string): uses that camera index.
      - anything else (a string): treated as a recorded clip; runs
        `python -m cv.pipeline play <source>` through the same window (click-to-pick still works).
    """
    """
    `source` skips the bench check.  A number is a camera index; anything else
    is a recorded clip, which the pipeline plays through the same window at
    real speed with click-to-pick working -- so the whole app can be exercised
    at a desk instead of in a lab slot with the dongle wired in.

    Returns a handle dict; pass it to stop_live() when the UI is done.
    """
    if source is None or str(source).strip() == "":
        device = _bench_check()
        command = [sys.executable, "-m", "cv.pipeline", "live", str(device)]
    elif str(source).strip().isdigit():
        device = int(str(source).strip())
        command = [sys.executable, "-m", "cv.pipeline", "live", str(device)]
    else:
        device = str(source).strip()
        command = [sys.executable, "-m", "cv.pipeline", "play", device]

    # Ask the pipeline to send us its frames directly.  We listen, it connects: no
    # port bound by the measurement process.  If it never connects -- an older
    # pipeline, a firewall -- we fall back to capturing its window.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    command += ["--publish", str(listener.getsockname()[1])]
    port = stream_port or _find_free_port()
    print("Command:", " ".join(command))
    # stdin stays open for the life of the session: the browser's clicks go down it,
    # one JSON object per line.  See send_click().
    process = subprocess.Popen(command, stdin = subprocess.PIPE)

    hwnd = _find_window(WINDOW_TITLE, timeout = window_timeout)
    if hwnd is None:
        process.terminate()
        # command is [python, -m, cv.pipeline, live|play, source] -- only a live
        # run spends its first ~30s learning the background before the window
        # appears, so only a live run gets the extra explanation. (This used to
        # test command[0], which is the interpreter and is never "live", so the
        # placeholder text on the other branch went out to the operator.)
        learning_note = (
            " (including the background-learning wait)"
            if len(command) > 3 and command[3] == "live" else ""
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
        # Z-order only.  This process is not DPI-aware, so the size it reads is
        # scaled down and the size it writes back is scaled up again, rounded: at
        # 175% the pipeline's 1280x720 window came back 1273x696 and lost its
        # bottom 24 px, which is the banner.  AUTOSIZE never grows it back.
        win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW | win32con.SWP_NOSIZE,
    )

    streamer = FrameStreamer(port).start()
    stop_event = threading.Event()

    def feed():
        # Published frames cost the pipeline 4 ms each; capturing its window costs
        # 59 ms and caps the browser near 17 fps.  Prefer the cheap one, keep the
        # other as the fallback it now is.
        if not _serve_published_frames(listener, streamer, stop_event):
            print("Pipeline did not publish frames; mirroring its window instead.")
            _mirror_window(hwnd, streamer, stop_event)

    mirror_thread = threading.Thread(target = feed, daemon = True)
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


def send_click(handle, x, y):
    """Forward a click from the browser to the running pipeline.

    `x` and `y` are fractions of the frame; the pipeline converts them to pixels.
    """
    if handle is None:
        raise RuntimeError("no live session")
    process = handle.get("process")
    if process is None or process.poll() is not None or process.stdin is None:
        raise RuntimeError("the pipeline is not running")
    x = min(1.0, max(0.0, float(x)))
    y = min(1.0, max(0.0, float(y)))
    line = json.dumps({"x": x, "y": y}) + "\n"
    process.stdin.write(line.encode("utf-8"))
    process.stdin.flush()


def click_live(handle, x, y):
    """Forward one left-click to the pipeline's native window at pixel coords (x, y).

    This posts Win32 mouse messages so pipeline.py receives the same WM_LBUTTON events
    as a real mouse click.
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