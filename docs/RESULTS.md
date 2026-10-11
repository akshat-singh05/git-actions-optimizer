# Consolidated results (2026-10-11)

Everything below is copied from reports generated from the database (`gha-optimizer dataset-report`, `eval-models`,
`replay-impact`) or from the pre-registered plans; the source file is named in each section. Nothing here is a
wall-clock measurement of an optimized workflow: **no optimized workflow has been run**, so no speed-up is MEASURED.
"ESTIMATED" always means derived from predicted durations; "MEASURED on history" means computed from recorded runs
(a counterfactual when a policy was not actually executed).

## 1. Data (`docs/DATASET_REPORT.md`, `docs/HELDOUT_DATASET_REPORT.md`)

Real GitHub Actions history, ingested with `gha-optimizer ingest`. Only runs whose jobs and diff are known are admitted.

| repository (workflow) | runs | (run, job) rows | failure positives (pooled test) | notes |
|---|---|---|---|---|
| python-poetry/poetry (`tests.yaml`) | 596 | 3,576 | 76 | graph rules developed on it |
| fastapi/fastapi (`test.yml`) | 682 | 3,975 | 160 | " |
| prometheus/prometheus (`ci.yml`) | 2,163 | 44,507 | 808 | " |
| apache/dubbo (`build-and-test-pr.yml`) | 293 | 4,102 | 93 | " |
| quarkusio/quarkus (`ci-actions-incremental.yml`) | 24 | 504 | 9 | too small to judge |
| pydantic/pydantic (`ci.yml`) | 196 | 5,473 | 75 | **held out** (never read before the plan) |
| cli/cli (`go.yml`) | 293 | 586 | 2 | **held out**; 1 failing run: inconclusive for failure |
| astral-sh/ruff (`ci.yaml`) | 183 | 5,962 | 140 | **held out #2**; script-gated CI; ingested without logs |
| astral-sh/uv (`ci.yml`) | 148 | 3,700 | 34 | **held out #2**; script-gated CI; ingested without logs |

Cache labels exist only where job logs were downloadable (retention about 45-90 days; a per-invocation download budget):
tens to a few hundred labelled runs per repository.

## 2. Does the dependency graph describe what the workflow does? (`docs/GRAPH_FEATURES.md`, dataset reports)

The structural feature `job_structurally_impacted` is checked against the workflow's own behaviour (a job skipped by the
workflow vs executed):

* **poetry** - skipped 1% of the time when impacted vs 93% when not: agrees (the workflow gates jobs with `paths-filter`).
* **fastapi** - 3% vs 37%: agrees.
* **prometheus, quarkus** - wrong direction; **dubbo** - uninformative.
* **pydantic, cli/cli (held out)** - neither workflow has file-based gating; pydantic's flag points the wrong way
  (skipped 36.5% when impacted vs 23.8% when not) and cli/cli never skips a job, so agreement could not be shown.

* **ruff, uv (held out #2, `docs/HELDOUT_RESULTS_2.md`)** - both gate jobs with a shell-script job, not a declarative filter.
  The flag points the right way (ruff: jobs it calls unreached are skipped 40.6% vs 26.7%; uv 37.2% vs 29.6%, among
  jobs that skip at least once) but weakly, and the plain skip policy disagrees with the workflow's own gate on 81.5% of
  the jobs it ran in ruff.

Reading: the graph is only informative where the workflow itself states a file-to-job relationship. It is a static
approximation (toolchain rules over-approximate, intra-repo imports are unresolved).

## 3. Prediction models vs naive baselines (`docs/MODEL_EVAL.md`, `docs/HELDOUT_MODEL_EVAL.md`)

Method: expanding-window rolling-origin folds, pooled out-of-sample predictions, paired cluster bootstrap (1,000
resamples, seed 13). Baselines use no model: the job's prior failure rate, prior mean duration, prior cache-hit rate.
Intervals are optimistic (runs of one PR share a lineage). Many comparisons were made; treat single cells as leads.

**Failure (default: regularised logistic regression), PR-AUC, model vs baseline (difference, 95% interval):**

| repo | positives (base rate) | model | baseline | difference |
|---|---|---|---|---|
| poetry | 76 (8.2%) | 0.2047 | 0.1403 | +0.0703 [-0.0116, +0.1796] |
| fastapi | 160 (9.0%) | 0.4187 | 0.3325 | **+0.0851 [+0.0161, +0.1579]** |
| prometheus | 808 (4.5%) | 0.1732 | 0.1561 | +0.0178 [-0.0078, +0.0500] |
| dubbo | 93 (5.0%) | 0.2141 | 0.2168 | -0.0066 [-0.0619, +0.0562] |
| quarkus | 9 (17.3%) | 0.6807 | 0.6952 | -0.0101 [-0.1224, +0.0804] |
| pydantic (held out) | 75 (4.0%) | 0.3291 | 0.2270 | **+0.1104 [+0.0141, +0.2340]** |
| ruff (held out #2) | 140 (6.9%) | 0.5684 | 0.3992 | +0.1688 [-0.0001, +0.3175] |
| uv (held out #2) | 34 (3.1%) | 0.1605 | 0.1510 | +0.0089 [-0.0683, +0.1070] |

In ruff both tree variants beat the baseline by about +0.39 PR-AUC with intervals far from 0 (original tree +0.399 [+0.274, +0.511]) while the logistic default does not clear it, so the safest default can leave a large gain unused there (a lead, one repository). The logistic model is the only variant never significantly worse than the baseline; it was chosen from the same folds
it is judged on, so `docs/FROZEN_CHOICE.md` pre-states a fresh-data test. The original boosted tree is significantly
worse than the baseline in dubbo and quarkus. **Interim fresh-data run (3 days of new runs, 2026-10-10):** passed in the
only repository with at least 20 fresh failures (prometheus, +0.002 [-0.058, +0.067]); the other three could not be
judged. Final verdict due about 2026-10-21.

**Duration (default: `log_ratio` target), MAE in seconds, model minus baseline (negative = model better):**
poetry -1.43 [-1.95, -0.87]; fastapi -0.23 [-0.62, +0.15]; prometheus -0.92 [-1.69, -0.04]; dubbo **+0.78** [+0.13, +1.47]
(worse); quarkus +60.9 [-8.4, +116.9]; pydantic (held out) -1.04 [-1.77, -0.32]; cli/cli (held out) **+6.5** (worse); ruff (held out #2) -1.06 [-2.14, +0.06]; uv (held out #2) -2.16 [-3.75, -0.67] (better). The raw-seconds
target is significantly worse than the baseline in every repository (e.g. poetry +18.0 s).

**Cache any-miss:** fastapi +0.071 [+0.032, +0.112] (better); prometheus -0.039 [-0.057, -0.022] (worse); poetry +0.043
and dubbo +0.003 (indistinguishable); cli/cli +0.120 [-0.020, +0.318] on 50 test rows. Samples are small.

**Do the graph features help the models?** Removing them lowers failure PR-AUC for the logistic model in poetry (+0.037,
interval excludes 0) and fastapi (+0.038), and fastapi's cache target (+0.028); elsewhere the effect is indistinguishable
from 0 (prometheus: -0.009, slightly worse with them; pydantic: +0.013, includes 0). These are leads, not established effects.

## 4. Scheduler (`docs/SCHEDULER_ASSUMPTIONS.md`)

* The default plan **equals the baseline** (0.0% estimated saving on real poetry). Fail-fast ordering does not change the
  estimate, and the cache-miss probability is not consumed by the scheduler.
* Skip recommendations (opt-in): under GitHub's `needs` semantics a correct prediction saves nothing and a wrong one
  removes tests; on held-out runs the flags at the 0.85 threshold were right only 0-25% of the time.
* Runner upsizing (opt-in) uses an **assumed** speed-up law (exponent 0.6); the ingested data has one runner size per OS,
  so it cannot be estimated. The earlier ~46-48% figure exists only as a labelled what-if with cost +64-67%.

## 5. Graph-based skipping, measured on history (`docs/IMPACT_REPLAY.md`, `docs/HELDOUT_IMPACT_REPLAY.md`)

Policy: skip a job the graph says the diff cannot reach (MEASURED on history; counterfactual - the policy was not run).

| repo | DAG-safe: time saved | failing runs that would look green | readable variant: time saved |
|---|---|---|---|
| poetry | 0.2% | 1 of 72 | 0.0% |
| fastapi | 13.4% [10.6%, 16.4%] | 14 of 133 (10.5%) | 12.4% [9.8%, 15.1%] |
| prometheus | 17.1% [15.9%, 18.5%] | 38 of 698 (5.4%) | 1.6% |
| dubbo | 3.8% [1.8%, 6.4%] | 5 of 85 (5.9%) | 0.4% |
| pydantic (held out) | 27.3% [21.2%, 34.4%] | 4 of 45 (8.9%) | 0.8% (skipped jobs fail *more* often: lift 1.20) |
| cli/cli (held out) | 26.6% | 0 of 1 | 21.2% (1 failing run: inconclusive) |
| ruff (held out #2) | 81.6% [75.9%, 87.6%] | 52 of 77 (67.5%) | 2.0% (20 of 77 look green; lift 1.05) |
| uv (held out #2) | 36.5% [28.0%, 45.8%] | 18 of 47 (38.3%) | 0.0% (lift 7.19) |

Reading: the saving is real where a workflow runs many jobs a diff cannot reach, but it costs failures hidden from the
developer and **did not generalise** to the held-out repositories (`docs/HELDOUT_RESULTS.md`, `docs/HELDOUT_RESULTS_2.md`). So it is only an advisory
report (`gha_optimizer/advisory.py`), never applied automatically. Deferral to default-branch pushes was evaluated under a
pre-registered plan and is not supported: the readable policy defers 0.0-1.5% of total billable time in every repository
with at least 20 failing PR runs (`docs/DEFERRAL_PLAN.md`, `docs/DEFERRAL_REPLAY.md`; ruff 1.9% and uv 0.0% in `docs/HELDOUT2_DEFERRAL_REPLAY.md`); recurrence after merge could not
be measured.

## 6. Removed after verification

* **GNN cache predictor** - never ran (torch was never installed), schema-incompatible with the current graph, no
  held-out metric. Removed 2026-10-08.
* **Matrix reducer** - never wired into an entry point; on real per-cell outcomes its own coverage estimate was 1.00 while
  fastapi's kept cells caught 13 of 17 later failing runs. Removed 2026-10-08.

## 7. Threats to validity

* One CI workflow per repository, nine repositories, 5-7 weeks of history each; results may not transfer. The four
  held-out repositories were chosen by me from prior knowledge of their CI design, not drawn at random.
* Failure positives are few in several repositories; intervals are optimistic and wide.
* The failure-model default was selected on the same folds it is evaluated on; the fresh-data check is interim.
* The graph rules were written after reading the first five workflows; pydantic, cli/cli, ruff and uv are held out.
* Replay results are counterfactual: they assume skipping a job changes no other job's outcome.
* All improvement numbers are ESTIMATED; none has been confirmed by running both workflows.
