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
* Not exercised: the `sync_repository` task, `generate_dependency_graph`, `extract_features`, `retrain_models`,
  `run_benchmark`, GitHub's real signature header from github.com, and a run with at least 30 ingested runs through
  `/api/v1/optimize` inside the containers (the endpoint is covered by `tests/test_server.py` outside Docker).
