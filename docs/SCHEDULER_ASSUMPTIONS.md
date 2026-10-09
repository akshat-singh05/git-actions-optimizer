# What the scheduler's estimate rests on

Written 2026-10-07, revised 2026-10-08 (skip guards became opt-in). Every figure below was produced by running the code on the ingested data
(`python examples/demo.py --repo python-poetry/poetry`, and `POST /api/v1/optimize` against a live server).
All savings are **ESTIMATED from predicted durations**; none is a measured execution.

## 1. What the estimate is made of

`scheduler/optimizer.py::build_schedule` computes wall-clock as the **sum over DAG levels of the maximum
predicted duration at that level** (jobs of one level run concurrently). The baseline runs every job; the
optimized plan differs in exactly two ways:

| lever | what it does | what it depends on |
|---|---|---|
| **skip recommendations** (**off by default**, `--enable-skips` / `allow_skip_recommendations`) | jobs downstream of a job with predicted failure probability >= 0.85 (or cumulative skip probability > 0.85) get an `if: ${{ false }}` guard | the failure predictions, **and** a baseline that assumes every job runs - see section 3b: it cannot save time under GitHub's `needs` rules |
| **runner upsizing** | a job moves to a larger-runner tier when that lowers `α·time + β·cost + γ·energy` | an **assumed** speed-up law `speedup = price_multiplier ** exponent` (default exponent 0.6) |

Two things the earlier headline implied but the code does not do:

* **Fail-fast ordering has no effect on the estimate.** Wall-clock is a function of levels and durations only,
  so reordering jobs inside a level changes nothing in the figures.
* **The cache-miss probability is not consumed by the scheduler.** It is reported per job and unused in the plan.

## 1b. Why skip guards are opt-in (measured 2026-10-08)

Two independent reasons, one logical and one empirical.

**Logical.** GitHub already skips a job whose `needs` upstream failed (unless the job says `if: always()` or
similar). The plan only skips jobs *downstream* of a job predicted to fail. If the prediction is right, GitHub
would have skipped them anyway, so nothing is saved; if it is wrong, the guard removes tests that would have run
and passed. The "saving" the skip lever is credited with exists only against a baseline in which every job runs.

**Empirical.** On held-out runs (train on the first 80% of runs, score the last 20%), how often do the failure models
reach the thresholds, and how often are those flags right?

| repo (held-out base failure rate) | model | rows >= 0.85 (precision) | rows >= 0.5 (precision) |
|---|---|---|---|
| poetry (7.3%) | production tree / regularised tree / logistic | 0 / 0 / 0 | 7 (0.43) / 2 (1.00) / 0 |
| fastapi (4.9%) | production tree / regularised tree / logistic | 0 / 0 / **12 (0.25)** | 4 (0.50) / 5 (0.20) / 21 (0.19) |
| prometheus (3.1%) | production tree / regularised tree / logistic | **1 (0.00)** / 0 / 0 | 28 (0.32) / 4 (0.00) / 41 (0.10) |
| dubbo (5.2%) | production tree / regularised tree / logistic | 0 / 0 / 0 | 2 (0.00) / 0 / 2 (0.00) |

The 0.85 threshold is almost never reached; when it is, the flagged job failed 0-25% of the time. Mean predicted
probabilities are close to the observed rates (calibrated on average), but the tail is over-confident, so no
decision should act on a single probability. Skip candidates are still identified and reported ("would skip (not
applied)"); only the guards are opt-in.

## 1c. Replaying a graph-based skip policy on real history (measured 2026-10-08)

The one lever that needs neither a speed-up assumption nor a failure prediction is the graph itself: skip a job
the diff cannot reach. `gha-optimizer replay-impact` replays that policy over every ingested run
(`docs/IMPACT_REPLAY.md`, `gha_optimizer/replay.py`). The figures are **MEASURED on history** - billable time is the
summed cell durations of what actually ran - but the policy was not run, so it is a counterfactual that assumes
skipping a job changes no other job's outcome. Only jobs that really ran are counted: what is measured is the
*additional* saving over the workflow as written. Three variants: *naive*; *dag_safe* (a job is skipped only if every
`needs` dependent also did not run or is also unreached, because skipping a job makes GitHub skip its dependents);
and *readable* (additionally never skip a job, or let one be skipped through `needs`, whose steps the graph cannot
read: `make`, remote / composite actions, reusable-workflow calls).

| repo (runs) | DAG-safe: billable time saved | DAG-safe: failing runs that would look green | readable: time saved | readable: failing runs that would look green |
|---|---|---|---|---|
| poetry (596) | 0.2% | 1 of 72 | 0.0% | 0 of 72 |
| fastapi (682) | 13.4% [10.6%, 16.4%] | 14 of 133 (10.5%) | 12.4% [9.8%, 15.1%] | 14 of 133 (10.5%) |
| prometheus (2,163) | 17.1% [15.9%, 18.5%] | 38 of 698 (5.4%) | 1.6% [1.5%, 1.8%] | 22 of 698 (3.2%) |
| dubbo (293) | 3.8% [1.8%, 6.4%] | 5 of 85 (5.9%) | 0.4% | 2 of 85 (2.4%) |
| quarkus (24) | 6.5% [0.0%, 21.1%] | 1 of 20 | 0.0% | 0 of 20 |

What it shows:

* **The saving is real where a workflow runs many jobs for diffs the graph cannot reach** (fastapi about 12-13%,
  prometheus 17%), with intervals that exclude 0, and it is **not free**: the plain policy would make 5-10% of failing
  runs look green. Poetry's workflow already gates its jobs with `paths-filter`, so there is nothing extra to save.
* **The misses are graph blind spots, not noise.** In prometheus the largest miss is the Go test job: 147 failures
  among the 491 runs the graph calls "unreached" (30% vs 19% overall), almost all bot dependency-update branches
  changing `web/ui` - because that job runs through `make`, which also builds the UI, and the Makefile is not read.
* **The readable guard removes most of that saving where the opacity is real** (prometheus 17.1% -> 1.6%, dubbo
  3.8% -> 0.4%) and does not make the remaining flags safer there (failure rate of skipped jobs 10.1% vs 5.7% in
  prometheus, 15.6% vs 4.3% over 32 jobs in dubbo). In fastapi it keeps the saving (12.4%) and the skipped jobs do fail
  less than the rest (3.7% vs 8.3%, lift 0.49), but 14 failing runs would still look green; they include failures of
  the `alls-green` aggregator, which can be cancellations of superseded runs rather than real failures - the stored data
  cannot separate the two.
* Counting is per job, so an aggregator that fails because an upstream job failed is counted again; the run-level
  column ("failing runs that would look green") is the fairer safety figure.

**Conclusion.** A static graph-skip policy trades roughly 12-17% of billable time (in the two repos where it applies)
for hiding 3-10% of failing runs. That is not shippable as an automatic default, so no generator for it was built;
`replay-impact` is the evidence. Defensible uses are advisory (report which jobs the graph considers unreached, with
these measured rates) or deferral (run unreached jobs only on pushes to the default branch).

**Status (2026-10-10): the advisory use is IMPLEMENTED; deferral is not.** `gha_optimizer/advisory.py` classifies the
unreached jobs of a live diff with the same `skipped_jobs` / `opaque_job_keys` code that was replayed (skip candidate /
kept because a dependent is reached or unreadable / kept because its own steps are unreadable; jobs with no file
evidence are never advised) and `analyze` / `/api/v1/optimize` append the section "Advisory: jobs the dependency graph
says this diff cannot reach (NOT applied)" with the repository's replay figures. It changes neither the schedule nor the
proposed YAML (checked by a test). Deferral is not built because the replay cannot measure it - it shows what skipping
costs but not how often a failure hidden on a pull request recurs at merge - and by trigger event the saving is
concentrated where deferral would not touch it: fastapi 2% of its saving is on `pull_request` runs (85% on `push`),
prometheus 78% (naive) / 92% (readable) on pull requests, dubbo 71% / 54%. "push" here is any push, not only pushes to
the default branch.

Caveats: the rule set was written after reading these repositories' workflows, so this is not a held-out test of the
rules; the opacity guard and the `alls-green` exemption were added after seeing the first results; intervals are
cluster bootstraps over runs and optimistic (runs of one PR share a lineage); quarkus has 24 runs.

## 2. Why the speed-up law cannot be measured here

The ingested history contains **one standard runner size per operating system** (poetry: ubuntu/windows/macos
images, fastapi: `*-latest`, prometheus: ubuntu + a little windows, dubbo: `ubuntu-22.04`; quarkus has three
jobs on a custom `runs-on=...m6a.xlarge` runner, too few to say anything). With no job ever run on two sizes, how
much faster a larger runner is has no support in the data. The tier prices follow GitHub's public larger-runner
prices; the power figures and the speed-up exponent are configuration assumptions.

## 3. What the plan now reports (poetry, final data)

Default plan (no larger runners, no assumption): **0.0% wall-clock saved, 0 jobs skipped** - no poetry job
reaches the failure threshold, so the plan equals the baseline.

What-if with larger runners allowed, as a function of the **assumed** exponent (same run):

| assumed speed-up exponent | wall-clock saved | cost saved | energy saved |
|---|---|---|---|
| 0.0 (a larger runner is not faster) | 0.0% | 0.0% | 0.0% |
| 0.3 | 0.0% | 0.0% | 0.0% |
| 0.6 (default) | 48.4% | -67.2% (more expensive) | -56.1% |
| 0.9 | 72.1% | -46.3% | -27.7% |

Reading it: the previously quoted ~44-48% wall-clock "improvement" is entirely the 0.6 row - an assumption, paid
for with ~67% higher cost - and it jumps from 0% to 48% between exponents 0.3 and 0.6 because the weights
(α=0.7, β=0.25, γ=0.05) make the planner switch tiers all at once once the assumed speed-up outweighs the price.
That threshold behaviour is a property of the objective, not evidence about real runners.

## 4. Changes made because of this

* Skip guards are **opt-in** (`config.scheduler.allow_skip_recommendations`, `--enable-skips`, API
  `allow_skip_recommendations`); with both levers off the plan equals the baseline, which is the honest default.
* Larger-runner relabelling is **opt-in** (`config.scheduler.allow_runner_upsizing`, `--larger-runners`,
  API `allow_runner_upsizing`); the larger-tier labels only exist if the organisation configured them.
* The speed-up exponent is a named, documented configuration value (`config.scheduler.speedup_exponent`),
  not a literal in a formula, and every plan carries the exponent it used.
* `SchedulePlan` and the explanation now separate the skip effect, the larger-runner effect and their combination,
  and print the sensitivity table above.

## 5. Defects found while tracing this (all fixed, all covered by tests)

| defect | why it mattered |
|---|---|
| the proposed workflow contained a top-level `# gha-optimizer:` **key** | GitHub rejects unknown top-level keys, so the file would not load |
| the skip guard was `if: "${{ false }}  # reason"` | text after the expression makes the value a non-empty string, i.e. truthy: the job would not be skipped. It is now exactly `${{ false }}` (the original condition is kept inside it) and the reason is a header comment |
| `runs-on` was overwritten with the base tier for **any** job | a `windows-latest`, `macos-*` or `${{ matrix.os }}` job would have changed operating system. Only a job already on the base tier can move, and only with upsizing enabled |
| `dashboard/app.js` rendered hard-coded results ("42.2% faster", ROC-AUC 0.89, random accuracy trends) | fabricated values presented as output. It now shows only what the API returns, or an explicit empty state, and views the API has no data for say so |
| `/health` always reported `db_connected: false`; `/ready` never touched the DB | `db.execute("SELECT 1")` fails on SQLAlchemy 2.x; readiness only built a session |

## 6. What still cannot be claimed

A measured improvement. That needs the original and the proposed workflow run on real runners, repeatedly and
alternately, with the wall-clock read back from the GitHub API - none of which has been done.
