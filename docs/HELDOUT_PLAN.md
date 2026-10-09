# Held-out test of the graph rules and the skip policy (pre-registered 2026-10-10)

**Why.** The fusion rules (`graph/fusion.py`, `graph/ci_semantics.py`) and the replayed skip policy
(`gha_optimizer/replay.py`) were written after reading the workflows of poetry, fastapi, prometheus, dubbo and quarkus,
and two guards (`make`/opaque-action, `alls-green`) were added after seeing replay results. `docs/IMPACT_REPLAY.md` is
therefore not a held-out test. This document fixes a test on repositories that were never ingested or read, **before**
any of their data is looked at.

**Freeze.** The code under test is the tree at commit `17d802a` (graph feature version 2, the replay policy, the
`SAFE_ACTION_PREFIXES` list as it is). No rule, threshold, safe-action entry or guard may be changed in response to
the results below. If a defect in the *tooling* (not the rules) is found - e.g. a parse failure - it is fixed and
disclosed, and the numbers are regenerated.

**Repositories and workflows (chosen from the public workflow list only, not from their contents or history):**

| repo | workflow | why |
|---|---|---|
| `pydantic/pydantic` | `.github/workflows/ci.yml` | Python library, multi-job CI, aggregator job, many runs |
| `cli/cli` | `.github/workflows/go.yml` | Go application (a second toolchain), multi-job CI |

**Procedure.** `gha-optimizer ingest --repo R --workflow <path> --since 40d --max-runs 400` (bounded; never without
`--max-runs`), `build-dataset`, `replay-impact`, `eval-models` for failure / duration_ratio / cache targets, all with
default settings.

**What is reported, whatever it shows:**

1. Whether the graph's impacted flag agrees with executed-vs-skipped (as in `docs/GRAPH_FEATURES.md`).
2. The replay table (billable time saved, failing runs that would look green, failure rate skipped vs rest, lift vs
   random) for the naive, DAG-safe and readable policies, with the bootstrap intervals.
3. The rolling-origin model evaluation against the naive baselines.

**Pre-stated reading (set now, applied as written):**

* The *readable* skip policy **generalises** only if, on a repository it was not built on, the failure rate of the
  skipped jobs is below that of the other jobs (lift vs random < 1) **and** fewer than 5% of failing runs would look
  green. Otherwise it is reported as not generalising.
* The graph features **generalise** only if the impacted flag separates executed from skipped jobs in the direction the
  workflow's own gating implies (fewer executions when "not impacted") - or, where the workflow has no file-based
  gating, they are reported as uninformative there, as for prometheus.
* A repository with too few failing runs (< 20) is reported as inconclusive for the failure targets, not as a pass.
* With two repositories nothing here is a general claim; it is evidence for or against the two statements above.
