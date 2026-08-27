"""CV module — the contract with the app and LLM layers. STUB.

Key names, nesting and units are FINAL. Values are fake, so the app and LLM can be
built before the real pipeline is wired in. Replace the bodies, keep the shapes.

CHANGED FROM VERSION 1 — read this before you reuse old code:
  * `fall_time_s`       -> `seconds`
  * `fall_speed_grid_s` -> `speedGridPerSec`
  * keys are camelCase throughout, matching the pipeline
  * one dict per video became a session: droplets, each holding its own falls
  * `legal` / `fieldOff` / `kinematicLegal` added — a fall time on its own does not
    tell you whether the fall counts

Sources for every rule below: the lab manual (Ⅳ.2 selection, Ⅳ.3 balancing,
Ⅳ.4 static method), `test/docs/README.md` §8, and Topic 6 rubric 三.1.4 / 三.1.5.
Layer-by-layer reasoning lives in `test/stuff.md`.
"""

CONTRACT_VERSION = 2

# Targets come from the manual, Ⅳ.4: at least 3 droplets, at least 5 falls each.
TARGET_DROPLETS = 3
TARGET_FALLS_PER_DROPLET = 5


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
# --------------------------------------------------------------------------

LLM_KEYS = (
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
    "droplets", "falls",
)


def llm_context(d: dict) -> dict:
    """Filter any dict from this module down to what may enter a prompt.

    Nested lists are filtered too. Use this on everything; do not hand-pick.
    """
    if isinstance(d, list):
        return [llm_context(x) for x in d]
    if not isinstance(d, dict):
        return d
    return {k: llm_context(v) for k, v in d.items() if k in LLM_KEYS}


def survey(video_path: str) -> list:
    """Every complete `0` -> `16` traverse in the clip, in time order.

    This is the SELECTION aid, not the measurement. The manual has no
    multi-droplet method — Ⅳ.4 is one droplet at a time — so this exists to
    find candidates in crowded footage, the way Ⅳ.2 describes a human doing it
    by eye. Expect a lot of rows: a 30-minute clip returned 124, of which 2
    were legal.
    """
    return [
        {
            "id": 51143,                 # track number. App only — see stuff.md.
            "startFrame": 51143.055,     # sub-frame, interpolated at the crossing
            "seconds": 25.544,           # `0` -> `16`, 8 divisions, 1.6 mm
            "speedGridPerSec": 0.313,    # Ⅳ.2's criterion: 0.2-0.5
            "kinematicLegal": True,      # 8 div AND 16-40 s AND 0.2-0.5 grid/s
            "fieldOff": True,            # `V:` read +0000 during the fall
            "legal": True,               # both of the above
            # Sideways travel during the fall. Magnitude and character are
            # separate questions: "wander" is air currents or Brownian, one-way
            # is Ⅳ.3's signal that the plates are not level.
            "fallDrift": 0.004,          # divisions/s, unsigned magnitude
            "fallDirection": "wander",   # "left" | "right" | "wander"
            # The hold that PRECEDED this fall. Ⅳ.4 re-balances before every
            # repeat, so there is one of these per fall, not one per droplet.
            # The dial does not move between repeats -- so if this changes across
            # a series at constant voltage, the droplet did. Ⅴ.2: the oil
            # volatilizes, the droplet gets lighter, the same field now
            # overbalances gravity, and it drifts UP by a growing amount.
            "holdDrift": 0.006,          # divisions/s vertical during the hold
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
            "fallDirection": "left",
            "holdDrift": 0.021,
            "holdDirection": "up",       # one-way: Ⅳ.3 says readjust the voltage
        },
        {
            "id": 2291,
            "startFrame": 1062.319,
            "seconds": 3.546,
            "speedGridPerSec": 2.256,    # 4.5x the ceiling
            "kinematicLegal": False,
            "fieldOff": None,            # NOT False. Never tested — see below.
            "legal": False,
            "fallDrift": 0.031,
            "fallDirection": "wander",
            "holdDrift": None,           # never balanced; nothing to measure
            "holdDirection": None,
        },
    ]


def analyze(video_path: str, binding=None) -> dict:
    """One measurement session: health, droplets, and each droplet's falls.

    `binding` groups fall ids into droplets, e.g. [[51143, 51890], [60011, 60422]].
    Ⅳ.4 lifts and re-drops the SAME droplet five times, and the tracker loses it
    during each motionless hold, so one droplet produces several track ids. Nothing
    in a recording says which ids belong together — only the operator watching the
    bench knows. Guessing it (falls close in time, similar speed) would silently
    file five falls from two droplets as one, and the resulting charge looks
    perfectly normal. So it is passed in, or not at all.

    With no binding, `droplets` comes back empty and you want survey() instead.

    NOTE: the signature changes again when live capture lands. A live session is not
    a `video_path` — it will take a frame source and stream updates. Everything
    below keeps its shape; only how you start it changes.
    """
    return {
        "contractVersion": CONTRACT_VERSION,

        # --- health -------------------------------------------------------
        # roiOk is the one to check first. A misplaced ROI produces
        # plausible-looking numbers all the way through; nothing downstream
        # can detect it. If this is False, discard the session.
        "roiOk": True,
        "gridSpacingPx": 80.5,           # report only — calibration constant
        "detectionsPerFrame": 5.4,       # a spike means somebody sprayed
        "fps": 30.0,
        "droppedFrames": 0,              # check at setup, not mid-session

        # --- session ------------------------------------------------------
        "dropletsCompleted": 0,   # this one is mid-series: 2 of 5
        "targetDroplets": TARGET_DROPLETS,
        "targetFallsPerDroplet": TARGET_FALLS_PER_DROPLET,
        # Frames where the detection count spiked, i.e. somebody sprayed.
        # Frames, not a flag: the app compares these against each fall's
        # startFrame to work out WHICH series was broken. A spray mid-series
        # means the table now mixes two droplets, and the operator cannot see
        # that by eye -- a fresh droplet at the same speed looks identical.
        "sprayFrames": [],

        # Empty unless `binding` said which fall ids belong to which droplet.
        "droplets": [
            {
                "id": 51143,             # App only. Binds these falls to one droplet.
                "label": "Droplet 1",    # show this to the student, never the id
                "fallCount": 2,          # == len(falls). Keep them consistent.
                # holdDrift lives on each fall, not here -- see the note there.
                "falls": survey(video_path)[:2],
            },
        ],
    }


# --------------------------------------------------------------------------
#  fieldOff is TRI-STATE.
#
#      True   the panel showed +0000 during the fall
#      False  the field was live; the fall measured nothing
#      None   never tested, because the fall already failed on kinematics
#
#  `None` is a blank column in falls.csv. Do not read blank as False.
#
#  ONLY `legal` decides whether a fall counts, and it is exactly
#  `kinematicLegal and fieldOff`. Everything else on a fall describes quality.
#  Nothing else rejects one.
#
#  Live state (phase, trackAlive, the annotated frame) arrives through the live
#  API once frame capture exists. `trackAlive` matters more than it looks: if the
#  tracker silently re-acquires, five falls get filed under one droplet that are
#  really two, and the resulting charge looks perfectly normal.
# --------------------------------------------------------------------------
