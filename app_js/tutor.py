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
import os
import re
import threading
import time

from cv.api import llm_context, liveStatusPath

# Being stuck is a stretch of time with no progress, so every threshold here is in
# seconds and every one of them is deliberately long. A tutor that interrupts a
# student who is doing fine is worse than one that says nothing at all.
STALL_TUNING_S = 90.0           # balancing this long with no balance reached
STALL_NO_FALL_S = 180.0         # a droplet held this long with no legal fall
STALL_IDLE_S = 120.0            # ...or this long of a running session with no legal
                                # fall at all, picked droplet or not.  An operator sat
                                # for four minutes getting nowhere on 2026-09-20 and
                                # the tutor said nothing, because every other rule
                                # needs a droplet to be selected -- and having nothing
                                # selected for four minutes IS the problem.
LOST_REPEATS = 3                # times the pick is lost...
LOST_WINDOW_S = 60.0            # ...inside this window
FLIP_REPEATS = 3                # USABLE/DISCARD reversals...
FLIP_WINDOW_S = 60.0            # ...inside this window

COOLDOWN_S = 120.0              # never two messages closer than this
KIND_COOLDOWN_S = 420.0         # ...and far longer before the SAME complaint again.
                                # Replaying a real 40-minute session, all six
                                # messages the tutor was allowed were "the aid keeps
                                # losing your droplet", spent inside the first
                                # quarter of an hour. A student who has heard that
                                # once does not need it five more times; the budget
                                # is better kept for something they have not been
                                # told yet.
MAX_PER_SESSION = 6             # ...and never more than this many in one run
MAX_PER_KIND = 2                # ...of which any one complaint may be at most this.
                                # Replayed against two real sessions, the whole
                                # budget went on "the aid keeps losing your droplet"
                                # -- true, and said once it is advice; said six times
                                # it is nagging, and it crowds out everything else.
MIN_SESSION_S = 45.0            # nothing at all in the opening moments

# Defence in depth for THE SEAL. The prompt carries no voltage, but a model can
# still invent one ("try 250 V"), and a number like that on screen is a voltage
# the student did not read off the apparatus. A reply that states one is dropped
# rather than shown: silence is always safe, a fabricated instruction is not.
FORBIDDEN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:V\b|volts?\b)"          # "250 V", "190 volts"
    r"|\d\s*(?:e|x\s*10|\u00d7\s*10)\s*-?\s*19",   # "1.6e-19", "1.6 x 10-19"
    re.IGNORECASE)
# The same defence, for physics instead of numbers. Told the aid kept losing a
# balanced droplet, the model answered: "The software struggles to track droplets
# when they are perfectly stationary. Try slightly adjusting the field to keep the
# droplet in slow motion rather than holding it completely still, which will help
# the tracker maintain a consistent lock."
#
# That is the experiment backwards. A motionless droplet IS the measurement -- IV.4
# asks for exactly that -- and unbalancing one to suit the software would cost the
# student the reading they are there to take. A model cannot be argued out of this
# reliably, so a reply that says it is not shown.
BAD_ADVICE = re.compile(
    r"slow motion"
    r"|keep (?:it|the drop(?:let)?) (?:slightly |gently )?(?:moving|drifting)"
    r"|rather than holding it (?:completely |perfectly )?still"
    r"|(?:maintain|keep) (?:a )?(?:consistent )?lock"
    r"|help the (?:tracker|software|aid)"
    r"|(?:un|off[- ])balanc\w* (?:it|the drop(?:let)?)",
    re.IGNORECASE)


def withhold(text):
    """Why this reply must not be shown, or None if it may be."""
    if not text:
        return None
    if FORBIDDEN.search(text):
        return "it stated a voltage or a charge"
    if BAD_ADVICE.search(text):
        return "it advised against balancing the droplet"
    return None


TUTOR_LOG = "tutor_log.jsonl"   # every trigger, prompt and reply, for review after


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
        self._lastProgress = None   # when a legal fall last landed
        self._toldOffline = False   # ...and whether the student has been told the
                                    # model is unreachable, which is said once
        self._kindAt = {}           # stall kind -> when it was last said
        self._kindSaid = {}         # ...and how often, this session

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
            self._lastProgress = now
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
        kind = stall[0] if stall else None
        if stall and self._maySpeak(now, picked, kind):
            self._kindAt[kind] = now
            self._kindSaid[kind] = self._kindSaid.get(kind, 0) + 1
            self._lastSaid, self._said = now, self._said + 1
            self._tuningSince = None                # the clock restarts either way
            self._pickedSince = now
            self._lostAt, self._flipAt = [], []
            threading.Thread(target = self._speak, args = (stall[1], status),
                             daemon = True).start()

    def _stall(self, now, status, picked):
        if self._tuningSince and now - self._tuningSince >= STALL_TUNING_S:
            return ("tuning", "the student has been tuning the same droplet for "
                    "%.0f seconds without reaching balance"
                    % (now - self._tuningSince))
        if len(self._lostAt) >= LOST_REPEATS:
            # Say whose fault it is. Told only that the droplet "was lost", the
            # model explained it as physics -- "your droplet is falling too
            # slowly to be tracked, it is probably too small" -- about a droplet
            # the operator was holding balanced and could see perfectly well.
            # The tracker losing sight of something is a limitation of ours, and
            # advice that starts by blaming the student is worse than silence.
            return ("lost", "the aid's TRACKER keeps losing sight of the droplet "
                    "(%d times in the last minute) -- this is a limitation of the "
                    "software, not a property of their droplet, and it happens most "
                    "to a droplet held still by the field. Tell them how to carry on "
                    "measuring despite it" % len(self._lostAt))
        if len(self._flipAt) >= FLIP_REPEATS:
            return ("flapping", "the droplet's verdict has flipped between USABLE "
                    "and DISCARD %d times in the last minute" % len(self._flipAt))
        if (self._started and now - max(self._started, self._lastProgress or 0)
                >= STALL_IDLE_S):
            return ("idle", "the session has been running for %.0f seconds without a "
                    "single legal fall being completed, and right now %s"
                    % (now - self._started,
                       "no droplet is selected" if picked.get("id") is None
                       else "a droplet is selected but not producing falls"))
        if (picked.get("id") is not None and self._pickedSince
                and now - self._pickedSince >= STALL_NO_FALL_S):
            return ("nofall", "the student has held the same droplet for %.0f "
                    "seconds without completing a legal fall"
                    % (now - self._pickedSince))
        return None

    def _maySpeak(self, now, picked, kind = None):
        if self._busy or self._said >= MAX_PER_SESSION:
            return False
        if kind and now - self._kindAt.get(kind, -1e9) < KIND_COOLDOWN_S:
            return False
        if kind and self._kindSaid.get(kind, 0) >= MAX_PER_KIND:
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
        prompt = self.prompt(stall, status)
        text, dropped = None, None
        try:
            text = self._ask(prompt)
        except Exception as exc:                    # a tutor that fails is silent
            print("live tutor: %s" % exc, flush = True)
        finally:
            self._busy = False
        if text is None and not self._toldOffline:
            # The rules found a stall and the model could not be reached -- on
            # 2026-09-20 because the lab's proxy had stopped routing the API. The
            # tutor's silence then looks exactly like "nothing is wrong", and the
            # operator waited six minutes for a message that had already been
            # composed and thrown away. Say so once, and never again this session.
            self._toldOffline = True
            with self._lock:
                self._pending.append({
                    "text": "The tutor cannot reach the AI service, so it will stay "
                            "quiet this session. The measurement aid is unaffected.",
                    "at": time.time(), "offline": True})
        why = withhold(text)
        if why:
            # See FORBIDDEN and BAD_ADVICE. The student reads a voltage off the
            # apparatus, never off a model that guessed one -- and they balance the
            # droplet because the experiment says to, not because the tracker copes.
            dropped, text = text, None
            print("live tutor: reply withheld, %s" % why, flush = True)
        self._log(stall, prompt, text, dropped)
        if text:
            with self._lock:
                self._pending.append({"text": text.strip(), "at": time.time()})

    def _log(self, stall, prompt, text, dropped):
        """Every trigger, prompt and reply, beside the session's other artefacts.

        A tutor that says something odd at the bench is worth reviewing afterwards,
        and "what was it looking at when it said that" is unanswerable without the
        prompt it was given. Never raises: a logging failure must not cost the run.
        """
        try:
            path = os.path.join(os.path.dirname(liveStatusPath()), TUTOR_LOG)
            with open(path, "a", encoding = "utf-8") as f:
                f.write(json.dumps({"at": time.time(), "stall": stall,
                                    "prompt": prompt, "said": text,
                                    "withheld": dropped}, ensure_ascii = False)
                        + chr(10))
        except Exception:
            pass

    def prompt(self, stall, status):
        safe = llm_context(status)                  # THE SEAL -- see the module docstring
        return (
            "You are tutoring a student through the Millikan oil-drop experiment, "
            "watching their live session. They appear to be stuck: %s.\n\n"
            "Session state (times in seconds, speeds in grid divisions per second; "
            "plate voltages are deliberately withheld):\n%s\n\n"
            "Their goal is a droplet balanced motionless between the plates: that "
            "stillness IS the measurement, and the aid exists to serve it. Never "
            "suggest keeping the droplet moving, leaving it off balance, or "
            "changing the field to help the software follow it.\n\n"
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


# ---------------------------------------------------------------------------
#  What the student ASKS for, rather than the tutor volunteering.
#
#  It cannot flood anything: it answers a button. It is built from the same
#  sealed material as everything else here -- times, speeds, counts, states,
#  never a voltage.
# ---------------------------------------------------------------------------

# What the banner is saying, as a KIND rather than its words. The banner names
# the plate voltage in plain text ("BALANCED to 0.0% at 153 V"), so it cannot be
# forwarded; the situation it describes can.
BANNER_KINDS = (
    ("too slow for", "the droplet falls too slowly to measure"),
    ("too fast for", "the droplet falls too fast to measure"),
    ("is RISING", "the droplet is rising while the panel reads 0 V"),
    ("is not moving", "the droplet is not moving with the field off"),
    ("USABLE", "the droplet falls at a usable speed"),
    ("BALANCED", "the droplet is balanced and ready to be timed"),
    ("balanced to", "the droplet is nearly balanced"),
    ("as good as it gets", "the droplet is at the Brownian limit"),
    ("barely held", "the field can hardly hold the droplet"),
    ("does NOT answer the field", "the droplet does not respond to the field"),
    ("no response seen yet", "the aid cannot tell yet whether the droplet is charged"),
    ("left the view", "the droplet has left the picture"),
    ("lost your droplet", "the aid has lost track of the droplet"),
    ("DIFFERENT droplet", "the aid may have switched to another droplet"),
    ("will not hold still", "the droplet will not hold still enough to measure"),
    ("still calculating", "the aid is still working out where balance is"),
    ("settling after", "the field has just changed and nothing is settled yet"),
    ("MEASURING", "a measurement is in progress"),
    ("free-fall pace", "the field is not holding the droplet at all"),
    ("quiet now", "there is nothing to measure yet"),
)


def bannerKind(message):
    """The situation the banner is describing, in words that name no voltage."""
    for needle, meaning in BANNER_KINDS:
        if needle.lower() in (message or "").lower():
            return meaning
    return None


def whyPrompt(status):
    """Explain the instruction the student is looking at right now."""
    kind = bannerKind((status or {}).get("message")) or "the session is under way"
    safe = llm_context(status or {})
    return (
        "A student is doing the Millikan oil-drop experiment. Their measurement "
        "aid is telling them: %s.\n\nSession state (times in seconds, speeds in "
        "grid divisions per second; plate voltages are deliberately withheld):\n"
        "%s\n\nTheir goal is a droplet balanced motionless between the plates: "
        "that stillness IS the measurement, and the aid exists to serve it. Never "
        "suggest keeping the droplet moving or off balance to help the software "
        "follow it.\n\n"
        "They asked WHY. Reply in ENGLISH, at most three sentences: explain "
        "the physics behind what the aid is telling them, and what it means for "
        "their measurement. Do not compute or state any charge, electron count or "
        "voltage."
        % (kind, json.dumps(safe, ensure_ascii = False))
    )
