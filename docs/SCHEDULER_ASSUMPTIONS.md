# What the scheduler's estimate rests on

Written 2026-10-07. Every figure below was produced by running the code on the ingested data
(`python examples/demo.py --repo python-poetry/poetry`, and `POST /api/v1/optimize` against a live server).
All savings are **ESTIMATED from predicted durations**; none is a measured execution.

## 1. What the estimate is made of

`scheduler/optimizer.py::build_schedule` computes wall-clock as the **sum over DAG levels of the maximum
predicted duration at that level** (jobs of one level run concurrently). The baseline runs every job; the
optimized plan differs in exactly two ways:

| lever | what it does | what it depends on |
|---|---|---|
| **skip recommendations** | jobs downstream of a job with predicted failure probability >= 0.85 (or cumulative skip probability > 0.85) are removed from the plan | the failure predictions - which do **not** beat a naive baseline (`docs/MODEL_EVAL.md`) |
| **runner upsizing** | a job moves to a larger-runner tier when that lowers `α·time + β·cost + γ·energy` | an **assumed** speed-up law `speedup = price_multiplier ** exponent` (default exponent 0.6) |

Two things the earlier headline implied but the code does not do:

* **Fail-fast ordering has no effect on the estimate.** Wall-clock is a function of levels and durations only,
  so reordering jobs inside a level changes nothing in the figures.
* **The cache-miss probability is not consumed by the scheduler.** It is reported per job and unused in the plan.

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
