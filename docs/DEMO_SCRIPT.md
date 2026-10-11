# Demo script (about 10 minutes)

Everything here runs on **real ingested GitHub Actions history** already in the local database (poetry, fastapi,
prometheus, dubbo, quarkus, pydantic, cli/cli, ruff, uv). No token and no network are needed for the demo itself. All
commands run from the repository root with the project virtualenv (`.venv\Scripts\python.exe` on Windows).

What was timed when this script was written (2026-10-11): `python demo.py --repo python-poetry/poetry` took 43 s and
printed 282 lines. Nothing else below was timed; run it once beforehand.

## 0. Before the audience arrives

* `python demo.py --repo python-poetry/poetry` once, so the persisted state and caches are warm.
* Have `docs/RESULTS.md`, `docs/HELDOUT_RESULTS.md` and `docs/IMPACT_REPLAY.md` open in tabs.
* If a tree-sitter warning appears ("Tree-sitter ... unavailable"), the Windows Application Control policy is blocking its DLL
  on that machine: the demo still runs (source files are then not parsed into the graph), but say so rather than hide it.

## 1. The data is real (1 min)

```bash
gha-optimizer ingest-status --repo python-poetry/poetry
```

Point out real SQL row counts, the ingestion ledger (log downloads, expired logs), and that cache labels exist for only a
small fraction of runs (log retention). Say how it was collected: GitHub REST API, a local bare git mirror for the workflow
YAML and diffs at each commit, job logs for cache events.

## 2. The pipeline, stage by stage (4 min)

```bash
python demo.py --repo python-poetry/poetry
```

Talking points, in the order the output appears:

| output section | what to say |
|---|---|
| STEP 1 | the workflow has 6 jobs and 9 `needs` edges; this is the real `tests.yaml` as of the latest ingested run |
| STEP 2 | source graph + CI graph fused; 650 cross-domain edges are inferred by five static rules, each with a confidence |
| STEP 5 | the diff of a real run is mapped to jobs; unknown is `NaN`, never "all jobs"; `lockfile`/`smoke` show `∞ (no path)` |
| STEP 6 | features use only strictly earlier runs; missing values are kept as `NaN` (617 rows have no structural evidence) |
| STEP 7 | three models, each printed with its **baseline** (e.g. failure PR-AUC 0.442 vs base rate 0.109). **Note the cache model**: its Brier score (0.223) is *worse* than the baseline's (0.040) on this single holdout - say it first; it is why the cache probability is marked "informational, not used by the scheduler" |
| STEP 8 | the generated order is checked: all jobs preserved, `needs` respected, acyclic |
| STEP 10 | a **proposed** file plus a diff; the original is untouched. Estimated saving is **0.0%** because skip guards and runner upsizing are opt-in. The what-if line (49.2% faster, 66% more expensive) depends on an *assumed* speed-up law and says so |
| STEP 11 | every figure is labelled ESTIMATED |

Say explicitly: *a 0.0% result is the honest outcome here. Poetry already gates its jobs with `paths-filter`.*

## 3. What the model evaluation says (2 min)

Show `docs/RESULTS.md` section 3. Three sentences:

1. Rolling-origin folds, paired bootstrap, baselines with no model.
2. The logistic failure model beats the naive baseline significantly in fastapi (+0.085 PR-AUC) and in held-out pydantic
   (+0.110), is indistinguishable elsewhere, and is never significantly worse.
3. In held-out ruff the tree variants beat the baseline by about +0.39 while the logistic default does not clear it - a lead
   we report, not a fix we apply (the default is frozen pending a pre-registered fresh-data test).

## 4. The graph skip policy and why it is only advisory (2 min)

Show `docs/IMPACT_REPLAY.md`, then `docs/HELDOUT_RESULTS.md` and `docs/HELDOUT_RESULTS_2.md`.

* On the development repositories it saves 12-17% of billable time (fastapi, prometheus) but would make 3-10% of failing
  runs look green.
* On four **held-out** repositories (pydantic, cli/cli, ruff, uv) it did not generalise; the safe variant saves 0.0-2.0%.
* So the system reports it (the "Advisory" section of `analyze`), with the measured risk, and never applies it.
* Deferral to default-branch pushes was pre-registered and evaluated: not supported.

## 5. Optional extras (only if asked)

* `gha-optimizer analyze <checkout>/.github/workflows/ci.yml --repo owner/repo --changed a.py` on a real checkout shows
  the Advisory section; needs a git checkout of the repo (not available for the mirrors alone).
* `python demo_server.py --repo python-poetry/poetry` serves a UI at http://localhost:8501 (not re-run when this was written).
* The full stack (`docker compose up --build`) was run once end to end: `docs/FULL_STACK_RUN.md`.
* The LLM summary (`analyze --llm-summary`) needs `GEMINI_API_KEY` and `GHA_LLM_MODEL`; it has not been run against the
  live service, so do not demo it unless it has been tried beforehand.

## If something goes wrong

* `Declining to predict` / exit code 2 is the system working: too little history. It never substitutes data.
* A repository not in the database must be ingested first (`gha-optimizer ingest ... --max-runs N`, needs `GITHUB_TOKEN`).
