# Frozen choice: logistic regression as the default failure model

Frozen 2026-10-08. This file exists so the choice can be tested on data that did not exist when it was made,
with the pass/fail criteria written down **before** that data is looked at.

## The choice
`FailurePredictor` defaults to `variant="logistic"`: median-imputed, standardised, L2-regularised logistic
regression (C = 0.3) over the features in `FEATURE_COLUMNS`. The alternatives, kept as variants, are the
original boosted tree (`"tree"`) and a heavily regularised tree (`"regularized_tree"`). The settings of all three
are in `gha_optimizer/models/failure_predictor.py` and were fixed before any evaluation fold was computed.

## Why it was picked (and why that is not yet proof)
In `docs/MODEL_EVAL.md` the logistic model was the only variant that was never significantly worse than the
"job's prior failure rate" baseline in any of the five repositories, and it beat the baseline in fastapi. The
regularised tree had the highest scores in poetry, fastapi and prometheus but lost clearly in dubbo and quarkus.
The rule used - avoid significant losses - is less selection-prone than "pick the biggest wins", but the pick was
still made from the same folds it is judged on, so those folds are not evidence for it. Many comparisons were made
(3 failure models x 5 repositories x 3 contrasts), so some "significant" cells are expected by chance.

## The fresh-data test
**Cutoff: `2026-10-07T07:51:04Z`** - the latest usable run in the database when the choice was frozen. Every run at
or before it may be used for training; the test is every run that starts after it.

1. Ingest the new runs. Keep each repository bounded - an unbounded `--since` once pulled in ~1,500 unwanted
   prometheus runs:
   `gha-optimizer ingest --repo <owner/repo> --workflow <file> --since 2026-10-07 --max-runs <N>`
   (poetry `tests.yaml`, fastapi `test.yml`, prometheus `ci.yml`, dubbo `build-and-test-pr.yml`; quarkus has too few runs).
2. `gha-optimizer build-dataset --repo <owner/repo>` for each (graph features for the new runs).
3. `gha-optimizer eval-models --repo ... --after 2026-10-07T07:51:04Z --target failure --target failure_tree --target failure_regularized_tree`
   (writes `docs/MODEL_EVAL_FRESH.md`).

## Pass / fail, stated in advance
Applies only to repositories with **at least 20 positives (failures) in the fresh test set**; others are reported
and not judged.

* **PASS** for a repository: the 95% interval of `logistic - baseline` (PR-AUC) has an upper bound above 0, i.e.
  the logistic model is not significantly worse than "the job's prior failure rate".
* The choice is **confirmed** if it passes in every judged repository. Beating the baseline is not required - the
  claim is only that it is a safe default - but any repository where it does beat the baseline significantly is
  reported as a gain.
* The choice is **rejected** if it is significantly worse than the baseline in any judged repository; then
  `variant` goes back to a decision, and the result is recorded here.
* The tree variants are reported for context only. They are not allowed to change the verdict.

## Result
Not yet run - no runs after the cutoff have been ingested.
