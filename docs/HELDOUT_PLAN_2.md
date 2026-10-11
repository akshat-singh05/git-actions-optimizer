# Second held-out test: workflows that gate on changed files (pre-registered 2026-10-11)

**Why.** `docs/HELDOUT_RESULTS.md` found that the graph helps only where a workflow states a file-to-job relationship, but
neither held-out repository (pydantic, cli/cli) had file-based gating, so that pattern rests on poetry and fastapi alone.
This second test uses two repositories whose CI **does** gate jobs on the changed files, to see whether the graph captures it.

**Freeze.** Code under test: the tree at commit `d144425` (graph feature version 2, replay policy, `SAFE_ACTION_PREFIXES`).
No rule, threshold or safe-action entry is changed in response to the results. A defect in the *tooling* (parse failure,
job-mapping bug) may be fixed and is disclosed; the numbers are then regenerated.

**Repositories (never ingested or analysed before; chosen because their CI is known to compute "what changed" and gate jobs on it;
only the workflow text was read to confirm this, no run outcomes):**

| repo | workflow | gating mechanism (read from the workflow text) |
|---|---|---|
| `astral-sh/ruff` | `.github/workflows/ci.yaml` | a `determine_changes` job computes outputs from `git diff`; later jobs use `needs: determine_changes` and `if:` on its outputs |
| `astral-sh/uv` | `.github/workflows/ci.yml` | a `plan` job computes outputs; many jobs are local reusable workflows gated on `needs.plan.outputs.*` |

**Stated expectation, before any data.** The graph's static rules read declarative gates (`paths-filter`, path
arguments, toolchain scope, cache globs). Both repositories compute their gates in **shell script**, which the rules do not
parse, so I expect the impacted flag to fall back to the weaker toolchain / path-argument rules or to "unknown" and *not*
to agree with executed-vs-skipped. A weak result here is the predicted outcome, not a surprise; if the flag does agree,
that is the more informative result.

**Procedure** (default settings; bounded, no logs because cache labels are not under test):
`gha-optimizer ingest --repo R --workflow <path> --since 40d --max-runs 150 --no-logs`, `build-dataset`, `replay-impact`,
`replay-impact --deferral`, `dataset-report`, `eval-models`. If ingestion throughput makes 150 runs impractical it is stopped
by hand at a number decided on throughput alone and the number is reported. Runs are the newest in the window.

**Pre-stated readings (same bars as `docs/HELDOUT_PLAN.md`, applied as written):**

* The *readable* skip policy generalises on a repository only if the failure rate of the skipped jobs is below that of the
  other jobs (lift < 1) **and** fewer than 5% of failing runs would look green. Fewer than 20 failing runs: inconclusive.
* The graph features generalise only if the impacted flag separates executed from skipped jobs in the direction the
  workflow's own gating implies; otherwise they are reported as uninformative there.
* Deferral is judged by `docs/DEFERRAL_PLAN.md` unchanged.
* The logistic failure model and the log-ratio duration target are reported against their baselines, nothing more.
* Two more repositories change the weight of the evidence, not its kind: no general claim is made.
