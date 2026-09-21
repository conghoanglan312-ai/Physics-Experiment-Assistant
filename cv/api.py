"""CV module — the contract with the app and LLM layers.

**Import from here, never from `cv.pipeline` directly.** This is the only public
door; what sits behind it will keep moving.

    from cv.api import survey, analyze, llm_context, sample

  survey(clip)    Every complete `0` -> `16` traverse in a recording, in time
                  order. The selection aid — the manual has no multi-droplet
                  method (Ⅳ.4 is one droplet at a time), so this exists to find
                  candidates in crowded footage the way Ⅳ.2 describes a human
                  doing by eye. Expect many rows: a 30-minute clip returns ~130,
                  of which 2 are legal.

  analyze(clip, binding=None)
                  One measurement session: health, droplets, each droplet's
                  falls. `binding` groups fall ids into droplets. Nothing in a
                  recording says which ids belong together — Ⅳ.4 re-drops the
                  SAME droplet five times and the tracker loses it during each
                  motionless hold, so one droplet yields several ids. Only the
                  operator knows. Guessing would file two droplets as one and
                  the resulting charge would look perfectly normal. Without a
                  binding, `droplets` comes back empty and you want survey().

  live_status()   What a RUNNING session is doing, for the app to display beside
                  the video: the phase (still learning the background, or
                  running), the counts the HUD shows, the plate voltage, and the
                  picked droplet's clock and speed. Returns None when no session
                  has ever run, and a `phase` of "finished" once one has ended.
                  Poll it; it is rewritten about once a second.

  sample()        The shapes with fake values, for building UI without footage.

Key names, nesting and units are FINAL. Which layer gets what, and why, is in
`test/stuff.md`. Sources: the lab manual (Ⅳ.2 selection, Ⅳ.3 balancing, Ⅳ.4
static method), `test/docs/README.md` §8, and Topic 6 rubric 三.1.4 / 三.1.5.

CHANGED FROM VERSION 1 — read before reusing old code:
  * `fall_time_s`       -> `seconds`
  * `fall_speed_grid_s` -> `speedGridPerSec`
  * camelCase throughout, matching the pipeline
  * one dict per video became a session: droplets, each holding its own falls
  * `legal` / `fieldOff` / `kinematicLegal` added — a fall time on its own does
    not tell you whether the fall counts

`fieldOff` is TRI-STATE:
    True   the panel showed +0000 during the fall
    False  the field was live; the fall measured nothing
    None   never tested, because the fall already failed on kinematics
`None` is a blank column in falls.csv. Do not read blank as False.

ONLY `legal` decides whether a fall counts, and it is exactly
`kinematicLegal and fieldOff`. Everything else on a fall describes quality.
"""

# Re-exported: these ARE the implementation, imported so there is one import path.
from cv.pipeline import survey, analyze                      # noqa: F401
from cv.pipeline import TARGET_DROPLETS, TARGET_FALLS_PER_DROPLET
from cv.pipeline import liveStatusPath                       # noqa: F401
# Where a session's artefacts land. The app needs it to review the run it
# started -- falls.csv and the rest -- rather than guessing at the newest
# folder, which is a gate fixture's output as often as not.
from cv.pipeline import outDirFor                            # noqa: F401
CONTRACT_VERSION = 2


# --------------------------------------------------------------------------
#  THE SEAL — read this before you put anything in a prompt.
#
#  Charge (q), electron count (n) and the elementary charge (e) are computed
#  inside this module and NEVER cross this boundary. Neither does the balance
#  voltage in volts: q = K(t) * d / U_n, and the LLM has the lab manual in
#  retrieval, which contains that equation. Hand it `seconds` and `U_n` together
#  and it can produce the student's final answer — which rubric 三.1.4 forbids:
#
#     "Data measurement and analysis must emphasize training the participant's
#      ability, rather than AI simply generating the final result."
#
#  Enforced structurally: the values are not in the dicts. Not by asking the
#  model nicely.
#
#  LLM_KEYS is an ALLOW-list. Add a key here only after deciding it is safe.
#
#  ONE EXCEPTION, decided by the operator on 2026-09-16 and deliberately narrow:
#  the plate voltage MAY cross to the APP, through live_status() only, so the
#  browser can show what the apparatus's own panel already shows to anyone
#  standing in front of it. Advising a voltage is not computing a charge.
#
#  It does NOT enter LLM_KEYS, and live_status() output must not be put in a
#  prompt. The reason is unchanged and specific: q = K(t) * d / U_n, the tutor
#  has the manual in retrieval, and `seconds` together with `U_n` is the
#  student's final answer. q, n and e still never leave this module at all.
# --------------------------------------------------------------------------

LLM_KEYS = (
    # From a LIVE session, so the tutor can talk about what is happening now. The
    # test for adding one is whether it names a voltage: times, speeds, counts and
    # states do not, and none of them is a step from q. `voltageV` and `message`
    # are absent on purpose -- the banner says "BALANCED to 0.0% at 153 V" in plain
    # text, which is the reading itself. `voltageInWorkingRange` is the boolean
    # llm/util/voltageSafeguard.py replaces the reading with.
    "phase", "elapsedSeconds", "fieldState", "voltageInWorkingRange",
    "tracked", "legalFalls", "id", "trackId", "fallSeconds", "fallRunning", "trackAgeS",
    "balancePercent",
    # per fall
    "seconds", "speedGridPerSec", "kinematicLegal", "fieldOff", "legal",
    "fallDrift", "fallDirection", "holdDrift", "holdDirection",
    # per droplet
    "label", "fallCount",
    # per session
    "dropletsCompleted", "targetDroplets", "targetFallsPerDroplet", "sprayFrames",
    # health
    "roiOk", "detectionsPerFrame",
    # containers — not values, but the filter has to descend through them
    "droplets", "falls", "picked",
)


def llm_context(d):
    """Filter anything from this module down to what may enter a prompt.

    Call this at the single point where the prompt is assembled. If a prompt can
    be built without going through it, the seal is decoration.
    """
    if isinstance(d, list):
        return [llm_context(x) for x in d]
    if not isinstance(d, dict):
        return d
    return {k: llm_context(v) for k, v in d.items() if k in LLM_KEYS}


LIVE_KEYS = (
    "phase", "message", "elapsedSeconds", "learnSeconds", "startedAt", "updatedAt",
    "tracked", "falls", "legalFalls", "dropletsCompleted", "targetFallsPerDroplet",
    "voltageV", "fieldState",           # the panel reading -- app only, see THE SEAL
    "frameWidth", "frameHeight",        # so a click can be sent as a fraction
    "picked", "id", "trackId", "speedGridPerSec", "fallSeconds", "fallRunning", "trackAgeS",
    "balancePercent",
)


def live_status():
    """What the running session is doing, or None if nothing has ever run.

    For the app's own display. Not for prompts -- see THE SEAL above; this carries
    the plate voltage, which llm_context() would strip.
    """
    import json as _json
    try:
        with open(liveStatusPath(), encoding = "utf-8") as f:
            raw = _json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    def keep(d):
        if isinstance(d, dict):
            return {k: keep(v) for k, v in d.items() if k in LIVE_KEYS}
        return d

    return keep(raw)


def sample():
    """The shapes with fake values. For building UI before footage exists."""
    falls = [
        {
            "id": 51143,                 # track number. App only — see stuff.md.
            "startFrame": 51143.055,     # sub-frame, interpolated at the crossing
            "seconds": 25.544,           # `0` -> `16`, 8 divisions, 1.6 mm
            "speedGridPerSec": 0.313,    # Ⅳ.2's criterion: 0.2-0.5
            "kinematicLegal": True,      # 8 div AND 16-40 s AND 0.2-0.5 grid/s
            "fieldOff": True,            # `V:` read +0000 during the fall
            "legal": True,               # both of the above
            "fallDrift": 0.004,          # divisions/s, unsigned magnitude
            "fallDirection": "wander",   # "left" | "right" | "wander"
            # the hold that PRECEDED this fall. Ⅳ.4 re-balances before every
            # repeat, so there is one per fall, not one per droplet.
            "holdDrift": 0.006,
            "holdDirection": "wobble",   # "up" | "down" | "wobble"
        },
        {
            "id": 2758,
            "startFrame": 2758.412,
            "seconds": 18.263,
            "speedGridPerSec": 0.438,
            "kinematicLegal": True,      # passes every speed and time test...
            "fieldOff": False,           # ...but the panel read +0235V
            "legal": False,
            "fallDrift": 0.011,
            "fallDirection": "left",     # one-way: Ⅳ.3 says readjust the voltage
            "holdDrift": 0.021,
            "holdDirection": "up",
        },
    ]
    return {
        "contractVersion": CONTRACT_VERSION,
        # roiOk is the one to check first. A misplaced ROI produces
        # plausible-looking numbers all the way through and nothing downstream
        # can detect it. If this is False, discard the session.
        "roiOk": True,
        "gridSpacingPx": 67.5,           # report only — calibration constant
        "detectionsPerFrame": 10.2,      # a spike means somebody sprayed
        "fps": 30.01,
        "droppedFrames": 0,              # check at setup, not mid-session
        "dropletsCompleted": 0,          # this one is mid-series: 2 of 5
        "targetDroplets": TARGET_DROPLETS,
        "targetFallsPerDroplet": TARGET_FALLS_PER_DROPLET,
        # Frames where the detection count spiked, i.e. somebody sprayed. Frames,
        # not a flag: the app compares these against each fall's startFrame to
        # work out WHICH series was broken. A spray mid-series means the table
        # mixes two droplets, and the operator cannot see that by eye — a fresh
        # droplet at the same speed looks identical.
        "sprayFrames": [],
        "droplets": [
            {
                "id": 51143,             # App only. Binds these falls to one droplet.
                "label": "Droplet 1",    # show this to the student, never the id
                "fallCount": len(falls),
                "falls": falls,
            },
        ],
    }
