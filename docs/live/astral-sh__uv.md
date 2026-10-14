# Live (prospective) failure detection

Ledger `.gha_optimizer\live\astral-sh__uv.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (2026-10-12T15:23:55Z to 2026-10-13T20:48:56Z); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 47 (runs skipped because a job had already completed or the diff was unavailable: 2); runs with final outcomes: 46
* job predictions with an outcome: 739, of which failures: 43

* base rate of failure among scored jobs: 5.8%

| model | PR-AUC | ROC-AUC | precision in the top 10% of scores | share of all failures caught there |
|---|---|---|---|---|
| baseline | 0.341 | 0.896 | 0.39 | 0.67 |
| old_logistic | 0.154 | 0.831 | 0.09 | 0.16 |
| v2_hgb | 0.252 | 0.882 | 0.24 | 0.42 |

* PR-AUC of `old_logistic` minus baseline: -0.173 [-0.271, -0.045] (excludes 0; run-level bootstrap)
* PR-AUC of `v2_hgb` minus baseline: -0.082 [-0.166, +0.007] (includes 0; run-level bootstrap)
