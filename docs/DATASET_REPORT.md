# Dataset report — real GitHub Actions history

*Generated 2026-10-07 07:02 UTC by `gha-optimizer dataset-report` from database `sqlite:///gha_optimizer.db`. Every figure below was computed from the ingested rows at generation time; nothing is hand-entered.*

## 1. What this dataset is

One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from the cells that have one. Labels:

* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.
* **duration** — `completed_at − started_at` from the API, success/failure rows only.
* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — never defaulted.

Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. The split is temporal at run level.

## 2. Collection (what was actually fetched)

| repo | licence | language | runs listed | runs fully processed | job cells | run date range | API requests* | rate-limit waits* | failures* |
|---|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | MIT | Python | 607 | 600 | 30961 | 2026-05-16 07:11:05 UTC → 2026-10-06 15:57:53 UTC | 2619 | 0 | 0 |
| fastapi/fastapi | MIT | Python | 640 | 600 | 9862 | 2026-07-27 00:09:12 UTC → 2026-10-06 21:07:24 UTC | 2684 | 0 | 0 |
| prometheus/prometheus | Apache-2.0 | Go | 611 | 600 | 17191 | 2026-09-16 19:30:17 UTC → 2026-10-06 23:31:13 UTC | 2053 | 6 | 0 |
| apache/dubbo | Apache-2.0 | Java | 301 | 300 | 8911 | 2026-06-15 06:58:21 UTC → 2026-10-06 08:56:03 UTC | 1702 | 6 | 0 |
| quarkusio/quarkus | Apache-2.0 | Java | 80 | 30 | 1180 | 2026-10-04 18:43:31 UTC → 2026-10-06 14:22:51 UTC | 759 | 0 | 0 |

\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).

### python-poetry/poetry

* workflows ingested: {'.github/workflows/tests.yaml': 607}
* triggering events: {'pull_request': 480, 'push': 127}
* diff source per run: {None: 7, 'commit': 127, 'compare_pr': 145, 'git_merge_base': 328} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 30961 total, 30961 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 1, 'failure': 775, 'skipped': 8734, 'success': 21451}
* log ledger: {'parsed_events': 3360, 'parsed_no_events': 1302, 'skipped_conclusion': 2745, 'skipped_not_cache_capable': 286} — 8741 cache events parsed from 1849.0 MB of logs

### fastapi/fastapi

* workflows ingested: {'.github/workflows/test.yml': 640}
* triggering events: {'pull_request': 497, 'push': 132, 'schedule': 11}
* diff source per run: {None: 40, 'commit': 127, 'compare_pr': 196, 'git_merge_base': 277} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 9862 total, 9860 mapped to a YAML job key (100.0%)
* cell conclusions: {None: 3, 'cancelled': 27, 'failure': 716, 'skipped': 488, 'success': 8628}
* log ledger: {'parsed_events': 292, 'parsed_no_events': 1277, 'skipped_conclusion': 146, 'skipped_not_cache_capable': 251} — 556 cache events parsed from 371.4 MB of logs

### prometheus/prometheus

* workflows ingested: {'.github/workflows/ci.yml': 611}
* triggering events: {'pull_request': 518, 'push': 93}
* diff source per run: {None: 11, 'commit': 92, 'compare_pr': 213, 'git_merge_base': 295} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 17191 total, 16587 mapped to a YAML job key (96.5%)
* cell conclusions: {'cancelled': 126, 'failure': 383, 'skipped': 2500, 'success': 14182}
* log ledger: {'parsed_events': 678, 'parsed_no_events': 191, 'skipped_conclusion': 525, 'skipped_not_cache_capable': 1530} — 1247 cache events parsed from 840.7 MB of logs

### apache/dubbo

* workflows ingested: {'.github/workflows/build-and-test-pr.yml': 301}
* triggering events: {'pull_request': 243, 'push': 58}
* diff source per run: {None: 1, 'commit': 57, 'compare_pr': 57, 'git_merge_base': 186} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 8911 total, 8911 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 328, 'failure': 300, 'skipped': 213, 'success': 8070}
* log ledger: {'parsed_events': 998, 'parsed_no_events': 56, 'skipped_conclusion': 137, 'skipped_not_cache_capable': 481} — 2306 cache events parsed from 8886.4 MB of logs

### quarkusio/quarkus

* workflows ingested: {'.github/workflows/ci-actions-incremental.yml': 80}
* triggering events: {'pull_request': 75, 'push': 1, 'schedule': 4}
* diff source per run: {None: 50, 'commit': 2, 'compare_pr': 6, 'git_merge_base': 22} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 1180 total, 1180 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 22, 'failure': 47, 'skipped': 143, 'success': 968}
* log ledger: {'parsed_events': 879, 'parsed_no_events': 44, 'skipped_conclusion': 165, 'skipped_not_cache_capable': 91, 'unavailable': 1} — 1526 cache events parsed from 1812.8 MB of logs

## 3. Dataset rows (run × job key)

| repo | rows | runs in dataset | processed runs not in dataset* | job keys | date range (run created_at) |
|---|---|---|---|---|---|
| python-poetry/poetry | 3576 | 596 | 4 | 6 | 2026-05-16T15:31:50Z → 2026-10-06T15:57:53Z |
| fastapi/fastapi | 3546 | 596 | 4 | 6 | 2026-07-27T17:25:04Z → 2026-10-06T21:07:24Z |
| prometheus/prometheus | 12587 | 600 | 0 | 22 | 2026-09-17T11:24:36Z → 2026-10-06T23:31:13Z |
| apache/dubbo | 4102 | 293 | 7 | 14 | 2026-06-15T06:58:26Z → 2026-10-06T08:56:03Z |
| quarkusio/quarkus | 504 | 24 | 6 | 21 | 2026-10-05T16:36:31Z → 2026-10-06T14:22:51Z |
| **total** | 24315 |  |  |  |  |

\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs could be mapped to a YAML job key.

## 4. Label distributions

### python-poetry/poetry

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 1798 | 143 | 8.0% | 1778 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 94 | 58 | 61.7% | 3482 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 1798 |  |  | mean 97.7s, median 6.0s, p90 286.0s, max 12353s |

**Failure rate: 7.95%** (143 of 1798 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 25, 'miss': 58, 'partial_hit': 11, nan: 3482}

### fastapi/fastapi

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 3037 | 242 | 8.0% | 509 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 122 | 52 | 42.6% | 3424 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 3037 |  |  | mean 69.3s, median 13.0s, p90 201.0s, max 616s |

**Failure rate: 7.97%** (242 of 3037 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 70, 'miss': 52, nan: 3424}

### prometheus/prometheus

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 10031 | 338 | 3.4% | 2556 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 394 | 344 | 87.3% | 12193 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 10031 |  |  | mean 465.3s, median 349.0s, p90 1051.0s, max 10987s |

**Failure rate: 3.37%** (338 of 10031 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 49, 'hit_unresolved': 1, 'miss': 344, nan: 12193}

### apache/dubbo

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 3755 | 166 | 4.4% | 347 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 340 | 92 | 27.1% | 3762 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 3755 |  |  | mean 367.1s, median 58.0s, p90 1086.0s, max 3154s |

**Failure rate: 4.42%** (166 of 3755 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 64, 'miss': 92, 'partial_hit': 184, nan: 3762}

### quarkusio/quarkus

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 349 | 34 | 9.7% | 155 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 220 | 110 | 50.0% | 284 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 349 |  |  | mean 1530.5s, median 280.0s, p90 4908.0s, max 17172s |

**Failure rate: 9.74%** (34 of 349 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 100, 'hit_unresolved': 2, 'miss': 110, 'partial_hit': 8, nan: 284}

### ALL REPOS

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 18970 | 923 | 4.9% | 5345 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 1170 | 656 | 56.1% | 23145 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 18970 |  |  | mean 367.2s, median 151.0s, p90 989.0s, max 17172s |

**Failure rate: 4.87%** (923 of 18970 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 308, 'hit_unresolved': 3, 'miss': 656, 'partial_hit': 203, nan: 23145}

## 5. Cache-label coverage

| repo | job cells | cells hit | cells miss | cells NULL | rows | rows labelled | rows NULL | row coverage |
|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 30961 | 2097 | 1263 | 27601 | 3576 | 94 | 3482 | 2.6% |
| fastapi/fastapi | 9862 | 70 | 222 | 9570 | 3546 | 122 | 3424 | 3.4% |
| prometheus/prometheus | 17191 | 99 | 579 | 16513 | 12587 | 394 | 12193 | 3.1% |
| apache/dubbo | 8911 | 906 | 92 | 7913 | 4102 | 340 | 3762 | 8.3% |
| quarkusio/quarkus | 1180 | 335 | 544 | 301 | 504 | 220 | 284 | 43.7% |

* **python-poetry/poetry** — why cells have no label (`cache_label_status`): {None: 23268, 'labelled': 3360, 'parsed_no_events': 1302, 'skipped_conclusion': 2745, 'skipped_not_cache_capable': 286}

* **fastapi/fastapi** — why cells have no label (`cache_label_status`): {None: 7896, 'labelled': 292, 'parsed_no_events': 1277, 'skipped_conclusion': 146, 'skipped_not_cache_capable': 251}

* **prometheus/prometheus** — why cells have no label (`cache_label_status`): {None: 14267, 'labelled': 678, 'parsed_no_events': 191, 'skipped_conclusion': 525, 'skipped_not_cache_capable': 1530}

* **apache/dubbo** — why cells have no label (`cache_label_status`): {None: 7239, 'labelled': 998, 'parsed_no_events': 56, 'skipped_conclusion': 137, 'skipped_not_cache_capable': 481}

* **quarkusio/quarkus** — why cells have no label (`cache_label_status`): {'labelled': 879, 'parsed_no_events': 44, 'skipped_conclusion': 165, 'skipped_not_cache_capable': 91, 'unavailable': 1}


Status meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.

## 6. Missing values per feature (rows with NaN)

| feature | python-poetry/poetry | fastapi/fastapi | prometheus/prometheus | apache/dubbo | quarkusio/quarkus |
|---|---|---|---|---|---|
| files_changed_count | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_added | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_deleted | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| job_structurally_impacted | 611 (17.1%) | 631 (17.8%) | 5397 (42.9%) | 14 (0.3%) | 72 (14.3%) |
| cache_key_touched | 0 (0.0%) | 0 (0.0%) | 527 (4.2%) | 1041 (25.4%) | 336 (66.7%) |
| dep_graph_distance | 2491 (69.7%) | 1792 (50.5%) | 6953 (55.2%) | 182 (4.4%) | 90 (17.9%) |
| historical_failure_rate | 131 (3.7%) | 6 (0.2%) | 439 (3.5%) | 14 (0.3%) | 55 (10.9%) |
| historical_mean_duration | 131 (3.7%) | 6 (0.2%) | 439 (3.5%) | 14 (0.3%) | 55 (10.9%) |
| recent_failure_streak | 131 (3.7%) | 6 (0.2%) | 439 (3.5%) | 14 (0.3%) | 55 (10.9%) |
| recent_success_streak | 131 (3.7%) | 6 (0.2%) | 439 (3.5%) | 14 (0.3%) | 55 (10.9%) |
| author_failure_rate | 594 (16.6%) | 611 (17.2%) | 1719 (13.7%) | 812 (19.8%) | 210 (41.7%) |
| cache_key_stability | 3446 (96.4%) | 3179 (89.7%) | 12175 (96.7%) | 3710 (90.4%) | 210 (41.7%) |
| hour_of_day | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| day_of_week | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| is_weekend | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |

Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. `author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph features were not computed for that run.

## 7. Graph-feature quality

Feature definition version 2. `impacted` = a changed file reaches the job through File -> (Directory | BuildTarget) -> Job edges and `needs`; **unknown** = the job has no file-level evidence in its graph (e.g. it only runs `make` targets or remote actions), which is NOT the same as unaffected.

| repo | (run, job) rows | no changed file in graph | job has file evidence | impacted = 1 | impacted = 0 | impacted unknown | cache touched = 1 | cache touched = 0 | cache unknown | distinct distances |
|---|---|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 3576 | 18 (0.5%) | 2980 (83.3%) | 1085 (30.3%) | 1880 (52.6%) | 611 (17.1%) | 102 | 3474 | 0 | 2 |
| fastapi/fastapi | 3546 | 42 (1.2%) | 2950 (83.2%) | 1754 (49.5%) | 1161 (32.7%) | 631 (17.8%) | 305 | 3241 | 0 | 2 |
| prometheus/prometheus | 12587 | 21 (0.2%) | 7202 (57.2%) | 5634 (44.8%) | 1556 (12.4%) | 5397 (42.9%) | 291 | 11769 | 527 | 3 |
| apache/dubbo | 4102 | 14 (0.3%) | 4102 (100.0%) | 3920 (95.6%) | 168 (4.1%) | 14 (0.3%) | 530 | 2531 | 1041 | 2 |
| quarkusio/quarkus | 504 | 0 (0.0%) | 432 (85.7%) | 414 (82.1%) | 18 (3.6%) | 72 (14.3%) | 0 | 168 | 336 | 2 |

### 7.1 Does `impacted` agree with what the workflow actually did?

Ground truth available without any model: whether a job was **skipped** (every matrix cell skipped) or **executed**.  A job that runs on every change is never skipped, so the all-jobs row is diluted by jobs the workflow does not gate; the second row restricts to jobs that were both skipped and executed in this repository (selected after the fact, so read it as a consistency check, not as a test of the rules).  Fisher exact p-values assume independent rows, which the data does not satisfy - they are optimistic.

| repo | scope | impacted=1 rows | of which skipped | impacted=0 rows | of which skipped | Fisher p |
|---|---|---|---|---|---|---|
| python-poetry/poetry | all jobs | 1085 | 1.2% | 1880 | 93.2% | 0 |
| python-poetry/poetry | jobs that skip at least once (4) | 614 | 2.1% | 1758 | 99.7% | 0 |
| fastapi/fastapi | all jobs | 1754 | 2.8% | 1161 | 36.5% | 3.9e-135 |
| fastapi/fastapi | jobs that skip at least once (3) | 1157 | 4.2% | 609 | 69.6% | 6.6e-197 |
| prometheus/prometheus | all jobs | 5634 | 18.8% | 1556 | 13.0% | 6.9e-08 |
| prometheus/prometheus | jobs that skip at least once (5) | 2108 | 50.3% | 886 | 22.9% | 2.2e-45 |
| apache/dubbo | all jobs | 3920 | 5.3% | 168 | 2.4% | 0.11 |
| apache/dubbo | jobs that skip at least once (11) | 3080 | 6.8% | 132 | 3.0% | 0.11 |
| quarkusio/quarkus | all jobs | 414 | 33.8% | 18 | 5.6% | 0.0097 |
| quarkusio/quarkus | jobs that skip at least once (17) | 391 | 29.9% | 17 | 0.0% | 0.0045 |

### 7.2 Do the structural features associate with the labels the models predict?

Descriptive only (no model): the label rate of all labelled rows split by the feature value.  Rows are not independent (jobs of a run share a commit; consecutive runs share a PR), so differences are optimistic, and failure positives are few - read the counts, not just the percentages.

| repo | label | feature | feature = 1 | feature = 0 | feature unknown |
|---|---|---|---|---|---|
| python-poetry/poetry | failure rate | `job_structurally_impacted` | 13.1% (n=1071) | 1.6% (n=128) | 0.2% (n=599) |
| python-poetry/poetry | cache-miss rate | `cache_key_touched` | 0.0% (n=9) | 68.2% (n=85) | n/a |
| python-poetry/poetry | exact key not restored (miss or partial hit) | `cache_key_touched` | 100.0% (n=9) | 70.6% (n=85) | n/a |
| fastapi/fastapi | failure rate | `job_structurally_impacted` | 13.1% (n=1699) | 2.8% (n=724) | 0.0% (n=614) |
| fastapi/fastapi | cache-miss rate | `cache_key_touched` | 86.5% (n=37) | 23.5% (n=85) | n/a |
| fastapi/fastapi | exact key not restored (miss or partial hit) | `cache_key_touched` | 86.5% (n=37) | 23.5% (n=85) | n/a |
| prometheus/prometheus | failure rate | `job_structurally_impacted` | 4.7% (n=4574) | 2.6% (n=1314) | 2.1% (n=4143) |
| prometheus/prometheus | cache-miss rate | `cache_key_touched` | 79.2% (n=24) | 88.5% (n=279) | 85.7% (n=91) |
| prometheus/prometheus | exact key not restored (miss or partial hit) | `cache_key_touched` | 79.2% (n=24) | 88.8% (n=278) | 85.7% (n=91) |
| apache/dubbo | failure rate | `job_structurally_impacted` | 4.4% (n=3594) | 5.4% (n=147) | 0.0% (n=14) |
| apache/dubbo | cache-miss rate | `cache_key_touched` | 20.0% (n=115) | 66.2% (n=74) | 13.2% (n=151) |
| apache/dubbo | exact key not restored (miss or partial hit) | `cache_key_touched` | 97.4% (n=115) | 100.0% (n=74) | 59.6% (n=151) |
| quarkusio/quarkus | failure rate | `job_structurally_impacted` | 12.2% (n=263) | 11.8% (n=17) | 0.0% (n=69) |
| quarkusio/quarkus | cache-miss rate | `cache_key_touched` | n/a | n/a | 50.0% (n=220) |
| quarkusio/quarkus | exact key not restored (miss or partial hit) | `cache_key_touched` | n/a | n/a | 54.1% (n=218) |

## 8. Temporal train/test split

| repo | test period starts (run created_at) | train rows | train runs | test rows | test runs | failure rate train | failure rate test |
|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 2026-09-19T17:58:19Z | 2862 | 477 | 714 | 119 | 8.11% (119/1468) | 7.27% (24/330) |
| fastapi/fastapi | 2026-09-29T10:20:00Z | 2832 | 477 | 714 | 119 | 9.09% (223/2454) | 3.26% (19/583) |
| prometheus/prometheus | 2026-10-02T14:40:14Z | 10073 | 480 | 2514 | 120 | 3.68% (297/8078) | 2.10% (41/1953) |
| apache/dubbo | 2026-09-06T00:31:56Z | 3276 | 234 | 826 | 59 | 4.22% (128/3030) | 5.24% (38/725) |
| quarkusio/quarkus | 2026-10-06T14:14:53Z | 399 | 19 | 105 | 5 | 8.24% (23/279) | 15.71% (11/70) |

Test fraction = last 20% of runs by creation time (`config.model.temporal_test_fraction`); all jobs of a run are on the same side; the split is never random.

## 9. What this data does NOT support

* Rows are **not independent**: jobs of one run, and consecutive runs of one PR, share a commit lineage. Treat any metric as optimistic by that margin.
* Cache labels exist only inside GitHub's log-retention window (measured ≈45 days for quarkus, ≈90 days for the others); older runs contribute failure/duration labels only.
* A PR run's changed-file set is the whole-PR diff (API compare, or merge-base..head from the git mirror, assuming the PR targets the default branch); runs whose PR could not be determined, and multi-commit pushes, fall back to the head commit's diff (see `diff_source`).
* Only the **latest attempt** of a re-run is ingested.
* `cancelled` jobs (mostly concurrency cancel-in-progress) and `skipped` jobs carry no failure or duration label; cancellation is not modelled.
* The scheduler's cost / energy / CO₂ constants are configuration assumptions, not measurements from this data.
* **python-poetry/poetry**: runs come from several events {'pull_request': 480, 'push': 127}; event type is not a model feature.
* **fastapi/fastapi**: 2 of 9862 job cells (0.0%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **fastapi/fastapi**: runs come from several events {'pull_request': 497, 'push': 132, 'schedule': 11}; event type is not a model feature.
* **prometheus/prometheus**: 604 of 17191 job cells (3.5%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **prometheus/prometheus**: runs come from several events {'pull_request': 518, 'push': 93}; event type is not a model feature.
* **apache/dubbo**: runs come from several events {'pull_request': 243, 'push': 58}; event type is not a model feature.
* **quarkusio/quarkus**: only 34 failure positives — failure-model metrics will have wide uncertainty.
* **quarkusio/quarkus**: runs come from several events {'pull_request': 75, 'push': 1, 'schedule': 4}; event type is not a model feature.
