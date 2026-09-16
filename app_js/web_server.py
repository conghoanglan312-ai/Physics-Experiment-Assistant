"""Minimal HTTP adapter for the HTML/CSS/JS frontend.

The existing CV and LLM Python code is intentionally left unchanged.
This file only exposes the existing wrapper.start_live()/stop_live() and
llm.main.getResponseFromGemini() as browser-accessible HTTP endpoints.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from llm.main import getResponseFromGemini
from app_js.wrapper import start_live, stop_live, click_live, send_click
from cv.api import live_status

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8502

_live_lock = threading.Lock()
_live_handle = None
_live_token = None

# Tracks the currently in-flight /api/chat generation per sessionID (the
# frontend always sends the same fixed sessionID, so back-to-back messages
# share one Gemini session). Without this, a request that times out on the
# client leaves its server-side worker thread possibly still blocked inside
# getResponseFromGemini() for that session; the *next* message's own
# generation can then get stuck contending for the same session before its
# own watchdog has a chance to do any real work, making it look like the new
# response is still governed by the previous timeout instead of starting a
# fresh, independent one.
_chat_lock = threading.Lock()
_chat_sessions: dict[str, dict] = {}


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def get_live():
    with _live_lock:
        return _live_handle, _live_token


def set_live(handle, token):
    global _live_handle, _live_token
    with _live_lock:
        _live_handle = handle
        _live_token = token


def clear_live():
    global _live_handle, _live_token
    with _live_lock:
        _live_handle = None
        _live_token = None


def process_is_running(handle) -> bool:
    return bool(handle and handle.get("process") is not None and handle["process"].poll() is None)


class Handler(BaseHTTPRequestHandler):
    server_version = "MillikanWeb/1.0"

    def log_message(self, fmt, *args):
        pass

    def _headers(self, content_type="application/json", status=200, content_length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _json(self, payload, status=200):
        body = json_bytes(payload)
        self._headers(content_length=len(body), status=status)
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/live/status":
            handle, token = get_live()
            running = process_is_running(handle)
            if handle is not None and not running:
                clear_live()
try:
    status = live_status()
except Exception:
    status = None
if status and not running and status.get("phase") != "finished":
    status["phase"] = "stopped"
self._json({
    "running": running,
    "device": handle.get("device") if running else None,
    "source": handle.get("source") if running else None,
    "status": status,
})
            return

        if path == "/health":
            self._json({"ok": True})
            return

        self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/live/start":
            self._start_live()
            return
        if path == "/api/live/click":
            self._click()
            return

        if path == "/api/live/stop":
            self._stop_live()
            return
        if path == "/api/live/click":
            self._click_live()
            return
        if path == "/api/chat":
            self._chat()
            return

        self._json({"error": "Not found"}, status=404)

    def _start_live(self):
        global _live_handle
        handle, token = get_live()
        if process_is_running(handle):
            self._json({
                "url": handle["url"],
                "device": handle["device"],
                "source": handle.get("source"),
                "token": token,
            })
            return

        # An optional {"source": ...} plays a recorded clip instead of running the
        # bench check, so the UI can be developed and tested with no apparatus.
        try:
payload = self._read_json()
except Exception:
    payload = {}
source = str(payload.get("source", "")).strip() or None

try:
    handle = start_live(source=source)
    token = secrets.token_urlsafe(24)
    set_live(handle, token)
    self._json({
        "url": handle["url"],
        "device": handle["device"],
        "source": handle.get("source"),
        "token": token,
    })
except Exception as exc:
    clear_live()
    self._json({"error": "Please plug in the dongle first."
                         if source is None else
                         "Could not play %s: %s" % (source, exc)}, status=500)

def _click(self):
    """{"x": 0.42, "y": 0.63, "token": "..."} -- fractions of the frame."""
    handle, token = get_live()
    if not process_is_running(handle):
        self._json({"error": "No live session."}, status=409)
        return
    try:
        payload = self._read_json()
        if str(payload.get("token", "")) != str(token):
            self._json({"error": "Wrong token."}, status=403)
            return
        x, y = float(payload["x"]), float(payload["y"])
    except Exception:
        self._json({"error": "Send x and y as fractions of the frame."}, status=400)
        return
    try:
        send_click(handle, x, y)
    except Exception as exc:
        self._json({"error": str(exc)}, status=500)
        return
    self._json({"ok": True})

    def _stop_live(self):
        try:
            body = self._read_json()
        except Exception:
            body = {}

        handle, token = get_live()
        if handle is None:
            self._json({"ok": True})
            return
        if body.get("token") != token:
            self._json({"error": "Invalid live-session token."}, status=403)
            return

        try:
            stop_live(handle)
        finally:
            clear_live()
        self._json({"ok": True})

    def _click_live(self):
        try:
            body = self._read_json()
        except Exception:
            self._json({"error": "Invalid click request."}, status=400)
            return

        handle, token = get_live()
        if handle is None or not process_is_running(handle):
            self._json({"error": "The live feed is not running."}, status=409)
            return
        if body.get("token") != token:
            self._json({"error": "Invalid live-session token."}, status=403)
            return

        try:
            x = float(body.get("x"))
            y = float(body.get("y"))
        except (TypeError, ValueError):
            self._json({"error": "x and y must be numbers."}, status=400)
            return

        try:
            click_live(handle, x, y)
            self._json({"ok": True})
        except Exception:
            self._json({"error": "Could not forward the click to the pipeline."}, status=500)

    def _chat(self):
        """Stream Gemini output with a 60s first-chunk and 5s inter-chunk timeout."""
        import queue
        import time

        FIRST_RESPONSE_TIMEOUT = 60.0
        BETWEEN_CHUNKS_TIMEOUT = 5.0
        STREAM_END = object()

        try:
            payload = self._read_json()
            user_query = str(payload.get("userQuery", ""))
            session_id = str(payload.get("sessionID", "millikan_test_session"))
        except Exception:
            self._json({"error": "Invalid chat request."}, status=400)
            return

        count = len(user_query)
        if count < 1 or count > 2000:
            self._json({"error": "Query must contain between 1 and 2000 characters."}, status=400)
            return

        events: queue.Queue[object] = queue.Queue()
        cancel = threading.Event()
        worker_done = threading.Event()
        first_chunk = threading.Event()
        last_generated = time.monotonic()
        state_lock = threading.Lock()

        # Retire any generation still running for this same session before
        # starting this one, so this request's watchdog always measures a
        # clean, independent timeout window rather than one contending with
        # leftover work from an earlier (possibly already-timed-out) request.
        with _chat_lock:
            previous = _chat_sessions.get(session_id)
            _chat_sessions[session_id] = {"cancel": cancel, "thread": None}
        if previous is not None:
            previous["cancel"].set()
            stale_thread = previous.get("thread")
            if stale_thread is not None and stale_thread.is_alive():
                # Best-effort: give a cooperative previous worker a brief
                # window to notice cancel and unwind before we proceed. We
                # can't forcibly kill a blocked thread, but this avoids the
                # common case of two requests racing to use the same
                # underlying Gemini session at once.
                stale_thread.join(timeout=1.0)

        def push(payload):
            events.put(payload)

        def produce():
            nonlocal last_generated
            try:
                stream = getResponseFromGemini(
                    userQuery=user_query,
                    sessionID=session_id,
                )
                for chunk in stream:
                    if cancel.is_set():
                        break

                    chunk_type = getattr(chunk, "type", None)
                    chunk_data = getattr(chunk, "data", None)

                    with state_lock:
                        last_generated = time.monotonic()

                    push({"type": chunk_type, "data": chunk_data})

                    if chunk_type == "text":
                        first_chunk.set()
                    if chunk_type in ("error", "done"):
                        break
            except Exception:
                if not cancel.is_set():
                    traceback.print_exc()  # server-side only; never shown to the browser
                    push({
                        "type": "error",
                        "data": "Unable to reach the AI assistant. It may not be configured correctly (e.g. a missing or invalid API key) -- check the server console for details.",
                    })
            finally:
                worker_done.set()
                events.put(STREAM_END)

        def watchdog():
            timed_out = False
            last_seen_delivered = None
            phase_start = time.monotonic()

            while not worker_done.is_set() and not cancel.is_set():
                now = time.monotonic()

                # Before the first text chunk, measure from request start.
                if not first_chunk.is_set():
                    if now - phase_start >= FIRST_RESPONSE_TIMEOUT:
                        timed_out = True
                        cancel.set()
                        push({
                            "type": "timeout",
                            "data": "The AI assistant did not respond within 60 seconds.",
                        })
                        return
                else:
                    # After first text, measure actual generator activity.
                    # This resets on every generated chunk.
                    with state_lock:
                        idle = now - last_generated
                    if idle >= BETWEEN_CHUNKS_TIMEOUT:
                        timed_out = True
                        cancel.set()
                        push({
                            "type": "timeout",
                            "data": "The AI assistant stopped responding for more than 5 seconds.",
                        })
                        return

                time.sleep(0.05)

        worker = threading.Thread(target=produce, daemon=True, name="gemini-stream")
        timer = threading.Thread(target=watchdog, daemon=True, name="gemini-timeout-watchdog")
        with _chat_lock:
            if _chat_sessions.get(session_id, {}).get("cancel") is cancel:
                _chat_sessions[session_id]["thread"] = worker
        worker.start()
        timer.start()

        def deregister():
            with _chat_lock:
                if _chat_sessions.get(session_id, {}).get("cancel") is cancel:
                    del _chat_sessions[session_id]

        # Give an outright, fast failure -- most commonly the Gemini client
        # couldn't even be constructed, e.g. a missing or invalid API key --
        # a chance to surface as a plain, immediate 500 the UI can show,
        # instead of always committing to an SSE stream that a construction
        # failure would otherwise leave hanging with no visible response.
        # A real, working request routinely takes far longer than this to
        # produce its first chunk, so the grace window is invisible to it;
        # a broken configuration typically fails within milliseconds, well
        # inside it.
        EARLY_FAILURE_GRACE = 2.5
        try:
            pending_item = events.get(timeout=EARLY_FAILURE_GRACE)
        except queue.Empty:
            pending_item = None

        if isinstance(pending_item, dict) and pending_item.get("type") == "error":
            cancel.set()
            deregister()
            self._json({
                "error": pending_item.get("data")
                or "The AI assistant is not configured. Please check the server's API key configuration.",
            }, status=500)
            return

        # Explicit SSE headers prevent browser/proxy buffering.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def send_event(payload):
            raw = json.dumps(payload, ensure_ascii=False)
            data = f"data: {raw}\n\n".encode("utf-8")
            self.wfile.write(data)
            self.wfile.flush()

        def next_event(timeout):
            nonlocal pending_item
            if pending_item is not None:
                item, pending_item = pending_item, None
                return item
            return events.get(timeout=timeout)

        try:
            while True:
                try:
                    item = next_event(0.5)
                except queue.Empty:
                    # SSE heartbeat keeps the browser connection alive and
                    # prevents intermediaries from deciding the stream is idle.
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    if worker_done.is_set() and events.empty():
                        break
                    continue

                if item is STREAM_END:
                    # Drain anything already generated before closing.
                    while True:
                        try:
                            queued = events.get_nowait()
                        except queue.Empty:
                            break
                        if queued is STREAM_END:
                            continue
                        send_event(queued)
                    break

                send_event(item)
                if item.get("type") in ("error", "done", "timeout"):
                    break

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            cancel.set()
        finally:
            cancel.set()
            deregister()

    def _serve_static(self, url_path):
        if url_path == "/":
            url_path = "/index.html"

        relative = url_path.lstrip("/")
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            self._json({"error": "Forbidden"}, status=403)
            return

        if not candidate.is_file():
            self._json({"error": "Not found"}, status=404)
            return

        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self._headers(content_type, content_length=len(body))
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Millikan web frontend: http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the web server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        handle, _ = get_live()
        if handle is not None:
            try:
                stop_live(handle)
            except Exception:
                traceback.print_exc()
            clear_live()


if __name__ == "__main__":
    main()
