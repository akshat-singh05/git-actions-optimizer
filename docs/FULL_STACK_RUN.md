# Full-stack run: Postgres + Redis + API + Celery worker (2026-10-11)

`docker compose up --build` was run for real (Docker Desktop on Windows, Linux containers) and exercised with a throwaway
webhook secret against a private repository (`akshat-singh05/git-actions-optimizer`). It found five defects that no
unit test had caught; all are fixed, each with a test where one is possible. The stack was torn down afterwards
(`docker compose down -v`).

## What was verified live

| step | result |
|---|---|
| images build (API, worker) | OK after the fixes below (the API / worker images had never been buildable) |
| `GET /health` | `db_connected: true`, `redis_connected: true`, `celery_connected: true` |
| `GET /ready` | 200 (real `SELECT 1` on Postgres) |
| webhook, no signature / wrong signature | 401 `Invalid signature`, nothing dispatched |
| webhook, invalid JSON (validly signed) | 400 |
| signed `workflow_job` event | 200, task id; worker processed it in 0.05 s |
| signed `workflow_run` completed | 200; worker started `ingest_run`, cloned the **private** repo with the token, called the GitHub API, and wrote 1 run, 3 jobs, 30 steps, 6 cache events to Postgres |
| the clone's stored remote URL | `https://github.com/...git` - no token in the URL or in the clone's config |
| `POST /api/v1/optimize` with 1 ingested run | 422 `insufficient_history` (`runs: 1, required: 30`), no guess |

## Defects found and fixed

1. **Images could not be built.** `setup.py` read `README.md`, which the Dockerfiles never copy (`FileNotFoundError`).
   `setup.py` now tolerates a missing README.
2. **Build context of about 3.2 GB** (ingested mirrors, SQLite DB, `.venv`): added `.dockerignore`.
3. **No `git` in the images.** The ingestion code shells out to git; the first real `ingest_run` failed with
   `No such file or directory: 'git'`. Both Dockerfiles now install git.
4. **Private repositories could never be mirrored.** `RepoMirror` cloned anonymously. It now takes the token and passes
   it through git's environment config (`http.https://github.com/.extraheader`), so it appears in no URL, command line or
   stored remote (`tests/test_mirror_auth.py`).
5. **Every webhook request crashed.** `app.py` read `config.github.webhook_secret`, which does not exist (the field lives on
   `config.server`); there was no webhook test. Fixed, with tests for rejected / accepted / unsigned / invalid-JSON cases.
   Also fixed: `/health` reported `celery_connected: false` unconditionally because nothing set it; it now pings workers.

Also changed: `docker-compose.yml` passes `GITHUB_WEBHOOK_SECRET` to the API.

## Still true / not covered

* **Without `GITHUB_WEBHOOK_SECRET` the endpoint accepts unsigned requests** (by design, for development) and a
  `workflow_run` event then makes the worker ingest an arbitrary repository / run id with the configured token. Set the
  secret in any deployment that is reachable from outside.
* The database tables are created lazily by the worker's `init_db()`; the compose flow does not run `alembic upgrade head`.
* Not exercised inside Docker: a run with at least 30 ingested runs through `/api/v1/optimize` (the endpoint is covered
  by `tests/test_server.py` outside Docker) and a delivery sent by github.com itself (the signature check is tested against
  GitHub's published test vector, `test_signature_check_matches_githubs_published_test_vector`).

## Follow-up the next day (2026-10-12): the remaining tasks, run for real

Run in eager mode (no broker) on real data, outside Docker:

| task | result |
|---|---|
| `sync_repository` (private repo, scratch database) | complete: 2 runs, 6 jobs, 60 steps, 12 cache events |
| `generate_dependency_graph` (poetry @ latest ingested commit) | complete: 5,178 source nodes / 7,147 edges; 6 workflow graphs fused (e.g. `tests.yaml` 5,278 nodes) in 12.5 s |
| `run_benchmark` (same commit) | complete: build 10.5 s, snapshot 9.9 MB, 5,278 nodes / 8,702 edges |
| `extract_features` (poetry) | complete: 602 runs, 3,612 rows, 28 columns |
| unknown commit | `blocked: commit_not_in_mirror` |

Defects found and fixed: `generate_dependency_graph` and `run_benchmark` built from a directory nothing populated and
returned `complete` on an empty graph (they now read the real graph of the commit from the git mirror, or return `blocked`);
the benchmark reported unmeasured quantities as `0` and silently failed to measure memory (`None` for unmeasured, serialised
size as a documented proxy).

**The feedback loop was retired.** Nothing ever wrote the `predictions` table, so drift detection always saw 0 samples and
`retrain_models` could only run with `force`; its artifacts were never read by the pipeline and `feedback/retrainer.py` was
unreferenced. Removed: the `feedback/` package, the `retrain_models` task, the `train` CLI command and the unused model-version
helpers. The `predictions` / `model_versions` tables remain in the schema (alembic history) and `/api/v1/metrics` reports
`sample_count: 0` truthfully. A real loop would need predictions logged at `optimize` time and matched to the later run of
the same commit: future work.
