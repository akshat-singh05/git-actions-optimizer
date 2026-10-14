# Live test - INTERIM evaluation (2026-10-14, about 08:20 UTC)

Produced by `gha-optimizer live-evaluate`. The scorers are still running (the final evaluation is scheduled for 2026-10-19, `docs/BACKGROUND_RUNBOOK.md`), so
runs that had not finished are excluded. Predictions were written when a run was queued, before any of its jobs finished; every ledger line carries the hash of the previous one.

Findings so far (about two days of data):

* **uv is the only repository with enough outcomes:** 46 runs with outcomes, 739 job predictions, 43 failures (5.8%). **Neither model beat the baseline** (the job's prior
  failure rate, PR-AUC 0.341): the old logistic model scored 0.154 (difference -0.173 [-0.271, -0.045], the interval excludes 0: significantly worse) and the v2 boosted-tree
  model 0.252 (-0.082 [-0.166, +0.007], indistinguishable from 0). On this sample the baseline is the best of the three.
  The structural graph features are blanked in the live scorer (no per-commit graph is available at queue time), which is a difference from the offline evaluation.
* **ruff:** 6 runs scored, 5 skipped; 0 failures among 148 predictions, so nothing can be scored.
* **prometheus:** 0 runs scored, 8 skipped: every run it saw already had a completed job when the 30-second poll reached it, so this poller cannot score prometheus.
* A live window of two days is a demonstration, not a significance test; weekend traffic is low and the runs of one pull request are correlated.

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


---

# Live (prospective) failure detection

Ledger `.gha_optimizer\live\astral-sh__ruff.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (2026-10-12T15:41:33Z to 2026-10-14T07:40:48Z); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 6 (runs skipped because a job had already completed or the diff was unavailable: 5); runs with final outcomes: 6
* job predictions with an outcome: 148, of which failures: 0

not enough outcomes of both kinds to score yet


---

# Live (prospective) failure detection

Ledger `.gha_optimizer\live\prometheus__prometheus.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (None to None); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 0 (runs skipped because a job had already completed or the diff was unavailable: 8); runs with final outcomes: 0
* job predictions with an outcome: 0, of which failures: 0

not enough outcomes of both kinds to score yet
