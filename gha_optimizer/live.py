"""
Prospective ("live") failure detection on real CI runs.

``live-score`` trains the failure models on a repository's ingested history, then watches GitHub for runs of one workflow
that have just been queued, and writes a prediction for every job of each such run into an append-only **ledger**
(JSON lines, hash-chained so that an edit after the fact is detectable) *before any job of the run has completed*.
``live-evaluate`` later reads the real outcomes of those runs and scores the ledger: PR-AUC, ROC-AUC, precision at the top 10%
of scores, against the baseline "the job's prior failure rate".

What is and is not the same as the offline evaluation (stated, not hidden):

* features known at queue time are the same ones the models were trained on; **structural (graph) features are not used**
  (they added nothing in every evaluation, and building the per-commit graph of an in-flight run is slow): the models are
  trained with those columns blanked, so there is no train / serve mismatch;
* the diff comes from the GitHub API at queue time: compare(PR base, head) when the run lists its pull request, else the head
  commit (``diff_source`` is recorded per run, ``commit_only`` being an approximation for fork PRs);
* a run is scored only if none of its jobs had completed when it was scored, and the ledger records when.

A live sample is small (a few hours of runs): it demonstrates the pipeline and gives an outcome that cannot have been fitted to,
but it is not a significance test, and the report prints the counts so that is visible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import networkx as nx
import numpy as np

from gha_optimizer.ingestion.github_client import GithubAPIError
from gha_optimizer.features.feature_extraction import (
    LineageState,
    _group_runs,
    build_inference_frame,
    build_training_frame,
    compute_history_stats,
    run_context_features,
)
from gha_optimizer.ingestion.github_client import WorkflowRunRecord
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text

logger = logging.getLogger(__name__)

STRUCTURAL = ["job_structurally_impacted", "cache_key_touched", "dep_graph_distance", "any_changed_file_in_graph"]
PR_EVENTS = ("pull_request", "pull_request_target")
MODELS = {"old_logistic": "logistic", "v2_hgb": "v2_hgb"}      # ledger name -> FailurePredictor variant
TOP_FRACTION = 0.10


# ---------------------------------------------------------------------------
# Ledger: append-only, hash-chained
# ---------------------------------------------------------------------------

def _digest(prev: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()


def append_ledger(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    prev = "0" * 64
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            prev = json.loads(lines[-1])["hash"]
    entry = {**entry, "prev_hash": prev}
    entry["hash"] = _digest(prev, {k: v for k, v in entry.items() if k != "hash"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return entry


def read_ledger(path: Path, verify: bool = True) -> list[dict[str, Any]]:
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if verify:
        prev = "0" * 64
        for i, r in enumerate(rows):
            body = {k: v for k, v in r.items() if k != "hash"}
            if r["prev_hash"] != prev or _digest(prev, body) != r["hash"]:
                raise ValueError(f"ledger entry {i} fails the hash chain: the ledger was modified after it was written")
            prev = r["hash"]
    return rows


# ---------------------------------------------------------------------------
# Scoring one run
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


OPEN_STATUSES = ("queued", "in_progress", "waiting", "requested", "pending")


class LiveScorer:
    """State needed to score newly queued runs of one workflow: fitted models + the history they were fitted on."""

    def __init__(self, repo: str, workflow_path: str, history: list[WorkflowRunRecord], client: Any, mirror: Any,
                 ledger: Path, models: dict[str, Any] | None = None):
        from gha_optimizer.models.failure_predictor import FailurePredictor

        self.repo, self.workflow_path, self.client, self.mirror, self.ledger = repo, workflow_path, client, mirror, ledger
        self.history = [r for r in history if r.workflow_path in (None, workflow_path)]
        if not self.history:
            raise ValueError("no ingested history for this workflow")
        df = build_training_frame(nx.DiGraph(), self.history)
        df[STRUCTURAL] = np.nan                                  # never use graph features live (see module docstring)
        self.models = models or {name: FailurePredictor(variant=v) for name, v in MODELS.items()}
        self.fit_info = {}
        for name, m in self.models.items():
            if getattr(m, "_fitted", False):
                continue
            m.fit(df)
            self.fit_info[name] = {"variant": m.variant, "rows": int(len(df)), "runs": int(df["run_id"].nunique())}
        self.job_stats, self.author_stats = compute_history_stats(self.history, workflow_path=workflow_path)
        self.lineage = LineageState()
        for group in _group_runs(self.history):
            self.lineage.update_run(group)
        self.history_until = max(r.timestamp for r in self.history)
        self.model_hash = hashlib.sha256(json.dumps(self.fit_info, sort_keys=True).encode()
                                         + self.history_until.encode()).hexdigest()[:16]

    # -- inputs known at queue time ------------------------------------------------------------------------
    def job_keys(self, sha: str) -> list[str] | None:
        got = self.mirror.read_file(sha, self.workflow_path)
        if got is None and self.mirror.fetch_sha(sha):
            got = self.mirror.read_file(sha, self.workflow_path)
        if got is None:
            return None
        try:
            return list(parse_workflow_text(got[1].decode("utf-8", "replace")).jobs)
        except Exception:
            return None

    def diff(self, run: dict[str, Any]) -> tuple[list[dict[str, Any]], str] | None:
        head = run["head_sha"]
        prs = run.get("pull_requests") or []
        if run.get("event") in PR_EVENTS and prs and prs[0].get("base", {}).get("sha"):
            data = self.client.compare(prs[0]["base"]["sha"], head)
            if data is not None:
                return data.get("files", []), "compare_pr"
        data = self.client.get_commit(head)
        if data is None:
            return None
        return data.get("files", []), ("commit_only" if run.get("event") in PR_EVENTS else "commit")

    def already_started(self, run_id: int) -> bool:
        return any(j.get("status") == "completed" for j in self.client.list_jobs(run_id))

    # -- scoring --------------------------------------------------------------------------------------------
    def score_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run["id"])
        base = {"run_id": run_id, "head_sha": run.get("head_sha"), "event": run.get("event")}
        if self.already_started(run["id"]):
            return append_ledger(self.ledger, {**base, "kind": "skipped", "reason": "a job had already completed",
                                               "scored_at": _iso_now()})
        keys = self.job_keys(run["head_sha"])
        d = self.diff(run)
        if not keys or d is None:
            return append_ledger(self.ledger, {**base, "kind": "skipped", "reason": "workflow or diff unavailable",
                                               "scored_at": _iso_now()})
        files, source = d
        names = [f["filename"] for f in files]
        created = run.get("created_at") or _iso_now()
        pr = (run.get("pull_requests") or [{}])[0].get("number")
        actor = (run.get("actor") or {}).get("login")
        proto = [WorkflowRunRecord(run_id=run_id, commit_sha=run["head_sha"], files_changed=names, job_id=k,
                                   duration_seconds=None, conclusion=None, cache_hit=None, author=actor, timestamp=created,
                                   branch=run.get("head_branch"), workflow_path=self.workflow_path, event=run.get("event"),
                                   pr_number=pr, run_attempt=run.get("run_attempt")) for k in keys]
        ctx = {**run_context_features(proto[0])}
        lin = {r.job_id: self.lineage.features(r) for r in proto}
        ctx["lineage_runs_so_far"] = lin[keys[0]]["lineage_runs_so_far"]
        ctx["lineage_prev_run_any_failure"] = lin[keys[0]]["lineage_prev_run_any_failure"]
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        frame = build_inference_frame(
            nx.DiGraph(), keys, names, self.job_stats, hour_of_day=ts.hour, day_of_week=ts.weekday(), author=actor,
            author_stats=self.author_stats, loc_added=sum(int(f.get("additions") or 0) for f in files),
            loc_deleted=sum(int(f.get("deletions") or 0) for f in files), context=ctx, lineage_stats=lin)
        frame[STRUCTURAL] = np.nan
        preds = {name: m.predict_proba(frame).to_numpy() for name, m in self.models.items()}
        baseline = frame["historical_failure_rate"].to_numpy()
        jobs = [{"job": k, **{f"p_{n}": float(p[i]) for n, p in preds.items()},
                 "p_baseline": None if np.isnan(baseline[i]) else float(baseline[i])} for i, k in enumerate(keys)]
        return append_ledger(self.ledger, {**base, "kind": "scored", "scored_at": _iso_now(),
                                           "run_status_at_scoring": run.get("status"), "jobs_completed_at_scoring": 0,
                                           "diff_source": source, "files_changed": len(names), "model_hash": self.model_hash,
                                           "history_until": self.history_until, "jobs": jobs})

    def poll_once(self, workflow_id: int, since: str, seen: set[str]) -> int:
        """Score every queued / in-progress run of the workflow created at or after ``since`` that is not yet in the ledger.

        One request per poll (runs created since ``since``, any status; finished ones are ignored here, exactly as the two
        status-filtered requests used before): half the API cost, which is what allows a poll interval of a few seconds."""
        n = 0
        data = self.client.get_json(f"/repos/{self.repo}/actions/workflows/{workflow_id}/runs",
                                    {"per_page": 100, "created": f">={since}"})
        for run in data.get("workflow_runs", []):
            if run.get("status") not in OPEN_STATUSES:
                continue
            rid = str(run["id"])
            if rid in seen:
                continue
            seen.add(rid)
            entry = self.score_run(run)
            n += entry["kind"] == "scored"
        return n


def run_live(scorer: LiveScorer, workflow_id: int, minutes: float, poll_seconds: float,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """Poll for new runs for ``minutes``; returns counts.  Runs already in the ledger are never rescored."""
    seen: set[str] = set()
    if scorer.ledger.exists():
        seen = {r["run_id"] for r in read_ledger(scorer.ledger)}
    since = _iso_now()
    deadline = time.time() + 60 * minutes
    scored = 0
    while time.time() < deadline:
        try:
            scored += scorer.poll_once(workflow_id, since, seen)
            logger.info("live: %d run(s) scored so far", scored)
        except GithubAPIError as exc:       # an API / network outage must pause the scorer, not end it (it used to exit with code 1)
            logger.warning("live: poll failed (%s); trying again in %.0f s", exc, max(poll_seconds, 30.0))
            sleep(max(poll_seconds, 30.0))
            continue
        sleep(poll_seconds)
    return {"scored_runs": scored, "ledger_runs": len(seen)}


# ---------------------------------------------------------------------------
# Evaluation of a ledger against the real outcomes
# ---------------------------------------------------------------------------

def evaluate_ledger(rows: list[dict[str, Any]], outcomes: dict[tuple[str, str], int | None],
                    n_boot: int = 1000, seed: int = 13) -> dict[str, Any]:
    """Score every ledger prediction that has a final success / failure outcome.

    ``outcomes[(run_id, job)]``: 1 failed, 0 passed, None unknown / not executed (excluded)."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    scored = [r for r in rows if r.get("kind") == "scored"]
    flat = []
    for r in scored:
        for j in r["jobs"]:
            outcome = outcomes.get((r["run_id"], j["job"]))
            if outcome is not None:
                flat.append((r["run_id"], j, int(outcome)))
    result: dict[str, Any] = {"runs_in_ledger": len(scored), "skipped_runs": len(rows) - len(scored),
                              "runs_with_outcomes": len({f[0] for f in flat}), "scored_jobs": len(flat),
                              "positives": int(sum(f[2] for f in flat))}
    if not flat or result["positives"] in (0, len(flat)):
        result["note"] = "not enough outcomes of both kinds to score yet"
        return result
    y = np.array([f[2] for f in flat])
    runs = np.array([f[0] for f in flat])
    models = [k[2:] for k in flat[0][1] if k.startswith("p_")]
    base_fill = float(y.mean())
    P = {m: np.array([f[1][f"p_{m}"] if f[1][f"p_{m}"] is not None else base_fill for f in flat]) for m in models}
    result["base_rate"] = float(y.mean())

    def top_precision(p: np.ndarray, yy: np.ndarray) -> tuple[float, float]:
        k = max(int(np.ceil(TOP_FRACTION * len(p))), 1)
        idx = np.argsort(-p, kind="stable")[:k]
        return float(yy[idx].mean()), float(yy[idx].sum()) / float(yy.sum())

    metrics = {}
    for m, p in P.items():
        prec, rec = top_precision(p, y)
        metrics[m] = {"pr_auc": float(average_precision_score(y, p)), "roc_auc": float(roc_auc_score(y, p)),
                      "precision_top10pct": prec, "recall_top10pct": rec}
    result["metrics"] = metrics
    rng = np.random.default_rng(seed)
    groups = {r: np.where(runs == r)[0] for r in set(runs)}
    keys = list(groups)
    diffs: dict[str, list[float]] = {m: [] for m in models if m != "baseline"}
    for _ in range(n_boot):
        ix = np.concatenate([groups[keys[i]] for i in rng.integers(0, len(keys), len(keys))])
        yb = y[ix]
        if not (0 < yb.sum() < len(yb)):
            continue
        for m in diffs:
            diffs[m].append(average_precision_score(yb, P[m][ix]) - average_precision_score(yb, P["baseline"][ix]))
    result["pr_auc_minus_baseline"] = {
        m: {"delta": float(np.mean(v)), "ci95": [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))], "n": len(v)}
        for m, v in diffs.items() if len(v) >= 50}
    return result


def render_live(result: dict[str, Any], ledger_path: str, first_scored_at: str | None, last_scored_at: str | None) -> str:
    out = ["# Live (prospective) failure detection\n",
           f"Ledger `{ledger_path}` (hash-chained; verified before scoring). Predictions were written when a run was queued, "
           f"before any of its jobs completed ({first_scored_at} to {last_scored_at}); outcomes were read afterwards. "
           "Method and limits: `gha_optimizer/live.py`. **A live sample this small demonstrates the pipeline; it is not a "
           "significance test.**\n",
           f"* runs scored: {result['runs_in_ledger']} (runs skipped because a job had already completed or the diff was "
           f"unavailable: {result['skipped_runs']}); runs with final outcomes: {result['runs_with_outcomes']}\n"
           f"* job predictions with an outcome: {result['scored_jobs']}, of which failures: {result['positives']}\n"]
    if "metrics" not in result:
        out.append(f"{result.get('note', '')}\n")
        return "\n".join(out)
    out.append(f"* base rate of failure among scored jobs: {100 * result['base_rate']:.1f}%\n")
    out.append("| model | PR-AUC | ROC-AUC | precision in the top 10% of scores | share of all failures caught there |")
    out.append("|---|---|---|---|---|")
    for m, v in result["metrics"].items():
        out.append(f"| {m} | {v['pr_auc']:.3f} | {v['roc_auc']:.3f} | {v['precision_top10pct']:.2f} | {v['recall_top10pct']:.2f} |")
    out.append("")
    for m, d in result.get("pr_auc_minus_baseline", {}).items():
        lo, hi = d["ci95"]
        out.append(f"* PR-AUC of `{m}` minus baseline: {d['delta']:+.3f} [{lo:+.3f}, {hi:+.3f}] "
                   f"({'excludes' if lo > 0 or hi < 0 else 'includes'} 0; run-level bootstrap)")
    return "\n".join(out) + "\n"
