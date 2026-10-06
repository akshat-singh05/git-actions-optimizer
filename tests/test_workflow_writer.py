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
