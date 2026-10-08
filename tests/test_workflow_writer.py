"""The proposed workflow must be a faithful, valid GitHub Actions file."""
from pathlib import Path

import yaml

from gha_optimizer.generator.workflow_writer import generate_optimized_workflow, write_workflow
from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.pipeline import job_only_dag
from gha_optimizer.scheduler.optimizer import build_schedule

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml"


def _plan(workflow):
    dag = job_only_dag(workflow)
    zero = {j: 0.01 for j in workflow.jobs}
    return build_schedule(dag, zero, zero, {j: 60.0 for j in workflow.jobs})


def test_trigger_key_is_written_as_on_not_true(tmp_path):
    """Regression: `on:` is parsed by PyYAML as boolean True and used to be dumped as `true:`."""
    workflow = parse_workflow(SAMPLE)
    out = write_workflow(generate_optimized_workflow(workflow, _plan(workflow)), tmp_path / "out.yml")
    text = out.read_text(encoding="utf-8")
    assert "\non:" in text and "\ntrue:" not in text
    reparsed = parse_workflow(out)                       # and our own parser still reads the triggers back
    assert set(reparsed.triggers) == set(workflow.triggers)


def test_every_needs_edge_survives_in_the_proposed_file(tmp_path):
    workflow = parse_workflow(SAMPLE)
    out = write_workflow(generate_optimized_workflow(workflow, _plan(workflow)), tmp_path / "out.yml")
    jobs = yaml.safe_load(out.read_text(encoding="utf-8"))["jobs"]
    reparsed_edges = {(dep, j) for j, body in jobs.items()
                      for dep in ([body["needs"]] if isinstance(body.get("needs"), str) else body.get("needs", []))}
    assert reparsed_edges == set(workflow.job_dag_edges())


# ---------------------------------------------------------------------------
# Validity guarantees of the proposed file (a file GitHub would reject is not a proposal)
# ---------------------------------------------------------------------------

import networkx as nx  # noqa: E402

from gha_optimizer.ingestion.workflow_parser import parse_workflow_text  # noqa: E402

ALLOWED_TOP_LEVEL = {"name", "run-name", "on", "permissions", "env", "defaults", "concurrency", "jobs"}

MIXED = """\
name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
  win:
    needs: lint
    runs-on: windows-latest
    steps:
      - run: pytest
  mac:
    needs: lint
    runs-on: macos-latest
    steps:
      - run: pytest
  mx:
    needs: lint
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest]
    steps:
      - run: pytest
  gated:
    needs: [win, mac, mx]
    if: needs.lint.result == 'success'
    runs-on: ubuntu-latest
    steps:
      - run: make
"""


def _mixed_plan(**kw):
    wf = parse_workflow_text(MIXED)
    dag = nx.DiGraph(wf.job_dag_edges())
    dag.add_nodes_from(wf.jobs)
    failure = kw.pop("failure", {j: 0.0 for j in wf.jobs})
    dur = {j: 400.0 for j in wf.jobs}
    return wf, build_schedule(dag, failure, {j: 0.0 for j in wf.jobs}, dur, **kw)


def _write(wf, plan, tmp_path):
    return yaml.safe_load(write_workflow(generate_optimized_workflow(wf, plan), tmp_path / "o.yml", plan).read_text(encoding="utf-8"))


def test_proposed_file_has_only_valid_top_level_keys(tmp_path):
    wf, plan = _mixed_plan()
    doc = _write(wf, plan, tmp_path)
    assert {("on" if k is True else k) for k in doc} <= ALLOWED_TOP_LEVEL, set(doc)


def test_no_job_changes_operating_system_or_runner_expression(tmp_path):
    for upsizing in (False, True):
        wf, plan = _mixed_plan(allow_upsizing=upsizing)
        jobs = _write(wf, plan, tmp_path)["jobs"]
        assert jobs["win"]["runs-on"] == "windows-latest"
        assert jobs["mac"]["runs-on"] == "macos-latest"
        assert jobs["mx"]["runs-on"] == "${{ matrix.os }}"


def test_runner_labels_change_only_with_upsizing_and_only_for_base_tier_jobs(tmp_path):
    wf, off = _mixed_plan(allow_upsizing=False)
    assert _write(wf, off, tmp_path)["jobs"]["lint"]["runs-on"] == "ubuntu-latest"
    wf, on = _mixed_plan(allow_upsizing=True, speedup_exponent=0.9)
    jobs = _write(wf, on, tmp_path)["jobs"]
    assert jobs["lint"]["runs-on"] != "ubuntu-latest" and jobs["lint"]["runs-on"].startswith("ubuntu-latest")


def test_skip_guard_is_exactly_an_expression_and_keeps_the_original_condition(tmp_path):
    wf, plan = _mixed_plan(failure={"lint": 0.95, "win": 0, "mac": 0, "mx": 0, "gated": 0}, allow_skipping=True)
    jobs = _write(wf, plan, tmp_path)["jobs"]
    assert jobs["win"]["if"] == "${{ false }}"                          # nothing after the closing braces
    assert jobs["gated"]["if"] == "${{ false && (needs.lint.result == 'success') }}"   # original condition preserved
    assert "#" not in jobs["win"]["if"]


def test_reasons_and_estimate_are_comments_in_the_header_not_values(tmp_path):
    wf, plan = _mixed_plan(failure={"lint": 0.95, "win": 0, "mac": 0, "mx": 0, "gated": 0}, allow_skipping=True)
    text = write_workflow(generate_optimized_workflow(wf, plan), tmp_path / "o.yml", plan).read_text(encoding="utf-8")
    head = text.split("\nname:")[0]
    assert "NOT measured" in head and "Skip guard on 'win'" in head and "off (no runner labels were changed)" in head
