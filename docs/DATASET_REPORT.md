# Dataset report — real GitHub Actions history

*Generated 2026-10-06 22:55 UTC by `gha-optimizer dataset-report` from database `sqlite:///gha_optimizer.db`. Every figure below was computed from the ingested rows at generation time; nothing is hand-entered.*

## 1. What this dataset is

One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from the cells that have one. Labels:

* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.
* **duration** — `completed_at − started_at` from the API, success/failure rows only.
* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — never defaulted.

Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. The split is temporal at run level.

## 2. Collection (what was actually fetched)

| repo | licence | language | runs listed | runs fully processed | job cells | run date range | API requests* | rate-limit waits* | failures* |
|---|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | MIT | Python | 138 | 130 | 7634 | 2026-09-19 07:07:03 UTC → 2026-10-06 14:11:53 UTC | 1388 | 0 | 0 |
| fastapi/fastapi | MIT | Python | 120 | 120 | 1840 | 2026-09-28 00:01:37 UTC → 2026-10-06 13:50:06 UTC | 1409 | 0 | 0 |
| prometheus/prometheus | Apache-2.0 | Go | 113 | 100 | 2814 | 2026-10-03 09:29:59 UTC → 2026-10-06 15:17:05 UTC | 916 | 6 | 0 |
| apache/dubbo | Apache-2.0 | Java | 62 | 60 | 1672 | 2026-09-06 00:31:56 UTC → 2026-10-06 08:56:03 UTC | 1070 | 6 | 0 |
| quarkusio/quarkus | Apache-2.0 | Java | 80 | 30 | 1180 | 2026-10-04 18:43:31 UTC → 2026-10-06 14:22:51 UTC | 759 | 0 | 0 |

\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).

### python-poetry/poetry

* workflows ingested: {'.github/workflows/tests.yaml': 138}
* triggering events: {'pull_request': 111, 'push': 27}
* diff source per run: {None: 8, 'commit': 23, 'compare_pr': 23, 'git_merge_base': 84} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 7634 total, 7634 mapped to a YAML job key (100.0%)
* cell conclusions: {'failure': 111, 'skipped': 2724, 'success': 4799}
* log ledger: {'parsed_events': 3324, 'parsed_no_events': 1302, 'skipped_conclusion': 2724, 'skipped_not_cache_capable': 284} — 8651 cache events parsed from 1830.5 MB of logs

### fastapi/fastapi

* workflows ingested: {'.github/workflows/test.yml': 120}
* triggering events: {'pull_request': 83, 'push': 35, 'schedule': 2}
* diff source per run: {'commit': 37, 'compare_pr': 59, 'git_merge_base': 24} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 1840 total, 1840 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 15, 'failure': 62, 'skipped': 128, 'success': 1635}
* log ledger: {'parsed_events': 271, 'parsed_no_events': 1189, 'skipped_conclusion': 143, 'skipped_not_cache_capable': 237} — 519 cache events parsed from 344.6 MB of logs

### prometheus/prometheus

* workflows ingested: {'.github/workflows/ci.yml': 113}
* triggering events: {'pull_request': 107, 'push': 6}
* diff source per run: {None: 13, 'commit': 5, 'compare_pr': 36, 'git_merge_base': 59} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 2814 total, 2708 mapped to a YAML job key (96.2%)
* cell conclusions: {'cancelled': 84, 'failure': 39, 'skipped': 423, 'success': 2268}
* log ledger: {'parsed_events': 651, 'parsed_no_events': 183, 'skipped_conclusion': 507, 'skipped_not_cache_capable': 1473} — 1194 cache events parsed from 807.3 MB of logs

### apache/dubbo

* workflows ingested: {'.github/workflows/build-and-test-pr.yml': 62}
* triggering events: {'pull_request': 53, 'push': 9}
* diff source per run: {None: 2, 'commit': 9, 'compare_pr': 19, 'git_merge_base': 32} (`compare_pr` = API compare of the whole PR; `git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; `commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; `commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)
* job cells: 1672 total, 1672 mapped to a YAML job key (100.0%)
* cell conclusions: {'cancelled': 66, 'failure': 46, 'skipped': 71, 'success': 1489}
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
| python-poetry/poetry | 780 | 130 | 0 | 6 | 2026-09-19T13:48:26Z → 2026-10-06T14:11:53Z |
| fastapi/fastapi | 720 | 120 | 0 | 6 | 2026-09-28T00:01:37Z → 2026-10-06T13:50:06Z |
| prometheus/prometheus | 2094 | 100 | 0 | 21 | 2026-10-03T15:10:33Z → 2026-10-06T15:17:05Z |
| apache/dubbo | 798 | 57 | 3 | 14 | 2026-09-06T01:47:08Z → 2026-10-06T08:56:03Z |
| quarkusio/quarkus | 504 | 24 | 6 | 21 | 2026-10-05T16:36:31Z → 2026-10-06T14:22:51Z |
| **total** | 4896 |  |  |  |  |

\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs could be mapped to a YAML job key.

## 4. Label distributions

### python-poetry/poetry

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 377 | 26 | 6.9% | 403 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 93 | 57 | 61.3% | 687 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 377 |  |  | mean 79.8s, median 6.0s, p90 278.4s, max 1438s |

**Failure rate: 6.90%** (26 of 377 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 25, 'miss': 57, 'partial_hit': 11, nan: 687}

### fastapi/fastapi

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 585 | 24 | 4.1% | 135 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 112 | 47 | 42.0% | 608 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 585 |  |  | mean 73.5s, median 10.0s, p90 236.6s, max 616s |

**Failure rate: 4.10%** (24 of 585 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 65, 'miss': 47, nan: 608}

### prometheus/prometheus

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 1615 | 38 | 2.4% | 479 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 379 | 332 | 87.6% | 1715 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 1615 |  |  | mean 508.0s, median 474.0s, p90 1132.8s, max 10987s |

**Failure rate: 2.35%** (38 of 1615 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 46, 'hit_unresolved': 1, 'miss': 332, nan: 1715}

### apache/dubbo

| target | labelled rows | positives | positive rate | note |
|---|---|---|---|---|
| failure (1 = failure/timed_out) | 697 | 38 | 5.5% | 101 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 340 | 92 | 27.1% | 458 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 697 |  |  | mean 362.3s, median 58.0s, p90 1084.0s, max 3154s |

**Failure rate: 5.45%** (38 of 697 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 64, 'miss': 92, 'partial_hit': 184, nan: 458}

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
| failure (1 = failure/timed_out) | 3623 | 160 | 4.4% | 1273 rows without label (cancelled/skipped/other) |
| cache miss (1 = miss; log-derived) | 1144 | 638 | 55.8% | 3752 rows NULL (excluded from cache-model training) |
| duration seconds (success/failure rows) | 3623 |  |  | mean 463.8s, median 148.0s, p90 1095.6s, max 17172s |

**Failure rate: 4.42%** (160 of 3623 labelled rows). The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence and report the base rate.

Cache outcome breakdown at row level: {'exact_hit': 300, 'hit_unresolved': 3, 'miss': 638, 'partial_hit': 203, nan: 3752}

## 5. Cache-label coverage

| repo | job cells | cells hit | cells miss | cells NULL | rows | rows labelled | rows NULL | row coverage |
|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 7634 | 2082 | 1242 | 4310 | 780 | 93 | 687 | 11.9% |
| fastapi/fastapi | 1840 | 65 | 206 | 1569 | 720 | 112 | 608 | 15.6% |
| prometheus/prometheus | 2814 | 93 | 558 | 2163 | 2094 | 379 | 1715 | 18.1% |
| apache/dubbo | 1672 | 906 | 92 | 674 | 798 | 340 | 458 | 42.6% |
| quarkusio/quarkus | 1180 | 335 | 544 | 301 | 504 | 220 | 284 | 43.7% |

* **python-poetry/poetry** — why cells have no label (`cache_label_status`): {'labelled': 3324, 'parsed_no_events': 1302, 'skipped_conclusion': 2724, 'skipped_not_cache_capable': 284}

* **fastapi/fastapi** — why cells have no label (`cache_label_status`): {'labelled': 271, 'parsed_no_events': 1189, 'skipped_conclusion': 143, 'skipped_not_cache_capable': 237}

* **prometheus/prometheus** — why cells have no label (`cache_label_status`): {'labelled': 651, 'parsed_no_events': 183, 'skipped_conclusion': 507, 'skipped_not_cache_capable': 1473}

* **apache/dubbo** — why cells have no label (`cache_label_status`): {'labelled': 998, 'parsed_no_events': 56, 'skipped_conclusion': 137, 'skipped_not_cache_capable': 481}

* **quarkusio/quarkus** — why cells have no label (`cache_label_status`): {'labelled': 879, 'parsed_no_events': 44, 'skipped_conclusion': 165, 'skipped_not_cache_capable': 91, 'unavailable': 1}


Status meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.

## 6. Missing values per feature (rows with NaN)

| feature | python-poetry/poetry | fastapi/fastapi | prometheus/prometheus | apache/dubbo | quarkusio/quarkus |
|---|---|---|---|---|---|
| files_changed_count | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_added | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| loc_deleted | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| job_structurally_impacted | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| cache_key_touched | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| dep_graph_distance | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| historical_failure_rate | 18 (2.3%) | 11 (1.5%) | 283 (13.5%) | 14 (1.8%) | 55 (10.9%) |
| historical_mean_duration | 18 (2.3%) | 11 (1.5%) | 283 (13.5%) | 14 (1.8%) | 55 (10.9%) |
| recent_failure_streak | 18 (2.3%) | 11 (1.5%) | 283 (13.5%) | 14 (1.8%) | 55 (10.9%) |
| recent_success_streak | 18 (2.3%) | 11 (1.5%) | 283 (13.5%) | 14 (1.8%) | 55 (10.9%) |
| author_failure_rate | 216 (27.7%) | 114 (15.8%) | 399 (19.1%) | 182 (22.8%) | 210 (41.7%) |
| cache_key_stability | 651 (83.5%) | 379 (52.6%) | 1698 (81.1%) | 406 (50.9%) | 210 (41.7%) |
| hour_of_day | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| day_of_week | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |
| is_weekend | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) |

Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. `author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph features were not computed for that run.

## 7. Graph-feature quality

| repo | rows with graph features | rows where fallback fired | fallback share | rows impacted via a REAL graph path | rows with cache_key_touched=1 | distinct: impacted | distinct: cache_key_touched | distinct: distance |
|---|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 780 | 276 | 35.4% | 0 | 0 | 2 | 1 | 1 |
| fastapi/fastapi | 720 | 438 | 60.8% | 0 | 0 | 2 | 1 | 1 |
| prometheus/prometheus | 2094 | 204 | 9.7% | 0 | 7 | 2 | 2 | 1 |
| apache/dubbo | 798 | 336 | 42.1% | 0 | 130 | 2 | 2 | 1 |
| quarkusio/quarkus | 504 | 168 | 33.3% | 0 | 0 | 2 | 1 | 1 |

* **python-poetry/poetry**: constant (zero-variance) structural feature(s) ['cache_key_touched', 'dep_graph_distance'] — they carry no information for this repository.

* **fastapi/fastapi**: constant (zero-variance) structural feature(s) ['cache_key_touched', 'dep_graph_distance'] — they carry no information for this repository.

* **fastapi/fastapi**: for 60.8% of rows no changed file was a graph node, so `job_structurally_impacted` fell back to "all jobs impacted" (uninformative).

* **prometheus/prometheus**: constant (zero-variance) structural feature(s) ['dep_graph_distance'] — they carry no information for this repository.

* **apache/dubbo**: constant (zero-variance) structural feature(s) ['dep_graph_distance'] — they carry no information for this repository.

* **quarkusio/quarkus**: constant (zero-variance) structural feature(s) ['cache_key_touched', 'dep_graph_distance'] — they carry no information for this repository.

## 8. Temporal train/test split

| repo | test period starts (run created_at) | train rows | train runs | test rows | test runs | failure rate train | failure rate test |
|---|---|---|---|---|---|---|---|
| python-poetry/poetry | 2026-10-02T08:10:10Z | 624 | 104 | 156 | 26 | 7.67% (24/313) | 3.12% (2/64) |
| fastapi/fastapi | 2026-10-02T11:14:40Z | 576 | 96 | 144 | 24 | 4.87% (23/472) | 0.88% (1/113) |
| prometheus/prometheus | 2026-10-06T07:11:02Z | 1674 | 80 | 420 | 20 | 2.67% (34/1275) | 1.18% (4/340) |
| apache/dubbo | 2026-09-24T13:33:18Z | 644 | 46 | 154 | 11 | 5.04% (28/556) | 7.09% (10/141) |
| quarkusio/quarkus | 2026-10-06T14:14:53Z | 399 | 19 | 105 | 5 | 8.24% (23/279) | 15.71% (11/70) |

Test fraction = last 20% of runs by creation time (`config.model.temporal_test_fraction`); all jobs of a run are on the same side; the split is never random.

## 9. What this data does NOT support

* Rows are **not independent**: jobs of one run, and consecutive runs of one PR, share a commit lineage. Treat any metric as optimistic by that margin.
* Cache labels exist only inside GitHub's log-retention window (measured ≈45 days for quarkus, ≈90 days for the others); older runs contribute failure/duration labels only.
* A PR run's changed-file set is the whole-PR diff (API compare, or merge-base..head from the git mirror, assuming the PR targets the default branch); runs whose PR could not be determined, and multi-commit pushes, fall back to the head commit's diff (see `diff_source`).
* Only the **latest attempt** of a re-run is ingested.
* `cancelled` jobs (mostly concurrency cancel-in-progress) and `skipped` jobs carry no failure or duration label; cancellation is not modelled.
* The scheduler's cost / energy / CO₂ constants are configuration assumptions, not measurements from this data.
* **python-poetry/poetry**: only 26 failure positives — failure-model metrics will have wide uncertainty.
* **python-poetry/poetry**: runs come from several events {'pull_request': 111, 'push': 27}; event type is not a model feature.
* **fastapi/fastapi**: only 24 failure positives — failure-model metrics will have wide uncertainty.
* **fastapi/fastapi**: runs come from several events {'pull_request': 83, 'push': 35, 'schedule': 2}; event type is not a model feature.
* **prometheus/prometheus**: only 38 failure positives — failure-model metrics will have wide uncertainty.
* **prometheus/prometheus**: 106 of 2814 job cells (3.8%) could not be mapped to a YAML job key (ambiguous or unmatched display names) and are absent from the dataset.
* **prometheus/prometheus**: runs come from several events {'pull_request': 107, 'push': 6}; event type is not a model feature.
* **apache/dubbo**: only 38 failure positives — failure-model metrics will have wide uncertainty.
* **apache/dubbo**: runs come from several events {'pull_request': 53, 'push': 9}; event type is not a model feature.
* **quarkusio/quarkus**: only 34 failure positives — failure-model metrics will have wide uncertainty.
* **quarkusio/quarkus**: runs come from several events {'pull_request': 75, 'push': 1, 'schedule': 4}; event type is not a model feature.
