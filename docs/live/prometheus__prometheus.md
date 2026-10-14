# Live (prospective) failure detection

Ledger `.gha_optimizer\live\prometheus__prometheus.jsonl` (hash-chained; verified before scoring). Predictions were written when a run was queued, before any of its jobs completed (None to None); outcomes were read afterwards. Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a significance test.**

* runs scored: 0 (runs skipped because a job had already completed or the diff was unavailable: 8); runs with final outcomes: 0
* job predictions with an outcome: 0, of which failures: 0

not enough outcomes of both kinds to score yet
