# Second held-out test: results (2026-10-11)

Plan and pre-stated readings: `docs/HELDOUT_PLAN_2.md` (committed as `3515ded` before any data was ingested). Code frozen at
`d144425`; no rule, threshold or safe-action entry was changed. Generated reports (all computed from the database,
**MEASURED on history**): `docs/HELDOUT2_IMPACT_REPLAY.md`, `docs/HELDOUT2_DEFERRAL_REPLAY.md`,
`docs/HELDOUT2_DATASET_REPORT.md`, `docs/HELDOUT2_MODEL_EVAL.md`. The boilerplate sentence in the replay reports saying the
rules were written after reading these workflows refers to the first five repositories, not to ruff and uv.

## What was ingested (and deviations from the plan)

* `--max-runs 150 --no-logs`, newest runs of the last 40 days. ruff: 150 runs processed, **183 runs** carry graph features
  and enter the analysis (5,962 job rows, 140 pooled-test failures); uv: **148 runs** (3,700 rows, 34 pooled-test failures;
  221 runs were seen, 150 processed).
* The first ruff attempt failed on a network drop during `git clone`, the retry hit repeated SSL / connection errors
  to the GitHub API and resumed after backoff; no partial data was used (ingestion is idempotent). No rule or code changed.
* No logs were downloaded, so there are **no cache labels** and the cache targets are unevaluated for both repositories.

## Statement 1: does the readable skip policy generalise?

Pre-stated test: lift vs random < 1 **and** fewer than 5% of failing runs would look green (>= 20 failing runs to judge).

| repo | readable policy: time saved | failing runs that would look green | failure rate skipped vs rest | lift | verdict |
|---|---|---|---|---|---|
| ruff | 2.0% [1.8%, 2.2%] | 20 of 77 (26.0% [16.7%, 36.6%]) | 6.6% vs 6.2% | 1.05 | **does not generalise** (both conditions fail) |
| uv | 0.0% | 3 of 47 (6.4% [0.0%, 14.0%]) | 20.8% vs 2.7% | 7.19 | **does not generalise** (both conditions fail; 24 jobs only) |

The plain DAG-safe policy (context): ruff would "skip" 81.5% of the job executions the workflow itself chose to run and
call 81.6% of billable time unnecessary, with 52 of 77 failing runs (67.5%) looking green; uv 36.5% and 18 of 47 (38.3%).
In both the skipped jobs fail somewhat *less* than the rest (lift 0.79 and 0.83), but the policy hides far too many
failures to be usable. The scale of the ruff figure is itself the finding: the graph's idea of what a diff reaches
disagrees strongly with the workflow's own script-based gating.

## Statement 2: do the graph features generalise?

Pre-stated test: the impacted flag must separate executed from skipped jobs in the direction the workflow's gating implies.

| repo | scope | impacted=1 rows: skipped | impacted=0 rows: skipped |
|---|---|---|---|
| ruff | all jobs | 13.6% | 16.9% |
| ruff | the 14 jobs that skip at least once | 26.7% | 40.6% |
| uv | all jobs | 33.9% | 40.9% |
| uv | the 22 jobs that skip at least once | 29.6% | 37.2% |

* **By the literal criterion (direction) it passes in both repositories**: jobs the graph calls unreached are skipped more
  often. **This is not what my plan predicted** (I expected no agreement, because both gates are computed by shell
  scripts the rules do not parse). The criterion I wrote tests direction only, which turns out to be lenient.
* **By magnitude it is weak**: a difference of 7-14 percentage points, against 1% vs 93% in poetry and 3% vs 37% in
  fastapi. With this much overlap the flag cannot stand in for the workflow's own gate, which the replay confirms.
* The flag is associated with failures (ruff: failure rate 13.5% when impacted vs 5.2% when not; uv: 4.1% vs 2.1%), but the
  ablation shows the failure models gain nothing from it: all minus no-structural PR-AUC is -0.005 [-0.036, +0.015] in
  ruff and +0.005 [-0.004, +0.015] in uv (both include 0).

## Statement 3: deferral (`docs/DEFERRAL_PLAN.md`, criteria unchanged)

| repo | failing PR runs | saved of total billable time (readable) | worst-case hidden PR runs | verdict |
|---|---|---|---|---|
| ruff | 77 | 1.9% [1.8%, 2.1%] | 26.0% [16.2%, 36.5%] | not supported (saving < 5%; also fails the 5% hidden bar) |
| uv | 27 | 0.0% | 11.1% [0.0%, 24.2%] | not supported (saving < 5%) |

Most hidden ruff runs are classed "unknown" (no push run at the PR's base commit: only 64 of 175 PR runs have one), so
the worst case is an upper bound; the saving alone already fails.

## Models (default settings)

| target | ruff | uv |
|---|---|---|
| failure, logistic (default) | PR-AUC 0.568 vs baseline 0.399: +0.169 [-0.0001, +0.318], includes 0 by 0.0001 (140 positives) | 0.161 vs 0.151: +0.009 [-0.068, +0.107] (34 positives) |
| failure, original tree | 0.812: **+0.399 [+0.274, +0.511]** | +0.007 [-0.065, +0.088] |
| failure, regularised tree | 0.801: **+0.388 [+0.266, +0.501]** | +0.050 [-0.019, +0.142] |
| duration, raw seconds | +0.68 s [-0.78, +2.26] | **+14.2 s [+6.7, +23.7] (worse)** |
| duration, log-ratio (default) | -1.06 s [-2.14, +0.06] | **-2.16 s [-3.75, -0.67] (better)** |
| cache | no labels (ingested with `--no-logs`) | no labels |

Reading: consistent with earlier results for duration (the log-ratio target is at least as good as the prior mean and
better in uv; the raw-seconds model is worse where it is distinguishable). **For failure, ruff is the first repository
where the logistic default is clearly dominated**: both tree variants beat the baseline by about +0.39 PR-AUC with
intervals far from 0, while the logistic model's +0.17 does not exclude 0. The default was chosen because it is never
significantly worse than the baseline, not because it is best, and that criterion still holds here; but this is evidence
that a safer default can leave a large gain unused. It is a lead (one repository, 140 positives; one possible explanation, not
tested here, is that failures persisting across consecutive runs of a PR are easy for a tree to exploit) and does not change the frozen choice, whose own
fresh-data verdict is still pending (`docs/FROZEN_CHOICE.md`).

## Overall (with `docs/HELDOUT_RESULTS.md`, four held-out repositories)

| claim | outcome across pydantic, cli/cli, ruff, uv |
|---|---|
| graph skip policy picks jobs that fail less than average | **not supported** where judgeable (pydantic 1.20, ruff 1.05, uv 7.19 readable); cli/cli inconclusive |
| graph skip policy saves time without hiding failures | **not supported**: the plain DAG-safe policy would make 9-68% of failing runs look green in the three judgeable held-out repositories (pydantic 8.9%, uv 38.3%, ruff 67.5%); the safe variant saves 0.0-2.0% (cli/cli: 21.2% on one failing run) |
| impacted flag reproduces the workflow's gating | **weak**: right direction in ruff and uv, small effect; not evaluable in pydantic / cli/cli |
| graph features improve the failure model | **not supported** (all four: interval includes 0) |
| deferral is worth building | **not supported** (0.0-2.0% of total time in every judgeable repository) |
| log-ratio duration beats the prior mean | significantly better in 4 of the 9 repositories evaluated so far (poetry, prometheus, pydantic, uv), indistinguishable in fastapi, ruff and quarkus, worse in dubbo and cli/cli |

Caveat: the repositories were chosen by me from prior knowledge of their CI design, not drawn at random; four
repositories are still four repositories.
