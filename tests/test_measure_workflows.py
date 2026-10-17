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
