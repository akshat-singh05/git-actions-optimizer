# Improvement programme - results (2026-10-12)

Plan and pass/fail rules, written before any result was read: `docs/IMPROVEMENT_PLAN.md`. Frozen follow-up test: `docs/POOLED_FROZEN.md`.
Every figure below comes from a command in the repository on real ingested history or real GitHub Actions runs; nothing is
simulated. MEASURED = observed on real data or real runs; ESTIMATED = derived from predicted durations.

| goal | what was built | status | result |
|---|---|---|---|
| A. Better failure predictor | protocol v2 (completion-aware visibility), 15 new features, `v2_logistic` / `v2_hgb` / `v2_blend` | IMPLEMENTED | **Pre-registered criterion NOT met** (2 of 9 repositories beaten, 0 lost; required >= 3). A pooled model reaches 3 of 9 but only as an exploratory lead; confirmation pending. |
| B. Act on real failures | `gha-optimizer triage` (class + failing step + action) and the same profile inside `analyze` | IMPLEMENTED | Run on all nine repositories (`docs/FAILURE_TRIAGE.md`). Classes are heuristics from recorded outcomes. |
| C. Speed | `analyze --relax-gates` (opt-in, provably-safe gate removal), `replay-gates` | IMPLEMENTED | Replay on history: ~0% on 9 mature repos. **Real run: critical path -15.1% (MEASURED, 6 vs 6 runs, p = 0.013)** on a simple lint -> test pipeline, billable time +4.1% (not significant). |
| D. Failure detector on real data, live | `live-score` / `live-evaluate` (hash-chained ledger, scored before any job completes) | IMPLEMENTED; evaluation see section D | see section D |

## A. Failure predictor

* **Protocol v2.** A run's outcome becomes visible to later runs only after it finished (`finished_at`), as it would be at
  scoring time. The old protocol (v1) let a model see outcomes of runs still in progress; `completion_aware=False` reproduces it.
* **15 new features** (`V2_FAILURE_COLUMNS`): event/actor/attempt, same-PR history of the job (bounded to 5 runs), what the diff
  touches (tests, dependencies, CI config, docs only, number of top-level directories), time since the last failure, last-5
  failure rate, number of prior runs.
* **Selection (development repos only, rule fixed in advance):** `v2_hgb` (regularised boosted trees): 1 won (prometheus), 0 lost
  on poetry/fastapi/prometheus/dubbo (`docs/IMPROVE_EVAL_DEV.md`). `v2_logistic` and the old tree lost in dubbo, so they were not
  eligible.
* **Test (5 repositories never used for choices, `docs/IMPROVE_EVAL_TEST.md`):** `v2_hgb` beats the prior-failure-rate baseline in
  ruff (PR-AUC 0.755 vs 0.345, interval excludes 0), is indistinguishable elsewhere, and is never significantly worse; quarkus and
  cli/cli have < 20 positives and count as not won. Over all nine: **2 won (prometheus, ruff), 0 lost**; the old default
  (regularised logistic regression) under the same protocol: 0 won. The criterion required a count above the old default's **and
  at least 3**, so it is **NOT MET**.
* **Pooled leave-one-repository-out model (exploratory, `docs/POOLED_EVAL.md`).** Training on the repository's own past plus the
  most recent past rows of the other repositories beats the baseline in 3 of 9 (prometheus +0.236, pydantic +0.107, ruff +0.495
  PR-AUC) and loses in none. It was designed *after* the pre-registered results had been read, so this is a lead. It is frozen
  (`docs/POOLED_FROZEN.md`) and will be judged on runs after 2026-10-12T14:58:29Z; verdict due about 2026-10-26.
* **What the evidence supports.** A real, large gain over the naive baseline exists in two repositories (prometheus, ruff:
  repositories with many failures that cluster in PRs); in the others the model is not distinguishable from "this job failed
  recently". The honest statement is "better than the baseline in some repositories", not a general improvement.

## B. Triage of real failures (`docs/FAILURE_TRIAGE.md`)

For every failed job: failing step (from step records), class, bucketed step type, recurring failing steps per job. Classes are
decided from recorded outcomes only: `derived_aggregator` (a "status" job that fails because something it needs failed - not an
independent failure), `flaky_same_commit`, `pre_existing` (also failed on the base), `fixed_by_later_commit`, `persistent`,
`unrelated_to_change`, `unclassified`.

Findings (counts in the report): in poetry and fastapi about half the failed jobs are aggregator jobs, so the independent failures
are about half of what a raw count says; prometheus failures are dominated by persistent and fixed-by-later-commit cases (56% and
28%); 10-25% of red runs in dubbo, ruff, uv, prometheus need no code fix (every independent failure is pre-existing, flaky or
unrelated). Limits: flakiness is under-detected (a re-run overwrites the earlier attempt), the classes are heuristics and not root
causes, and no recommendation has been checked against what developers actually did. `analyze` appends the same profile for the
analysed workflow (`triage_profile`).

## C. Speed: relaxing safe `needs` gates

Rule 7 was amended by the owner (2026-10-12): the opt-in `--relax-gates` may remove an edge only if `gha_optimizer/gates.py`
proves it carries no data and guards no side effect (no `needs.<job>` reference, no secrets other than `GITHUB_TOKEN`, no
environment / deploy / publish step, no status-function `if`, no deploy-like name). The output is validated against the original
(every job kept, remaining edges a subset, every removed edge classified relaxable). Trade-off, printed in the proposal: a
relaxed job also runs when its former upstream fails.

* **Replay on nine repositories' history (`docs/GATE_REPLAY.md`, MEASURED on history for the saving, ESTIMATED for the extra
  billable time):** no saving in seven of nine (the planner found no edge it could prove safe and useful); 0.3% in dubbo (7 s median), 0.1%
  in quarkus. The critical paths are dominated by `test` / `build` jobs themselves (table in the report), which removing gates
  cannot shorten.
* **Real run (`docs/relax_run/`, MEASURED):** the project's own CI (`lint` -> `test`, `typecheck`). The tool proposed removing
  `test`'s wait for `lint` (ESTIMATED 172 s -> 138 s, -19.9%). Six measured runs per arm after a warm-up run each, alternating
  order, on scratch PR branches: wall-clock critical path median **159.5 s -> 135.5 s (-24 s, -15.1%), permutation p = 0.013**;
  run time -14.6%; **billable job-seconds 206 -> 214.5 (+4.1%, p = 0.43, not significant)** because `test` and `typecheck` now
  start together; every job and step identical and successful in all 12 runs; `test` overlapped `lint` in every relaxed run and in
  none of the original runs. The estimate (-19.9%) was higher than the measurement (-15.1%); the relaxed `test` job's median was
  135.5 s against 121.5 s in the original arm (the cause was not investigated). Limits: one repository,
  one workflow, 6 runs per arm, no failing runs were exercised (the extra work on failures is estimated, not measured), so this
  shows the mechanism works on a simple pipeline and does not generalise to the nine repositories.

## D. Failure detector on real data, live (prospective)

`gha-optimizer live-score` polls GitHub for runs that are queued or in progress and, **before any of their jobs has completed**,
writes a probability per job to an append-only, hash-chained ledger (`read_ledger` rejects any edited line). Models are fitted
only on history up to the start; the structural graph features are blanked because no per-commit graph is available at that moment.
`gha-optimizer live-evaluate` later reads the real outcomes and reports PR-AUC and precision in the top 10% against the same
baseline. It is a demonstration that predictions can be made and judged prospectively, **not a significance test**: the window is a
few days.

**Interim result (`docs/LIVE_TEST.md`, 2026-10-14, scorers still running):** only uv has scoreable outcomes - 46 completed runs, 739 job predictions, 43 failures (5.8%).
PR-AUC: baseline (the job's prior failure rate) 0.341, old logistic 0.154, v2 boosted trees 0.252; the old logistic model is significantly worse than the baseline
(-0.173 [-0.271, -0.045]) and v2 is indistinguishable from it (-0.082 [-0.166, +0.007]). ruff had no failures among its scored runs and prometheus could not be scored (every run
already had a completed job when the poll reached it). So the live test shows the mechanism works and gives no evidence that either model beats the baseline in uv; it does not contradict the
offline uv results (v2 -0.09, not significant). Two days is a demonstration, not a significance test.

## E. Reproducibility (2026-10-14)

Symptom: the same repository gave different estimates at different times (poetry's estimated baseline wall-clock 307.4 s, later 303.8 s).
The models were already seeded (`random_state = 13` everywhere). The cause was an input, not a seed: the hour of day, the weekday and
`hours_since_last_failure` of the scored run defaulted to the wall clock when the inference frame was built, and the hour and weekday are
model features. Fix: the scoring time is an explicit parameter (`as_of`). In the dashboard and the demos it is the creation time of the
run whose diff is scored (`RepoSnapshot.as_of`); `analyze --as-of` and the API field `as_of` set it explicitly; without them `analyze`
still uses the current time and prints it. Verified by running the complete dashboard pipeline twice in separate processes with different
`PYTHONHASHSEED` values: the two result files were byte-identical (62,641 bytes); `tests/test_reproducibility.py` pins the time features
and the end-to-end pipeline. Limit: determinism was verified on poetry; other repositories use the same code path.

## F. Calibration of the failure probabilities (2026-10-14, `docs/CALIBRATION.md`)

Question: is "P(failure) = 0.91" a probability? Measured with the rolling-origin folds on poetry, fastapi, prometheus and dubbo:

* The raw logistic score is mildly over-confident at the top (top-decile predicted vs observed: poetry 0.35 vs 0.28, fastapi 0.49 vs 0.37,
  prometheus 0.20 vs 0.11, dubbo 0.21 vs 0.17) and its Brier score is **not** better than the job's prior failure rate's in poetry,
  fastapi and dubbo (marginally worse in prometheus too).
* Two recalibrations were tried: a Platt sigmoid fitted on the newest 25% of the training rows, and an increasing map learned from the earlier
  folds' out-of-sample predictions. Neither improved the Brier score in 3 of 4 repositories (significantly worse in fastapi; the out-of-sample map
  also in poetry and dubbo); both helped slightly in prometheus (4.0% base rate, 15k rows).
* Decision: no recalibration is applied. The dashboard instead shows the score next to the job's own base rate and the observed failure rate of
  unseen rows that scored alike (with their count), flags a score above the highest one the model ever produced on unseen rows (poetry's 0.91 for
  `tests-matrix` is above its highest unseen score of 0.58, so it is an extrapolation) and states the model-vs-baseline PR-AUC and Brier for the
  repository (poetry: the baseline is better on both).

## G. Data coverage: cache-miss labels (2026-10-14)

Before: five of nine repositories declined in the dashboard (quarkus, prometheus, uv, ruff, pydantic). Cause for uv, ruff and pydantic: too few cache-miss labels, because
they had been ingested with `--no-logs` (or a spent log budget) and `ingest` never revisited a run it had marked done. Change: runs that finished without logs are revisited when logs
are requested (`tests/test_ingest.py::test_runs_finished_without_logs_are_backfilled_when_logs_are_requested`). Back-fill with `--log-budget 1500` per repository, newest runs first:

| repository | logs fetched | cache labels (job rows) | dashboard now |
|---|---|---|---|
| pydantic | 1,033 downloads, 311 with a cache line | 120 -> 311 | **full pipeline** |
| ruff | 1,500 downloads, 645 with a cache line | 0 -> 645 | **full pipeline** (its job-level model-vs-baseline: baseline better on PR-AUC and Brier) |
| uv | 1,500 downloads, 55 with a cache line | 39 -> 55 | **still declines**: 33 labelled (run, job) rows, all of one class (cache hit), so a cache-miss model cannot be trained; most uv logs contain no cache line the parser recognises |

quarkus (24 admitted runs) and prometheus (a job of its current workflow that never ran in the ingested window) still decline, as they should: the system does not fill gaps.
Limit: the back-fill only affects labels, so the earlier model comparisons (which did not use these runs' cache labels) are unchanged.

## What remains open

* Final verdicts (scheduled, `docs/BACKGROUND_RUNBOOK.md`): live test 2026-10-19, logistic default 2026-10-21 (interim run 3 on 2026-10-14: two judged repositories, both pass, not yet confirmed),
  pooled model 2026-10-26 (interim on 2026-10-14: three judged repositories, 1 win, 0 losses - **would be REJECTED if final**, but only two days of data exist).
* Relaxed gates measured on one repository only; no failing run measured.
* Triage recommendations not validated against developer actions; flakiness under-detected.
* Live scoring cannot score prometheus (its runs already have a completed job when the 30-second poll reaches them).
