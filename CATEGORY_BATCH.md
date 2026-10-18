# Category batches

The controller uses the main `pipeline/` implementation. The normal path is:

**Inspect source → build complete environment → automated checks and independent
semantic review → calibration screen (5 episodes x 2 models) → concurrent native
qualification (10 x 2) for every task whose screen measured cleanly → publish.**

pass@1 < 0.5 on both models is the strict target and never holds a task. Only a
screen that did not measure cleanly (voids, unattributable failures, unresolved
reward hacking) holds it, as `unverified`, with its scores and outlook.

There is no preliminary plan approval gate. Each environment has one initial
design attempt, which must aim for a high-quality, hard task from the outset.
Authors verify consequential decisions and legitimate scenario-grounded traps
before submitting; they must not rely on later hardening to add difficulty or
correctness. Review and calibration report the result. The user's 22 September
continuation instruction authorizes validity corrections across the retained
`envs-cat-v2` candidates before calibration. This run gives each selected candidate
up to two bounded correction passes, with at most 100 author responses per pass.
Those passes have their own recorded authorization and do not reset consumed
authoring, gate or qualification allowances. Findings concern source support,
public instructions, grader correctness, privacy and professional coverage.
Calibration scores and individual solver answers are not correction feedback.
Difficulty hardening remains manual, after the user reviews calibration.
Only an accepted release bound to the exact task and current policy counts.

For `envs-cat-v2`, the target is 40 qualified environments by 22 September 2026,
23:59 IST: ten each in `rich/`, `strong/`, `fixture-led/`, and `thin/`, with exactly
two substantive graded steps per environment. Existing source families are
excluded, and a new source family can occupy only one slot.

```sh
.venv/bin/python category_batch.py --out envs-cat-v2 \
  --work pipeline-artifacts/envs-cat-v2 --per-category 10 \
  --selected-only --eager-selected --task-workers 48 --classify-workers 2 \
  --provider-concurrency 48 --review-provider-concurrency 48 \
  --qualification-workers 48 --native-workers 10 --native-concurrency 48 \
  --local-workers 32
```

The Coder calibration profile uses 48 FIFO Anthropic request slots and
48 OpenAI slots for independent review and file-workspace qualification. Both
queues share the retained request/elapsed budget. Authoring and qualification
have separate pools. The two native calibration models run concurrently, with
up to ten episodes each; all task/model campaigns share 48 native episode slots.
Native controls use the same pool. `--selected-only` disables further source
screening. These are capacity limits, not a delivery forecast.

`--eager-selected` starts remaining selected initial drafts while other tasks
qualify. Publication claims enforce the category quota; additional qualified
versions remain in the artifact workspace. It does not reopen rejected designs
or trigger revisions. CPU-heavy draft checks, source projections and qualification
checks use up to 32 separate Python processes so they cannot stall the model
request threads through Python's interpreter lock. The status distinguishes
preflight checks from active native calibration and reports both worker pools.

Hardening is never automatically triggered by the category batch, including on
restart. Authorized defect corrections use `validity-queued`; unresolved defects
remain `validity-blocked`. Successful two-model measurement that does not qualify
for release is retained as `calibrated-awaiting-review`. These states do not count
as accepted environments. Unselected revisions remain held. To explicitly inspect and
apply difficulty hardening, the user can run:

```sh
./arena harden /path/to/candidate
./arena harden /path/to/candidate --apply
```

The revised version still requires qualification. Explicit standalone
`./arena qualify ... --repair-attempts 1` remains available for a requested
bounded revision; the automated batch always passes zero repair attempts.

`STATUS.md` reports actual qualified counts, active authors and native campaigns.
The detailed state is `pipeline-artifacts/envs-cat-v2/status.json`; drafts and all
unsuccessful outcomes remain in that artifact workspace. An empty category,
source rejection, missing result or infrastructure error never counts as delivery.

## Recovery

Each candidate has one `verification/checkpoint.json`. The pipeline records the
saved author conversation, draft/source hashes, submitted design and remaining
repair allowances there. Restarting the same batch command resumes verified
request boundaries or submitted designs. It preserves work and does not re-extract
over a resumed fixture or reset consumed budgets. An interrupted local mutation
or changed checkpoint is retained for inspection rather than silently replayed.
Qualification repairs use the same checkpoint and charge their allowance before
starting another author response. Accepted releases remain separate certificates.

The controller freezes main pipeline code when creating a batch. Existing runs
retain their frozen implementation. Historical `pipeline-overrides/` files and
migration receipts remain as evidence; new snapshots no longer apply them. The
reviewed native-transfer and scenario-staging fixes now live in `pipeline/`.

## Quality and traps

Authors can add legitimate traps grounded in the trace scenario, including cases
that were not observed corrections. Authored traps cite the supporting fixture,
explain a plausible professional mistake and demonstrate both a wrong-answer
probe and a valid solution. Public evidence must resolve the trap. Authored
sources remain disclosed, receive zero recovered-source credit and respect the
existing category fidelity limits. Hidden rules and unrelated distractions fail.

Exactly two steps are checked before native qualification, publication and final
batch acceptance. Source fidelity, privacy, substantive independent decisions,
source-derived grading, independent semantic review, a blind solve and ten native
episodes on each of two named models remain required. The final corpus is checked
before the batch closes. Successful evidence is reused only when its relevant
inputs match; changed artifacts or review context require fresh verification.

Measured checks above 70% and below 90% are allowed and retain the descriptive
transition label. Their presence does not reject a task; they do not replace
the required measured core and stretch decisions.

```sh
.venv/bin/python -m unittest discover -s pipeline/tests -t .
```
