# Dataset report — real GitHub Actions history

*Generated 2026-10-07 19:13 UTC by `gha-optimizer dataset-report` from database `sqlite:///gha_optimizer.db`. Every figure below was computed from the ingested rows at generation time; nothing is hand-entered.*

## 1. What this dataset is

One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from the cells that have one. Labels:

* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.
* **duration** — `completed_at − started_at` from the API, success/failure rows only.
* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — never defaulted.

Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. The split is temporal at run level.

## 2. Collection (what was actually fetched)

| repo | licence | language | runs listed | runs fully processed | job cells | run date range | API requests* | rate-limit waits* | failures* |
|---|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | MIT | Python | 607 | 600 | 30961 | 2026-05-16 07:11:05 UTC → 2026-10-06 15:57:53 UTC | 3740 | 0 | 0 |
| fastapi/fastapi | MIT | Python | 686 | 669 | 11357 | 2026-07-19 21:05:59 UTC → 2026-10-07 07:51:04 UTC | 4432 | 0 | 0 |
| prometheus/prometheus | Apache-2.0 | Go | 2168 | 725 | 61872 | 2026-07-19 09:39:10 UTC → 2026-10-07 05:49:30 UTC | 7051 | 6 | 0 |
| apache/dubbo | Apache-2.0 | Java | 301 | 300 | 8911 | 2026-06-15 06:58:21 UTC → 2026-10-06 08:56:03 UTC | 1702 | 6 | 0 |
| quarkusio/quarkus | Apache-2.0 | Java | 80 | 30 | 1180 | 2026-10-04 18:43:31 UTC → 2026-10-06 14:22:51 UTC | 759 | 0 | 0 |

\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).

### python-poetry/poetry

* workflows ingested: {'.github/workflows/tests.yaml': 607}
* triggering events: {'pull_request': 480, 'push': 127}
* diff source per run: {None: 7, 'commit': 127, 'compare_pr': 145, 'git_merge_base': 328} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 30961 total, 30961 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 1, 'failure': 775, 'skipped': 8734, 'success': 21451}
* log ledger: {'parsed_events': 4080, 'parsed_no_events': 1664, 'skipped_conclusion': 3039, 'skipped_not_cache_capable': 360} — 10662 cache events parsed from 2246.3 MB of logs

### fastapi/fastapi

* workflows ingested: {'.github/workflows/test.yml': 686}
* triggering events: {'pull_request': 535, 'push': 139, 'schedule': 12}
* diff source per run: {'commit': 151, 'compare_pr': 230, 'git_merge_base': 305} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 11357 total, 11353 mapped to a YAML job key (100.0%)
* cell conclusions: {None: 3, 'cancelled': 28, 'failure': 763, 'skipped': 520, 'success': 10043}
* log ledger: {'parsed_events': 530, 'parsed_no_events': 2539, 'skipped_budget': 280, 'skipped_conclusion': 199, 'skipped_not_cache_capable': 489} — 1047 cache events parsed from 736.6 MB of logs

### prometheus/prometheus

* workflows ingested: {'.github/workflows/ci.yml': 2168}
* triggering events: {'pull_request': 1780, 'push': 388}
* diff source per run: {None: 2, 'commit': 388, 'compare_pr': 706, 'git_merge_base': 1072} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 61872 total, 60506 mapped to a YAML job key (97.8%)
* cell conclusions: {'cancelled': 625, 'failure': 2299, 'skipped': 9082, 'success': 49866}
* log ledger: {'parsed_events': 1841, 'parsed_no_events': 528, 'skipped_budget': 11271, 'skipped_conclusion': 7832, 'skipped_not_cache_capable': 27721} — 3604 cache events parsed from 2274.7 MB of logs

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
| fastapi/fastapi | 3975 | 682 | -13 | 6 | 2026-07-19T21:05:59Z → 2026-10-07T07:51:04Z |
| prometheus/prometheus | 44507 | 2163 | -1438 | 22 | 2026-07-19T14:10:35Z → 2026-10-07T05:49:30Z |
| apache/dubbo | 4102 | 293 | 7 | 14 | 2026-06-15T06:58:26Z → 2026-10-06T08:56:03Z |
| quarkusio/quarkus | 504 | 24 | 6 | 21 | 2026-10-05T16:36:31Z → 2026-10-06T14:22:51Z |
| **total** | 56664 |  |  |  |  |

\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs could be mapped to a YAML job key.

## 4. Label distributions

### python-poetry/poetry

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 1798 | 143 | 8.0% | 1778 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 118 | 66 | 55.9% | 3458 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 1798 |  |  | mean 97.7s, median 6.0s, p90 286.0s, max 12353s |

**Failure rate: 7.95%** (143 of 1798 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 38, 'miss': 66, 'partial_hit': 14, nan: 3458}

### fastapi/fastapi

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 3433 | 259 | 7.5% | 542 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 233 | 89 | 38.2% | 3742 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 3433 |  |  | mean 70.3s, median 17.0s, p90 201.0s, max 616s |

**Failure rate: 7.54%** (259 of 3433 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 144, 'miss': 89, nan: 3742}

### prometheus/prometheus

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 35337 | 2047 | 5.8% | 9170 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 1083 | 803 | 74.1% | 43424 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 35337 |  |  | mean 435.1s, median 303.0s, p90 971.0s, max 10987s |

**Failure rate: 5.79%** (2047 of 35337 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 255, 'hit_unresolved': 25, 'miss': 803, nan: 43424}

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
| failure (1 = failure/timed_out) | 44672 | 2649 | 5.9% | 11992 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 1994 | 1160 | 58.2% | 54670 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 44672 |  |  | mean 396.3s, median 215.0s, p90 962.0s, max 17172s |

**Failure rate: 5.93%** (2649 of 44672 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 601, 'hit_unresolved': 27, 'miss': 1160, 'partial_hit': 206, nan: 54670}

## 5. Cache-label coverage

| repo | job cells | cells hit | cells miss | cells NULL | rows | rows labelled | rows NULL | row coverage |
|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 30961 | 2656 | 1424 | 26881 | 3576 | 118 | 3458 | 3.3% |
| fastapi/fastapi | 11357 | 144 | 386 | 10827 | 3975 | 233 | 3742 | 5.9% |
| prometheus/prometheus | 61872 | 572 | 1269 | 60031 | 44507 | 1083 | 43424 | 2.4% |
| apache/dubbo | 8911 | 906 | 92 | 7913 | 4102 | 340 | 3762 | 8.3% |
| quarkusio/quarkus | 1180 | 335 | 544 | 301 | 504 | 220 | 284 | 43.7% |

* **python-poetry/poetry** — why cells have no label (`cache_label_status`): {None: 21818, 'labelled': 4080, 'parsed_no_events': 1664, 'skipped_conclusion': 3039, 'skipped_not_cache_capable': 360}

* **fastapi/fastapi** — why cells have no label (`cache_label_status`): {None: 7320, 'labelled': 530, 'parsed_no_events': 2539, 'skipped_budget': 280, 'skipped_conclusion': 199, 'skipped_not_cache_capable': 489}

* **prometheus/prometheus** — why cells have no label (`cache_label_status`): {None: 12679, 'labelled': 1841, 'parsed_no_events': 528, 'skipped_budget': 11271, 'skipped_conclusion': 7832, 'skipped_not_cache_capable': 27721}

* **apache/dubbo** — why cells have no label (`cache_label_status`): {None: 7239, 'labelled': 998, 'parsed_no_events': 56, 'skipped_conclusion': 137, 'skipped_not_cache_capable': 481}

* **quarkusio/quarkus** — why cells have no label (`cache_label_status`): {'labelled': 879, 'parsed_no_events': 44, 'skipped_conclusion': 165, 'skipped_not_cache_capable': 91, 'unavailable': 1}


Status meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.

## 6. Missing values per feature (rows with NaN)

| feature | python-poetry/poetry | fastapi/fastapi | prometheus/prometheus | apache/dubbo | quarkusio/quarkus |
|---|---|---|---|---|---|
| files_changed_count | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_added | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_deleted | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| job_structurally_impacted | 611 (17.1%) | 717 (18.0%) | 18649 (41.9%) | 14 (0.3%) | 72 (14.3%) |
| cache_key_touched | 0 (0.0%) | 0 (0.0%) | 1825 (4.1%) | 1041 (25.4%) | 336 (66.7%) |
| dep_graph_distance | 2491 (69.7%) | 1934 (48.7%) | 25789 (57.9%) | 182 (4.4%) | 90 (17.9%) |
| historical_failure_rate | 131 (3.7%) | 6 (0.2%) | 573 (1.3%) | 14 (0.3%) | 55 (10.9%) |
| historical_mean_duration | 131 (3.7%) | 6 (0.2%) | 573 (1.3%) | 14 (0.3%) | 55 (10.9%) |
| recent_failure_streak | 131 (3.7%) | 6 (0.2%) | 573 (1.3%) | 14 (0.3%) | 55 (10.9%) |
| recent_success_streak | 131 (3.7%) | 6 (0.2%) | 573 (1.3%) | 14 (0.3%) | 55 (10.9%) |
| author_failure_rate | 594 (16.6%) | 674 (17.0%) | 3850 (8.7%) | 812 (19.8%) | 210 (41.7%) |
| cache_key_stability | 3417 (95.6%) | 2497 (62.8%) | 41620 (93.5%) | 3710 (90.4%) | 210 (41.7%) |
| hour_of_day | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| day_of_week | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| is_weekend | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |

Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. `author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph features were not computed for that run.

## 7. Graph-feature quality

Feature definition version 2. `impacted` = a changed file reaches the job through File -> (Directory | BuildTarget) -> Job edges and `needs`; **unknown** = the job has no file-level evidence in its graph (e.g. it only runs `make` targets or remote actions), which is NOT the same as unaffected.

| repo | (run, job) rows | no changed file in graph | job has file evidence | impacted = 1 | impacted = 0 | impacted unknown | cache touched = 1 | cache touched = 0 | cache unknown | distinct distances |
|---|---|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 3576 | 18 (0.5%) | 2980 (83.3%) | 1085 (30.3%) | 1880 (52.6%) | 611 (17.1%) | 102 | 3474 | 0 | 2 |
| fastapi/fastapi | 3975 | 42 (1.1%) | 3293 (82.8%) | 2041 (51.3%) | 1217 (30.6%) | 717 (18.0%) | 326 | 3649 | 0 | 2 |
| prometheus/prometheus | 44507 | 144 (0.3%) | 25942 (58.3%) | 18718 (42.1%) | 7140 (16.0%) | 18649 (41.9%) | 1345 | 41337 | 1825 | 3 |
| apache/dubbo | 4102 | 14 (0.3%) | 4102 (100.0%) | 3920 (95.6%) | 168 (4.1%) | 14 (0.3%) | 530 | 2531 | 1041 | 2 |
| quarkusio/quarkus | 504 | 0 (0.0%) | 432 (85.7%) | 414 (82.1%) | 18 (3.6%) | 72 (14.3%) | 0 | 168 | 336 | 2 |

### 7.1 Does `impacted` agree with what the workflow actually did?

Ground truth available without any model: whether a job was **skipped** (every matrix cell skipped) or **executed**.  A job that runs on every change is never skipped, so the all-jobs row is diluted by jobs the workflow does not gate; the second row restricts to jobs that were both skipped and executed in this repository (selected after the fact, so read it as a consistency check, not as a test of the rules).  Fisher exact p-values assume independent rows, which the data does not satisfy - they are optimistic.

| repo | scope | impacted=1 rows | of which skipped | impacted=0 rows | of which skipped | Fisher p |
|---|---|---|---|---|---|---|
| python-poetry/poetry | all jobs | 1085 | 1.2% | 1880 | 93.2% | 0 |
| python-poetry/poetry | jobs that skip at least once (4) | 614 | 2.1% | 1758 | 99.7% | 0 |
| fastapi/fastapi | all jobs | 2041 | 2.5% | 1217 | 37.3% | 2.1e-159 |
| fastapi/fastapi | jobs that skip at least once (3) | 1373 | 3.7% | 651 | 69.7% | 1.4e-227 |
| prometheus/prometheus | all jobs | 18718 | 19.0% | 7140 | 14.5% | 6.4e-18 |
| prometheus/prometheus | jobs that skip at least once (5) | 7080 | 50.2% | 3688 | 28.0% | 7.1e-111 |
| apache/dubbo | all jobs | 3920 | 5.3% | 168 | 2.4% | 0.11 |
| apache/dubbo | jobs that skip at least once (11) | 3080 | 6.8% | 132 | 3.0% | 0.11 |
| quarkusio/quarkus | all jobs | 414 | 33.8% | 18 | 5.6% | 0.0097 |
| quarkusio/quarkus | jobs that skip at least once (17) | 391 | 29.9% | 17 | 0.0% | 0.0045 |

### 7.2 Do the structural features associate with the labels the models predict?

Descriptive only (no model): the label rate of all labelled rows split by the feature value.  Rows are not independent (jobs of a run share a commit; consecutive runs share a PR), so differences are optimistic, and failure positives are few - read the counts, not just the percentages.

| repo | label | feature | feature = 1 | feature = 0 | feature unknown |
|---|---|---|---|---|---|
| python-poetry/poetry | failure rate | `job_structurally_impacted` | 13.1% (n=1071) | 1.6% (n=128) | 0.2% (n=599) |
| python-poetry/poetry | cache-miss rate | `cache_key_touched` | 0.0% (n=12) | 62.3% (n=106) | n/a |
| python-poetry/poetry | exact key not restored (miss or partial hit) | `cache_key_touched` | 100.0% (n=12) | 64.2% (n=106) | n/a |
| fastapi/fastapi | failure rate | `job_structurally_impacted` | 12.1% (n=1983) | 2.7% (n=750) | 0.0% (n=700) |
| fastapi/fastapi | cache-miss rate | `cache_key_touched` | 89.4% (n=66) | 18.0% (n=167) | n/a |
| fastapi/fastapi | exact key not restored (miss or partial hit) | `cache_key_touched` | 89.4% (n=66) | 18.0% (n=167) | n/a |
| prometheus/prometheus | failure rate | `job_structurally_impacted` | 7.4% (n=15144) | 3.5% (n=6068) | 5.0% (n=14125) |
| prometheus/prometheus | cache-miss rate | `cache_key_touched` | 90.0% (n=70) | 75.9% (n=755) | 64.7% (n=258) |
| prometheus/prometheus | exact key not restored (miss or partial hit) | `cache_key_touched` | 91.3% (n=69) | 76.9% (n=745) | 68.4% (n=244) |
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
| fastapi/fastapi | 2026-09-25T06:28:58Z | 3159 | 546 | 816 | 136 | 8.18% (226/2763) | 4.93% (33/670) |
| prometheus/prometheus | 2026-09-22T06:07:34Z | 35429 | 1730 | 9078 | 433 | 6.48% (1822/28118) | 3.12% (225/7219) |
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
* **fastapi/fastapi**: 4 of 11357 job cells (0.0%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **fastapi/fastapi**: runs come from several events {'pull_request': 535, 'push': 139, 'schedule': 12}; event type is not a model feature.
* **prometheus/prometheus**: 1366 of 61872 job cells (2.2%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **prometheus/prometheus**: runs come from several events {'pull_request': 1780, 'push': 388}; event type is not a model feature.
* **apache/dubbo**: runs come from several events {'pull_request': 243, 'push': 58}; event type is not a model feature.
* **quarkusio/quarkus**: only 34 failure positives — failure-model metrics will have wide uncertainty.
* **quarkusio/quarkus**: runs come from several events {'pull_request': 75, 'push': 1, 'schedule': 4}; event type is not a model feature.
