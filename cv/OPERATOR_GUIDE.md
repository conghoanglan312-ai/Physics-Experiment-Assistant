# Running a measurement session

For someone who has never touched this pipeline. You need the apparatus, a USB capture
dongle on its video output, and a laptop.

## Set up, once

1. Install **Python 3.11** and **git**.
2. `git clone <repo url>` and `cd Physics-Experiment-Assistant`
3. `pip install -r requirements.txt`
4. Plug in the dongle, then run `python cv/UVC_check.py`. It prints which camera shows
   the apparatus grid. Note that number.

## Start a session

5. **Level the apparatus** (manual Ⅳ.1). A tilted platform makes droplets drift sideways
   and leave the picture.
6. `python -m cv.pipeline live 0` — replace `0` with the camera number from step 4.
7. It learns the background for 30 seconds. **Do not spray during this.** The chamber
   must be empty, or a droplet is burnt into the background and subtracted away for the
   rest of the session.

## Measure a droplet

8. Spray, then **click your droplet** in the window. The banner then talks about that one.
9. **Toggle the field** so it can see whether the droplet responds. Droplets that ignore
   the field get crossed out with ✗ — they can never be balanced, so skip them.
10. **Balance it.** The banner says which way to turn and to what voltage, for example
    `off balance by 4.2% (drift +0.013 gr/s) -- RAISE to about 209 V`. Keep tuning until
    it reads **`BALANCED to 2.0% or less`**.
    Do not judge this by eye. A droplet that looks perfectly still can be 7% out.
11. **Park it just above the top line** — about half a division above, not on it — and
    leave it there for 15 seconds. The banner switches to `MEASURING`.
    The grid lines are masked out of the picture so they are not mistaken for droplets,
    so a droplet sitting *on* a line is invisible: its track dies, and the fall that
    follows is never timed. This has cost real measurements.
12. Cut to 0 V, let it fall, lift it back with 提升, and repeat until you have **five
    falls**. Keep the same voltage for all five.
13. Watch `balance X%` in the banner during the run. If it turns orange and says retune,
    retune before the next fall.
14. Press **`q`** to stop. Spray again for the next droplet.

A good droplet: five falls, each 16–40 s, same voltage, balance 2% or better.

## What comes out, and where

The pipeline creates its own output folder — nothing to set up. On a fresh clone it
writes to `output/` inside whatever folder you ran the command from:

```
output/live_<date>_<time>/falls.csv              one row per traverse
output/live_<date>_<time>/charges.csv            q, n and e per droplet
output/live_<date>_<time>/annotated_multi.mp4    the video with the overlay drawn on
```

`falls.csv` is written fall by fall, so a crash or a closed window keeps what you already
measured. `charges.csv` is written when you stop the run: any droplet with at least two
legal falls and a voltage read from the panel gets its charge, electron count and
elementary charge, with the per-droplet mean and error printed in the terminal too. No
notes and no second tool are needed -- the falls are grouped by column and fall time, and
the voltage is the one the pipeline read off the apparatus.

**Send back after each session:** `falls.csv` and `charges.csv`, the apparatus number, and
the room temperature and humidity.

## Do not

- Touch the focus mid-session. The background goes stale and the screen fills with false
  detections. It rebuilds itself after about 45 s, but the minute in between is wasted.
- Spray during the 30 s background learn.
- Change the voltage in the middle of a droplet's five falls.
- Trust "it looks balanced". Trust the percentage.

## If something looks wrong

| what you see | what it means |
|---|---|
| `UVC_check` finds no grid | dongle or BNC cable not connected, or the apparatus output is off |
| rings everywhere, nothing sensible | background is stale, usually after a focus change; wait ~45 s for the rebuild or restart |
| `V ?` in the corner | the panel is not readable; falls are still timed, and the voltage may be recovered from the live readout |
| nothing gets ringed at all | droplets are too fast or too slow, or no balance voltage is reachable; spray again |
| banner says a droplet does not answer the field | it is uncharged; it can never be balanced, so spray again |
| a droplet's ring disappears while it sits still | it is parked on a grid line, where nothing is detected. Nudge it off the line |
| `mid-fall track ... died` in the terminal | a fall was lost. The line says whether the droplet was invisible or whether the track refused a blob beside it |
