"""Advisory report on the jobs the graph says a diff cannot reach: classification and rendering."""
from __future__ import annotations

import math

from gha_optimizer.advisory import (
    ADVISE_SKIP,
    KEPT_NEEDED,
    KEPT_UNREADABLE,
    advise_unreached_jobs,
    render_advisory,
)
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text

WORKFLOW = """
name: x
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: ruff check .}]
  build:
    runs-on: ubuntu-latest
    steps: [{run: python -m build}]
  test:
    needs: build
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  docs:
    runs-on: ubuntu-latest
    steps: [{run: make docs}]
  e2e:
    runs-on: ubuntu-latest
    steps: [{run: ./e2e.sh}]
"""


def wf():
    return parse_workflow_text(WORKFLOW)


def by_job(advice):
    return {a.job: a for a in advice}


def test_classification_of_unreached_jobs():
    # lint unreached; build unreached but test (needs build) is reached; docs unreached but runs make;
    # e2e: no file evidence (NaN) -> never advised
    advice, unknown = advise_unreached_jobs(
        wf(), {"lint": 0.0, "build": 0.0, "test": 1.0, "docs": 0.0, "e2e": math.nan})
    a = by_job(advice)
    assert a["lint"].verdict == ADVISE_SKIP
    assert a["build"].verdict == KEPT_NEEDED and "test" in a["build"].reason
    assert a["docs"].verdict == KEPT_UNREADABLE
    assert "test" not in a and "e2e" not in a                 # reached / unknown jobs get no advice
    assert unknown == ["e2e"]


def test_a_dependent_with_unknown_reachability_blocks_the_skip():
    advice, _ = advise_unreached_jobs(wf(), {"lint": 0.0, "build": 0.0, "test": None, "docs": 1.0, "e2e": 1.0})
    assert by_job(advice)["build"].verdict == KEPT_NEEDED


def test_whole_unreached_cone_is_advised_together():
    advice, _ = advise_unreached_jobs(wf(), {"lint": 0, "build": 0, "test": 0, "docs": 1, "e2e": 1})
    a = by_job(advice)
    assert a["build"].verdict == ADVISE_SKIP and a["test"].verdict == ADVISE_SKIP


def test_nothing_is_advised_when_every_job_is_reached():
    advice, unknown = advise_unreached_jobs(wf(), {k: 1.0 for k in wf().jobs})
    assert advice == [] and unknown == []
    assert "nothing to advise" in render_advisory(advice, unknown, None)


STATS = {
    "runs": 40,
    "dag_safe_readable": {
        "saved_seconds_share": 0.124, "masked_runs": 14, "failing_runs": 133, "masked_run_share": 14 / 133,
        "failure_rate_skipped": 0.037, "failure_rate_rest": 0.083,
        "by_job": {"lint": {"executed": 30, "skipped": 12, "skipped_failures": 1, "job_failures": 4}},
    },
}


def test_rendering_attaches_measured_rates_and_says_nothing_is_applied():
    advice, unknown = advise_unreached_jobs(wf(), {"lint": 0.0, "build": 0.0, "test": 1.0, "docs": 0.0, "e2e": None})
    text = render_advisory(advice, unknown, STATS)
    assert "NOT applied" in text and "still runs all of these" in text
    assert "skipped it 12 of 30 times" in text and "failed in 1 of those" in text      # per-job history
    assert "12.4%" in text and "14 of 133" in text and "3.7% vs 8.3%" in text          # repo-level replay figures
    assert "MEASURED on history" in text and "not a held-out test" in text
    assert "`e2e`" in text                                                           # unknown job listed, not advised


def test_rendering_without_replay_figures_says_safety_is_unknown():
    advice, unknown = advise_unreached_jobs(wf(), {"lint": 0.0, "build": 1.0, "test": 1.0, "docs": 1.0, "e2e": 1.0})
    assert "safety of this advice on past runs is unknown" in render_advisory(advice, unknown, None)
