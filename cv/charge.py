"""Charge of an oil droplet, from a fall time and a balance voltage.

Static method (平衡法), the manual's equation (1-14):

               18*pi        [        eta * l         ]^(3/2)         d
    q  =  -------------  *  [ ---------------------- ]         *  --------------
            sqrt(2)         [  t * (1 + b/(p * a))   ]              U_n * sqrt(rho*g)

with the uncorrected droplet radius from (1-13):

    a  =  sqrt( 9 * eta * l / (2 * rho * g * t) )

`a` appears inside its own correction term.  The manual (after 1-21) says explicitly
that when the accuracy requirement is not high, the uncorrected `a` may be used in the
correction, and that is what this does.

EVERY constant below is quoted from section V of
`Millikan Oil Drop Experiment.pdf`, "Related Reference Data Used in the Experiment".
None of them is chosen, fitted or inferred.  p is the Shenzhen value the manual gives.

THE SEAL.  q, n, e and the balance voltage in volts are computed here and must never
enter a dict that crosses `cv/api.py` to the app or the LLM.  Rubric 3.1.4 requires
that the student's analysis ability be exercised, not replaced -- and the LLM has the
manual in retrieval, so `seconds` plus `U_n` is already the whole answer.  These
numbers reach the student through a CSV on disk, which is layer-2 output, and by no
other route.  Do not add them to `LLM_KEYS`.
"""
import math

# --- manual, section V -----------------------------------------------------------
OIL_DENSITY = 981.0             # rho,  kg m^-3
GRAVITY = 9.78                  # g,    m s^-2
AIR_VISCOSITY = 1.83e-5         # eta,  kg m^-1 s^-1
FALL_DISTANCE = 1.60e-3         # l,    m   (the `0` -> `16` span, 8 divisions)
CORRECTION_B = 8.22e-3          # b,    m Pa
AIR_PRESSURE = 1.0098e5         # p,    Pa  (Shenzhen)
PLATE_GAP = 5.00e-3             # d,    m
ELEMENTARY_CHARGE = 1.602e-19   # e,    C   the accepted value to compare against

# The manual's own guidance, V.5: "select an oil droplet with a balance voltage of
# about 150~250V and a drop time of 16-40s for 1.6mm".
VOLTAGE_ADVISORY = (150.0, 250.0)


def dropletRadius(seconds):
    """Uncorrected radius `a` in metres, equation (1-13)."""
    if seconds <= 0:
        raise ValueError("fall time must be positive, got %r" % (seconds,))
    return math.sqrt(9.0 * AIR_VISCOSITY * FALL_DISTANCE
                     / (2.0 * OIL_DENSITY * GRAVITY * seconds))


def charge(seconds, voltage):
    """Charge in coulombs, equation (1-14).  `voltage` is the balance voltage U_n."""
    if voltage <= 0:
        raise ValueError("balance voltage must be positive, got %r. A fall recorded "
                         "with the field off still needs the voltage that BALANCED "
                         "the droplet, which only the operator's notes carry."
                         % (voltage,))
    a = dropletRadius(seconds)
    corrected = seconds * (1.0 + CORRECTION_B / (AIR_PRESSURE * a))
    return ((18.0 * math.pi / math.sqrt(2.0))
            * (AIR_VISCOSITY * FALL_DISTANCE / corrected) ** 1.5
            * PLATE_GAP / (voltage * math.sqrt(OIL_DENSITY * GRAVITY)))


def electronCount(q):
    """n = round(q/e), floored at 1.

    The manual (VI.2) says to divide by the accepted e and round.  A droplet cannot
    carry zero charge and still balance, so a quotient under 0.5 means the measurement
    is wrong rather than the droplet neutral -- it is reported as n=1 and the resulting
    e_i will be visibly off, which is the honest outcome.
    """
    return max(1, int(round(q / ELEMENTARY_CHARGE)))


def perFall(seconds, voltage):
    """Everything derivable from one fall: radius, q, n and this fall's estimate of e."""
    q = charge(seconds, voltage)
    n = electronCount(q)
    return {"seconds": seconds,
            "balanceVoltage": voltage,
            "radiusM": dropletRadius(seconds),
            "chargeC": q,
            "electrons": n,
            "elementaryC": q / n}


def summarise(rows):
    """Mean e over a set of falls, its scatter, and the error against the accepted e."""
    es = [r["elementaryC"] for r in rows]
    if not es:
        return None
    mean = sum(es) / len(es)
    sd = (math.sqrt(sum((v - mean) ** 2 for v in es) / (len(es) - 1))
          if len(es) > 1 else 0.0)
    return {"falls": len(es),
            "meanElementaryC": mean,
            "sdElementaryC": sd,
            "relativeSdPercent": 100.0 * sd / mean if mean else 0.0,
            "errorPercent": 100.0 * (mean - ELEMENTARY_CHARGE) / ELEMENTARY_CHARGE}


def table(droplets, voltages):
    """Rows for every fall of every droplet, plus a per-droplet and overall summary.

    `droplets` is analyze()'s droplet list; `voltages` the balance voltage of each,
    in the same order.  A voltage per DROPLET, not per fall: IV.4 balances once and
    re-drops the same droplet five times.
    """
    if len(voltages) != len(droplets):
        raise ValueError("%d droplets but %d balance voltages. IV.4 balances each "
                         "droplet once, so there must be exactly one voltage per "
                         "droplet, in the same order as `binding`."
                         % (len(droplets), len(voltages)))
    rows, perDroplet = [], []
    for d, u in zip(droplets, voltages):
        mine = []
        for f in d["falls"]:
            r = perFall(f["seconds"], u)
            r["label"] = d["label"]
            r["fallId"] = f["id"]
            rows.append(r)
            mine.append(r)
        s = summarise(mine)
        if s:
            s["label"] = d["label"]
            s["balanceVoltage"] = u
            perDroplet.append(s)
    return rows, perDroplet, summarise(rows)
