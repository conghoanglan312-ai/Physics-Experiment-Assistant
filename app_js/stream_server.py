"""Tiny MJPEG server so a live pipeline session can be watched from a browser
instead of (or alongside) the native cv2 window.

Used when the UI drives cv/wrapper.py: the pipeline process pushes each
annotated frame in here, and the Streamlit page just points a plain <img> tag
at http://127.0.0.1:<port>/stream. This module only ever serves bytes OUT --
it has no endpoint that accepts input -- which is what keeps the UI's embed
pure display. Nothing a viewer does in their browser reaches this process.

No third-party dependency: http.server is standard library, and the only
encoding step (BGR ndarray -> JPEG bytes) reuses cv2, which the pipeline
already depends on.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

BOUNDARY = "millikanframe"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the pipeline's own stdout uncluttered by request logging

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/stream"):
            self._stream()
        elif path == "/frame.jpg":
            self._single()
        else:
            self.send_response(404)
            self.end_headers()

    def _single(self):
        jpg = self.server.streamer.latest_jpeg()
        if jpg is None:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self):
        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=%s" % BOUNDARY
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        streamer = self.server.streamer
        try:
            for jpg in streamer.frames():
                self.wfile.write(("--%s\r\n" % BOUNDARY).encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpg)).encode())
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser tab closed or reloaded -- not our problem


class FrameStreamer:
    """Push BGR frames in from the capture loop, pull MJPEG bytes out over HTTP.

    One writer (the pipeline's live loop), any number of readers (a browser
    tab, and whatever it reconnects to on a Streamlit rerun). Readers wait on
    a condition variable rather than pulling from a queue, so a slow or
    momentarily-absent reader can never make push() block or build up a
    backlog -- every reader always gets the newest frame, and simply misses
    whichever ones it was too slow for. That trade is exactly right for a
    live view: a dropped frame is invisible, a growing lag is not.
    """

    def __init__(self, port, quality = 80):
        self.port = port
        self.quality = quality
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0
        self._server = None
        self._thread = None

    def push(self, frame_bgr):
        ok, buf = cv2.imencode(
            ".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        )
        if not ok:
            return
        with self._cond:
            self._jpeg = buf.tobytes()
            self._seq += 1
            self._cond.notify_all()

    def push_jpeg(self, jpeg_bytes):
        """Already encoded, straight from the pipeline -- no decode, no re-encode.

        The pipeline draws the frame, so it can encode it for the 4 ms an encode
        costs.  Capturing its window instead cost 59 ms, nearly all of it Windows
        re-rendering a picture that already existed.
        """
        if not jpeg_bytes:
            return
        with self._cond:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cond.notify_all()

    def latest_jpeg(self):
        with self._cond:
            return self._jpeg

    def frames(self):
        """Generator a request handler iterates over; blocks until the next
        frame that hasn't been sent to this particular reader yet."""
        lastSeq = -1
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._seq != lastSeq, timeout = 5.0)
                jpg, lastSeq = self._jpeg, self._seq
            if jpg is not None:
                yield jpg

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._server.streamer = self
        self._server.daemon_threads = True
        self._thread = threading.Thread(target = self._server.serve_forever, daemon = True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
