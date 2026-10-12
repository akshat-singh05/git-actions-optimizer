# Likely viva questions, with answers grounded in the repository

Every figure below is taken from a generated report in `docs/` (named in brackets). If a reviewer asks for a number that is
not here, say it is not measured rather than estimating it on the spot.

## What was built, and what is the result?

**1. In one sentence, what does the project do?**
It ingests real GitHub Actions history, links a dependency graph (files, jobs, steps, cache keys, `needs`) to it, predicts
job failure / duration / cache misses, schedules jobs without violating `needs`, and writes a *proposed* workflow with a
reviewable diff. [README, CLAUDE.md]

**2. Does it make CI faster?**
Not shown. The default plan equals the baseline (0.0% estimated saving on poetry), and a real run agrees: the original and the
proposed workflow were executed 6 times each on GitHub Actions with no detectable difference (+0.3%, p = 0.92) and identical
behaviour (`docs/VALIDITY_RUN.md`). The only figures above zero are
labelled ESTIMATED what-ifs that rest on an assumed speed-up law (exponent 0.6) which the data cannot confirm. A measured
claim needs real runs of both workflows, which have not been done. [SCHEDULER_ASSUMPTIONS 1-2, demo output]

**3. Then what is the contribution?**
(a) A real, reproducible dataset pipeline over nine repositories; (b) a graph-to-features-to-predictions-to-scheduler chain
that runs end to end and refuses to predict on thin data; (c) an evaluation methodology that is stricter than the usual
(rolling-origin folds, paired bootstrap, baselines, pre-registered held-out tests); (d) honest negative results about what
a static dependency graph can and cannot add. We say plainly that (d) is the main finding. [RESULTS.md]

## Data and method

**4. Is the data real? Where does it come from?**
Yes. GitHub REST API for runs, jobs, steps and logs; a local bare git mirror for workflow YAML and diffs at each commit.
About 4,200 runs and 62,700 (run, job) rows over nine repositories. Limits: job logs expire (about 45-90 days), so cache
labels exist for only tens to a few hundred runs per repository. The only synthetic generator lives in the test fixtures and
cannot be imported by the package. [DATASET_REPORT, REAL_DATA_FOUNDATION]

**5. How do you avoid leakage?**
History features use only strictly earlier runs; evaluation uses expanding-window rolling-origin folds over time (never a
random split); the decayed-prior baselines are computed the same way. [MODEL_EVAL, rolling_eval.py]

**6. Why a bootstrap and not a t-test?**
Rows of one run (and runs of one pull request) are not independent. Intervals are a cluster bootstrap over runs (1,000
resamples, fixed seed). We still call them optimistic. [MODEL_EVAL]

**7. You compared many things. Is that p-hacking?**
It is a risk and we say so: 3 failure models x repositories x contrasts. That is why single cells are called leads, why the
default model was frozen with a pre-stated pass/fail rule, and why the graph rules were retested on repositories chosen and
planned in writing before any data was looked at. [FROZEN_CHOICE, HELDOUT_PLAN, HELDOUT_PLAN_2]

## Models

**8. Do the models work?**
Mixed, and always shown next to a baseline. Failure (logistic default): significantly better than "the job's prior failure
rate" in fastapi (+0.085 PR-AUC) and in held-out pydantic (+0.110), indistinguishable in the rest, never significantly
worse. Duration: the raw-seconds model is worse than the prior mean wherever it is distinguishable; the log-ratio target is
better in poetry, prometheus, pydantic and uv, worse in dubbo and cli/cli. Cache: small samples, mixed. [RESULTS.md 3]

**9. Why is logistic regression the default failure model?**
It was the only variant never significantly worse than the baseline in the five development repositories. The choice was
made from the same folds it is judged on, so it is not proof; a fresh-data test is pre-registered (an interim run passed in the
one repository with enough failures; the final verdict is due about 2026-10-21). In held-out ruff the tree variants did far
better (+0.39) - we report that and did not change the default. [FROZEN_CHOICE, HELDOUT_RESULTS_2]

**10. The demo shows the cache model with a Brier score worse than its baseline. Why keep it?**
It is reported as informational only and the scheduler does not consume it, precisely because it does not reliably beat its
baseline. [demo output, SCHEDULER_ASSUMPTIONS]

## The graph

**11. Does the dependency graph help?**
Only where the workflow itself states which files affect which job. In poetry (1% vs 93% skipped when impacted vs not) and
fastapi (3% vs 37%) it agrees with the workflow's own gating. Elsewhere it is uninformative or weak: on held-out ruff and uv
the flag points the right way but only by 7-14 points. The failure models gain nothing measurable from it in the held-out
repositories (intervals include 0). [GRAPH_FEATURES, HELDOUT_RESULTS, HELDOUT_RESULTS_2]

**12. Why not just rely on GitHub's `paths` filters?**
That is what poetry does, and the replay shows nothing extra to gain there. Where gating is done in scripts (ruff, uv) our
static rules do not parse it, which is a limitation of the approach, not hidden.

**13. Why was the GNN removed?**
Verification showed it had never run (torch was never installed), could not have worked on the current graph (attribute
names and vocabularies did not match; several node features were never set), and trained on one copy of today's graph with no
held-out metric. The graph already reaches the models through structural features, and the tabular cache model does not beat
its baseline in most repositories. It is recoverable from git history. [CLAUDE.md, RESULTS.md 6]

**14. Why was matrix reduction removed?**
It was never wired into an entry point, and on real per-cell outcomes its own "coverage 1.00" claim was false: fastapi's kept
cells caught only 13 of 17 later failing runs. Dropping matrix cells silently shrinks CI coverage. [RESULTS.md 6]

## Scheduler and generator

**15. Why can't skipping save time?**
Under GitHub's default `needs` behaviour a dependent job is skipped anyway when its upstream really fails, so a *correct*
failure prediction saves nothing and a *wrong* one removes tests. On held-out runs the flags at the 0.85 threshold were right
only 0-25% of the time. Skip guards are therefore opt-in. [SCHEDULER_ASSUMPTIONS 1b]

**16. How do you guarantee the generated workflow is valid?**
Checks run on every plan: all jobs preserved, `needs` respected, acyclic, valid YAML; the proposed file has no non-standard
keys and never edits `runs-on` of Windows/macOS/matrix jobs. The original file is never modified. Known limitation: the
generator re-serialises YAML, so **comments and blank lines are not preserved** in the proposal (visible in the demo diff),
though semantics are. [workflow_writer.py, demo output]

**17. What is the "Advisory" section?**
The graph-based skip idea, reported but never applied: which jobs the graph says the diff cannot reach, with the measured
rates on the repository's history (time saved, failing runs that would look green). It exists because the replay showed the
policy is unsafe to apply automatically. [advisory.py, IMPACT_REPLAY]

## The LLM, security, engineering

**18. Does an LLM make decisions?**
No. It is an opt-in wording layer that runs after everything is decided; the plan and the written workflow are identical with
and without it (tested). Its output is discarded unless every number appears in the facts it was given. It has not yet been
run against the live service. [LLM_EXPLANATION]

**19. What data leaves the machine?**
Normal use: GitHub API calls and git clones. With `--llm-summary`: job ids, runner labels and computed numbers only - no
file paths, commit ids, repository names or workflow text. The GitHub token travels in a header / git environment config,
never in a URL. [LLM_EXPLANATION, FULL_STACK_RUN]

**20. Is the server production ready?**
No. The full stack was run once end to end (webhook -> Redis -> worker -> authenticated clone -> Postgres), which exposed five
defects that are now fixed, and the remaining Celery tasks were then run on real data. Caveats: without
`GITHUB_WEBHOOK_SECRET` the webhook accepts unsigned requests, and the feedback / drift loop was removed because nothing ever fed
it. [FULL_STACK_RUN]

## Hard questions - honest answers

**21. "So the project failed?"**
The *hypothesis* that a static graph plus tabular ML yields a scheduler that speeds up CI was not supported. The project
produced a working, tested system, a real dataset, and a pre-registered, multi-repository test that says so. That is a
legitimate research outcome; the failure mode we avoided is claiming a speed-up we cannot measure.

**22. "What would you do next?"**
Parse script-computed gates (the ruff/uv case); a measured experiment running original vs proposed workflows on a controlled
repository (needs paid larger runners for the runner lever); more held-out repositories chosen by a written rule; wider cache
labels. [README, RESULTS.md 7]

**23. "Did you improve the failure predictor?"**
Not by the standard we set in advance. We pre-registered (`docs/IMPROVEMENT_PLAN.md`) that a candidate must beat the
prior-failure-rate baseline in at least 3 of 9 repositories with none significantly worse. The chosen v2 model beat it in 2
(prometheus, ruff) and lost in none, so the criterion is reported as not met. A pooled model reached 3 but was designed after
looking at results, so it is only a lead; it is frozen and will be judged on runs after 2026-10-12T14:58:29Z.
[IMPROVEMENT_RESULTS.md A, POOLED_FROZEN.md]

**24. "What do you do about real failures?"**
`gha-optimizer triage` classifies each failed job from recorded outcomes (aggregator, flaky on the same commit, pre-existing,
fixed by a later commit, persistent, unrelated to the change) with the failing step and an action. They are heuristics, not root
causes; flakiness is under-detected because a re-run overwrites the earlier attempt; the recommendations have not been compared
with what developers did. [FAILURE_TRIAGE.md, IMPROVEMENT_RESULTS.md B]

**25. "You said DAG correctness is sacred. Why does --relax-gates remove `needs`?"**
The owner amended the rule on 2026-10-12 for one opt-in case: only gate-only edges that code can prove carry no data and guard
no side effect (no `needs.x` reference, no secrets, no deploy/publish, no status-function condition). Every removal is listed,
the output is validated against the original, and the trade-off is printed: the job now also runs when its upstream fails.
Anything not provably safe is kept. [CLAUDE.md rule 7, gates.py]

**26. "Did relaxing gates make CI faster?"**
Only in the case we ran for real: on our own lint -> test pipeline the critical path went from 159.5 s to 135.5 s (-15.1%,
6 vs 6 runs, p = 0.013, MEASURED) while billable time rose 4.1% (not significant). On nine mature repositories' history the
replay finds about 0%: their slow path is the test/build jobs themselves. One repository; failing runs were not measured.
[IMPROVEMENT_RESULTS.md C, relax_run/, GATE_REPLAY.md]

