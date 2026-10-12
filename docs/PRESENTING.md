# Presenting with different repositories

The demo UI (`demo_server.py` + `demo_ui.html`) can show any repository that has been ingested into the local database.
A repository is chosen in the **Repository** drop-down in the header; the choice is kept in the address (`?repo=owner/name`),
so a repository can also be opened directly, for example `http://localhost:8501/?repo=fastapi/fastapi#step-8`.

## Before the panel (about 6 minutes, once)

```bash
.venv\Scripts\python.exe demo_server.py --repo python-poetry/poetry --warm --snapshot-root D:/ghas
```

* `--warm` computes every ingested repository in the background, one at a time, so switching in front of the panel is
  instant instead of taking 40-110 s. The drop-down marks finished repositories with a tick; wait until the ones you plan to
  show have it (poetry first, then the others in alphabetical order; dubbo takes the longest, about 2 minutes).
* `--snapshot-root D:/ghas` keeps the exported file paths short. Without it `apache/dubbo` fails on Windows (paths over about
  260 characters). Any short folder works.
* Open http://localhost:8501 and click through the repositories once to confirm.
* Figures can differ in the last digit between runs (the models are refitted), so do not quote a number you did not see on screen.

## Which repositories work (as ingested on 2026-10-12)

| repository | result in the UI | what it is good for |
|---|---|---|
| `python-poetry/poetry` | full pipeline (614 runs, 6 jobs, 9 `needs` edges) | the main walk-through: small enough to read every page; `tests-matrix` is predicted to fail often and is the longest job |
| `fastapi/fastapi` | full pipeline (699 runs, 6 jobs) | second example with a different workflow shape (aggregator job `test-alls-green`, low failure probabilities) |
| `apache/dubbo` | full pipeline (298 runs, 14 jobs, 23 edges) | a large workflow and 4,050 source files: shows the graph and the timeline at scale |
| `cli/cli` | full pipeline (293 runs, 2 jobs, no `needs`) | a degenerate case: nothing to reorder, so the plan is trivially the original |
| `prometheus/prometheus`, `pydantic/pydantic`, `astral-sh/uv`, `astral-sh/ruff`, `quarkusio/quarkus` | **declines** with the reason | show it on purpose: the system refuses to predict when there is too little real history for a model (for example no cache labels, a job with no history, fewer than 30 runs), and never fills the gap with made-up values |

Declines are a feature, not a bug: say so before the panel asks.

## Suggested 10-minute flow

1. **Dashboard of poetry (2 min).** Real history size, jobs, graph size, per-job predicted duration and failure probability,
   the estimated timeline that respects `needs`. Point at the three pills: REAL history, ESTIMATED savings.
2. **Pages 1-6 (2 min).** Workflow and DAG, source graph, CI graph, fusion edges, impact analysis of the real diff, the 15 features.
3. **Pages 7-10 (3 min).** Models and their validation, the schedule and its safety checks (all `needs` preserved), the proposed
   YAML and its diff. Be explicit: the default plan changes nothing (0% estimated saving); the levers that could change the time
   are opt-in.
4. **Switch to fastapi, then dubbo (1 min).** Same pipeline, different workflow; dubbo shows scale.
5. **Switch to uv or prometheus (1 min).** The decline page: honesty rule in action.
6. **Close (1 min).** What was measured and what was not: `docs/RESULTS.md`, `docs/IMPROVEMENT_RESULTS.md`.

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
