# Deferral: what can and cannot be measured (pre-registered 2026-10-11)

**The policy.** *Deferral*: on `pull_request` runs, do not run the jobs the readable DAG-safe graph policy would skip
(`gha_optimizer/replay.py`, `dag_safe_readable`); they still run in the workflow's `push` runs. Nothing is generated;
this document only fixes how the idea is evaluated, before any figure is computed.

**What the database cannot tell us.** Whether a failure hidden on a PR would have shown up *after merge*. That needs a
PR -> merge-commit link: `pr_number` is set on only about 30% of PR runs (it is empty for fork PRs) and squash merges
leave no trace of the PR head on the default branch. So **"recurs at merge" is not measured and no figure about it is
reported.**

**What it can tell us (all MEASURED on history, counterfactual in the same sense as `docs/IMPACT_REPLAY.md`):**

1. *Saving.* Billable seconds of the jobs the policy would defer on `pull_request` runs, as a share of (a) the
   `pull_request` runs' billable time and (b) the repository's total billable time over all events. Pushes and
   scheduled runs are unchanged, so only PR time can be saved.
2. *Hidden failing PR runs.* PR runs whose every failed job would be deferred (the run would look green).
3. *Failures that are new on the PR.* For each deferred job that failed in a PR run, look up the push run on the
   default branch at that PR run's base commit (`diff_base_sha`):
   * **pre-existing** - that job also failed there, so the failure was not introduced by the PR and deferring it hides
     nothing new;
   * **new on PR** - that job ran and passed there, so the failure appeared with the PR (or is a flake);
   * **unknown** - there is no push run at the base commit, or the job did not run there.
   A masked run is *new on PR* if any of its deferred failures is new; *unknown* if none is new but some are unknown.
   For the safety criterion below, unknown is counted as new (worst case); the optimistic count is reported next to it.

**Pre-stated reading, applied as written to every repository with at least 20 failing PR runs** (fewer: inconclusive):

* Deferral is **supported** for a repository only if (A) it would save at least 5% of the repository's total billable
  time **and** (B) masked PR runs counted worst-case (new + unknown) are fewer than 5% of the failing PR runs.
* It is **not supported** if (A) fails (nothing worth deferring) or (B) fails (too many failures would first show
  after merge).
* A "supported" repository is still not a claim that deferral is safe: "recurs at merge" is unmeasured, and a deferred
  failure on `main` costs more than one on a PR.

**Repositories:** poetry, fastapi, prometheus, dubbo (the repositories the policy was developed on), and the held-out
pydantic and cli/cli. quarkus has 24 runs and is left out. Rules and thresholds above are fixed; they are not changed in
response to the results.

## Result (2026-10-11; generated figures: `docs/DEFERRAL_REPLAY.md`)

Command: `gha-optimizer replay-impact --deferral --repo python-poetry/poetry --repo fastapi/fastapi --repo prometheus/prometheus --repo apache/dubbo --repo pydantic/pydantic --repo cli/cli`. The criteria above were applied unchanged.

| repo | failing PR runs | saved of total billable time (readable policy) | worst-case hidden PR runs | pre-stated verdict |
|---|---|---|---|---|
| poetry | 76 | 0.0% | 0.0% | not supported (A: saving < 5%) |
| fastapi | 117 | 0.2% | 0.0% | not supported (A) |
| prometheus | 645 | 1.5% [1.4%, 1.6%] | 2.3% [1.2%, 3.5%] | not supported (A) |
| dubbo | 77 | 0.2% [0.0%, 0.6%] | 2.6% [0.0%, 6.7%] | not supported (A) |
| pydantic (held out) | 37 | 0.5% [0.3%, 0.7%] | 0.0% | not supported (A) |
| cli/cli (held out) | 1 | 13.4% [10.1%, 16.7%] | 0.0% | inconclusive (1 failing PR run) |

Reading, as pre-stated: **deferral is not supported in any repository where it can be judged.** The obstacle is value, not
safety: criterion B (fewer than 5% of failing PR runs hidden, worst case) is met in all five judged repositories (0.0% to
2.6%), but the policy that is safe enough - the readable one - defers almost nothing (0.0% to 1.5% of total billable
time), because the jobs it may skip are few once `make`, remote and composite actions are excluded. cli/cli would save
13.4% but has a single failing PR run, so nothing can be said about what it would hide.

Context, not a verdict (the plain DAG-safe policy is not the pre-registered one): it would defer 13.4% of prometheus's
total billable time and 10.2% of pydantic's, with worst-case hidden shares of 4.3% [2.9%, 6.0%] and 2.7% [0.0%, 9.1%].
At job level, 181 of the 202 deferred-job failures in prometheus passed on the base commit (i.e. they are new with the
PR). Only 30 PR runs are masked, so most of those job failures sit in runs that also contain a failure in a job the
policy would still run; the run-level figure therefore understates what the plain policy hides at job level. (The
earlier replay traced the largest source of such misses in prometheus to the `make`-driven Go test job; that was not
re-tabulated here.)

Limits: "recurs after merge" is not measured; only 78 of 539 fastapi PR runs have a push run at their base commit
(prometheus 1,732 of 1,842, poetry 461 of 475), which matters only where failures were masked (none in fastapi);
pull_request_target runs are counted with pull_request; results are counts on 5-7 weeks of history with intervals that are
optimistic (PR runs of one lineage are not independent).
