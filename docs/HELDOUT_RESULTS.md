# Held-out test: results (2026-10-10)

Plan and pre-stated readings: `docs/HELDOUT_PLAN.md` (committed before any data was ingested). Rules and policy were not
changed. Generated reports (all computed from the database, **MEASURED on history**; the boilerplate sentence in the
replay report saying "the rules were written after reading these workflows" refers to the original five repositories and
does not apply to these two): `docs/HELDOUT_IMPACT_REPLAY.md`, `docs/HELDOUT_DATASET_REPORT.md`,
`docs/HELDOUT_MODEL_EVAL.md`.

## What was actually ingested (deviations from the plan, disclosed)

* The plan said `--max-runs 400`. pydantic's CI has 100+ matrix jobs per run and ingestion ran at about 2 runs/minute, so
  it was stopped by hand at **196 admitted runs** (5,473 job rows, 45 failing runs). cli/cli was stopped at **293 runs**
  (586 rows) once it was clear it has almost no failures. Both are the newest runs of the last 40 days (ingestion goes
  newest-first); stopping was decided on throughput, not on results.
* The 300-download log budget was consumed by the first 4-5 pydantic runs (146 cache events) and 300 cli/cli logs, so
  **cache labels are tiny** (pydantic 28 usable test rows, cli/cli 100 labelled rows).
* cli/cli has **1 failing run and 2 failed jobs** in the window. By the pre-stated rule (< 20 failing runs) it is
  *inconclusive* for every failure statement.

## Statement 1: does the readable skip policy generalise?

Pre-stated test: lift vs random < 1 **and** < 5% of failing runs would look green.

| repo | readable policy: time saved | failing runs that would look green | failure rate skipped vs rest | lift | verdict |
|---|---|---|---|---|---|
| pydantic/pydantic | 0.8% [0.6%, 1.0%] | 0 of 45 | 4.4% vs 3.6% | **1.20** | **does not generalise** (fails the lift condition) |
| cli/cli | 21.2% [17.3%, 25.5%] | 0 of 1 | 0.0% vs 0.4% | 0.00 on 2 failures | **inconclusive** (1 failing run) |

For the plain DAG-safe policy pydantic saves 27.3% [21.2%, 34.4%] of billable time but 4 of 45 failing runs (8.9%,
[2.0%, 17.8%]) would look green and the skipped jobs fail slightly *more* often than the rest (3.9% vs 3.6%, lift 1.07):
the same trade-off seen in fastapi and prometheus, and again nothing that selects safer jobs than chance. The readable
guard removes nearly all of pydantic's saving (27.3% -> 0.8%) because most of the jobs it would skip contain a step the
guard treats as unreadable (`make`, or a `uses:` outside the safe list). That is inferred from the size of the drop;
the per-job breakdown was not tabulated.

Reading: on a repository the rules were not built on, the graph does **not** identify jobs that fail less than average.
The savings that exist are real but unselective.

## Statement 2: do the graph features generalise?

Pre-stated test: the impacted flag must separate executed from skipped jobs in the direction the workflow's own gating
implies, or be reported as uninformative.

* **pydantic:** the workflow has no file-based gate (`paths_filter_edges` 0, `gated_jobs` 0); its skips come from event /
  branch conditions. Jobs with `impacted = 1` were skipped *more* often than `impacted = 0` (36.5% vs 23.8%; among the 13
  jobs that ever skip: 70.1% vs 64.2%). Wrong direction, **uninformative**, as for prometheus.
* **cli/cli:** no job is ever skipped, so agreement cannot be evaluated.
* Failure model, ablation: removing the structural features changes failure PR-AUC by +0.013 [-0.019, +0.058] in
  pydantic (includes 0) - no demonstrable value, consistent with the earlier finding that the effect on the failure
  model is indistinguishable from 0.
* One positive, descriptive signal: in cli/cli `cache_key_touched = 1` rows have a 68.4% cache-miss rate (n = 38) vs 3.2%
  (n = 62) otherwise, and the cache model with structural features scores PR-AUC 0.913 vs 0.839 without and 0.790 for the
  baseline - but on 50 test rows from 25 runs with intervals that include 0 (+0.077 [-0.105, +0.304]). A lead, not a
  result.

## Model evaluation on the held-out repositories (default settings)

| target | pydantic | cli/cli |
|---|---|---|
| failure (logistic, default) | PR-AUC 0.329 vs prior-rate baseline 0.227: +0.110 [+0.014, +0.234] (75 positives; base rate 4.0%) | no usable fold (2 positives) |
| failure_tree (original tree) | 0.174; vs baseline -0.054 [-0.177, +0.062] | no usable fold |
| failure_regularized_tree | 0.261; vs baseline +0.039 [-0.089, +0.180] | no usable fold |
| duration (raw seconds) | MAE 28.7 s vs baseline 25.7 s: significantly worse | 66.5 s vs 43.6 s: significantly worse |
| duration_ratio (default) | 24.6 s vs 25.7 s: significantly better (-1.04 [-1.77, -0.32]) | 50.1 s vs 43.6 s: significantly **worse** (+6.5) |
| cache any-miss | 28 rows, no usable fold | PR-AUC 0.913 vs baseline 0.790: +0.120 [-0.020, +0.318] (50 rows) |

Reading: these are consistent with the earlier results. The logistic default beats the naive failure baseline on the one
held-out repository that has enough failures (the same pattern as fastapi); the log-ratio duration target beats the
prior mean in one of the two (and loses in the other, as in dubbo); the raw-seconds model remains worse than the
baseline everywhere. One repository with usable failure labels is not a general claim. This is **not** the fresh-data
test of `docs/FROZEN_CHOICE.md` (that needs later runs of the original repositories); it is an independent repository
evaluated with the already-chosen default, so it is separate evidence that the default is not merely fitted to the
first five.

## Overall

| claim | held-out outcome |
|---|---|
| Graph skip policy picks jobs that fail less than average | **not supported** (pydantic lift 1.20 / 1.07); cli/cli inconclusive |
| Graph skip policy saves time without hiding failures | **not supported**: 27% saving costs 8.9% of failing runs (DAG-safe); the guard that avoids it saves 0.8% |
| Impacted-flag agrees with the workflow's own gating | **not evaluable / not supported**: no file-based gating in either repository |
| Logistic failure model beats the naive baseline | supported in pydantic (one repo); not testable in cli/cli |
| Log-ratio duration target beats the prior mean | mixed (pydantic yes, cli/cli no) |
| Cache-key-touched feature carries signal | lead in cli/cli, too few rows to claim |

These results strengthen the existing position (the advisory report is the only use of the graph skip policy, and the
report states its measured risks) and add a held-out limit to it: the graph does not help in workflows that do not state
their file-to-job relationships. Nothing here changes a default.
