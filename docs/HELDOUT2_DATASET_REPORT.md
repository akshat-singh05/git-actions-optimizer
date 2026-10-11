# Dataset report — real GitHub Actions history

*Generated 2026-10-11 11:48 UTC by `gha-optimizer dataset-report` from database `sqlite:///gha_optimizer.db`. Every figure below was computed from the ingested rows at generation time; nothing is hand-entered.*

## 1. What this dataset is

One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from the cells that have one. Labels:

* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.
* **duration** — `completed_at − started_at` from the API, success/failure rows only.
* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — never defaulted.

Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. The split is temporal at run level.

## 2. Collection (what was actually fetched)

| repo | licence | language | runs listed | runs fully processed | job cells | run date range | API requests* | rate-limit waits* | failures* |
|---|---|---|---|---|---|---|---|---|---|
| astral-sh/ruff | MIT | Rust | 309 | 198 | 8339 | 2026-10-07 18:50:25 UTC → 2026-10-11 01:46:31 UTC | 412 | 0 | 0 |
| astral-sh/uv | Apache-2.0 | Rust | 221 | 150 | 12707 | 2026-10-08 18:51:38 UTC → 2026-10-11 02:06:12 UTC | 419 | 0 | 0 |

\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).

### astral-sh/ruff

* workflows ingested: {'.github/workflows/ci.yaml': 309}
* triggering events: {'pull_request': 281, 'push': 25, 'workflow_dispatch': 3}
* diff source per run: {None: 111, 'commit': 8, 'compare_pr': 132, 'git_merge_base': 58} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 8339 total, 8339 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 1099, 'failure': 284, 'skipped': 1113, 'success': 5843}
* log ledger: {} — 0 cache events parsed from 0.0 MB of logs

### astral-sh/uv

* workflows ingested: {'.github/workflows/ci.yml': 221}
* triggering events: {'pull_request': 167, 'push': 53, 'workflow_dispatch': 1}
* diff source per run: {None: 71, 'commit': 45, 'compare_pr': 84, 'git_merge_base': 21} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 12707 total, 12707 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 284, 'failure': 91, 'skipped': 1681, 'success': 10651}
* log ledger: {} — 0 cache events parsed from 0.0 MB of logs

## 3. Dataset rows (run × job key)

| repo | rows | runs in dataset | processed runs not in dataset* | job keys | date range (run created_at) |
|---|---|---|---|---|---|
| astral-sh/ruff | 5962 | 183 | 15 | 33 | 2026-10-08T19:24:44Z → 2026-10-11T01:35:40Z |
| astral-sh/uv | 3700 | 148 | 2 | 25 | 2026-10-09T16:58:38Z → 2026-10-11T02:06:12Z |
| **total** | 9662 |  |  |  |  |

\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs could be mapped to a YAML job key.

## 4. Label distributions

### astral-sh/ruff

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 4198 | 268 | 6.4% | 1764 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 0 | 0 | n/a | 5962 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 4198 |  |  | mean 187.4s, median 146.0s, p90 374.3s, max 1500s |

**Failure rate: 6.38%** (268 of 4198 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {nan: 5962}

### astral-sh/uv

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 2190 | 65 | 3.0% | 1510 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 0 | 0 | n/a | 3700 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 2190 |  |  | mean 182.5s, median 80.5s, p90 375.0s, max 5667s |

**Failure rate: 2.97%** (65 of 2190 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {nan: 3700}

### ALL REPOS

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 6388 | 333 | 5.2% | 3274 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 0 | 0 | n/a | 9662 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 6388 |  |  | mean 185.7s, median 130.0s, p90 375.0s, max 5667s |

**Failure rate: 5.21%** (333 of 6388 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {nan: 9662}

## 5. Cache-label coverage

| repo | job cells | cells hit | cells miss | cells NULL | rows | rows labelled | rows NULL | row coverage |
|---|---|---|---|---|---|---|---|---|
| astral-sh/ruff | 8339 | 0 | 0 | 8339 | 5962 | 0 | 5962 | 0.0% |
| astral-sh/uv | 12707 | 0 | 0 | 12707 | 3700 | 0 | 3700 | 0.0% |

* **astral-sh/ruff** — why cells have no label (`cache_label_status`): {None: 8339}

* **astral-sh/uv** — why cells have no label (`cache_label_status`): {None: 12707}


Status meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.

## 6. Missing values per feature (rows with NaN)

| feature | astral-sh/ruff | astral-sh/uv |
|---|---|---|
| files_changed_count | 0 (0.0%) | 0 (0.0%) |
| loc_added | 0 (0.0%) | 0 (0.0%) |
| loc_deleted | 0 (0.0%) | 0 (0.0%) |
| job_structurally_impacted | 0 (0.0%) | 25 (0.7%) |
| cache_key_touched | 0 (0.0%) | 0 (0.0%) |
| dep_graph_distance | 5072 (85.1%) | 2171 (58.7%) |
| historical_failure_rate | 365 (6.1%) | 378 (10.2%) |
| historical_mean_duration | 365 (6.1%) | 378 (10.2%) |
| recent_failure_streak | 365 (6.1%) | 378 (10.2%) |
| recent_success_streak | 365 (6.1%) | 378 (10.2%) |
| author_failure_rate | 618 (10.4%) | 250 (6.8%) |
| cache_key_stability | 5962 (100.0%) | 3700 (100.0%) |
| hour_of_day | 0 (0.0%) | 0 (0.0%) |
| day_of_week | 0 (0.0%) | 0 (0.0%) |
| is_weekend | 0 (0.0%) | 0 (0.0%) |

Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. `author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph features were not computed for that run.

## 7. Graph-feature quality

Feature definition version 2. `impacted` = a changed file reaches the job through File -> (Directory | BuildTarget) -> Job edges and `needs`; **unknown** = the job has no file-level evidence in its graph (e.g. it only runs `make` targets or remote actions), which is NOT the same as unaffected.

| repo | (run, job) rows | no changed file in graph | job has file evidence | impacted = 1 | impacted = 0 | impacted unknown | cache touched = 1 | cache touched = 0 | cache unknown | distinct distances |
|---|---|---|---|---|---|---|---|---|---|---|
| astral-sh/ruff | 5962 | 0 (0.0%) | 5962 (100.0%) | 890 (14.9%) | 5072 (85.1%) | 0 (0.0%) | 108 | 5854 | 0 | 3 |
| astral-sh/uv | 3700 | 25 (0.7%) | 3700 (100.0%) | 1529 (41.3%) | 2146 (58.0%) | 25 (0.7%) | 60 | 3640 | 0 | 4 |

### 7.1 Does `impacted` agree with what the workflow actually did?

Ground truth available without any model: whether a job was **skipped** (every matrix cell skipped) or **executed**.  A job that runs on every change is never skipped, so the all-jobs row is diluted by jobs the workflow does not gate; the second row restricts to jobs that were both skipped and executed in this repository (selected after the fact, so read it as a consistency check, not as a test of the rules).  Fisher exact p-values assume independent rows, which the data does not satisfy - they are optimistic.

| repo | scope | impacted=1 rows | of which skipped | impacted=0 rows | of which skipped | Fisher p |
|---|---|---|---|---|---|---|
| astral-sh/ruff | all jobs | 890 | 13.6% | 5072 | 16.9% | 0.016 |
| astral-sh/ruff | jobs that skip at least once (14) | 454 | 26.7% | 2106 | 40.6% | 2e-08 |
| astral-sh/uv | all jobs | 1529 | 33.9% | 2146 | 40.9% | 1.6e-05 |
| astral-sh/uv | jobs that skip at least once (22) | 1356 | 29.6% | 1878 | 37.2% | 7.5e-06 |

### 7.2 Do the structural features associate with the labels the models predict?

Descriptive only (no model): the label rate of all labelled rows split by the feature value.  Rows are not independent (jobs of a run share a commit; consecutive runs share a PR), so differences are optimistic, and failure positives are few - read the counts, not just the percentages.

| repo | label | feature | feature = 1 | feature = 0 | feature unknown |
|---|---|---|---|---|---|
| astral-sh/ruff | failure rate | `job_structurally_impacted` | 13.5% (n=577) | 5.2% (n=3621) | n/a |
| astral-sh/ruff | cache-miss rate | `cache_key_touched` | n/a | n/a | n/a |
| astral-sh/ruff | exact key not restored (miss or partial hit) | `cache_key_touched` | n/a | n/a | n/a |
| astral-sh/uv | failure rate | `job_structurally_impacted` | 4.1% (n=967) | 2.1% (n=1206) | 0.0% (n=17) |
| astral-sh/uv | cache-miss rate | `cache_key_touched` | n/a | n/a | n/a |
| astral-sh/uv | exact key not restored (miss or partial hit) | `cache_key_touched` | n/a | n/a | n/a |

## 8. Temporal train/test split

| repo | test period starts (run created_at) | train rows | train runs | test rows | test runs | failure rate train | failure rate test |
|---|---|---|---|---|---|---|---|
| astral-sh/ruff | 2026-10-09T20:12:55Z | 4778 | 146 | 1184 | 37 | 7.00% (230/3285) | 4.16% (38/913) |
| astral-sh/uv | 2026-10-09T20:49:26Z | 2950 | 118 | 750 | 30 | 2.83% (49/1731) | 3.49% (16/459) |

Test fraction = last 20% of runs by creation time (`config.model.temporal_test_fraction`); all jobs of a run are on the same side; the split is never random.

## 9. What this data does NOT support

* Rows are **not independent**: jobs of one run, and consecutive runs of one PR, share a commit lineage. Treat any metric as optimistic by that margin.
* Cache labels exist only inside GitHub's log-retention window (measured ≈45 days for quarkus, ≈90 days for the others); older runs contribute failure/duration labels only.
* A PR run's changed-file set is the whole-PR diff (API compare, or merge-base..head from the git mirror, assuming the PR targets the default branch); runs whose PR could not be determined, and multi-commit pushes, fall back to the head commit's diff (see `diff_source`).
* Only the **latest attempt** of a re-run is ingested.
* `cancelled` jobs (mostly concurrency cancel-in-progress) and `skipped` jobs carry no failure or duration label; cancellation is not modelled.
* The scheduler's cost / energy / CO₂ constants are configuration assumptions, not measurements from this data.
* **astral-sh/ruff**: cache labels insufficient for a cache-miss model (0 labelled rows, classes []); the pipeline will decline to predict.
* **astral-sh/ruff**: runs come from several events {'pull_request': 281, 'push': 25, 'workflow_dispatch': 3}; event type is not a model feature.
* **astral-sh/uv**: only 65 failure positives — failure-model metrics will have wide uncertainty.
* **astral-sh/uv**: cache labels insufficient for a cache-miss model (0 labelled rows, classes []); the pipeline will decline to predict.
* **astral-sh/uv**: runs come from several events {'pull_request': 167, 'push': 53, 'workflow_dispatch': 1}; event type is not a model feature.
