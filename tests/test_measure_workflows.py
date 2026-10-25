"""Statistics and timing extraction of scripts/measure_workflows.py (the GitHub part needs a real repository and is not run here)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("measure_workflows", Path(__file__).parent.parent / "scripts" / "measure_workflows.py")
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)


def test_permutation_p_is_small_for_separated_groups_and_one_for_identical():
    assert mw.permutation_p([1, 2, 3, 4], [10, 11, 12, 13]) < 0.1
    assert mw.permutation_p([5, 5, 5], [5, 5, 5]) == 1.0


def test_summary_reports_median_difference_in_percent():
    s = mw.summarize_metric([100.0, 110.0, 120.0], [80.0, 85.0, 90.0])
    assert s["median_diff_B_minus_A_s"] == -25.0 and s["median_diff_pct"] == pytest.approx(-22.73, abs=0.01)


def job(name, start, end, conclusion="success"):
    return {"name": name, "started_at": f"2026-10-17T10:{start // 60:02d}:{start % 60:02d}Z", "completed_at": f"2026-10-17T10:{end // 60:02d}:{end % 60:02d}Z",
            "conclusion": conclusion, "steps": [{"name": "s", "conclusion": "success"}]}


def test_run_metrics_and_needs_check():
    jobs = [job("lint", 3, 30), job("test", 31, 120)]
    run_obj = {"created_at": "2026-10-17T10:00:00Z"}
    m = mw.run_metrics(jobs, run_obj)
    assert m["critical_path_s"] == 117 and m["billable_job_s"] == 27 + 89 and m["job_s"]["test"] == 89
    assert mw.needs_respected(jobs, {"test": ["lint"]}) is True
    assert mw.needs_respected([job("lint", 3, 30), job("test", 10, 120)], {"test": ["lint"]}) is False


def test_rank_permutation_p_separates_what_the_median_test_cannot():
    """Fully separated groups of 7: the exact rank test gives 2/3432; the difference-of-medians test stays near 0.1 (many splits share a median)."""
    a, b = [49, 45, 47, 45, 39, 43, 46], [29, 26, 28, 28, 27, 30, 26]
    assert mw.rank_permutation_p(a, b) == pytest.approx(2 / 3432, abs=1e-6)
    assert mw.permutation_p(a, b) > 0.05
    assert mw.rank_permutation_p([5, 5, 5], [5, 5, 5]) == 1.0


def record(arm, conclusion, jobs, warmup=False, rnd=1):
    return {"arm": arm, "round": rnd, "warmup": warmup, "run_id": 1, "conclusion": conclusion, "job_conclusion": jobs, "needs_ok": True,
            "critical_path_s": 50.0, "run_s": 55.0, "billable_job_s": 60.0, "job_s": {j: 10.0 for j in jobs}}


def test_failing_run_mode_accepts_only_the_expected_failure_and_counts_downstream_jobs():
    a = record("A", "failure", {"lint": "failure", "test": "skipped"})
    b = record("B", "failure", {"lint": "failure", "test": "success"})
    unexpected = record("B", "failure", {"lint": "failure", "test": "failure"})        # a second failing job: not the experiment we meant
    green = record("A", "success", {"lint": "success", "test": "success"})              # the injected failure did not trip lint
    s = mw.build_summary([a, b, unexpected, green], "o/r", expect_failed_job="lint")
    assert s["n_per_arm"] == {"A_original": 1, "B_proposed": 1}
    assert len(s["excluded_invalid_runs"]) == 2
    assert s["jobs_run_to_success_per_run"] == {"A_original": [0], "B_proposed": [1]}
    assert s["expected_failed_job"] == "lint"


def test_green_run_mode_still_rejects_a_failed_run():
    ok_a = record("A", "success", {"lint": "success", "test": "success"})
    ok_b = record("B", "success", {"lint": "success", "test": "success"})
    bad = record("B", "failure", {"lint": "failure", "test": "skipped"})
    s = mw.build_summary([ok_a, ok_b, bad], "o/r")
    assert s["n_per_arm"] == {"A_original": 1, "B_proposed": 1} and len(s["excluded_invalid_runs"]) == 1 and "expected_failed_job" not in s


def test_needs_check_tolerates_a_skipped_job_without_timestamps():
    jobs = [job("lint", 3, 30, "failure"), {"name": "test", "conclusion": "skipped", "started_at": None, "completed_at": None, "steps": []}]
    assert mw.needs_respected(jobs, {"test": ["lint"]}) is True


def test_skipped_jobs_are_not_billed_and_do_not_distort_timings():
    skipped = {"name": "test", "conclusion": "skipped", "started_at": "2026-10-17T10:00:31Z", "completed_at": "2026-10-17T10:00:30Z", "steps": []}   # negative duration
    m = mw.run_metrics([job("lint", 3, 30, "failure"), skipped], {"created_at": "2026-10-17T10:00:00Z"})
    assert m["billable_job_s"] == 27 and m["critical_path_s"] == 27 and "test" not in m["job_s"]
    assert m["job_conclusion"] == {"lint": "failure", "test": "skipped"}
