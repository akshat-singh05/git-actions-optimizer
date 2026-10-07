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

## 4. Measured result (version 2, recomputed 2026-10-07 from the ingested data)

Source: `docs/DATASET_REPORT.md` sections 7, 7.1, 7.2 (regenerate with `gha-optimizer dataset-report`).
All five repositories were recomputed with the final code; the 207-test suite passes.

**Coverage - before (version 1) → after (version 2)**

| repo | rows impacted via a real graph path | rows with `cache_key_touched = 1` |
|---|---|---|
| python-poetry/poetry | 0 → 226 of 780 | 0 → 26 |
| fastapi/fastapi | 0 → 265 of 720 | 0 → 76 |
| prometheus/prometheus | 0 → 997 of 2094 (42.7% of rows unknown: `make` / remote actions) | 7 → 31 |
| apache/dubbo | 0 → 784 of 798 | 130 → 130 |
| quarkusio/quarkus | 0 → 414 of 504 | 0 → 0 (cache keys carry no `hashFiles`: 336 rows unknown) |

**Does `impacted` agree with what the workflow did?** (skipped vs executed, no model involved)

| repo | skipped when impacted = 1 | skipped when impacted = 0 | reading |
|---|---|---|---|
| poetry | 5.3% (n=226) | 92.2% (n=424) | strong - the workflow gates on paths-filter and the graph reproduces that gate |
| fastapi | 2.3% (n=265) | 36.4% (n=335) | strong where gated (jobs that skip: 3.4% vs 65.6%) |
| prometheus | 18.8% (n=997) | 11.3% (n=203) | **wrong direction / no signal** - its jobs skip on event / tag conditions, not on files |
| dubbo | 9.1% (n=784) | n/a (no impacted = 0 row) | **uninformative** - 98.2% of rows impacted; the Maven toolchain rule links every Java file to every Maven job |
| quarkus | 33.8% (n=414) | 5.6% (n=18) | **wrong direction** - jobs are skipped by a script-computed matrix the static graph cannot see |

So the structural features carry real signal **where the workflow itself states a file→job relationship**
(poetry, fastapi) and little or none where it does not (prometheus, dubbo, quarkus). That is a property
of those workflows, not something more rules of the same kind will fix.

**Label association** (descriptive, rows not independent): `cache_key_touched = 1` → exact cache key not
restored in 85.7% of fastapi rows vs 22.1% when 0 (n=35 / 77). In poetry and dubbo the touched rows are
almost all *partial* hits (a `restore-keys` prefix fallback) and the binary `label_cache_hit` counts those
as hits, so the feature looks anti-predictive against that label there; against "exact key not restored"
it is saturated (poetry 100% of 9 vs 70% of 84; dubbo 97% vs 100%). Failure rate by `impacted`:
poetry 12.1% (n=214) vs 0.0% (n=33); fastapi 7.8% vs 1.9%; prometheus 3.6% vs 2.1%; quarkus 12.2% vs 11.8%.
Failure positives per repository are 17-38, so none of these differences is established.

**What was NOT measured:** whether the models improved. The temporal holdouts contain 1-11 failure
positives per repository, so a with/without-graph-features model comparison would not be meaningful and
is not reported. `dep_graph_distance` now varies, but takes only 2 distinct values in every repository and is
nearly redundant with `impacted`.

**Cost:** with a warm parse cache a per-commit graph took about 9 s per run for poetry (five repositories
recomputing in parallel) and about 90 s for quarkus (32k files, 176k nodes, 380k edges); quarkus dominates
the recompute (24 distinct commits).
