# Presenting with different repositories

The dashboard (`gha-optimizer dashboard`; `python demo_server.py ...` is the same command) can show any repository that has been ingested into the local database.
A repository is chosen in the **Repository** drop-down in the header; the choice is kept in the address (`?repo=owner/name`),
so a repository can also be opened directly, for example `http://localhost:8501/?repo=fastapi/fastapi#step-8`.

## Before the panel (about 6 minutes, once)

```bash
gha-optimizer dashboard --repo python-poetry/poetry --warm --snapshot-root D:/ghas
```

* `--warm` computes every ingested repository in the background, one at a time, so switching in front of the panel is
  instant instead of taking 40-110 s. Finished results are also kept on disk (`.gha_optimizer/dashboard_cache/`) and reused after a
  restart while the ingested runs, the tip commit and the library versions are unchanged, so the second start needs no warm-up. The drop-down marks finished repositories with a tick; wait until the ones you plan to
  show have it (poetry first, then the others in alphabetical order; dubbo takes the longest, about 2 minutes).
* `--snapshot-root D:/ghas` keeps the exported file paths short. Without it `apache/dubbo` fails on Windows (paths over about
  260 characters). Any short folder works.
* Open http://localhost:8501 and click through the repositories once to confirm.
* Numbers are reproducible: the scoring time is the scored run's own creation time, so the same repository shows the same figures every time
  (this was not true before 2026-10-14, when the hour of the day leaked in).

## Which repositories work (as ingested on 2026-10-12)

| repository | result in the UI | what it is good for |
|---|---|---|
| `python-poetry/poetry` | full pipeline (614 runs, 6 jobs, 9 `needs` edges) | the main walk-through: small enough to read every page; `tests-matrix` is predicted to fail often and is the longest job |
| `fastapi/fastapi` | full pipeline (699 runs, 6 jobs) | second example with a different workflow shape (aggregator job `test-alls-green`, low failure probabilities) |
| `pydantic/pydantic` | full pipeline (about 200 runs, 20+ jobs) | many jobs, and a job (`send-tweet`) whose base rate is 100%: shows why the score is shown beside the base rate (cache labels were back-filled on 2026-10-14) |
| `astral-sh/ruff` | full pipeline (about 180 runs, a long critical path) | script-gated CI; the model is not better than the baseline here (cache labels back-filled on 2026-10-14) |
| `apache/dubbo` | full pipeline (298 runs, 14 jobs, 23 edges) | a large workflow and 4,050 source files: shows the graph and the timeline at scale |
| `cli/cli` | full pipeline (293 runs, 2 jobs, no `needs`) | a degenerate case: nothing to reorder, so the plan is trivially the original |
| `prometheus/prometheus`, `astral-sh/uv`, `quarkusio/quarkus` | **declines** with the reason | show it on purpose: the system refuses to predict when there is too little real history (a job with no history; cache labels of only one class; fewer than 30 runs), and never fills the gap with made-up values |

Declines are a feature, not a bug: say so before the panel asks.

## Suggested 10-minute flow

1. **Dashboard of poetry (2 min).** Real history size, the job graph coloured by predicted failure risk, per-job duration and failure bars,
   the estimated timeline that respects `needs`. Point at the pills: REAL history, ESTIMATED savings.
2. **Pipeline pages (2 min).** Repository (job DAG), graph fusion, impact analysis of the real diff, the features.
3. **Models and probabilities (2 min).** Each failure score next to the job's own base rate and the failure rate seen on unseen rows that
   scored alike; the red "above anything seen" tag; model vs baseline on this repository. Say it plainly: on poetry the baseline is at
   least as good, which is why the score is not presented as a probability.
4. **What-if (2 min) - the live moment.** Click "A dependency file", then "Docs only", then search a path of the audience's choice. Impacted
   jobs, scores and the estimated timeline update; say what does and does not react (only the structural features react to the files).
5. **Workflow page (30 s).** Original and proposed side by side, the download button; the original file is never modified.
6. **Switch repository (1 min).** fastapi or dubbo for scale; then a declining repository (see the table above) to show the honesty rule.
7. **Evidence and close (30 s).** The Evidence page lists the generated report rows for the repository; `docs/RESULTS.md` and
   `docs/IMPROVEMENT_RESULTS.md` state what was measured and what was not.

## Likely questions from this screen

* *"Why is the saving 0%?"* The default scheduler keeps the job structure; skipping jobs and larger runners are opt-in and are
  not measured savings (`docs/SCHEDULER_ASSUMPTIONS.md`). The one real run of relaxed gates is in `docs/relax_run/`.
* *"Why does the scheduler's wall-clock differ from the timeline's critical path?"* The scheduler's figure is level-based (it waits
  for the slowest job of each DAG level); the timeline uses the exact `needs` edges. Both use predicted durations.
* *"Is a failure probability of 0.93 meaningful?"* Compare it with the job's base failure rate and the baseline in
  `docs/MODEL_EVAL.md`; a probability alone is not evidence the model beats a naive baseline.
* *"Can it run on a repository we choose?"* Yes, after `gha-optimizer ingest --repo owner/repo --since 40d` (needs `GITHUB_TOKEN`;
  minutes to hours depending on the repository), then restart the server. It declines if the history is insufficient.

## If something goes wrong

* The page shows a spinner for a long time: the repository is still being computed (watch the state label next to the selector).
* A repository fails to load: check the server console; `--snapshot-root` is the usual fix on Windows.
* Windows blocks scikit-learn at import (Smart App Control, see CLAUDE.md "Gotchas"): do not disable it; run the demo before
  the panel on a machine where it starts, or present from `docs/` and screenshots.
