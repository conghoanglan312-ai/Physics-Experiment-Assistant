# -*- coding: utf-8 -*-
"""Replay a recorded live-status stream through the tutor's rules.

    python -m app_js.tutor_replay <samples.jsonl> [--ask]

Each line is one poll of /api/live/status as it was actually served, so the run
the tutor sees here is a real session: real picks, real losses, real verdict
flips, real falls. The clock is faked from the samples, so forty minutes replay
in seconds and every threshold in app_js/tutor.py is exercised at its true
spacing.

Without --ask nothing leaves the machine: the model is stubbed and the script
reports only WHEN the tutor would have spoken, which is the half of its
behaviour that has to be right before the words matter. With --ask it calls the
real model for each trigger and prints what it actually said.

This exists so the rules can be changed with evidence instead of a bench
session: run it before and after, and the difference is the whole review.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_js import tutor as T


def replay(path, ask):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    fired = []

    def stub(prompt):
        fired[-1]["prompt"] = prompt
        return "(stub reply)"

    def real(prompt):
        from app_js.web_server import _ask_model
        fired[-1]["prompt"] = prompt
        return _ask_model(prompt)

    t = T.Tutor(real if ask else stub)
    clock = [0.0]
    T.time.monotonic = lambda: clock[0]
    said_before = [0]

    for r in rows:
        clock[0] = float(r.get("el") or 0.0)
        status = {"phase": "running", "message": r.get("msg") or "",
                  "elapsedSeconds": r.get("vid"), "fieldState": r.get("field"),
                  "voltageV": r.get("volts"), "falls": r.get("falls"),
                  "legalFalls": r.get("legal"), "dropletsCompleted": r.get("done"),
                  "picked": r.get("picked")}
        fired.append({"at": clock[0]})
        t.poll(status)
        if t._said > said_before[0]:                    # a trigger this sample
            said_before[0] = t._said
        else:
            fired.pop()

    import time as _t
    _t.sleep(2.0 if ask else 0.5)                       # the reply arrives on a thread
    messages = t.take()
    minutes = (rows[-1].get("el") or 0) / 60.0
    print("%s: %.0f min of session, tutor spoke %d time(s)" % (os.path.basename(path),
                                                               minutes, len(fired)))
    for i, f in enumerate(fired):
        stall = f.get("prompt", "").split("stuck: ")[-1].split(".\n")[0]
        print("  %5.0fs  %s" % (f["at"], stall[:96]))
        if ask and i < len(messages):
            print("         -> %s" % messages[i]["text"][:220])
    return fired


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("samples")
    p.add_argument("--ask", action="store_true", help="call the real model")
    a = p.parse_args()
    replay(a.samples, a.ask)
