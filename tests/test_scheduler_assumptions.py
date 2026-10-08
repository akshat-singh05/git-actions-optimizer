"""The scheduler's estimate must separate what needs an assumption from what does not.

The ingested history has one runner size per OS, so the speed-up of a larger runner cannot be measured.
Default plans therefore contain no speed-up assumption; the larger-runner case is an explicit what-if.
"""
from __future__ import annotations

import networkx as nx
import pytest

from gha_optimizer.scheduler.optimizer import build_schedule, speedup_sensitivity

DAG = nx.DiGraph([("lint", "unit"), ("lint", "integ"), ("unit", "build"), ("integ", "build")])
DUR = {"lint": 120.0, "unit": 400.0, "integ": 300.0, "build": 200.0}
ZERO = {j: 0.0 for j in DAG}


def plan(**kw):
    return build_schedule(DAG, kw.pop("failure", ZERO), ZERO, DUR, **kw)


def test_default_plan_contains_no_speedup_assumption_and_changes_nothing_without_skips():
    p = plan()
    assert p.runner_upsizing is False
    assert {jp.runner for jp in p.jobs} == {"ubuntu-latest"}                  # nothing relabelled
    assert p.estimated_wall_clock_seconds == p.baseline_wall_clock_seconds     # no skips, no upsizing -> no claimed gain
    assert p.time_saved_pct == 0.0 and p.cost_saved_pct == 0.0


def test_default_figures_do_not_depend_on_the_assumed_exponent():
    a, b = plan(speedup_exponent=0.0), plan(speedup_exponent=0.9)
    for field in ("estimated_wall_clock_seconds", "estimated_cost_usd", "skip_only_wall_clock_seconds"):
        assert getattr(a, field) == getattr(b, field)


def test_skip_effect_is_separated_from_the_runner_assumption():
    failure = {**ZERO, "lint": 0.95}                                           # lint predicted to fail -> descendants skipped
    p = plan(failure=failure, allow_skipping=True)
    assert {jp.job_id for jp in p.jobs if jp.skip_recommended} == {"unit", "integ", "build"}
    assert p.skip_applied is True and p.time_saved_by_skipping_pct > 0
    assert p.estimated_wall_clock_seconds == p.skip_only_wall_clock_seconds    # skipping on, upsizing off == skip-only
    assert p.time_saved_by_skipping_pct == pytest.approx(p.time_saved_pct)


def test_skip_guards_are_off_by_default_but_candidates_are_still_reported():
    failure = {**ZERO, "lint": 0.95}
    p = plan(failure=failure)
    assert p.skip_applied is False
    assert not any(jp.skip_recommended for jp in p.jobs)                        # nothing is guarded ...
    assert {jp.job_id for jp in p.jobs if jp.skip_candidate} == {"unit", "integ", "build"}   # ... but it is visible
    assert p.estimated_wall_clock_seconds == p.baseline_wall_clock_seconds     # headline figures stay at the baseline
    assert p.time_saved_by_skipping_pct > 0                                    # what the credit WOULD be
    d = p.summary_dict()
    assert d["jobs_skipped"] == 0 and d["skip_candidates"] == 3 and d["skip_recommendations_applied"] is False


def test_skipping_and_upsizing_are_independent_switches():
    failure = {**ZERO, "lint": 0.95}
    both = plan(failure=failure, allow_skipping=True, allow_upsizing=True)
    assert both.skip_applied and both.runner_upsizing
    assert both.estimated_wall_clock_seconds == both.both_wall_clock_seconds
    only_up = plan(failure=failure, allow_upsizing=True)
    assert only_up.runner_upsizing and not only_up.skip_applied
    assert only_up.estimated_wall_clock_seconds == only_up.runner_only_wall_clock_seconds


def test_exponent_zero_means_larger_runners_never_help_so_the_whatif_collapses():
    p = plan(speedup_exponent=0.0, allow_upsizing=True)
    assert {jp.runner for jp in p.jobs} == {"ubuntu-latest"}                   # a pricier runner with no speed-up is never chosen
    assert p.runner_only_wall_clock_seconds == p.baseline_wall_clock_seconds


def test_larger_runner_whatif_is_faster_and_dearer_and_flagged_as_assumed():
    p = plan(speedup_exponent=0.6, allow_upsizing=True)
    assert p.runner_upsizing is True and p.speedup_exponent == 0.6
    assert p.runner_only_wall_clock_seconds < p.baseline_wall_clock_seconds
    assert p.runner_only_cost_usd > p.baseline_cost_usd                         # the multi-objective trade-off is visible
    assert p.estimated_wall_clock_seconds == p.both_wall_clock_seconds          # upsizing on: headline == what-if
    d = p.summary_dict()
    assert d["basis"].startswith("ESTIMATED") and "ASSUMED" in d["assumed_whatif_larger_runners"]["note"]


def test_the_whatif_is_reported_even_when_upsizing_is_off():
    p = plan()                                                                  # default: off
    assert p.runner_only_wall_clock_seconds < p.baseline_wall_clock_seconds     # what-if still computed ...
    assert p.estimated_wall_clock_seconds == p.baseline_wall_clock_seconds      # ... but not applied
    assert p.summary_dict()["assumed_whatif_larger_runners"]["time_saved_pct"] > 0


def test_sensitivity_rows_are_ordered_and_start_at_the_no_assumption_case():
    rows = speedup_sensitivity(DAG, ZERO, ZERO, DUR, exponents=(0.0, 0.3, 0.6, 0.9))
    assert [r["speedup_exponent"] for r in rows] == [0.0, 0.3, 0.6, 0.9]
    assert rows[0]["time_saved_pct"] == 0.0 and rows[0]["cost_saved_pct"] == 0.0
    saved = [r["time_saved_pct"] for r in rows]
    assert saved == sorted(saved) and saved[-1] > saved[0]


def test_dag_levels_are_unchanged_by_runner_choice():
    assert [jp.dag_level for jp in plan().jobs if jp.job_id == "build"] == [2]
    assert [jp.dag_level for jp in plan(allow_upsizing=True).jobs if jp.job_id == "build"] == [2]
