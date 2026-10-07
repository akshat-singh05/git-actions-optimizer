# Graph features that carry signal

Status of this document: written 2026-10-07 for **graph feature definition version 2**
(`GRAPH_FEATURE_VERSION` in `gha_optimizer/graph/dependency_graph.py`). Every number in
section 4 is read from the database by `gha-optimizer dataset-report` (section 7 of
`docs/DATASET_REPORT.md`); none is typed in by hand.

## 1. Why version 1 was degenerate (measured, before this change)

On all five ingested repositories **0 rows were "impacted via a real graph path"**,
`dep_graph_distance` was constant, and `cache_key_touched` varied only for prometheus (7 rows)
and dubbo (130 rows). Causes, found by reading the code and the real workflows:

| cause | where |
|---|---|
| a step's label was its `name` when it had one, so its command was invisible to fusion | `graph/builder.py` |
| only parseable source files were `File` nodes; docs, manifests, lock files, Makefiles were "not in the graph" | `features/graph_features.py` |
| fusion linked files to jobs only through `pytest <dir>` / `go test <prefix>` style commands | `graph/fusion.py` |
| local reusable workflows and composite actions (poetry, quarkus) hide all commands | not resolved |
| `dorny/paths-filter` + `if: needs.changes.outputs.X` (poetry, fastapi) - the authoritative file→job map - ignored | not parsed |
| cache keys: only the first `hashFiles()` argument, `**/x` did not match a root-level `x`, `setup-go` / `setup-uv` / `setup-java` built-in caches unknown | `graph/fusion.py` |
| distance was measured on the undirected graph through the shared `Workflow` node, so every job was 3 hops from every file | `graph/dependency_graph.py` |
| when no changed file was a node, every job was declared "impacted" - a fabricated 1 | `graph/dependency_graph.py` |

## 2. What version 2 does

Fusion (`graph/fusion.py`) links `File` nodes to the jobs that *read* them. Rules, strongest
evidence first; each edge carries its rule in `provenance.creation_source` and a fixed confidence:

| rule | conf. | evidence |
|---|---|---|
| `paths_filter_gate` | 0.95 | job gated on `needs.<job>.outputs.<name>` of a `dorny/paths-filter` job → the files matching that filter. A gate is a precondition for the job to run, so it **supersedes** rules 2-4 for that job. |
| `step_path_reference` | 0.80 | a path / directory argument of a `run:` command that exists in the tree (`pytest tests/unit`, `./scripts/x.sh`) |
| `toolchain_command` | 0.50 | the command drives a toolchain: `test`/`build` read the language's sources under the working directory, `check` (ruff, golangci-lint, eslint …) reads only the paths it is given, `install` reads only manifests |
| `setup_action_toolchain` | 0.40 | job runs `actions/setup-go` etc. and no command was recognised |
| `cache_key_hashfiles` / `implicit_cache_default` | 0.90 / 0.80 | `File → CacheKey` for every glob a key is a hash of; built-in caches of `setup-go`, `setup-python`, `setup-node`, `setup-java` (maven), `setup-uv` use their *documented* default globs |

Supporting changes: step nodes keep the full `run` / `uses` / `with` / `working-directory` / `if`
(`NodeMetadata.attributes`); local reusable workflows and composite actions are inlined from the
git tree at the run's commit (`graph/workflow_expand.py`, `inputs.x` is bound to the caller's `with:`
expression so a gate reaches the inlined job); every tree path is a `File` node; coarse scopes are
`BuildTarget` / `Directory` nodes rather than one edge per file and job; the unused Package→Job and
Directory→Job passes were removed.

### Feature semantics (one implementation: `dependency_graph.job_graph_features`)

* `job_structurally_impacted` - **1** a changed file reaches the job over `File → (Directory | BuildTarget) → Job → (Job | Artifact)` edges (so jobs downstream via `needs` inherit impact); **0** the job has file-level evidence but no changed file reaches it; **NaN (unknown)** the job has no file-level evidence at all, or no changed file is a graph node. The old "all jobs impacted" fallback is gone from the features.
* `dep_graph_distance` - hops from the nearest changed file along those edges; NaN when unreached.
* `cache_key_touched` - 1 if a cache the job restores is keyed on a changed file; 0 if the job restores no cache, or every cache it restores is keyed on known files none of which changed; NaN if some cache's key cannot be tied to files (no `hashFiles`, undocumented built-in cache such as gradle or golangci-lint).
* `job_has_file_links` - stored per row for reporting.

`impacted_jobs_for_diff` (used by the demos) stays **conservative**: reached jobs *plus* every
unlinked job, because a scheduler may not skip a job it cannot prove unaffected. Features keep
"unknown" as NaN; scheduling keeps it as "assume impacted". Those are different questions.

Rows computed under an older definition are marked by `feature_version` (migration
`003_graph_feature_version`) and ignored by `load_history`; `build-dataset` recomputes them.

## 3. Deliberately not done

* **Intra-repo import resolution** (Python/Java/Go/JS). Toolchain scope already links every source
  file of the language to the jobs that build or test it, so resolving imports would only refine
  `dep_graph_distance`, at the price of four resolvers. Parsers still emit unresolved `Package` nodes.
* **`make` targets.** The Makefile is not read; a job that only runs `make <target>` (most of
  prometheus) stays *unknown* unless it also has a path argument or a `setup-*` action.
* **Runtime module selection** (quarkus's Scalpel impact analysis computes the module matrix inside a
  script). The toolchain rule over-approximates there: every Maven job is linked to every Java file.
* **Remote actions** (`owner/repo@ref`, e.g. `prometheus/promci-setup`) are opaque.
* Workflow-level `on.push.paths` filters and `paths-filter` filter *files* (not inline) are not read.

The toolchain rules are an explicit **over-approximation** (confidence 0.5): they say "this job reads
that language's sources", not "this job is affected by this particular file".

## 4. Measured result (version 2, final data: 2026-10-07)

Source: `docs/DATASET_REPORT.md` sections 7, 7.1, 7.2 and `docs/MODEL_EVAL.md` (regenerate with
`gha-optimizer dataset-report` / `eval-models`). The history was extended after the first measurement
(poetry / fastapi / prometheus 600 runs each, dubbo 300, quarkus still 30), so these figures replace the
~120-run figures an earlier revision of this file quoted. The 220-test suite passes.

**Coverage - before (version 1, ~100 runs) → after (version 2, final data)**

| repo | (run, job) rows | impacted = 1 | impacted = 0 | unknown | `cache_key_touched = 1` |
|---|---|---|---|---|---|
| python-poetry/poetry | 3576 | 0 → 1085 (30.3%) | 1880 (52.6%) | 611 (17.1%) | 0 → 102 |
| fastapi/fastapi | 3546 | 0 → 1754 (49.5%) | 1161 (32.7%) | 631 (17.8%) | 0 → 305 |
| prometheus/prometheus | 12587 | 0 → 5634 (44.8%) | 1556 (12.4%) | 5397 (42.9%: `make` / remote actions) | 7 → 291 |
| apache/dubbo | 4102 | 0 → 3920 (95.6%) | 168 (4.1%) | 14 | 130 → 530 |
| quarkusio/quarkus | 504 | 0 → 414 (82.1%) | 18 (3.6%) | 72 | 0 → 0 (cache keys carry no `hashFiles`: 336 unknown) |

**Does `impacted` agree with what the workflow did?** (skipped vs executed, no model involved)

| repo | skipped when impacted = 1 | skipped when impacted = 0 | reading |
|---|---|---|---|
| poetry | 1.2% (n=1085) | 93.2% (n=1880) | strong - the workflow gates on paths-filter and the graph reproduces the gate |
| fastapi | 2.8% (n=1754) | 36.5% (n=1161) | strong where gated (jobs that skip at least once: 4.2% vs 69.6%) |
| prometheus | 18.8% (n=5634) | 13.0% (n=1556) | **wrong direction** (jobs that skip at least once: 50.3% vs 22.9%): its jobs skip on event / tag conditions, not on files |
| dubbo | 5.3% (n=3920) | 2.4% (n=168) | **uninformative** (p = 0.11): 95.6% of rows are impacted; the Maven toolchain rule links every Java file to every Maven job |
| quarkus | 33.8% (n=414) | 5.6% (n=18) | **wrong direction**: jobs are skipped by a script-computed matrix the static graph cannot see |

So the structural features carry real signal **where the workflow itself states a file→job relationship**
(poetry, fastapi) and none, or the opposite, where it does not (prometheus, dubbo, quarkus). That is a
property of those workflows; more rules of the same kind will not fix it.

**Label association** (descriptive, rows not independent). Failure rate when `impacted = 1` vs `0`:
poetry 13.1% (n=1071) vs 1.6% (n=128); fastapi 13.1% (n=1699) vs 2.8% (n=724); prometheus 4.7% vs 2.6%;
dubbo 4.4% vs 5.4% (reversed); quarkus 12.2% vs 11.8%. Cache: `cache_key_touched = 1` → exact key not
restored in 86.5% of fastapi rows (n=37) vs 23.5% when 0 (n=85); in poetry and dubbo almost every touched
row is a *partial* hit (a `restore-keys` fallback) that `label_cache_hit` counts as a hit, and against
"exact key not restored" the feature is saturated (dubbo 97% vs 100%). The cache-labelled sample is small:
cache labels exist only for runs inside GitHub's log-retention window (about 100 runs per repository).

## 5. Do the graph features help the models? (`docs/MODEL_EVAL.md`)

Rolling-origin evaluation (expanding window, 5 folds over the runs that carry the target's label, pooled
out-of-sample predictions, paired bootstrap over runs). Feature sets compared on identical folds: `all`,
`no_structural`, `structural_only`, and a naive baseline that needs no model.

* **Failure** (76-146 pooled test positives per repository): `all - no_structural` PR-AUC is not
  distinguishable from 0 in any repository. No fitted model beats the prior-failure-rate baseline
  (poetry / fastapi / prometheus: interval includes 0); in dubbo and quarkus the baseline is significantly
  better.
* **Cache** (25-197 labelled test rows - small): the graph features raise PR-AUC for fastapi
  (`all - no_structural` +0.117, 95% interval [+0.016, +0.272]) but not for the others, and in prometheus the
  baseline is significantly better than any model. For the `exact_miss` target dubbo's models beat the
  baseline significantly, but that target is 84% positive there and the graph features are not the cause.
* **Duration**: the raw-seconds models are **significantly worse than the prior-mean baseline in every
  repository** (MAE poetry 33.7 s vs 15.6 s, prometheus 121.8 vs 109.4, dubbo 44.1 vs 29.9). Predicting the
  log-ratio to the job's prior mean instead (`duration_ratio` in `docs/MODEL_EVAL.md`) removes most of that gap
  (poetry 14.1 s, fastapi 9.7 s, prometheus 114.6 s, dubbo 30.7 s) and beats the baseline significantly in poetry
  (-1.49 s) and fastapi (-0.69 s), while staying significantly worse in prometheus (+5.3 s) and dubbo (+0.8 s).
  Graph features under that target: fastapi -0.38 s (helps), dubbo +1.45 s (hurts), others inconclusive.

Conclusion that the data supports: the graph features are a correct, informative description of the workflow
for poetry and fastapi (section 4) but add **no demonstrable predictive value over history-based features**
on failure and duration, and a possible one on fastapi's cache target. These are findings, not defects of
the evaluation; the cache evidence rests on few rows.

Procedure note: the first run of `eval-models` cut the folds over all runs, which left most cache folds
without test labels (1-2 of 5 used). The folds are now cut over the runs that carry the target's label.
That change was made after seeing the sparse folds; it is a correction of the procedure, not a tuning of results.
