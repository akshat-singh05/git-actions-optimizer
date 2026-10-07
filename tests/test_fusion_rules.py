"""Fusion rules on small workflows shaped like the real ones they were written for.

No file exists on disk: ``tracked_files`` gives path-only File nodes and ``read_file`` serves the
reusable workflows / composite actions, which is exactly how the per-commit graph builder works.
"""
from __future__ import annotations

import math
from pathlib import Path

from gha_optimizer.graph.dependency_graph import (
    build_dependency_graph,
    cache_keys_touched_by,
    graph_distance_to_job,
    impacted_jobs_for_diff,
    job_graph_features,
)
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text


def graph(workflow: str, files: list[str], extra: dict[str, str] | None = None):
    return build_dependency_graph(
        parse_workflow_text(workflow), Path("."), tracked_files=files, read_file=(extra or {}).get
    )


def feats(g, changed, jobs):
    return job_graph_features(g, changed, jobs)


def impacted(f, job):
    return f[job]["job_structurally_impacted"]


# ---- rule 1: dorny/paths-filter gates (the poetry / fastapi shape) ------------------------------

GATED = """\
name: CI
on: push
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      src: ${{ steps.filter.outputs.src }}
      docs: ${{ steps.filter.outputs.docs }}
    steps:
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            src:
              - 'src/**/*.py'
              - pyproject.toml
            docs:
              - 'docs/**'
  unit:
    needs: changes
    if: needs.changes.outputs.src == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: make check
  status:
    needs: unit
    if: always()
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
GATED_FILES = ["src/pkg/a.py", "src/pkg/b.py", "docs/index.md", "pyproject.toml", "README.md"]


def test_paths_filter_gate_links_filter_files_to_the_gated_job_only():
    g = graph(GATED, GATED_FILES)
    code = feats(g, ["src/pkg/a.py"], ["changes", "unit", "status"])
    assert impacted(code, "unit") == 1.0
    assert code["unit"]["dep_graph_distance"] == 2.0                    # file -> filter scope -> job
    docs = feats(g, ["docs/index.md"], ["changes", "unit", "status"])
    assert impacted(docs, "unit") == 0.0                                 # file-linked job, not reached
    assert math.isnan(docs["unit"]["dep_graph_distance"])


def test_downstream_job_inherits_impact_through_needs_with_one_more_hop():
    f = feats(graph(GATED, GATED_FILES), ["pyproject.toml"], ["unit", "status"])
    assert impacted(f, "status") == 1.0
    assert f["status"]["dep_graph_distance"] == f["unit"]["dep_graph_distance"] + 1
    assert graph_distance_to_job(graph(GATED, GATED_FILES), "pyproject.toml", "status") == 3


def test_job_with_no_file_evidence_is_unknown_and_scheduling_stays_conservative():
    g = graph(GATED, GATED_FILES)
    f = feats(g, ["src/pkg/a.py"], ["changes"])
    assert f["changes"]["job_has_file_links"] == 0.0 and math.isnan(impacted(f, "changes"))
    # a scheduler may not skip a job it cannot prove unaffected
    assert "changes" in impacted_jobs_for_diff(g, ["docs/index.md"])
    assert "unit" not in impacted_jobs_for_diff(g, ["docs/index.md"])


def test_a_gate_supersedes_toolchain_inference_for_the_gated_job():
    wf = GATED.replace("      - run: make check", "      - run: pytest tests")
    g = graph(wf, GATED_FILES + ["tests/test_x.py"])
    # pytest would read tests/test_x.py, but `unit` only runs when the `src` filter matched, and it does not
    assert impacted(feats(g, ["tests/test_x.py"], ["unit"]), "unit") == 0.0
    assert impacted(feats(g, ["src/pkg/a.py"], ["unit"]), "unit") == 1.0
    assert g.graph["fusion_stats"]["gated_jobs"] == 1


# ---- reusable workflows and composite actions ----------------------------------------------------

CALLER = """\
name: Tests
on: push
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      tests: ${{ steps.c.outputs.tests }}
    steps:
      - uses: dorny/paths-filter@v3
        id: c
        with:
          filters: |
            tests:
              - 'src/**/*.py'
              - 'tests/**'
  matrix:
    needs: changes
    uses: $/.github/workflows/.matrix.yaml
    with:
      run-pytest: ${{ needs.changes.outputs.tests == 'true' }}
"""
CALLED = """\
on:
  workflow_call:
    inputs:
      run-pytest:
        type: boolean
jobs:
  pytest:
    runs-on: ubuntu-latest
    if: inputs.run-pytest
    steps:
      - uses: $/.github/actions/bootstrap
      - run: poetry run pytest -v
"""
COMPOSITE = """\
runs:
  using: composite
  steps:
    - run: ./scripts/bootstrap.sh
      shell: bash
"""


def test_local_reusable_workflow_is_inlined_and_its_input_bound_to_the_callers_gate():
    files = ["src/pkg/a.py", "tests/test_a.py", "docs/x.md", "scripts/bootstrap.sh"]
    g = graph(CALLER, files, {".github/workflows/.matrix.yaml": CALLED,
                              ".github/actions/bootstrap/action.yml": COMPOSITE})
    node = g.nodes["WorkflowJob::matrix"]["attributes"]
    assert any("needs.changes.outputs.tests" in c for c in node["conditions"])   # inputs.run-pytest was bound
    f = feats(g, ["tests/test_a.py"], ["matrix"])
    assert impacted(f, "matrix") == 1.0
    srcs = {d["provenance"]["creation_source"] for _, _, d in g.in_edges("WorkflowJob::matrix", data=True)
            if d["provenance"]["origin_parser"] == "GraphFusion"}
    assert srcs == {"paths_filter_gate"}                  # the gate supersedes toolchain / path inference
    # both the called workflow's step and the composite action's step were inlined into the job
    runs = [d["attributes"]["run"] for n, d in g.nodes(data=True)
            if d["node_type"] == "WorkflowStep" and n.startswith("WorkflowStep::matrix::")]
    assert "poetry run pytest -v" in runs and "./scripts/bootstrap.sh" in runs
    assert impacted(feats(g, ["docs/x.md"], ["matrix"]), "matrix") == 0.0
    assert impacted(feats(g, ["scripts/bootstrap.sh"], ["matrix"]), "matrix") == 0.0   # not in the gating filter


def test_composite_action_commands_link_files_for_an_ungated_job():
    wf = (
        "name: x\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/bootstrap\n"
    )
    g = graph(wf, ["scripts/bootstrap.sh", "README.md"], {".github/actions/bootstrap/action.yml": COMPOSITE})
    assert g.has_edge("File::scripts/bootstrap.sh", "WorkflowJob::j")
    assert impacted(feats(g, ["scripts/bootstrap.sh"], ["j"]), "j") == 1.0
    assert impacted(feats(g, ["README.md"], ["j"]), "j") == 0.0


def test_remote_actions_stay_opaque():
    wf = "name: x\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: owner/action@v1\n"
    f = feats(graph(wf, ["a.py"]), ["a.py"], ["j"])
    assert f["j"]["job_has_file_links"] == 0.0 and math.isnan(impacted(f, "j"))


# ---- rules 2-4: path arguments, toolchain scope, working directory -------------------------------

TOOLS = """\
name: x
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check src
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/unit
  gotest:
    runs-on: ubuntu-latest
    steps:
      - run: go test ./...
  ui:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - run: npm test
  setup_only:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
      - run: make everything
"""
TOOL_FILES = ["src/a.py", "tests/unit/test_a.py", "tests/other/test_b.py", "cmd/main.go", "pkg/x.go", "go.mod",
              "web/app.ts", "web/package.json", "other/app.ts", "README.md"]
ALL_TOOL_JOBS = ["lint", "unit", "gotest", "ui", "setup_only"]


def test_checker_reads_only_its_path_arguments_but_test_runner_reads_the_whole_language():
    g = graph(TOOLS, TOOL_FILES)
    f = feats(g, ["tests/other/test_b.py"], ALL_TOOL_JOBS)
    assert impacted(f, "lint") == 0.0           # `ruff check src` never reads tests/
    assert impacted(f, "unit") == 1.0           # pytest imports the whole project, not just tests/unit
    f = feats(g, ["src/a.py"], ALL_TOOL_JOBS)
    assert impacted(f, "lint") == 1.0 and impacted(f, "unit") == 1.0


def test_toolchains_are_language_specific_and_docs_touch_nothing():
    g = graph(TOOLS, TOOL_FILES)
    f = feats(g, ["pkg/x.go"], ALL_TOOL_JOBS)
    assert impacted(f, "gotest") == 1.0 and impacted(f, "setup_only") == 1.0
    assert impacted(f, "unit") == 0.0 and impacted(f, "ui") == 0.0
    docs = feats(g, ["README.md"], ALL_TOOL_JOBS)
    assert {impacted(docs, j) for j in ALL_TOOL_JOBS} == {0.0}


def test_working_directory_scopes_the_toolchain():
    g = graph(TOOLS, TOOL_FILES)
    assert impacted(feats(g, ["web/app.ts"], ["ui"]), "ui") == 1.0
    assert impacted(feats(g, ["other/app.ts"], ["ui"]), "ui") == 0.0


def test_setup_action_toolchain_is_weaker_evidence_than_a_command():
    g = graph(TOOLS, TOOL_FILES)
    confs = {d["provenance"]["creation_source"]: d["confidence"]
             for _, _, d in g.in_edges("WorkflowJob::setup_only", data=True)
             if d["provenance"]["origin_parser"] == "GraphFusion"}
    assert confs == {"setup_action_toolchain": 0.4}
    cmd = {d["provenance"]["creation_source"] for _, _, d in g.in_edges("WorkflowJob::gotest", data=True)
           if d["provenance"]["origin_parser"] == "GraphFusion"}
    assert cmd == {"toolchain_command"}


# ---- rule 5: caches ------------------------------------------------------------------------------

CACHES = """\
name: x
on: push
jobs:
  mvn:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.m2
          key: m2-${{ hashFiles('**/pom.xml') }}
      - run: ./mvnw verify
  go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
      - run: go build ./...
  opaque:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.thing
          key: thing-${{ github.sha }}
      - run: ./mvnw verify
  nocache:
    runs-on: ubuntu-latest
    steps:
      - run: ./mvnw verify
"""
CACHE_FILES = ["pom.xml", "module/pom.xml", "go.sum", "go.mod", "module/A.java", "README.md"]
CACHE_JOBS = ["mvn", "go", "opaque", "nocache"]


def test_cache_key_touched_per_job_with_glob_semantics():
    g = graph(CACHES, CACHE_FILES)
    f = feats(g, ["module/pom.xml"], CACHE_JOBS)
    assert f["mvn"]["cache_key_touched"] == 1.0                          # **/pom.xml matches a nested pom
    assert f["go"]["cache_key_touched"] == 0.0                           # setup-go keys on go.sum
    assert f["nocache"]["cache_key_touched"] == 0.0                      # restores no cache at all
    assert math.isnan(f["opaque"]["cache_key_touched"])                  # key has no hashFiles: cannot tell
    assert feats(g, ["go.sum"], CACHE_JOBS)["go"]["cache_key_touched"] == 1.0
    assert cache_keys_touched_by(g, ["go.sum"]) == {"implicit::setup-go"}


def test_cache_inputs_are_linked_as_invalidating_edges():
    g = graph(CACHES, CACHE_FILES)
    key = next(n for n in g if n.startswith("CacheKey::m2-"))
    assert g.has_edge("File::pom.xml", key) and g.has_edge("File::module/pom.xml", key)
    assert not g.has_edge("File::README.md", key)
