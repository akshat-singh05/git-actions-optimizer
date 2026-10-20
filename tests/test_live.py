"""Prospective (live) scoring with fakes: ledger integrity, the "score before anything finished" rule, and the evaluation maths.

The history is the explicitly SYNTHETIC test fixture and the GitHub client / mirror are fakes: no number here is a result.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from gha_optimizer.live import (
    LiveScorer,
    append_ledger,
    evaluate_ledger,
    read_ledger,
    render_live,
    run_live,
)
from tests.fixtures.synthetic_history import generate_synthetic_test_history

WORKFLOW = (Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml").read_bytes()
KEYS = ["lint", "unit_tests", "integration_tests", "build", "deploy"]


class FakeMirror:
    def __init__(self, available=True):
        self.available = available

    def read_file(self, sha, path):
        return ("blob", WORKFLOW) if self.available else None

    def fetch_sha(self, sha):
        return False


class FakeClient:
    def __init__(self, runs=None, completed_jobs=False, files=None):
        self.runs, self.completed_jobs = runs or [], completed_jobs
        self.files = files if files is not None else [{"filename": "src/app.py", "additions": 10, "deletions": 2}]
        self.calls = []

    def list_jobs(self, run_id):
        return [{"name": "lint", "status": "completed" if self.completed_jobs else "queued"}]

    def compare(self, base, head):
        self.calls.append(("compare", base, head))
        return {"files": self.files}

    def get_commit(self, sha):
        self.calls.append(("commit", sha))
        return {"files": self.files} if self.files is not None else None

    def get_json(self, path, params=None):
        return {"workflow_runs": [r for r in self.runs if params.get("status") in (None, r["status"])]}


def run_json(i, status="queued", event="pull_request", pr=True):
    return {"id": 9000 + i, "head_sha": f"head{i}", "event": event, "status": status, "created_at": "2026-10-12T10:00:00Z",
            "head_branch": "feat", "run_attempt": 1, "actor": {"login": "dev"},
            "pull_requests": [{"number": 7, "base": {"sha": "base0"}}] if pr else []}


@pytest.fixture(scope="module")
def models():
    from gha_optimizer.models.failure_predictor import FailurePredictor
    return {"old_logistic": FailurePredictor(variant="logistic"), "v2_hgb": FailurePredictor(variant="v2_hgb")}


@pytest.fixture()
def scorer(tmp_path, models):
    history = generate_synthetic_test_history(n_runs=80, seed=3)
    return LiveScorer("o/r", ".github/workflows/ci.yml", history, FakeClient(), FakeMirror(), tmp_path / "ledger.jsonl", models=models)


# ---- ledger -----------------------------------------------------------------------------------------------

def test_ledger_is_hash_chained_and_detects_any_edit(tmp_path):
    p = tmp_path / "l.jsonl"
    for i in range(3):
        append_ledger(p, {"run_id": str(i), "kind": "scored", "jobs": [{"job": "a", "p_x": 0.1 * i}]})
    assert [r["run_id"] for r in read_ledger(p)] == ["0", "1", "2"]
    lines = p.read_text().splitlines()
    forged = json.loads(lines[1])
    forged["jobs"][0]["p_x"] = 0.99                      # "improve" a prediction after the fact
    lines[1] = json.dumps(forged, sort_keys=True)
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="hash chain"):
        read_ledger(p)
    assert len(read_ledger(p, verify=False)) == 3


# ---- scoring a run ----------------------------------------------------------------------------------------

def test_a_queued_run_gets_a_probability_for_every_job_before_anything_finished(scorer):
    entry = scorer.score_run(run_json(1))
    assert entry["kind"] == "scored" and entry["jobs_completed_at_scoring"] == 0 and entry["run_status_at_scoring"] == "queued"
    assert entry["diff_source"] == "compare_pr" and entry["files_changed"] == 1
    assert [j["job"] for j in entry["jobs"]] == KEYS
    for j in entry["jobs"]:
        assert 0.0 <= j["p_old_logistic"] <= 1.0 and 0.0 <= j["p_v2_hgb"] <= 1.0
    assert scorer.client.calls == [("compare", "base0", "head1")]
    assert read_ledger(scorer.ledger)[-1]["hash"] == entry["hash"]


def test_non_pr_runs_use_the_head_commit_and_fork_prs_are_marked_as_an_approximation(scorer):
    assert scorer.score_run(run_json(2, event="push", pr=False))["diff_source"] == "commit"
    assert scorer.score_run(run_json(3, pr=False))["diff_source"] == "commit_only"


def test_a_run_with_a_completed_job_is_skipped_not_scored(scorer):
    scorer.client.completed_jobs = True
    entry = scorer.score_run(run_json(4))
    assert entry["kind"] == "skipped" and "already completed" in entry["reason"]


def test_missing_workflow_or_diff_is_skipped(scorer):
    scorer.mirror.available = False
    assert scorer.score_run(run_json(5))["kind"] == "skipped"
    scorer.mirror.available = True
    scorer.client.files = None
    assert scorer.score_run(run_json(6, pr=False))["kind"] == "skipped"


def test_structural_features_are_never_used_live(scorer):
    for m in scorer.models.values():
        assert not ({"job_structurally_impacted", "cache_key_touched", "dep_graph_distance"} & set(m._features))


def test_polling_scores_each_run_once_and_the_loop_stops_at_its_deadline(scorer):
    scorer.client.runs = [run_json(10, "queued"), run_json(11, "in_progress")]
    seen = set()
    assert scorer.poll_once(123, "2026-10-12T00:00:00Z", seen) == 2
    assert scorer.poll_once(123, "2026-10-12T00:00:00Z", seen) == 0            # nothing is scored twice
    scorer.client.runs = [run_json(12, "queued")]
    t0 = time.time()
    out = run_live(scorer, 123, minutes=0.01, poll_seconds=0.05, sleep=time.sleep)
    assert time.time() - t0 < 5 and out["scored_runs"] == 1
    assert out["ledger_runs"] >= 3                                             # earlier ledger runs are never rescored


# ---- evaluation -------------------------------------------------------------------------------------------

def _ledger_rows(spec):
    """spec: list of (run_id, {job: (p_model, p_old, p_baseline)})."""
    return [{"kind": "scored", "run_id": rid, "scored_at": "t",
             "jobs": [{"job": j, "p_v2_hgb": a, "p_old_logistic": b, "p_baseline": c} for j, (a, b, c) in jobs.items()]}
            for rid, jobs in spec]


def test_evaluation_scores_only_jobs_with_outcomes_and_a_perfect_model_beats_a_blind_baseline():
    rng = np.random.default_rng(0)
    spec, outcomes = [], {}
    for r in range(40):
        jobs = {}
        for j in ("a", "b", "c"):
            y = int(rng.random() < 0.2)
            outcomes[(f"r{r}", j)] = y
            jobs[j] = (0.9 if y else 0.05, 0.5, 0.2)       # model perfect, old model blind, baseline flat
        spec.append((f"r{r}", jobs))
    spec.append(("pending", {"a": (0.5, 0.5, 0.2)}))        # no outcome yet: excluded
    rows = _ledger_rows(spec) + [{"kind": "skipped", "run_id": "s", "reason": "x"}]
    res = evaluate_ledger(rows, outcomes, n_boot=200)
    assert res["runs_in_ledger"] == 41 and res["skipped_runs"] == 1 and res["runs_with_outcomes"] == 40
    assert res["scored_jobs"] == 120 and res["metrics"]["v2_hgb"]["pr_auc"] == pytest.approx(1.0)
    assert res["metrics"]["v2_hgb"]["precision_top10pct"] == pytest.approx(1.0)
    assert res["metrics"]["old_logistic"]["pr_auc"] < 0.5
    d = res["pr_auc_minus_baseline"]["v2_hgb"]
    assert d["delta"] > 0.5 and d["ci95"][0] > 0
    text = render_live(res, "ledger.jsonl", "t0", "t1")
    assert "not a significance test" in text and "v2_hgb" in text and "precision in the top 10%" in text


def test_evaluation_says_so_when_there_is_nothing_to_score():
    rows = _ledger_rows([("r1", {"a": (0.2, 0.2, 0.1)})])
    assert "note" in evaluate_ledger(rows, {})
    assert "note" in evaluate_ledger(rows, {("r1", "a"): 0})                   # one outcome class only


def test_an_api_outage_pauses_the_scorer_instead_of_ending_it(scorer):
    """The scorers used to exit with code 1 when the network dropped; a failed poll is now logged and retried."""
    from gha_optimizer.ingestion.github_client import GithubAPIError

    scorer.client.runs = [run_json(20, "queued")]
    real_get_json, calls = scorer.client.get_json, {"n": 0}

    def flaky(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GithubAPIError(0, path, "ConnectionError: no network")
        return real_get_json(path, params)

    scorer.client.get_json = flaky
    sleeps = []
    out = run_live(scorer, 123, minutes=0.05, poll_seconds=0.01, sleep=lambda s: (sleeps.append(s), time.sleep(0.02))[1])
    assert out["scored_runs"] == 1 and sleeps[0] >= 30.0           # waited after the failure, then scored the run
