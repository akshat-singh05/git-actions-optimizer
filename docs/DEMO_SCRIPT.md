# Live demo script (about 12 minutes)

A working demo on **real ingested GitHub Actions history**: nothing is simulated or pre-recorded. Everything runs locally from the
project folder; no network or token is needed during the demo itself.

## 0. Before the guide arrives (start 15 minutes early)

1. Open a terminal in the project folder and run **one command**; leave that tab open:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_demo.ps1
   ```

   It checks that the models can load, starts the dashboard, warms every ingested repository and opens http://localhost:8501.
   The repository selector shows `(ready)` next to each repository as it finishes (6-8 minutes the first time; results are
   cached on disk and reused after a restart while the ingested runs are unchanged). Wait for `(ready)` on the ones you will show.
2. Optional, in a second tab: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/live_scorers.ps1` keeps the live
   test running (`-Status` shows its ledger sizes). Leave it open.
3. Do not disable Windows Smart App Control. If the start script says scikit-learn could not be imported, wait a minute and retry.

Repositories you can show (full pipeline): `python-poetry/poetry`, `fastapi/fastapi`, `pydantic/pydantic`, `astral-sh/ruff`, `apache/dubbo`,
`cli/cli`, `quarkusio/quarkus` and `akshat-singh05/git-actions-optimizer` (this project's own CI, 63 runs). Repositories that **decline**
on purpose: `astral-sh/uv` (cache labels of one class only) and `prometheus/prometheus` (a job with no history).

## 1. The dashboard (2 min) - poetry

Open **Dashboard**. Point at: real run count and job rows, the job graph coloured by predicted failure risk, per-job predicted
duration and failure bars, the estimated timeline that respects `needs`. Say: *"every saving shown is ESTIMATED; the default plan keeps the
workflow's structure, so the estimate is 0%"*.

## 2. The pipeline, stage by stage (2 min)

Click through **Repository analysis** (job DAG), **Graph fusion** (source files linked to jobs and cache keys), **Impact analysis**
(which jobs the real diff reaches) and **Feature extraction**. Say how the graph reaches the models.

## 3. Models and probabilities (2 min) - the honest page

Open **Models and probabilities**. For each job the model score sits next to the job's own failure rate and the failure rate actually
seen on unseen rows that scored alike; a red **above anything seen** tag marks an extrapolation (poetry's `tests-matrix`). Say:
*"on poetry the baseline is at least as good as the model, so we do not present the score as a probability"*. Show the model-vs-baseline card.

## 4. What-if (2 min) - the live moment

Open **What-if: change files**. Click **A dependency file**, then **Docs only**, then type a path from the audience in the search box and add it.
Impacted jobs, scores and the estimated timeline update. Say what reacts (the structural features) and what does not (history).

## 5. Safe gate relaxation on our own CI (2 min)

Switch the selector to **akshat-singh05/git-actions-optimizer**, open **DAG-aware scheduling**: the opt-in panel shows `test` no longer waiting for `lint`,
estimated critical path 171 s to 131 s (ESTIMATED, 23%), the trade-off, and why the other gate is kept. Then **Original vs proposed workflow**:
download the relaxed workflow, show the diff. Say: *"we measured this on real runs: 159.5 s to 135.5 s, -15.1%, p = 0.013, billable time +4.1% not significant;
the estimate is higher than the measurement"* (`docs/relax_run/`). Then switch to poetry or fastapi to show a mature pipeline where no gate can be removed.

## 6. The command line (1 min)

In a new tab (the proposed file is written to the snapshot folder, never over your workflow):

```powershell
.venv\Scripts\python.exe -m gha_optimizer.cli analyze D:/ghas/akshat-singh05__git-actions-optimizer/.github/workflows/ci.yml --repo akshat-singh05/git-actions-optimizer --repo-root D:/ghas/akshat-singh05__git-actions-optimizer --changed gha_optimizer/cli.py --relax-gates --as-of 2026-10-12T12:00:00Z
.venv\Scripts\python.exe -m gha_optimizer.cli triage --repo python-poetry/poetry --out $env:TEMP\triage.md
```

`analyze` prints the plan, the ESTIMATED figures, the relaxed gates with their trade-off, the failure triage of the workflow and the diff; the same `--as-of`
gives the same numbers every time. `triage` classifies every real failure (15 s). `gha-optimizer ingest-status --repo python-poetry/poetry` shows the real SQL counts.

## 7. Refusing to guess, evidence and the live test (1 min)

Select **astral-sh/uv**: the page explains why the system declined and shows no numbers. Open **Evidence: this repository** (rows of the generated reports),
**Project reports** (`IMPROVEMENT_RESULTS`, `CALIBRATION`, `LIVE_TEST`) and **Live test ledger** (hash-chain verified, entries per repository).

## What to say about the results

* The failure model beats a naive baseline only in some repositories (fastapi, pydantic); the improved model missed its pre-registered bar (2 of 9 against 3).
* Scheduling is not faster by default; one measured case (-15%) and about zero on mature pipelines.
* The live test (interim, two days) shows neither model beating the baseline on uv; final verdicts are 19, 21 and 26 October. See `docs/IMPROVEMENT_RESULTS.md`.

## If something goes wrong

* A repository shows a spinner: it is still being computed (the status label next to the selector shows the stage); pick another and come back.
* The page cannot reach the API: the tab running `start_demo.ps1` was closed; run it again (cached results load in seconds).
* `apache/dubbo` fails to load: the dashboard must be started with `--snapshot-root D:/ghas` (the start script does this).
* Smart App Control blocks a library: never disable it; retry, or show the CLI output and the Evidence pages instead.
* The what-if page says "preparing the models": the server restarted since the result was cached; wait for the bar (about 40 s).
