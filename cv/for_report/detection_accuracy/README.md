# Per-frame detection test - raw data

Method and results: `../HANDOVER.md`, sections 7.7 and 8.5.

| file | what it is |
|---|---|
| `sample.json` | the 165 screens shown: 150 frames drawn at random (fixed seed) from the four phases of the 09-08 recording, plus 15 unannounced repeats |
| `labels.json` | the labeller's marks per screen, in full-frame pixels (x, y, sure = 1 / unsure = 0), and seconds spent per screen |
| `review.json` | blind second look at the 459 spots where the marks and the tracker disagreed: y droplet, n not a droplet, u unsure |
| `control_answers.json`, `control_key.json` | the control: 40 empty spots and 40 real droplets shown blind, the answers given, and which spot was which |
| `per_frame.csv` | per frame, after the review, inside the detection area: phase, marks, and found / false / missed for the detector, the tracker and the rings |
| `detection_examples_detector.jpg`, `detection_examples_overlay.jpg` | the same four example frames as `../detection_examples_tracker.jpg`, for the raw detector and for the rings |
