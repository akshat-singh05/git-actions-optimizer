# Dataset report — real GitHub Actions history

*Generated 2026-10-09 21:49 UTC by `gha-optimizer dataset-report` from database `sqlite:///gha_optimizer.db`. Every figure below was computed from the ingested rows at generation time; nothing is hand-entered.*

## 1. What this dataset is

One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from the cells that have one. Labels:

* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.
* **duration** — `completed_at − started_at` from the API, success/failure rows only.
* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — never defaulted.

Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. The split is temporal at run level.

## 2. Collection (what was actually fetched)

| repo | licence | language | runs listed | runs fully processed | job cells | run date range | API requests* | rate-limit waits* | failures* |
|---|---|---|---|---|---|---|---|---|---|
| pydantic/pydantic | MIT | Python | 208 | 8 | 16152 | 2026-09-24 18:39:41 UTC → 2026-10-09 14:21:31 UTC | 0 | 0 | 0 |
| cli/cli | MIT | Go | 334 | 82 | 1764 | 2026-09-02 20:15:42 UTC → 2026-10-09 14:47:48 UTC | 0 | 0 | 0 |

\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).

### pydantic/pydantic

* workflows ingested: {'.github/workflows/ci.yml': 208}
* triggering events: {'pull_request': 145, 'push': 63}
* diff source per run: {None: 8, 'commit': 60, 'compare_pr': 100, 'git_merge_base': 40} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 16152 total, 16076 mapped to a YAML job key (99.5%)
* cell conclusions: {'cancelled': 3, 'failure': 433, 'skipped': 1764, 'success': 13952}
* log ledger: {'parsed_events': 120, 'parsed_no_events': 180, 'skipped_budget': 13423, 'skipped_conclusion': 1757, 'skipped_not_cache_capable': 606} — 146 cache events parsed from 45.1 MB of logs

### cli/cli

* workflows ingested: {'.github/workflows/go.yml': 334}
* triggering events: {'pull_request': 256, 'push': 78}
* diff source per run: {None: 9, 'commit': 75, 'compare_pr': 155, 'git_merge_base': 95} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 1764 total, 1758 mapped to a YAML job key (99.7%)
* cell conclusions: {'failure': 6, 'success': 1758}
* log ledger: {'parsed_events': 300, 'skipped_budget': 1458} — 517 cache events parsed from 22.7 MB of logs

## 3. Dataset rows (run × job key)

| repo | rows | runs in dataset | processed runs not in dataset* | job keys | date range (run created_at) |
|---|---|---|---|---|---|
| pydantic/pydantic | 5473 | 196 | -188 | 28 | 2026-09-25T01:31:28Z → 2026-10-09T14:21:31Z |
| cli/cli | 586 | 293 | -211 | 2 | 2026-09-03T11:25:01Z → 2026-10-09T14:47:48Z |
| **total** | 6059 |  |  |  |  |

\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs could be mapped to a YAML job key.

## 4. Label distributions

### pydantic/pydantic

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 3726 | 136 | 3.7% | 1747 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 28 | 26 | 92.9% | 5445 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 3726 |  |  | mean 165.0s, median 118.0s, p90 366.0s, max 9317s |

**Failure rate: 3.65%** (136 of 3726 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 2, 'miss': 26, nan: 5445}

### cli/cli

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 586 | 2 | 0.3% | 0 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 100 | 28 | 28.0% | 486 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 586 |  |  | mean 381.0s, median 322.5s, p90 633.0s, max 992s |

**Failure rate: 0.34%** (2 of 586 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 72, 'miss': 28, nan: 486}

### ALL REPOS

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 4312 | 138 | 3.2% | 1747 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 128 | 54 | 42.2% | 5931 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 4312 |  |  | mean 194.4s, median 138.0s, p90 445.7s, max 9317s |

**Failure rate: 3.20%** (138 of 4312 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 74, 'miss': 54, nan: 5931}

## 5. Cache-label coverage

| repo | job cells | cells hit | cells miss | cells NULL | rows | rows labelled | rows NULL | row coverage |
|---|---|---|---|---|---|---|---|---|
| pydantic/pydantic | 16152 | 12 | 108 | 16032 | 5473 | 28 | 5445 | 0.5% |
| cli/cli | 1764 | 217 | 83 | 1464 | 586 | 100 | 486 | 17.1% |

* **pydantic/pydantic** — why cells have no label (`cache_label_status`): {None: 66, 'labelled': 120, 'parsed_no_events': 180, 'skipped_budget': 13423, 'skipped_conclusion': 1757, 'skipped_not_cache_capable': 606}

* **cli/cli** — why cells have no label (`cache_label_status`): {None: 6, 'labelled': 300, 'skipped_budget': 1458}


Status meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.

## 6. Missing values per feature (rows with NaN)

| feature | pydantic/pydantic | cli/cli |
|---|---|---|
| files_changed_count | 0 (0.0%) | 0 (0.0%) |
| loc_added | 0 (0.0%) | 0 (0.0%) |
| loc_deleted | 0 (0.0%) | 0 (0.0%) |
| job_structurally_impacted | 196 (3.6%) | 0 (0.0%) |
| cache_key_touched | 0 (0.0%) | 0 (0.0%) |
| dep_graph_distance | 1643 (30.0%) | 158 (27.0%) |
| historical_failure_rate | 809 (14.8%) | 2 (0.3%) |
| historical_mean_duration | 809 (14.8%) | 2 (0.3%) |
| recent_failure_streak | 809 (14.8%) | 2 (0.3%) |
| recent_success_streak | 809 (14.8%) | 2 (0.3%) |
| author_failure_rate | 783 (14.3%) | 26 (4.4%) |
| cache_key_stability | 5435 (99.3%) | 488 (83.3%) |
| hour_of_day | 0 (0.0%) | 0 (0.0%) |
| day_of_week | 0 (0.0%) | 0 (0.0%) |
| is_weekend | 0 (0.0%) | 0 (0.0%) |

Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. `author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph features were not computed for that run.

## 7. Graph-feature quality

Feature definition version 2. `impacted` = a changed file reaches the job through File -> (Directory | BuildTarget) -> Job edges and `needs`; **unknown** = the job has no file-level evidence in its graph (e.g. it only runs `make` targets or remote actions), which is NOT the same as unaffected.

| repo | (run, job) rows | no changed file in graph | job has file evidence | impacted = 1 | impacted = 0 | impacted unknown | cache touched = 1 | cache touched = 0 | cache unknown | distinct distances |
|---|---|---|---|---|---|---|---|---|---|---|
| pydantic/pydantic | 5473 | 0 (0.0%) | 5277 (96.4%) | 3830 (70.0%) | 1447 (26.4%) | 196 (3.6%) | 194 | 5279 | 0 | 5 |
| cli/cli | 586 | 0 (0.0%) | 586 (100.0%) | 428 (73.0%) | 158 (27.0%) | 0 (0.0%) | 156 | 430 | 0 | 1 |

### 7.1 Does `impacted` agree with what the workflow actually did?

Ground truth available without any model: whether a job was **skipped** (every matrix cell skipped) or **executed**.  A job that runs on every change is never skipped, so the all-jobs row is diluted by jobs the workflow does not gate; the second row restricts to jobs that were both skipped and executed in this repository (selected after the fact, so read it as a consistency check, not as a test of the rules).  Fisher exact p-values assume independent rows, which the data does not satisfy - they are optimistic.

| repo | scope | impacted=1 rows | of which skipped | impacted=0 rows | of which skipped | Fisher p |
|---|---|---|---|---|---|---|
| pydantic/pydantic | all jobs | 3830 | 36.5% | 1447 | 23.8% | 6.5e-19 |
| pydantic/pydantic | jobs that skip at least once (13) | 1996 | 70.1% | 537 | 64.2% | 0.01 |
| cli/cli | all jobs | 428 | 0.0% | 158 | 0.0% | 1 |

### 7.2 Do the structural features associate with the labels the models predict?

Descriptive only (no model): the label rate of all labelled rows split by the feature value.  Rows are not independent (jobs of a run share a commit; consecutive runs share a PR), so differences are optimistic, and failure positives are few - read the counts, not just the percentages.

| repo | label | feature | feature = 1 | feature = 0 | feature unknown |
|---|---|---|---|---|---|
| pydantic/pydantic | failure rate | `job_structurally_impacted` | 3.5% (n=2428) | 4.5% (n=1102) | 0.5% (n=196) |
| pydantic/pydantic | cache-miss rate | `cache_key_touched` | n/a | 92.9% (n=28) | n/a |
| pydantic/pydantic | exact key not restored (miss or partial hit) | `cache_key_touched` | n/a | 92.9% (n=28) | n/a |
| cli/cli | failure rate | `job_structurally_impacted` | 0.5% (n=428) | 0.0% (n=158) | n/a |
| cli/cli | cache-miss rate | `cache_key_touched` | 68.4% (n=38) | 3.2% (n=62) | n/a |
| cli/cli | exact key not restored (miss or partial hit) | `cache_key_touched` | 68.4% (n=38) | 3.2% (n=62) | n/a |

## 8. Temporal train/test split

| repo | test period starts (run created_at) | train rows | train runs | test rows | test runs | failure rate train | failure rate test |
|---|---|---|---|---|---|---|---|
| pydantic/pydantic | 2026-10-07T19:05:05Z | 4382 | 157 | 1091 | 39 | 4.01% (119/2969) | 2.25% (17/757) |
| cli/cli | 2026-09-28T14:04:27Z | 468 | 234 | 118 | 59 | 0.00% (0/468) | 1.69% (2/118) |

Test fraction = last 20% of runs by creation time (`config.model.temporal_test_fraction`); all jobs of a run are on the same side; the split is never random.

## 9. What this data does NOT support

* Rows are **not independent**: jobs of one run, and consecutive runs of one PR, share a commit lineage. Treat any metric as optimistic by that margin.
* Cache labels exist only inside GitHub's log-retention window (measured ≈45 days for quarkus, ≈90 days for the others); older runs contribute failure/duration labels only.
* A PR run's changed-file set is the whole-PR diff (API compare, or merge-base..head from the git mirror, assuming the PR targets the default branch); runs whose PR could not be determined, and multi-commit pushes, fall back to the head commit's diff (see `diff_source`).
* Only the **latest attempt** of a re-run is ingested.
* `cancelled` jobs (mostly concurrency cancel-in-progress) and `skipped` jobs carry no failure or duration label; cancellation is not modelled.
* The scheduler's cost / energy / CO₂ constants are configuration assumptions, not measurements from this data.
* **pydantic/pydantic**: cache labels insufficient for a cache-miss model (28 labelled rows, classes [0.0, 1.0]); the pipeline will decline to predict.
* **pydantic/pydantic**: 76 of 16152 job cells (0.5%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **pydantic/pydantic**: runs come from several events {'pull_request': 145, 'push': 63}; event type is not a model feature.
* **cli/cli**: only 2 failure positives — failure-model metrics will have wide uncertainty.
* **cli/cli**: 6 of 1764 job cells (0.3%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **cli/cli**: runs come from several events {'pull_request': 256, 'push': 78}; event type is not a model feature.
