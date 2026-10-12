# Live (prospective) failure detection

Ledger `.gha_optimizer/live/astral-sh__uv.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (2026-10-12T15:23:55Z to 2026-10-12T16:38:49Z); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 27 (runs skipped because a job had already completed or the diff was unavailable: 0); runs with final outcomes: 13
* job predictions with an outcome: 190, of which failures: 21

* base rate of failure among scored jobs: 11.1%

| model | PR-AUC | ROC-AUC | precision in the top 10% of scores | share of all failures caught there |
|---|---|---|---|---|
| baseline | 0.510 | 0.888 | 0.58 | 0.52 |
| old_logistic | 0.453 | 0.883 | 0.53 | 0.48 |
| v2_hgb | 0.429 | 0.884 | 0.37 | 0.33 |

* PR-AUC of `old_logistic` minus baseline: -0.039 [-0.128, +0.059] (includes 0; run-level bootstrap)
* PR-AUC of `v2_hgb` minus baseline: -0.071 [-0.183, +0.069] (includes 0; run-level bootstrap)

14 scored run(s) had not completed when this was evaluated and are not included.
