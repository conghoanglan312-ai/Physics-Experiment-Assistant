# Where the AI is, and why the measurement layer is not

For the report's positioning, and for the question a judge is most likely to ask:
*this is Topic 6, AI + 物理实验 — so where is the model?*

## The system has two layers, and only one of them should be learned

| layer | what it is | AI? |
|---|---|---|
| `llm/` | RAG assistant: Gemini + ZAI, embeddings, retrieval, prompt construction, conversation memory, rate limiting | **yes — this is the AI** |
| `cv/` | droplet detection, tracking, fall timing, panel reading | no, and deliberately |

The student interacts with the assistant; the CV layer is the **instrument** that makes
the assistant's answers about a real experiment rather than about plausible-sounding
numbers. "AI + 物理实验" is about applying AI *to* an experiment, and an AI whose inputs
cannot be trusted has not been applied to anything.

## Why the measurement layer is classical, on purpose

**Every threshold has a reason that can be stated.** Asked "why is the circularity gate
0.52?", a trained model can only answer that it was learned. This project answers: three
complete runs of a 56,547-frame session at 0.52, 0.48 and 0.45 all found the same 15 of
15 hand-timed falls, to the same hundredth of a second — see `circularity_sweep.txt`.
The same holds for the drift gate (derived from v = v_free(1 − U/U_b) and the supply
range), the 2–5 s division test (Ⅳ.2's band divided by eight), and the lift detector
(measured: 80 V/s by hand, 210 V instantly by switch). In a *physics* competition, a
number with a derivation behind it is worth more than a number with an accuracy score.

**It reproduces without a dataset.** No training data, no retraining when the lamp or
the focus changes, no distribution shift when the apparatus is serviced. Another team
can run it on their own apparatus tomorrow.

**It is what rubric 三.1.4 asks for.** The rubric requires the student's own analysis
ability to be exercised rather than replaced. `q`, `n`, `e` and the balance voltage are
computed inside the CV layer and written to a CSV, and never cross `cv/api.py` to the
assistant — the allow-list is enforced at the single point where a prompt is built. A
black-box model that returned the answer would work against the rubric, not with it.

## What "deployment and optimisation" covers here

Not model quantisation, but the same engineering questions, measured:

| | figures |
|---|---|
| LLM layer | model selection, embedding + retrieval, timeouts, per-user query limiting |
| CV layer | 23 fps live on a consumer laptop; profiled — the annotated-video writer costs 10.9 ms/frame, `assign()` ~15%, panel matcher ~22%; dropped frames cost 22 ms of temporal resolution on a 20 s fall, i.e. 0.1%, because every measurement runs off capture time rather than frame index |
| acceptance | a fixed gate re-run after every change: reference falls kept, traverse count within 5% |

## The one thing worth adding, if there is time

A **droplet-versus-noise classifier**, as a measured comparison against the classical
gates — not as a replacement.

- **The data already exists.** Legal falls are positives; the rejection census
  (`rejects.txt`) is a large, already-labelled negative set. No labelling session.
- **Start simple**: logistic regression on area, circularity and intensity. If that
  matches the hand-derived gates, that is itself the finding.
- **Score it on the same bar as everything else**: the fifteen hand-timed falls of
  `bench_0908`.
- **It supplies the missing section**: training procedure, inference cost per frame,
  deployment on the laptop that already runs the pipeline.
- **Cost**: 1–2 days. It must stay a comparison and never become load-bearing before
  the submission.

Either outcome is reportable. If it beats the classical gates, ship it and say so. If it
ties — the likely result — then "a learned classifier agrees with thresholds we derived
from the manual" is a stronger claim than either alone.
