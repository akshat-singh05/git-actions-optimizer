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

### Interim run, 2026-10-10 (report: `docs/MODEL_EVAL_FRESH.md`)
Only about 3 days of data exist after the cutoff, so this is an interim run of the procedure, not the final verdict.
Ingested with `--no-logs` and bounded `--max-runs` (poetry 6 runs, fastapi 14, prometheus 83, dubbo 1), then
`build-dataset` and `eval-models --after 2026-10-07T07:51:04Z`.

| repo | fresh test rows / runs | positives | judged (>= 20)? | logistic - baseline (PR-AUC) |
|---|---|---|---|---|
| prometheus | 1,396 / 83 | 47 | **yes** | +0.0016 [-0.0575, +0.0671] -> upper bound above 0: **PASS** |
| poetry | 18 / 6 | 12 | no | 0.0000 (both models and baseline score 1.0 on 18 rows) |
| fastapi | 83 / 14 | 1 | no | +0.0312 [0.0000, +0.1098] |
| dubbo | 12 / 1 | 2 | no | -0.2500 (one run; reported, not judged) |

By the stated rule the logistic default **passes in the one judged repository, and the choice is not yet confirmed**:
confirmation requires it to hold in every judged repository, and three of four repositories have too few fresh
positives. The tree variants are context only: in prometheus the original tree is significantly worse than the
baseline (-0.062 [-0.140, -0.005]) and the regularised tree is indistinguishable (-0.029 [-0.106, +0.052]); in fastapi
both beat the baseline but on a single positive.

**Plan for the final verdict:** repeat the same three commands once roughly two weeks of runs exist (about
2026-10-21) so that poetry, fastapi and dubbo can be judged; the verdict is taken from that run, and this interim
table stays in the file. This interim run is not used to change the pass/fail rule or the choice.

### Interim run 2, 2026-10-12 (report: `docs/MODEL_EVAL_FRESH_2.md`)
Same procedure, run to see how close each repository is to the 20-failure threshold (runs through 2026-10-11).

| repo | fresh test rows / runs | positives | judged (>= 20)? | logistic - baseline (PR-AUC) |
|---|---|---|---|---|
| poetry | 55 / 18 | 28 | **yes** | +0.0393 [-0.1526, +0.2504] -> upper bound above 0: **PASS** |
| prometheus | 2,323 / 138 | 100 | **yes** | +0.0421 [-0.0151, +0.1201] -> **PASS** |
| fastapi | 98 / 17 | 1 | no | -0.0334 [-0.1500, +0.0462] (one failure; not judged) |
| dubbo | 58 / 5 | 8 | no | -0.1229 [-0.2838, 0.0000] (8 failures; not judged) |

Two of four repositories are now judgeable and both pass. The choice is **still not confirmed**: fastapi has one fresh
failure in 98 rows (its fresh failure rate is far below its historical 9%), so it may never reach 20; dubbo may on the
21st. The plan does not change: the final verdict is one run on or after 2026-10-21 with whichever repositories then
have at least 20 failures, as pre-stated; this interim table is kept and is not used to alter the rule or the default.
(The fresh runs of this interim were ingested after the 2026-10-12 fix of a UTC-offset bug in the run-listing windows, which had
hidden the newest 5.5 hours of each earlier ingest; see `docs/VALIDITY_RUN.md`.)

A tooling defect found while doing this: `ingest --max-runs N` counted every run the repository already had in the
database, so on a repository holding at least N runs it stopped immediately (the first attempt ingested nothing). It
now counts only the runs finished by the current invocation (`ingestion/ingest.py::_cap_reached`, test
`test_max_runs_counts_only_this_invocations_runs`). The pydantic and cli/cli ingests started from empty
databases for those repositories, so the cap behaved as intended there; I did not audit the older ingests of the five
original repositories for this effect (those that used `--max-runs` on a partly filled repository may have stopped
early, which would only have meant fewer runs, never wrong ones).

### Interim run 3, 2026-10-14 (report: `docs/MODEL_EVAL_FRESH_INTERIM_2026-10-14.md`, run by `scripts/interim_verdicts.sh`)
Prepared for a review on 2026-10-16; same procedure and same rule as above, data through 2026-10-14. **Not the final verdict** (final run: 2026-10-21, `docs/BACKGROUND_RUNBOOK.md`).

| repo | fresh test rows / runs | positives | judged (>= 20)? | logistic - baseline (PR-AUC) |
|---|---|---|---|---|
| poetry | 90 / 31 | 33 | **yes** | +0.1995 [-0.0129, +0.4208] -> upper bound above 0: **PASS** |
| prometheus | 4,337 / 258 | 182 | **yes** | +0.0155 [-0.0165, +0.0516] -> **PASS** |
| fastapi | 104 / 18 | 1 | no | -0.0197 [-0.1167, +0.0659] (one failure; not judged) |
| dubbo | 125 / 11 | 16 | no | -0.0731 [-0.2211, +0.0528] (16 failures; not judged) |

Two of four repositories are judged and both pass; the choice is **still not confirmed** because fastapi (one fresh failure in 104 rows) may never reach 20 and dubbo is four failures short. No rule, threshold or default was changed.
Context only (the tree variants cannot change the verdict): the original tree beats the baseline in poetry (+0.356 [+0.169, +0.544]) and in fastapi on one failure; it does not in prometheus.

