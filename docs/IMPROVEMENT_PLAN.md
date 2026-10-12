# Improvement programme: failure prediction, failure triage, CI speed, live tests (pre-registered 2026-10-12)

Requested by the owner: (1) improve the failure predictor beyond "2 of 9 repositories", (2) find a way to deal with real
failures, (3) try to improve CI speed, (4) run the failure detector on real data, (5) finish with a live test. Owner choices:
speed = relax **safe** `needs` gates (opt-in; CLAUDE.md rule 7 is amended for exactly that), failures = **triage** (no LLM).
This file fixes the rules before the new results exist; nothing here is changed in response to them.

## A. Failure predictor v2

**Where we start.** Under the old protocol the default (logistic) model significantly beats the "job's prior failure rate"
baseline in 2 of the 9 repositories (fastapi, pydantic), is never significantly worse, and the tree variants win elsewhere
(`docs/RESULTS.md` 3, `docs/HELDOUT_RESULTS_2.md`).

**Two changes, both about realism rather than model tuning.**

1. *Completion-aware visibility* ("protocol v2"). The old feature builder treats every earlier-**started** run as known. A run
   that started earlier but finished after the target run started was not known at queue time. v2 lets a run's outcome enter the
   history only after that run's `finished_at` is not later than the target run's start (unknown `finished_at`: old behaviour).
   Baseline and models are both recomputed under v2, so the comparison stays like for like. Old numbers stay as "protocol v1".
2. *Run-context, lineage and change-content features* (all available at queue time, all NaN when unknown):
   `event_pull_request`, `actor_is_bot`, `run_attempt`; lineage = the PR (else branch + author): `lineage_runs_so_far`,
   `lineage_prev_job_failed`, `lineage_job_fail_count`, `lineage_prev_run_any_failure` (visible runs only);
   change content from file names: `touches_tests`, `touches_dependencies`, `touches_ci_config`, `docs_only`, `n_top_level_dirs`;
   job history: `hours_since_last_failure`, `failure_rate_last5`, `job_prior_runs`.
   These are standard CI-failure predictors; none is derived from the dependency graph.

**Candidates (fixed now; hyper-parameters are those of the existing classes, no tuning):**
`failure_v2_logistic` (logistic on old + new features), `failure_v2_hgb` (the existing regularised boosted trees on old + new
features), `failure_v2_blend` (mean of the two probabilities). Reference models re-run under v2: `failure` (old logistic),
`failure_regularized_tree`, `failure_tree`.

**Development / test split.** Choices may be informed by the four development repositories (poetry, fastapi, prometheus, dubbo).
The other five (quarkus, pydantic, cli/cli, ruff, uv) are the test set: no feature, candidate or setting is adjusted after
looking at them. (Disclosure: I have seen the *old* models' results on them, which cannot be unseen.)

**Selection rule (development repos only):** the candidate with the highest mean PR-AUC lift over the baseline across the four
development repositories, among candidates that are significantly worse than the baseline in none of them; ties go to the
simpler model. Exactly one candidate is carried to the test set.

**Success criterion (decided now).** Count the repositories, out of all nine, where the selected candidate beats the baseline by
a paired-bootstrap 95% interval of PR-AUC that excludes 0 (rolling-origin folds as in `eval-models`). Repositories with fewer
than 20 positives (quarkus, cli/cli) count as *not won*. Report this count next to the same count for the old default under v2.
**Success = a count above the old default's count and at least 3, with no repository where the candidate is significantly worse
than the baseline.** If it is not met, it is reported as not met.

## B. Failure triage (what to do about real failures)

From real history, per failed job: the failing **step** (from the step records), a **class** by evidence in the data
(not guesses), and a recommended action.

| class | evidence | action |
|---|---|---|
| `flaky_rerun` | the same job passed on a later attempt of the same run, or on a re-run of the same commit | re-run |
| `pre_existing` | the same job also failed in the push run on the PR's base commit | not caused by this change; fix on the base |
| `persistent` | the same job failed in two or more consecutive visible runs of the same lineage | fix needed |
| `unrelated_to_change` | the change reaches none of the job's files according to the graph (only where the graph has file evidence) | check infrastructure / flakiness |
| `first_failure` | none of the above | investigate |

Failing steps are also bucketed by name (`setup/install/checkout/network`, `test`, `lint/format/typecheck`, `build`, `other`).
These are heuristics from recorded outcomes, not root causes; the report says so and prints the counts of each class.

## C. CI speed: relaxing safe `needs` gates

*Why this lever.* On free runners the only mechanism that shortens wall-clock without paid resources is letting jobs run
concurrently that were serialised only as a gate. Example: `test needs lint` (the validity run's own CI): 173 s -> about 134 s.

*Safety (conservative; anything uncertain is kept).* A `needs` edge `up -> down` is **relaxable** only if every condition holds:
`down` has no `environment`; no secret other than `GITHUB_TOKEN` is referenced in `down`; `down` has no deploy / publish step
(known publish actions; commands such as `docker push`, `twine upload`, `npm publish`, `gh release`, `git push`, `terraform apply`,
`kubectl`, `helm`, cloud upload commands); `down` never references `needs.<up>`; no artifact uploaded by `up` is downloaded by
`down` (any `download-artifact` in `down` while `up` uploads counts); `down` has no `if` that mentions `needs` or `always()`.
Aggregator jobs (`needs` on many jobs with `always()`) therefore never qualify. The change is opt-in
(`analyze --relax-gates`), lists every removed edge in the proposed file and the explanation, and is validated: the remaining edges
are a subset of the original edges and every removed edge is in the relaxable set. **Rule 7 is amended** to say exactly this.

*Trade-off stated up front.* The downstream job now also runs when `up` fails, so a failing run can bill more minutes.

*Measured on history (counterfactual, per repository):* with actual job durations, the critical path of each run under the
original and the relaxed DAG; reported on runs where every job ran, plus the extra billable time the relaxed DAG would have caused
in runs where `up` failed (typical duration of the job: stated as an assumption). *Measured live:* the project's own CI,
original vs relaxed, 6 runs per arm, alternating, as in `docs/VALIDITY_RUN.md`.

## D. Failure detector on real data, then a live test

*On real data:* `eval-models` for the v2 candidates on all nine repositories; a triage report for all nine; a ranked "highest
risk" list for the most recent real runs.
*Live test, at the end:* (i) the relaxed workflow on real GitHub Actions (above); (ii) a **prospective** failure-detection test:
for real pull-request / push runs of a public repository that are *queued or in progress at scoring time*, predictions are
written to a ledger (with timestamp, run id, model hash) before the outcome exists; after the runs finish, the ledger is scored
(PR-AUC, precision at the top 10% of scores, against the baseline). Predictions are never edited after the fact.

## Reporting
Everything above is reported with its numbers whether or not it helps, in `docs/IMPROVEMENT_RESULTS.md`.
