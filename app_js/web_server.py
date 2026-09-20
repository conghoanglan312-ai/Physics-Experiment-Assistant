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
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from llm.main import getResponseFromGemini
from app_js.wrapper import start_live, stop_live, send_click
from app_js.quick_ai import ask as quick_ask, available as quick_available
from app_js.tutor import Tutor, whyPrompt
from cv.api import live_status

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8502

_live_lock = threading.Lock()
_live_handle = None
_live_token = None

# ---------------------------------------------------------------------------
#  The live tutor.
#
#  One per live session, fed from the same status the panel polls. It decides
#  for itself when the student is stuck -- see app_js/tutor.py -- and only then
#  asks the model anything. Its messages ride back on the status reply, which
#  the page is already polling, so nothing new has to be kept open.
#
#  It speaks into the SAME chat session as the student, so the two are one
#  conversation and "why?" can be typed straight back at it.
# ---------------------------------------------------------------------------
TUTOR_SESSION = "millikan_test_session"
_tutor = None


def _ask_model(prompt):
    """One turn from the LLM, or None if it will not answer right now.

    The quick path first: it is a single call with no retrieval and no session
    lock, which is what a student mid-measurement needs and also what stops the
    tutor being refused because the student is typing. The chat path stays as
    the fallback, so a missing quick-path key changes nothing.
    """
    if quick_available():
        quick = quick_ask(prompt)
        if quick:
            return quick
    out = []
    for event in getResponseFromGemini(userQuery = prompt, sessionID = TUTOR_SESSION):
        kind, data = getattr(event, "type", None), getattr(event, "data", None)
        if kind == "text":
            out.append(data or "")
        elif kind == "error":
            # Busy with the student's own question, or misconfigured. Either way
            # the tutor stays quiet rather than queueing a stale interruption.
            print("live tutor: model declined -- %s" % str(data)[:120], flush = True)
            return None
        elif kind == "done":
            break
    return "".join(out) or None

# ---------------------------------------------------------------------------
#  Chat session accounting.
#
#  Two separate things used to be one dict, which is what let a timed-out
#  request leave the session looking busy:
#
#  _chat_active  is the count the server answers "is this session generating?"
#                with. It is claimed once per request and released in ONE
#                finally that every exit path runs through -- normal end,
#                timeout, a client disconnect, a construction failure such as a
#                missing API key, or any other exception. Zero is represented by
#                the sessionID being absent, so a session can never be left
#                holding a stale count.
#
#  _chat_workers is bookkeeping for a generation THREAD that outlived its
#                request. A request that times out releases its slot at once --
#                that is the required behaviour -- but its worker may still be
#                blocked inside getResponseFromGemini(). Keeping a handle on it
#                lets the next request cancel it instead of racing it for the
#                same underlying Gemini session; the worker drops its own entry
#                as it unwinds, so a finished thread leaves nothing behind.
#
#  The frontend always sends the same fixed sessionID, so back-to-back messages
#  share one Gemini session and this matters on every message, not just on
#  concurrent ones.
# ---------------------------------------------------------------------------
_chat_lock = threading.Lock()
_chat_active: dict[str, int] = {}
_chat_workers: dict[str, dict] = {}


def active_generations(session_id: str) -> int:
    """How many generations the server currently considers in flight for a session.

    0 before a request starts, 1 while one is really running, and 0 again the
    moment it ends, however it ends.
    """
    with _chat_lock:
        return _chat_active.get(session_id, 0)


def begin_generation(session_id: str) -> dict:
    """Claim the session's active-generation slot. Pair with end_generation()."""
    with _chat_lock:
        _chat_active[session_id] = _chat_active.get(session_id, 0) + 1
    return {"session": session_id, "released": False}


def end_generation(slot: dict) -> None:
    """Release a slot claimed by begin_generation(). Idempotent and never raises."""
    if not slot:
        return
    with _chat_lock:
        if slot.get("released"):
            return
        slot["released"] = True
        session_id = slot["session"]
        remaining = _chat_active.get(session_id, 1) - 1
        if remaining > 0:
            _chat_active[session_id] = remaining
        else:
            _chat_active.pop(session_id, None)   # absent == zero active generations


def register_worker(session_id: str, record: dict) -> None:
    with _chat_lock:
        _chat_workers[session_id] = record


def release_worker(session_id: str, record: dict) -> None:
    """Called by the worker thread itself as it unwinds."""
    with _chat_lock:
        if _chat_workers.get(session_id) is record:
            del _chat_workers[session_id]


def retire_previous_worker(session_id: str, timeout: float = 1.0) -> None:
    """Cancel any generation thread left over from an earlier request.

    Signal it, give a cooperative worker a brief moment to notice and unwind,
    then forget it either way. A thread blocked in a library call cannot be
    killed, and waiting on the same dead worker again on every later request
    would let one broken request slow every request that follows it.
    """
    with _chat_lock:
        previous = _chat_workers.pop(session_id, None)
    if previous is None:
        return
    previous["cancel"].set()
    thread = previous.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


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


# ---------------------------------------------------------------------------
#  The selected droplet.
#
#  cv/pipeline.py already knows which droplet the operator clicked: it keeps it
#  as `picked` and republishes it about once a second in the live status file,
#  and cv/api.live_status() is the door onto that file. So there is no second
#  selection mechanism here -- this reads the one that already exists and
#  decides only what a browser panel may say about it right now.
#
#  Nothing below measures anything. The numbers are handed over exactly as the
#  CV API produced them; the only arithmetic is comparing them against the two
#  bands the lab manual gives for choosing a droplet, which cv/pipeline.py
#  states as MIN_SPEED/MAX_SPEED and ADVISORY_MIN_V/ADVISORY_MAX_V. Those are
#  not re-exported through cv/api.py, so they are repeated here as display
#  thresholds. q, n and e stay behind the seal in cv/api.py and are not
#  involved. The plate voltage crossing to the app is the exception that file
#  documents, and it goes no further than this panel -- never into a prompt.
# ---------------------------------------------------------------------------
FALL_SPEED_MIN, FALL_SPEED_MAX = 0.2, 0.5        # grid/s, with the field off
BALANCE_VOLTS_MIN, BALANCE_VOLTS_MAX = 150.0, 250.0
# A reading has to hold on the same side of the two bands above for this long,
# continuously, before the verdict actually changes. A single noisy sample --
# a hand still settling the knob, a momentary tracker glitch -- must not flip
# the light; only a real, sustained change in the droplet's numbers should.
SETTLE_SECONDS = 3.0

_selection_lock = threading.Lock()
_selection = {
    "id": None,        # the picked track the panel is currently describing
    "label": None,     # "Droplet 3": cv/api.py's convention is a label, never an id
    "picks": 0,        # how many distinct droplets have been picked this session
    "speed": None,     # the speed last seen while the plates were at 0 V...
    "volts": None,     # ...and the voltage it was last held balanced at
    "rangeState": None,   # tri-state: True (both readings in range), False
                          # (something out), None (not enough data to judge yet)
    "rangeSince": None,   # monotonic timestamp when rangeState last changed
    "suitable": "unknown",  # the debounced verdict actually shown to the student
}


def start_tutor():
    """Attach a tutor to the session that is starting, unless it is switched off.

    MILLIKAN_TUTOR=0 in the environment silences it for the whole run, without
    touching the pipeline or the panel. If it says something wrong at the bench,
    the operator should be able to shut it up in one restart rather than lose the
    session arguing with it.
    """
    global _tutor
    if os.environ.get("MILLIKAN_TUTOR", "1").strip().lower() in ("0", "off", "false", "no"):
        print("live tutor: off (MILLIKAN_TUTOR)", flush = True)
        _tutor = None
        return
    _tutor = Tutor(_ask_model)


def stop_tutor():
    global _tutor
    _tutor = None


def reset_selection():
    """Forget the current droplet. Called when a session starts or stops."""
    with _selection_lock:
        _selection.update(id=None, label=None, picks=0,
                           speed=None, volts=None,
                           rangeState=None, rangeSince=None, suitable="unknown")


def _number(value):
    """A float the browser can print, or None. Never a guess."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank_selection():
    return {
        "picked": False,
        "label": None,
        "voltageV": None,
        "fallSeconds": None,
        "speedGridPerSec": None,
        "balancePercent": None,
        "fieldState": None,
        "suitable": "unknown",
        "note": "",
    }


def selection_view(status, running):
    """What the Selected Droplet panel should show, from the live status file.

    Every field is either a value cv/api.py provided or None -- a missing value
    stays missing all the way to the browser, which prints a dash for it.

    `suitable` is "yes" / "no" / "unknown", and it only changes once the two
    readings below have sat on the same side of their bands for SETTLE_SECONDS
    straight -- see the constant above. Until there has been at least one
    balance-voltage reading AND one free-fall-speed reading for this droplet,
    the verdict is "unknown"; it is never inferred from just one of the two.
    """
    if not running:
        reset_selection()
        return _blank_selection()
    if not isinstance(status, dict):
        # A session that IS running whose status could not be read this once --
        # the file is rewritten by atomic replace several times a second, and a
        # reader occasionally arrives mid-swap. Resetting on that renumbered the
        # operator's droplets from 5 back to 1 in the middle of a session
        # (2026-09-20, 6:18 into the run). The droplet has not changed just
        # because one poll came back empty.
        with _selection_lock:
            blank = _blank_selection()
            blank["picked"] = _selection["id"] is not None
            blank["label"] = _selection["label"]
            blank["suitable"] = _selection["suitable"]
            blank["note"] = "Waiting for the next reading."
            return blank

    voltage = _number(status.get("voltageV"))
    field = status.get("fieldState")
    picked = status.get("picked")
    now = time.monotonic()

    if isinstance(picked, dict):
        droplet_id = picked.get("id")
        speed = _number(picked.get("speedGridPerSec"))
        fall = _number(picked.get("fallSeconds"))
        balance = _number(picked.get("balancePercent"))
    else:
        # picked can go missing for a poll or several without the operator
        # having picked anything new -- cv/api.py's own docstring notes the
        # tracker routinely loses a motionless droplet during a hold, which
        # is exactly when the balance voltage is being read. Treating that
        # as "nothing picked" would throw away the readings right when they
        # matter most, so it must NOT be confused with a fresh pick below.
        droplet_id, speed, fall, balance = None, None, None, None

    with _selection_lock:
        if droplet_id is not None and droplet_id != _selection["id"]:
            _selection.update(
                id=droplet_id,
                picks=_selection["picks"] + 1,
                speed=None, volts=None,
                rangeState=None, rangeSince=None, suitable="unknown",
            )
            _selection["label"] = "DROPLET %d" % _selection["picks"]

        if _selection["id"] is None:
            # Nothing has actually been picked yet (as opposed to picked-but-
            # momentarily-untracked) -- there is nothing to evaluate.
            view = _blank_selection()
            # The plate voltage is the apparatus's own panel reading, not a
            # property of any droplet, so it is worth showing regardless.
            view["voltageV"] = voltage
            view["fieldState"] = field
            view["note"] = "Click a droplet in the feed to select it."
            return view

        if field == "off" and speed is not None:
            # Field off, so this speed IS the free-fall speed Ⅳ.2 judges.
            _selection["speed"] = speed
        if (field == "work" and voltage is not None):
            # Held against a live field: this voltage IS its balance voltage.
            _selection["volts"] = voltage

        label = _selection["label"]
        free_fall = _selection["speed"]
        balance_volts = _selection["volts"]

        speed_ok = None if free_fall is None else FALL_SPEED_MIN <= free_fall <= FALL_SPEED_MAX
        volts_ok = None if balance_volts is None else BALANCE_VOLTS_MIN <= balance_volts <= BALANCE_VOLTS_MAX

        # The joint condition the manual actually cares about: both readings
        # in their band at once. None means there isn't a reading for both
        # yet, so there is nothing to debounce -- it is simply unknown.
        if speed_ok is None or volts_ok is None:
            in_range = None
        else:
            in_range = speed_ok and volts_ok

        if in_range != _selection["rangeState"]:
            _selection["rangeState"] = in_range
            _selection["rangeSince"] = now
        # rangeSince is still None the very first time in_range comes back
        # None too (nothing observed yet at all) -- there is nothing to time.
        elapsed = 0.0 if _selection["rangeSince"] is None else now - _selection["rangeSince"]

        if in_range is None:
            _selection["suitable"] = "unknown"
        elif elapsed >= SETTLE_SECONDS:
            _selection["suitable"] = "yes" if in_range else "no"
        # else: still settling -- keep whatever verdict was already showing,
        # so a brief blip can't flip the light early in either direction.

        suitable = _selection["suitable"]

    if speed_ok is None and volts_ok is None:
        note = "Balance it, then cut to 0 V to time its fall."
    elif speed_ok is None:
        note = "Balanced at %d V. Cut to 0 V to measure its falling speed." % round(balance_volts)
    elif volts_ok is None:
        note = "Falls at %.2f grid/s. Balance it to read its balance voltage." % free_fall
    elif in_range:
        if suitable == "yes":
            note = "Falls at %.2f grid/s and balances at %d V." % (
                free_fall, round(balance_volts))
        else:
            remaining = max(0.0, SETTLE_SECONDS - elapsed)
            note = "Falls at %.2f grid/s and balances at %d V -- confirming suitable in %.0fs." % (
                free_fall, round(balance_volts), remaining)
    else:
        problems = []
        if not speed_ok:
            problems.append("falls at %.2f grid/s (needs %g-%g)" % (
                free_fall, FALL_SPEED_MIN, FALL_SPEED_MAX))
        if not volts_ok:
            problems.append("balances at %d V (needs %g-%g)" % (
                round(balance_volts), BALANCE_VOLTS_MIN, BALANCE_VOLTS_MAX))
        reason = " and ".join(problems)
        if suitable == "yes":
            remaining = max(0.0, SETTLE_SECONDS - elapsed)
            note = "Drifted out of range (%s) -- reverts to unsuitable in %.0fs if it stays there." % (
                reason, remaining)
        else:
            note = "Currently %s." % reason

    return {
        "picked": True,
        "label": label,
        "voltageV": voltage,
        "fallSeconds": fall,
        "speedGridPerSec": speed,
        "balancePercent": balance,
        "fieldState": field,
        "suitable": suitable,
        "note": note,
    }


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
            self._live_status()
            return

        if path == "/api/chat/status":
            # What the server believes about a chat session right now. The browser
            # does not need it; it makes "a timed-out request leaves 0 active
            # generations behind" something that can be checked rather than
            # assumed. See the accounting block at the top of this file.
            query = parse_qs(urlparse(self.path).query)
            session_id = (query.get("sessionID") or ["millikan_test_session"])[0]
            self._json({
                "sessionID": session_id,
                "activeGenerations": active_generations(session_id),
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

        if path == "/api/ai/why":
            self._why()
            return
        if path == "/api/live/stop":
            self._stop_live()
            return
        if path == "/api/chat":
            self._chat()
            return

        self._json({"error": "Not found"}, status=404)

    def _live_status(self):
        """Live session state plus the droplet the operator currently has picked.

        Always answers. The earlier version only wrote a response when a stale
        status file was found for a session that had already stopped, so the
        browser's poll got an empty reply for every running session and the page
        could never learn anything about one.
        """
        handle, token = get_live()
        running = process_is_running(handle)
        if handle is not None and not running:
            clear_live()

        status = None
        if running:
            # No process is writing the status file when nothing is running,
            # so there is nothing to read -- skip the stat/open/json.load on
            # every idle poll and just answer with running: False.
            try:
                status = live_status()
            except Exception:
                status = None

        tutor, said = _tutor, []
        if running and tutor is not None:
            # The panel is the point of this endpoint; the tutor is a passenger.
            # A bug in its rules must not take the droplet readings down with it.
            try:
                tutor.poll(status)
                said = tutor.take()
            except Exception:
                traceback.print_exc()
                said = []

        self._json({
            "running": running,
            "device": handle.get("device") if running else None,
            "source": handle.get("source") if running else None,
            "status": status,
            "selected": selection_view(status, running),
            "tutor": said,
        })

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
            reset_selection()          # a new session picks its own droplets
            start_tutor()
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

    def _why(self):
        """Explain the line the student is looking at, on request.

        Nothing is volunteered here and nothing polls: it answers a button. What
        the aid is SAYING crosses as a kind rather than its words, because the
        banner names the plate voltage -- see bannerKind() in tutor.py.
        """
        status = None
        if process_is_running(get_live()[0]):
            try:
                status = live_status()
            except Exception:
                status = None
        if not status:
            self._json({"error": "Nothing is running to explain."}, status=409)
            return
        text = _ask_model(whyPrompt(status))
        if not text:
            self._json({"error": "The assistant is busy; try again in a moment."},
                       status=503)
            return
        self._json({"text": text.strip()})

    def _click(self):
        """{"x": 0.42, "y": 0.63, "token": "..."} -- fractions of the frame.

        Down the pipeline's stdin, via wrapper.send_click(). That is the path
        cv/pipeline.py documents for exactly this (readRemoteClicks: "the browser
        app mirrors this window as pixels... picking a droplet is the whole
        interaction and the click had nowhere to go"), it needs no window to have
        focus, and it is the same event its own mouse callback produces.
        wrapper.click_live() posts Win32 messages instead and is left in place for
        anyone who wants it, but this route is the one the browser uses -- there
        used to be two routes on this path and the second could never be reached.
        """
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
            reset_selection()
            stop_tutor()
        self._json({"ok": True})

    def _chat(self):
        """Claim the session's generation slot, stream the answer, always release.

        The slot is claimed here and released in the finally below, and the whole
        of the streaming lives in between. That is the entire fix for the stale
        session state: there is one place the count goes up, one place it comes
        back down, and no way to leave by any other door -- a normal finish, a
        60s or 5s timeout, a Gemini client that could not be constructed because
        the API key is missing or invalid, a browser that hung up mid-stream, or
        an exception nobody predicted all land on the same finally. The next
        request therefore always starts from zero.
        """
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

        # Rejected requests above never started a generation, so they never
        # claimed a slot: the count stays 0 rather than going 1 and back.
        cancel = threading.Event()
        slot = begin_generation(session_id)
        try:
            self._chat_stream(session_id, user_query, cancel)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                       # the browser closed the tab; nothing to send
        except Exception:
            traceback.print_exc()      # server-side only; never shown to the browser
        finally:
            cancel.set()               # stop this request's worker and watchdog
            end_generation(slot)

    def _chat_stream(self, session_id, user_query, cancel):
        """Stream Gemini output with a 60s first-chunk and 5s inter-chunk timeout."""
        import queue
        import time

        FIRST_RESPONSE_TIMEOUT = 60.0
        BETWEEN_CHUNKS_TIMEOUT = 5.0
        STREAM_END = object()

        events: queue.Queue[object] = queue.Queue()
        worker_done = threading.Event()
        first_chunk = threading.Event()
        last_generated = time.monotonic()
        state_lock = threading.Lock()

        # Cancel a worker left behind by an earlier request for this session, so
        # this request's watchdog measures a clean, independent timeout window
        # instead of contending with leftover work for the same Gemini session.
        # The slot that request held was released when it ended; this is only its
        # thread, which may still be blocked inside the library.
        retire_previous_worker(session_id)
        worker_record = {"cancel": cancel, "thread": None}

        def push(payload):
            events.put(payload)

        def produce():
            nonlocal last_generated
            stream = None
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
                # Give the LLM layer its own chance to unwind before this worker
                # disappears. Breaking out of a generator only suspends it, so
                # anything it does in its own finally -- releasing a session,
                # clearing an "active input" -- would otherwise wait on garbage
                # collection rather than happening here, on the cancel and
                # timeout paths where it matters most.
                if stream is not None:
                    try:
                        close = getattr(stream, "close", None)
                        if callable(close):
                            close()
                    except Exception:
                        pass       # the LLM layer's cleanup is not ours to police
                worker_done.set()
                events.put(STREAM_END)
                # A worker that has finished is not something a later request
                # needs to wait on, whether or not its request is still here.
                release_worker(session_id, worker_record)

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
        worker_record["thread"] = worker
        register_worker(session_id, worker_record)
        worker.start()
        timer.start()

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
            # A configuration failure -- a missing or invalid API key is the usual
            # one. Returning here still runs _chat()'s finally, so the session is
            # back to zero active generations before the browser sees the 500.
            cancel.set()
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
        # Not keep-alive. The body has no Content-Length and is not chunked, so the
        # close IS how a reader learns the stream ended -- and BaseHTTPRequestHandler
        # reads this header back: "keep-alive" left the socket, and the thread
        # serving it, parked waiting for a request that never came, one per message.
        self.send_header("Connection", "close")
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

        finally:
            # Stop the worker and the watchdog as soon as this stream is over.
            # The slot itself is released by _chat(), which cannot be skipped.
            cancel.set()

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
