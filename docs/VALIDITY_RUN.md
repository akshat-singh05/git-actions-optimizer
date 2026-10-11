# Validity run: the original and the proposed workflow, executed on real GitHub Actions (2026-10-12)

**Question.** Does a workflow produced by `gha-optimizer analyze` run on GitHub Actions, behave like the original, and is the
estimated saving close to what a real run shows? This is the first time a generated workflow was executed; before this,
validity was only checked by parsing and by structural tests.

**Result in one paragraph.** Over 12 measured runs (6 per arm, alternating, plus one warm-up per arm) every run succeeded; the
two arms ran the same 3 jobs and the same 30 steps with the same conclusions; `needs` was respected in every run; and the
measured wall-clock did not differ: critical path median 172.5 s (original) vs 173.0 s (proposed), +0.3%, permutation
p = 0.92. The estimate for this workflow was 173 s with a 0.0% saving. So: **the proposal is valid and equivalent, and it is
not faster, exactly as the 0.0% estimate said.** No speed-up is shown and none was expected for the default plan.

## Setup

* Repository: the project's own private repository; workflow `.github/workflows/ci.yml` (3 jobs: `lint`, then `test` and
  `typecheck` in parallel, `ubuntu-latest`, about 30 steps). Standard GitHub-hosted runners.
* **History for `analyze`** (it declines under 30 runs): 27 scratch pull-request commits (docs / a source comment /
  `requirements.txt`, so diffs and cache outcomes vary) were pushed to a throwaway branch; with the 9 earlier runs of this workflow this gave 36 runs
  (the 4 failures in the history are among the 9 earlier runs; the 27 new runs all passed). It is a **controlled** history
  produced by the experiment itself.
* `gha-optimizer ingest` (with job logs), `build-dataset` and `analyze --no-model-store` produced the proposed file
  (`docs/validity_run/arm_b_proposed_ci.yml`; original in `arm_a_original_ci.yml`). Parsed, the two files have identical
  triggers, jobs and name; the proposal differs textually (reserialised YAML, no comments, header comment).
* **Arms**: two throwaway draft PRs against `main` (A: original `ci.yml`; B: the proposal installed as `ci.yml`). Each arm had
  one warm-up run (pip caches) that is excluded; then 6 measured runs per arm, **alternating** (A,B / B,A / ...), one run at a time,
  each triggered by an empty commit. PRs and branches were closed and deleted afterwards; nothing was merged.
* Measured from the GitHub API: critical path = first job start to last job end; run time; summed job time; per-job time; step
  names and conclusions. Raw data: `docs/validity_run/timings.json`, summary `summary.json`.

## Results (12 measured runs)

| quantity | A original | B proposed | difference (median) | exact permutation p |
|---|---|---|---|---|
| critical path, median (min-max) | 172.5 s (131-177) | 173.0 s (120-182) | +0.5 s (+0.3%) | 0.92 |
| run time, median | 176.5 s | 177.0 s | +0.5 s (+0.3%) | 1.0 |
| summed job time (billable proxy), median | 211.0 s | 215.5 s | +4.5 s (+2.1%) | 0.81 |
| `lint` / `typecheck` / `test` median | 35 / 45 / 134 s | 36 / 44.5 / 133 s | within 1 s each | - |

Validity checks, all true for all 14 runs: every run `success`; every job `success`; the same 30 (job, step, conclusion)
triples in both arms; `typecheck` and `test` started after `lint` completed (the `needs` edge).

**Estimate vs measurement.** `analyze` estimated a baseline wall-clock of 173 s and an optimized wall-clock of 173 s (0.0%
saved). Measured: 172.5 s for the original, 173.0 s for the proposal. The estimate is close, but see the caveat: it is not
an independent test of the models.

## Caveats (read these before quoting a number)

1. **Noise is large.** Within an arm the critical path ranges 120-182 s (runner variance: the `test` job alone took 81-141 s),
   so with 6 runs per arm only a difference of tens of seconds would be detectable. The result is "no detectable
   difference", not "exactly equal".
2. **The estimate matching is not evidence of model skill.** The history was 31 near-identical runs of the same three jobs, so
   "predicted duration" is essentially the historical median of those jobs. A close match here says the estimate is
   computed sensibly, not that the models predict well; model skill is judged in `docs/RESULTS.md`.
3. **One small workflow on one repository, standard runners.** It says nothing about large matrices, other runner sizes, or the
   opt-in levers: skip guards and larger runners were **not** exercised (larger runners need paid organisation runners).
4. The controlled history is not natural history; the experiment tests the generated YAML, not the predictions.
5. The proposal drops YAML comments and rewrites `run: |` blocks as quoted scalars; semantics were identical here (same
   steps ran with the same results), but a human reviewer sees a noisier diff.

## What this closes, and what it does not

* **Closes:** "a generated workflow has never been run on GitHub". It runs, behaves identically, and respects `needs`.
* **Does not close:** "measured speed-up". The default plan is the baseline by design and measured as such. A measured
  improvement would need a lever that actually shortens the critical path (larger runners, or removing work), which this
  repository cannot demonstrate on free runners.

## Defects found while preparing the run (all fixed, with tests)

* **Run-window times were shifted by the machine's UTC offset** (`ingestion/github_client.py::_fmt` treated naive UTC
  datetimes as local time). On this UTC+5:30 machine every listing window was shifted back 5.5 h, so each ingest left out the
  newest 5.5 hours of runs (windows are contiguous, so later ingests recovered them; no run was lost permanently and no
  bias results, but the newest runs of any one ingest were missing). Found because only 1 of 31 fresh runs was listed.
* **`analyze` crashed with a raw traceback** when the history contained no failing job (`failure model needs both
  outcomes`). `require_history` now declines cleanly with the reason (`need_failure_labels`).
