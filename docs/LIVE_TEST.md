# Live test - INTERIM evaluation (2026-10-12, about 16:46 UTC)

Produced by `gha-optimizer live-evaluate`; per-repository reports follow. The scorers were still running (window ends about
19:00 UTC), so runs that had not finished are excluded and this file is to be regenerated at the end. Findings so far:

* **uv is the only repository with enough outcomes to score:** 13 runs with final outcomes, 190 job predictions, 21 failures.
  Neither model beat the baseline (prior failure rate): PR-AUC 0.453 (old_logistic) and 0.429 (v2_hgb) against 0.510; the
  differences (-0.039, -0.071) have intervals that include 0. On this sample the v2 model did not help.
* **ruff:** 4 runs scored, 3 skipped; 0 failures among the 101 predictions, so nothing can be scored.
* **prometheus:** 0 runs scored; all 4 seen had a job already completed when the scorer reached them.
* A sample of this size is not a significance test. It shows the prospective mechanism works (predictions written before any job
  finished, ledger verified by hash chain) and gives no evidence that the v2 model beats the baseline live.

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


---

# Live (prospective) failure detection

Ledger `.gha_optimizer/live/astral-sh__ruff.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (2026-10-12T15:41:33Z to 2026-10-12T16:10:08Z); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 4 (runs skipped because a job had already completed or the diff was unavailable: 3); runs with final outcomes: 4
* job predictions with an outcome: 101, of which failures: 0

not enough outcomes of both kinds to score yet


---

# Live (prospective) failure detection

Ledger `.gha_optimizer/live/prometheus__prometheus.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (None to None); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 0 (runs skipped because a job had already completed or the diff was unavailable: 4); runs with final outcomes: 0
* job predictions with an outcome: 0, of which failures: 0

not enough outcomes of both kinds to score yet
