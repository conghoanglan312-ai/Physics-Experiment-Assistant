"""The live tutor: when the aid's guidance is not landing, ask the LLM to help.

The banner already says what to do, every frame, for free, and it is never wrong
about what it measured. Restating that through a language model would be slower,
cost tokens and add nothing -- so this does not do it. It speaks only when the
student is STUCK, which is a thing the banner cannot notice: the banner sees one
frame, and being stuck is a shape in time.

What counts as stuck is decided here, in ordinary code, from the same live status
the panel reads. The model is asked only to explain the situation once our rules
have found one. That way the triggering is deterministic and reviewable, and the
model is used for the part it is actually good at.

THE SEAL. Everything handed to the model goes through cv.api.llm_context first --
see THE SEAL in cv/api.py. The plate voltage never crosses; `message`, which is
the banner and names the voltage in plain text, never crosses either. What the
tutor gets is times, speeds, counts, states, and voltageSafeguard's in-range
boolean. It cannot assemble q from those.
"""

import json
import threading
import time

from cv.api import llm_context

# Being stuck is a stretch of time with no progress, so every threshold here is in
# seconds and every one of them is deliberately long. A tutor that interrupts a
# student who is doing fine is worse than one that says nothing at all.
STALL_TUNING_S = 90.0           # balancing this long with no balance reached
STALL_NO_FALL_S = 180.0         # a droplet held this long with no legal fall
LOST_REPEATS = 3                # times the pick is lost...
LOST_WINDOW_S = 60.0            # ...inside this window
FLIP_REPEATS = 3                # USABLE/DISCARD reversals...
FLIP_WINDOW_S = 60.0            # ...inside this window

COOLDOWN_S = 120.0              # never two messages closer than this
MAX_PER_SESSION = 6             # ...and never more than this many in one run
MIN_SESSION_S = 45.0            # nothing at all in the opening moments


class Tutor:
    """Watches one session, decides when it is stuck, and queues what to say.

    `poll(status)` is called with each live status; it is pure bookkeeping and
    returns nothing. `take()` hands the browser any message that has arrived
    since it last asked.
    """

    def __init__(self, ask):
        # `ask` is injected so the rules can be tested without an API key, and so
        # the LLM layer stays the caller's business rather than this file's.
        self._ask = ask
        self._lock = threading.Lock()
        self._pending = []
        self._said = 0
        self._lastSaid = 0.0
        self._started = None
        self._busy = False
        # what the rules watch
        self._tuningSince = None
        self._pickedSince = None
        self._lastDroplet = None
        self._lostAt = []
        self._flipAt = []
        self._lastVerdict = None
        self._legalFalls = 0

    # ---- the rules ----------------------------------------------------------
    def poll(self, status):
        if not isinstance(status, dict):
            return
        now = time.monotonic()
        if self._started is None:
            self._started = now
        picked = status.get("picked") or {}
        droplet = picked.get("id")
        balance = picked.get("balancePercent")
        message = status.get("message") or ""
        legal = status.get("legalFalls") or 0

        if droplet != self._lastDroplet:            # a different droplet starts over
            self._lastDroplet = droplet
            self._pickedSince = now if droplet is not None else None
            self._tuningSince = None
        if legal != self._legalFalls:               # progress: the clock resets
            self._legalFalls = legal
            self._pickedSince = now
            self._tuningSince = None

        # Tuning: off balance, being worked on, and not getting there.
        if droplet is not None and balance is not None and balance > 2.0:
            self._tuningSince = self._tuningSince or now
        elif balance is not None:
            self._tuningSince = None

        if "lost your droplet" in message:
            if not self._lostAt or now - self._lostAt[-1] > 3.0:
                self._lostAt.append(now)
        verdict = ("USABLE" if "USABLE" in message else
                   "DISCARD" if "DISCARD" in message else None)
        if verdict and verdict != self._lastVerdict:
            if self._lastVerdict is not None:
                self._flipAt.append(now)
            self._lastVerdict = verdict
        self._lostAt = [t for t in self._lostAt if now - t <= LOST_WINDOW_S]
        self._flipAt = [t for t in self._flipAt if now - t <= FLIP_WINDOW_S]

        stall = self._stall(now, status, picked)
        if stall and self._maySpeak(now, picked):
            self._lastSaid, self._said = now, self._said + 1
            self._tuningSince = None                # the clock restarts either way
            self._pickedSince = now
            self._lostAt, self._flipAt = [], []
            threading.Thread(target = self._speak, args = (stall, status),
                             daemon = True).start()

    def _stall(self, now, status, picked):
        if self._tuningSince and now - self._tuningSince >= STALL_TUNING_S:
            return ("the student has been tuning the same droplet for %.0f seconds "
                    "without reaching balance" % (now - self._tuningSince))
        if len(self._lostAt) >= LOST_REPEATS:
            return ("the aid has lost the student's droplet %d times in the last "
                    "minute" % len(self._lostAt))
        if len(self._flipAt) >= FLIP_REPEATS:
            return ("the droplet's verdict has flipped between USABLE and DISCARD "
                    "%d times in the last minute" % len(self._flipAt))
        if (picked.get("id") is not None and self._pickedSince
                and now - self._pickedSince >= STALL_NO_FALL_S):
            return ("the student has held the same droplet for %.0f seconds without "
                    "completing a legal fall" % (now - self._pickedSince))
        return None

    def _maySpeak(self, now, picked):
        if self._busy or self._said >= MAX_PER_SESSION:
            return False
        if now - (self._started or now) < MIN_SESSION_S:
            return False
        if now - self._lastSaid < COOLDOWN_S:
            return False
        # Never mid-measurement. A running clock is the one moment attention must
        # stay on the droplet, and the aid's own rules say so too.
        return not picked.get("fallRunning")

    # ---- saying it ----------------------------------------------------------
    def _speak(self, stall, status):
        self._busy = True
        try:
            text = self._ask(self.prompt(stall, status))
        except Exception as exc:                    # a tutor that fails is silent
            text = None
            print("live tutor: %s" % exc, flush = True)
        finally:
            self._busy = False
        if text:
            with self._lock:
                self._pending.append({"text": text.strip(), "at": time.time()})

    def prompt(self, stall, status):
        safe = llm_context(status)                  # THE SEAL -- see the module docstring
        return (
            "You are tutoring a student through the Millikan oil-drop experiment, "
            "watching their live session. They appear to be stuck: %s.\n\n"
            "Session state (times in seconds, speeds in grid divisions per second; "
            "plate voltages are deliberately withheld):\n%s\n\n"
            "Reply in ENGLISH, at most two sentences, addressed to the student. Say "
            "what is most likely going wrong and the one thing to try next. Do not "
            "compute or state any charge, electron count or voltage. Do not repeat "
            "numbers back unless one of them is the point."
            % (stall, json.dumps(safe, ensure_ascii = False))
        )

    def take(self):
        with self._lock:
            out, self._pending = self._pending, []
        return out
