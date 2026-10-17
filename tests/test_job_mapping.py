"""API job name -> workflow YAML job key mapping, and cache-capability detection."""
from __future__ import annotations

from gha_optimizer.ingestion.job_mapping import build_matchers, job_is_cache_capable, match_job_key

JOBS = {
    "lint": {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@v4"}, {"run": "ruff check ."}]},
    "test": {
        "strategy": {"matrix": {"os": ["ubuntu", "windows"], "py": ["3.11", "3.12"]}},
        "steps": [{"uses": "actions/setup-python@v5", "with": {"cache": "pip"}}],
    },
    "build": {"name": "Build wheels (${{ matrix.target }})", "strategy": {"matrix": {"target": ["a", "b"]}}},
    "docs": {"name": "Documentation", "steps": [{"uses": "./.github/actions/setup-docs"}]},
    "release": {"uses": "./.github/workflows/release.yml"},
    "test-windows": {"steps": [{"run": "pytest"}]},
}


def keyof(name):
    return match_job_key(name, build_matchers(JOBS))


def test_plain_job_maps_to_its_key():
    assert keyof("lint") == "lint"


def test_matrix_cells_map_to_the_job_key():
    assert keyof("test (ubuntu, 3.11)") == "test"
    assert keyof("test (windows, 3.12)") == "test"


def test_templated_name_maps_through_the_expression():
    assert keyof("Build wheels (a)") == "build"


def test_explicit_name_is_matched_literally():
    assert keyof("Documentation") == "docs"


def test_reusable_workflow_call_children_map_to_the_caller():
    assert keyof("release / publish") == "release"


def test_similarly_named_jobs_do_not_collide():
    assert keyof("test-windows") == "test-windows"
    assert keyof("test (ubuntu, 3.11)") == "test"


def test_unknown_job_is_unmapped_not_guessed():
    assert keyof("completely different") is None


def test_ambiguous_matches_are_unmapped():
    jobs = {"a": {"name": "${{ matrix.x }}"}, "b": {"name": "${{ matrix.y }}"}}
    assert match_job_key("whatever", build_matchers(jobs)) is None


def test_cache_capability_from_yaml_steps():
    assert job_is_cache_capable(JOBS["test"], None) is True            # setup-python
    assert job_is_cache_capable(JOBS["docs"], None) is True            # local composite: contents unknown
    assert job_is_cache_capable(JOBS["release"], None) is True         # reusable workflow call
    assert job_is_cache_capable(JOBS["lint"], None) is False
    assert job_is_cache_capable({"steps": [{"uses": "actions/cache@v4"}]}, None) is True


def test_cache_capability_falls_back_to_api_step_names_when_unmapped():
    assert job_is_cache_capable(None, [{"name": "Set up job"}, {"name": "Run actions/cache@v4"}]) is True
    assert job_is_cache_capable(None, [{"name": "Set up job"}, {"name": "Run pytest"}, {"name": "Complete job"}]) is False


def test_expression_that_expands_to_nothing_still_maps():
    """Real case (apache/dubbo): ``(JobId: ${{matrix.job_id}} Java: ${{matrix.java}})`` with an unset job_id."""
    jobs = {"samples-test-result": {"name": "Samples Test Result on ubuntu-22.04 (JobId: ${{matrix.job_id}} Java: ${{matrix.java}})"}}
    assert match_job_key("Samples Test Result on ubuntu-22.04 (JobId:  Java: 8)", build_matchers(jobs)) == "samples-test-result"


def test_two_jobs_with_the_same_display_name_stay_unmapped():
    """Two plain jobs (no matrix) share one display name: nothing tells them apart, so guessing would mislabel data."""
    jobs = {"a": {"name": "Same name"}, "b": {"name": "Same name"}}
    assert match_job_key("Same name", build_matchers(jobs)) is None


def test_matrix_job_and_aggregator_with_the_same_display_name_are_told_apart():
    """Real case (prometheus): a matrix job and the status job that gives branch protection a stable name share one name.
    GitHub appends the matrix values to a matrix cell's name, so the suffix decides."""
    jobs = {"test_go_oldest": {"name": "Go tests with previous Go version", "strategy": {"matrix": {"go_version": ["1.26"]}}},
            "test_go_oldest_status": {"name": "Go tests with previous Go version"}}
    ms = build_matchers(jobs)
    assert match_job_key("Go tests with previous Go version (1.26)", ms) == "test_go_oldest"
    assert match_job_key("Go tests with previous Go version", ms) == "test_go_oldest_status"
