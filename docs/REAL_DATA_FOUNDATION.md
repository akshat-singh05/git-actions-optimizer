# Real data foundation — design, status and open issues

*Interim engineering note (not the README — that is written at the end of the project).
Figures that depend on the ingested data live in `docs/DATASET_REPORT.md`, which is generated from the
database; this note contains no data-dependent numbers.*

## 1. What changed

Synthetic training history is gone from every production path. History now comes from real GitHub
Actions executions, stored in the database by a resumable ingestion service, turned into a
leakage-free dataset, and consumed by the pipeline, CLI and API. With too little history the system
**declines to predict** (no synthetic fallback, no default values).

```
GitHub REST API ──► GithubClient ──► RepoIngestor ──► DB (runs, jobs, steps, diffs, log ledger, cache events)
 (+ job logs)        rate limits,      idempotent,        │
                     retries,          resumable,         ├─► git mirror (bare clone) ──► per-commit dependency graph
                     windowing         per-run commits    │        └─► run_graph_features
                                                          ▼
                              load_history ► (run, job-key) records ► build_training_frame (strictly-prior features)
                                                          │
                       temporal split ◄───────────────────┘──► models (NaN-capable GBT) ──► scheduler ──► workflow YAML
```

## 2. Status (rule 12 vocabulary)

| Component | Status | Evidence / caveat |
|---|---|---|
| `ingestion/github_client.py` — rate-limit sleeping, secondary-limit backoff, 5xx/network retry, `Link` pagination, run-window bisection past the 1000 cap | IMPLEMENTED | `tests/test_github_client.py` (18); exercised live (502/503 retries and a DNS outage were absorbed) |
| `ingestion/ingest.py` + `gha-optimizer ingest` — idempotent, resumable, per-run commits, log ledger | IMPLEMENTED | `tests/test_ingest.py` (14); live kill-and-resume on python-poetry produced 0 duplicates |
| `ingestion/log_parser.py` — cache outcome from real logs (exact / partial / miss) | IMPLEMENTED | 6 verbatim real log excerpts in `tests/fixtures/real_logs`; parsed live from >5k real job logs |
| `ingestion/job_mapping.py` — API job name → YAML job key via the YAML at the run's sha | IMPLEMENTED | matrix cells, templated names, reusable-workflow children; 100% of poetry cells mapped |
| `ingestion/mirror.py`, `features/graph_features.py` — dependency graph at each run's commit, parse cache per blob | IMPLEMENTED | tiny-git-repo test shows different features at two commits; **features are weakly informative on real repos (see §5)** |
| `features/feature_extraction.py` — strictly-prior historical features, NaN for unknown | IMPLEMENTED | `tests/test_feature_leakage.py` (13): perturbing a run's own/future outcomes does not change its features; the original code failed the same perturbation in all six historical features |
| Dataset, temporal split, `dataset-report` | IMPLEMENTED | `docs/DATASET_REPORT.md` is produced by `gha-optimizer dataset-report` |
| Alembic `002_real_ingestion` | IMPLEMENTED | upgrade → downgrade → upgrade verified; zero ORM/DB drift after upgrade |
| Models (cache-miss, failure, duration) | PARTIALLY IMPLEMENTED | NaN-capable GBT, temporal-holdout metrics with base rates and a naive baseline. `fit` still happens on every `run_pipeline()`; the pipeline now loads fingerprint-matched artifacts from `models/store.py` and refits otherwise (the old `train` command and its unused loose artifacts were removed 2026-10-12). **On the real data the models show little or no skill over naive baselines** (see `docs/DATASET_REPORT.md` and the metrics block printed by `analyze` and `demo.py`) |
| GNN cache predictor | REMOVED (2026-10-08) | never ran (torch not installed) and could not work on the current graph schema; see CLAUDE.md. Recoverable from git history |
| Scheduler | IMPLEMENTED, **assumption-driven** | see §5: the estimated gain is dominated by an assumed runner speed-up law |
| Demos (`demo.py`, `demo_server.py`, `examples/demo.py`) | IMPLEMENTED | re-pointed to real ingested repositories; their own synthetic generators were deleted |
| Synthetic generator | TEST-ONLY | `tests/fixtures/synthetic_history.py::generate_synthetic_test_history`; not importable from `gha_optimizer/` |

## 3. Label semantics (what a row means)

Grain: one row per **(workflow run, workflow job key)**; matrix cells are aggregated (failure if any cell
failed, duration = slowest cell, cache label from the cells that have one). Cell-level rows stay in
`workflow_jobs`.

* **failure** — `failure`/`timed_out` = 1, `success` = 0. `cancelled`, `skipped`, other = *no label*.
* **duration** — `completed_at − started_at` from the API, only for success/failure rows.
* **cache miss** — only from the job log. A job is a *miss* if any cache restore step restored nothing, a
  *hit* if every restore step restored (exact key, or via `restore-keys` → recorded as `partial_hit`).
  No cache line / expired log / job that cannot run a cache action ⇒ **NULL**, excluded from
  cache-model training and counted (`cache_label_status` records why).

## 4. Robustness facts (measured against the live API, 2026-10-07)

* Log retention differs per repository (≈90 days for most, ≈45 days for quarkus); 410 Gone marks expiry and the
  ingester learns the horizon from it.
* Filtered run listings return at most 1000 results per query and their page order is not reliable →
  day windows, bisection when `total_count ≥ 1000`, completeness check `unique ids == total_count`.
* GitHub leaves a run's `pull_requests[]` empty for fork PRs, and `GET /commits/{sha}/pulls` only returns
  *open* PRs for commits that are not on the default branch — so most runs of already merged/closed PRs cannot be
  resolved through the API. The whole-PR diff is therefore obtained, in order, from: API `compare` (PR known) →
  the git mirror, `merge-base(default-branch tip at run time, head)..head` (no API call; assumes the PR targeted the
  default branch) → the head commit only (`commit_only`, an approximation). `runs.diff_source` records which.
* Re-runs keep the run id; only the latest attempt is ingested and a changed attempt purges the stale jobs.
* A window is final only once it is >30 days old, because runs can be re-run for weeks.

## 5. Open issues found while building this (not fixed — they change what can be claimed)

1. **Graph-derived features were close to degenerate on real repositories - PARTIALLY FIXED 2026-10-07, see
   `docs/GRAPH_FEATURES.md`** (version 2 features; they now agree with executed-vs-skipped for poetry and
   fastapi but not for prometheus, dubbo or quarkus). The text below describes version 1.
   The fusion rules only link
   files to jobs when a step runs a recognised test command on a directory (`pytest <dir>`, `go test`, …),
   link files to cache keys only for `actions/cache` steps, and no parser resolves intra-repo imports
   (every import becomes a `Package` node). On `python-poetry/poetry` fusion inferred **zero** cross-domain
   edges, so `cache_key_touched` is constant 0, `dep_graph_distance` is constant −1 and
   `job_structurally_impacted` is mostly the conservative "all jobs" fallback. The dataset report
   quantifies this per repository: in **all five** ingested repositories **no row** is impacted through a real
   graph path (`job_structurally_impacted` is 1 only where the "no changed file in the graph" fallback fired);
   `cache_key_touched` is non-constant only for prometheus and dubbo. `dep_graph_distance` also routes through the shared `Workflow` node, so
   every job of a workflow is within 3 hops of any changed file.
2. **The scheduler's headline gain is an assumed runner speed-up law, not something learned from data.**
   `_evaluate_tier` takes `speedup = (price multiplier) ** 0.6` for larger runners and the objective then
   picks bigger runners; the wall-clock "saving" is largely that assumption applied to the real DAG. The
   effect of the ML predictions (fail-fast ordering, skipping) is not isolated. All such figures are
   **ESTIMATED**, never measured. Cost / energy / CO₂ additionally use constants from `config.scheduler`.
3. **The cache-miss probability is informational only** — the scheduler does not consume it.
4. Several models' temporal test sets are tiny or single-class (rare failures; one cache-labelled job);
   the metrics block reports the base rate next to every metric and `None` where undefined.
5. The pipeline still refits the models on every call (no artifact loading), and PR-run diffs are only as
   good as `pull_requests[]` / the commit→PR lookup (see §4).
6. **Sample sizes are capped by cost, not by availability.** Ingestion is newest-first with `--max-runs` caps
   (poetry 130, fastapi 120, prometheus 100, dubbo 60, quarkus 30 runs ≈ 13.7 GB of job logs; quarkus alone is
   ~60 job logs / ~100 MB per run). The datasets are contiguous recent windows, small for the failure class
   (17–38 positives per repository), and say nothing about older history or other workflows of the repository.
7. **The job-name → YAML-key mapping refuses to guess.** Two jobs that share a display name stay unmapped
   (prometheus: 3.8% of cells) and unmapped cells are absent from the dataset. (A wildcard bug that rejected
   templates expanding to an empty matrix value — 24% of dubbo's cells — was found on real data and fixed.)
8. **Parsing skips source files whose temporary path exceeds the Windows limit** (3 files in dubbo, 124 in
   quarkus) — they are missing from those commits' graphs.
9. **Generator bug found and fixed:** the proposed workflow was written with `true:` instead of `on:` (PyYAML
   loads `on` as a boolean), which silently broke the triggers of every generated file. The stale committed
   generated output (`examples/sample_repo/.github/workflows/ci.optimized.yml`) was deleted.
10. Housekeeping done after the phase: `demo_repository/`, `server/database.py` and the generated sample output were
    removed; the toy repo moved to `tests/fixtures/` so `examples/` holds only the real-data demo; `.pyc` and
    `egg-info` files were untracked; `CLAUDE.md` was updated to the post-phase state.

## 6. Reproducing

```bash
pip install -r requirements.txt && pip install -e ".[dev]" ruff
export GITHUB_TOKEN=...                      # PowerShell: $env:GITHUB_TOKEN = "..."
gha-optimizer ingest --repo python-poetry/poetry --workflow tests.yaml --since 40d --max-runs 130
gha-optimizer ingest-status --repo python-poetry/poetry
gha-optimizer build-dataset --repo python-poetry/poetry
gha-optimizer dataset-report --repo python-poetry/poetry --out docs/DATASET_REPORT.md
gha-optimizer analyze <checkout>/.github/workflows/tests.yaml --repo python-poetry/poetry --changed a.py,b.py
python demo.py --repo python-poetry/poetry
pytest tests/ -v && ruff check gha_optimizer/ --select E,F,W --ignore E501
```
