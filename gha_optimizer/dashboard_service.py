"""
The one backend of the dashboard UI (``dashboard/index.html``).

It runs the real pipeline on an ingested repository - the same layers as :func:`gha_optimizer.pipeline.run_pipeline` -
and returns every intermediate result as JSON for the UI, plus the pieces a presenter needs:

* **progress** - the run is a background job; the UI polls :meth:`DashboardService.status` and shows the stage that is running;
* **disk cache** - a finished result is stored under ``.gha_optimizer/dashboard_cache/`` with a fingerprint of everything it
  was computed from (ingested runs, the repository's tip commit, model and feature versions), and reused after a restart only
  when that fingerprint still matches;
* **what-if** - the trained models, graph and history statistics of a run are kept in memory so a presenter can pick other
  changed files and see the impacted jobs, predictions and schedule update (:meth:`DashboardService.what_if`);
* **evidence** - rows of the generated reports in ``docs/`` for the selected repository (:mod:`gha_optimizer.evidence`).

Nothing here invents a value: with too little real history the result is an explicit "declined" payload, as in the CLI.
All improvement figures are ESTIMATED from predicted durations; the scheduler's default plan keeps the workflow's job
structure, so the estimated saving is 0% unless an opt-in lever is used.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import networkx as nx
import numpy as np
import yaml
from sklearn.metrics import average_precision_score, brier_score_loss

from gha_optimizer import evidence
from gha_optimizer.config import config
from gha_optimizer.features.feature_extraction import (
    FEATURE_COLUMNS,
    build_inference_frame,
    build_training_frame,
    compute_history_stats,
)
from gha_optimizer.generator.workflow_writer import generate_diff, generate_optimized_workflow, write_workflow
from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.dependency_graph import (
    build_dependency_graph,
    cache_keys_touched_by,
    graph_summary,
    impacted_jobs_for_diff,
)
from gha_optimizer.graph.fusion import GraphFusion
from gha_optimizer.ingestion.history import InsufficientHistoryError, require_history
from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.ingestion.workspace import prepare_repo_snapshot
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
from gha_optimizer.models.calibration import OutOfSampleCalibrator, out_of_sample_predictions
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.pipeline import job_only_dag
from gha_optimizer.scheduler.optimizer import build_schedule
from gha_optimizer.server.db.session import SessionLocal, init_db

logger = logging.getLogger(__name__)

RESULT_VERSION = 3          # bump when the payload or anything it is computed from changes meaning
DEFAULT_CACHE_DIR = Path(".gha_optimizer") / "dashboard_cache"
STAGES = [
    "Repository snapshot and history",
    "Source code graph",
    "CI/CD graph",
    "Graph fusion",
    "Impact analysis",
    "Feature extraction",
    "Training the three models",
    "Out-of-sample check against the baseline",
    "DAG-aware scheduling",
    "DAG safety verification",
    "Writing the proposed workflow",
]


def _num(v: Any) -> Any:
    if isinstance(v, (int, float, np.integer, np.floating)):
        v = float(v)
        return v if v == v else None
    return str(v)


def _nan_to_none(v: Any) -> Any:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


@dataclass
class RepoContext:
    """What a what-if needs, kept in memory after a repository has been run."""
    repo: str
    workflow: Any
    simple_graph: nx.DiGraph
    full_graph: nx.MultiDiGraph
    tracked_files: list[str]
    real_diff: list[str]
    stats: dict
    as_of: Any
    job_ids: list[str]
    dag: nx.DiGraph
    models: dict[str, Any]
    calibrator: OutOfSampleCalibrator | None
    suggestions: list[dict[str, Any]]


@dataclass
class Progress:
    state: str = "idle"              # idle | queued | running | done | error
    stage: int = -1
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    from_disk_cache: bool = False
    stage_seconds: dict[int, float] = field(default_factory=dict)


class DashboardService:
    def __init__(self, default_repo: str | None = None, snapshot_root: str | Path | None = None,
                 cache_dir: str | Path = DEFAULT_CACHE_DIR, docs_dir: Path | None = None):
        self.default_repo = default_repo
        self.snapshot_root = Path(snapshot_root) if snapshot_root else None
        self.cache_dir = Path(cache_dir)
        self.docs_dir = docs_dir or evidence.DOCS_DIR
        self._results: dict[str, dict] = {}
        self._contexts: dict[str, RepoContext] = {}
        self._progress: dict[str, Progress] = {}
        self._lock = threading.Lock()            # one pipeline at a time (snapshots are written to disk)
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------ repositories
    def repos(self) -> list[dict]:
        from sqlalchemy import func

        from gha_optimizer.server.db import models as m

        init_db()
        db = SessionLocal()
        try:
            rows = (db.query(m.WorkflowRun.repo, m.WorkflowRun.workflow_path, func.count(m.WorkflowRun.id))
                    .group_by(m.WorkflowRun.repo, m.WorkflowRun.workflow_path).all())
        finally:
            db.close()
        best: dict[str, tuple[str, int]] = {}
        for repo, wf, n in rows:
            if repo not in best or n > best[repo][1]:
                best[repo] = (wf, n)
        out = []
        for repo, (wf, n) in sorted(best.items()):
            res = self._results.get(repo) or self._disk_result(repo, check_fingerprint=False)
            out.append({"repo": repo, "workflow": wf, "runs": n, "ready": res is not None,
                        "declined": bool(res and res.get("declined")), "state": self._progress.get(repo, Progress()).state})
        return out

    # ------------------------------------------------------------------ fingerprint and disk cache
    def fingerprint(self, repo: str) -> str:
        """Hash of everything a result depends on that can change without the code changing."""
        import sklearn
        from sqlalchemy import func

        from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
        from gha_optimizer.server.db import models as m

        init_db()
        db = SessionLocal()
        try:
            n, last_id, last_at = db.query(func.count(m.WorkflowRun.id), func.max(m.WorkflowRun.run_id),
                                           func.max(m.WorkflowRun.gh_updated_at)).filter(m.WorkflowRun.repo == repo).one()
            jobs = db.query(func.count(m.WorkflowJob.id)).join(m.WorkflowRun, m.WorkflowRun.run_id == m.WorkflowJob.run_id) \
                .filter(m.WorkflowRun.repo == repo).scalar()
            labelled = db.query(func.count(m.WorkflowJob.id)).join(m.WorkflowRun, m.WorkflowRun.run_id == m.WorkflowJob.run_id) \
                .filter(m.WorkflowRun.repo == repo, m.WorkflowJob.cache_hit.isnot(None)).scalar()
        finally:
            db.close()
        try:
            from gha_optimizer.ingestion.mirror import DEFAULT_MIRROR_ROOT, RepoMirror
            tip = RepoMirror(repo, DEFAULT_MIRROR_ROOT).head_sha()
        except Exception:
            tip = "no-mirror"
        parts = [RESULT_VERSION, repo, n, last_id, str(last_at), jobs, labelled, tip, GRAPH_FEATURE_VERSION,
                 sklearn.__version__, config.model.temporal_test_fraction, config.scheduler.speedup_exponent]
        return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:24]

    def _cache_path(self, repo: str) -> Path:
        return self.cache_dir / f"{repo.replace('/', '__')}.json"

    def _disk_result(self, repo: str, check_fingerprint: bool = True) -> dict | None:
        path = self._cache_path(repo)
        if not path.is_file():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if blob.get("version") != RESULT_VERSION:
                return None
            if check_fingerprint and blob.get("fingerprint") != self.fingerprint(repo):
                return None
            return blob["result"]
        except (OSError, ValueError, KeyError):
            return None

    def _write_disk(self, repo: str, result: dict) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path(repo).with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": RESULT_VERSION, "fingerprint": self.fingerprint(repo), "saved_at": time.time(),
                                       "result": result}), encoding="utf-8")
            tmp.replace(self._cache_path(repo))
        except OSError as exc:
            logger.warning("could not write the dashboard cache for %s: %s", repo, exc)

    # ------------------------------------------------------------------ running
    def status(self, repo: str) -> dict:
        p = self._progress.get(repo) or Progress()
        now = time.time()
        return {"repo": repo, "state": p.state, "stage": p.stage, "stage_name": STAGES[p.stage] if 0 <= p.stage < len(STAGES) else "",
                "stages": STAGES, "message": p.message, "error": p.error, "from_disk_cache": p.from_disk_cache,
                "elapsed": round((p.finished_at or now) - p.started_at, 1) if p.started_at else 0.0,
                "stage_seconds": {str(k): round(v, 1) for k, v in p.stage_seconds.items()},
                "context_ready": repo in self._contexts}

    def start(self, repo: str, force: bool = False, need_context: bool = False) -> dict:
        """Make sure a result for ``repo`` exists (idempotent).  Returns the status; compute runs in a background thread."""
        with self._state_lock:
            p = self._progress.get(repo)
            if p and p.state in ("queued", "running"):
                return self.status(repo)
            if not force and repo in self._results and (not need_context or repo in self._contexts):
                return self.status(repo)
            if not force and repo not in self._results:
                cached = self._disk_result(repo)
                if cached is not None:
                    self._results[repo] = cached
                    self._progress[repo] = Progress(state="done", stage=len(STAGES) - 1, from_disk_cache=True,
                                                    message="loaded from the disk cache (inputs unchanged)")
                    if not need_context:
                        return self.status(repo)
            self._progress[repo] = Progress(state="queued", message="waiting for another repository to finish")
            threading.Thread(target=self._job, args=(repo,), daemon=True).start()
        return self.status(repo)

    def _job(self, repo: str) -> None:
        with self._lock:
            p = self._progress[repo]
            p.state, p.started_at, p.message = "running", time.time(), ""
            marks = {"t": time.time(), "stage": -1}

            def progress(stage: int, message: str = "") -> None:
                now = time.time()
                if marks["stage"] >= 0:
                    p.stage_seconds[marks["stage"]] = now - marks["t"]
                marks.update(t=now, stage=stage)
                p.stage, p.message = stage, message

            try:
                result, ctx = self._run(repo, progress)
                if marks["stage"] >= 0:
                    p.stage_seconds[marks["stage"]] = time.time() - marks["t"]
                self._results[repo] = result
                if ctx is not None:
                    self._contexts[repo] = ctx
                self._write_disk(repo, result)
                p.state, p.finished_at, p.stage = "done", time.time(), len(STAGES) - 1
            except Exception as exc:                      # surfaced to the UI, never swallowed
                logger.exception("dashboard run of %s failed", repo)
                p.state, p.error, p.finished_at = "error", f"{type(exc).__name__}: {exc}", time.time()

    def result(self, repo: str) -> dict | None:
        return self._results.get(repo)

    def warm(self, repos: list[str]) -> None:
        """Compute (or load from the disk cache) these repositories one after another; returns when all are done."""
        for repo in repos:
            self.start(repo)
            while self._progress[repo].state in ("queued", "running"):
                time.sleep(0.5)

    # ------------------------------------------------------------------ the pipeline
    def _run(self, repo: str, progress: Callable[[int, str], None]) -> tuple[dict, RepoContext | None]:
        t0 = time.time()
        progress(0, "exporting the repository tree and loading the real history")
        init_db()
        db = SessionLocal()
        try:
            extra = {"snapshot_root": self.snapshot_root} if self.snapshot_root else {}
            snap = prepare_repo_snapshot(db, repo, **extra)
        except (InsufficientHistoryError, LookupError) as exc:
            return {"declined": True, "repo": repo, "reason": str(exc),
                    "hint": f"gha-optimizer ingest --repo {repo}  (no synthetic data is substituted)"}, None
        finally:
            db.close()

        results: dict[str, Any] = {"declined": False, "repo": repo, "result_version": RESULT_VERSION}
        repo_root, wf_path = snap.root, snap.workflow_path
        out_path = repo_root / ".github" / "workflows" / f"{wf_path.stem}.optimized.yml"
        changed, alt_changed = snap.changed_files, snap.alt_changed_files
        workflow = parse_workflow(wf_path)
        dag_edges = workflow.job_dag_edges()
        results["data_source"] = {
            "repo": snap.repo, "commit": snap.sha[:10], "workflow": snap.workflow_repo_path,
            "history_rows": len(snap.history), "history_runs": len({r.run_id for r in snap.history}),
            "diff_from_run": snap.changed_from_run,
            "scored_at": snap.as_of.isoformat() if snap.as_of is not None else None,
        }
        results["step1_repository"] = {
            "files_discovered": snap.source_files[:50], "file_count": len(snap.source_files), "workflow_name": workflow.name,
            "job_count": len(workflow.jobs), "dag_edge_count": len(dag_edges), "triggers": list(workflow.triggers.keys()),
            "jobs": {jid: {"runs_on": str(js.runs_on), "steps": len(js.steps), "needs": js.needs, "uses_cache": js.uses_cache_action}
                     for jid, js in workflow.jobs.items()},
            "dag_edges": [{"from": u, "to": d} for u, d in dag_edges],
        }

        progress(1, f"parsing {len(snap.source_files)} source files")
        source_builder = GraphBuilder(repo_root)
        n_parsed = source_builder.build_from_source([repo_root / f for f in snap.source_files])
        source_graph = source_builder.get_graph()
        results["step2_source_graph"] = {
            "files_parsed": n_parsed, "node_count": source_graph.number_of_nodes(), "edge_count": source_graph.number_of_edges(),
            "node_types": dict(Counter(d.get("node_type", "unknown") for _, d in source_graph.nodes(data=True))),
            "edges": [{"from": u.split("::")[-1], "to": v.split("::")[-1], "type": d.get("edge_type", "?")}
                      for u, v, d in list(source_graph.edges(data=True))[:200]],
        }

        progress(2, "building the workflow graph")
        ci_builder = GraphBuilder(repo_root)
        ci_builder.build_from_workflow(workflow)
        ci_graph = ci_builder.get_graph()
        results["step3_ci_graph"] = {
            "node_count": ci_graph.number_of_nodes(), "edge_count": ci_graph.number_of_edges(),
            "node_types": dict(Counter(d.get("node_type", "unknown") for _, d in ci_graph.nodes(data=True))),
        }

        progress(3, "linking source files to jobs and cache keys")
        fused_graph = GraphFusion.fuse(source_graph, ci_graph)
        fusion_edges = []
        for u, v, d in fused_graph.edges(data=True):
            prov = d.get("provenance", {})
            if isinstance(prov, dict) and prov.get("origin_parser") == "GraphFusion":
                fusion_edges.append({"from": u.split("::")[-1], "to": v.split("::")[-1], "type": d.get("edge_type", "?"),
                                     "confidence": d.get("confidence", 0)})
        results["step4_fusion"] = {
            "node_count": fused_graph.number_of_nodes(), "edge_count": fused_graph.number_of_edges(),
            "cross_domain_edges": len(fusion_edges), "fusion_edges": fusion_edges[:20], "summary": graph_summary(fused_graph),
        }

        progress(4, "which jobs does the real diff reach?")
        full_graph = build_dependency_graph(workflow, repo_root, snap.tracked_files)
        primary = changed[0] if changed else ""
        comparison = alt_changed[0] if alt_changed else ""
        results["step5_impact"] = {
            "primary_change": primary, "changed_file_count": len(changed),
            "primary_impacted_jobs": sorted(impacted_jobs_for_diff(full_graph, changed)),
            "primary_cache_touched": sorted(cache_keys_touched_by(full_graph, changed)),
            "comparison_change": comparison,
            "comparison_impacted_jobs": sorted(impacted_jobs_for_diff(full_graph, alt_changed)) if alt_changed else [],
            "comparison_cache_touched": sorted(cache_keys_touched_by(full_graph, alt_changed)) if alt_changed else [],
        }
        try:
            require_history(snap.history, workflow.jobs.keys(), config.model.min_history_runs, config.model.min_cache_labels,
                            need_failure_labels=True)
        except InsufficientHistoryError as exc:
            results.update(declined=True, reason=str(exc), hint="ingest more real history (with job logs); no synthetic data is substituted")
            return results, None

        progress(5, "building the training frame and the inference rows")
        simple_graph = nx.DiGraph(full_graph)
        train_df = build_training_frame(simple_graph, snap.history)
        stats, _ = compute_history_stats(snap.history, workflow_path=snap.workflow_repo_path, as_of=snap.as_of)
        job_ids = list(workflow.jobs.keys())
        infer_df = build_inference_frame(simple_graph, job_ids, changed, stats, as_of=snap.as_of)
        results["step6_features"] = {
            "history_records": len(snap.history), "training_shape": f"{train_df.shape[0]} x {train_df.shape[1]}",
            "feature_columns": FEATURE_COLUMNS,
            "per_job_features": {row["job_id"]: {c: _nan_to_none(round(float(row[c]), 4)) for c in FEATURE_COLUMNS}
                                 for _, row in infer_df.iterrows()},
        }

        progress(6, "cache-miss, failure and duration models")
        trained: dict[str, Any] = {}
        step7: dict[str, Any] = {}
        for name, model in {"cache_miss": CacheMissPredictor(), "failure": FailurePredictor(), "duration": DurationEstimator()}.items():
            try:
                metrics = model.fit(train_df)
                trained[name] = model
                step7[name] = {**{k: _num(v) for k, v in metrics.items()}, "status": "TRAINED"}
            except ValueError as exc:
                step7[name] = {"status": f"NOT TRAINED - {exc}"}
        if len(trained) < 3:
            results.update(declined=True, reason="not all models could be trained from the real labels", step7_models=step7)
            return results, None

        progress(7, "rolling-origin predictions on unseen rows (failure model vs the job's prior failure rate)")
        scores, outcomes, baseline = out_of_sample_predictions(train_df)
        calibrator = OutOfSampleCalibrator(scores, outcomes) if len(scores) else None
        if len(scores) and 0 < outcomes.sum() < len(outcomes):
            step7["failure_out_of_sample"] = {
                "rows": int(len(scores)), "failures": int(outcomes.sum()), "base_rate": round(float(outcomes.mean()), 4),
                "pr_auc_model": round(float(average_precision_score(outcomes, scores)), 4),
                "pr_auc_baseline": round(float(average_precision_score(outcomes, baseline)), 4),
                "brier_model": round(float(brier_score_loss(outcomes, np.clip(scores, 1e-6, 1 - 1e-6))), 4),
                "brier_baseline": round(float(brier_score_loss(outcomes, np.clip(baseline, 1e-6, 1 - 1e-6))), 4),
                "highest_score": round(float(scores.max()), 4),
                "note": "point estimates on the pooled rolling-origin folds; intervals and more repositories: docs/MODEL_EVAL.md, docs/CALIBRATION.md",
            }

        dag = job_only_dag(workflow)
        ctx = RepoContext(repo=repo, workflow=workflow, simple_graph=simple_graph, full_graph=full_graph,
                          tracked_files=snap.tracked_files, real_diff=list(changed), stats=stats, as_of=snap.as_of, job_ids=job_ids,
                          dag=dag, models=trained, calibrator=calibrator, suggestions=_suggest_changes(snap.tracked_files))
        scored = self._score(ctx, changed)
        step7["predictions"] = scored["predictions"]
        step7["feature_importance"] = [{"name": n, "value": round(float(v), 4)}
                                       for n, v in list(trained["cache_miss"].feature_importance.items())[:7]]
        step7["calibration"] = calibrator.describe() if calibrator is not None else None
        results["step7_models"] = step7

        progress(8, "fail-fast ordering, runner choice and the estimated plan")
        plan = scored["_plan"]
        results["step8_schedule"] = {"jobs": scored["schedule_jobs"], "original_order": job_ids}

        progress(9, "checking every needs relationship")
        level = {j["job_id"]: j["dag_level"] for j in scored["schedule_jobs"]}
        validation = {
            "all_jobs_present": set(workflow.jobs) == set(level),
            "no_cycles": nx.is_directed_acyclic_graph(dag),
            "needs_preserved": all(level[dep] < level[jid] for jid, js in workflow.jobs.items() for dep in js.needs if dep in level),
        }
        validation["workflow_executable"] = all(validation.values())
        results["step9_validation"] = validation

        progress(10, "generating the proposed YAML and its diff (your original file is not modified)")
        optimized_yaml = generate_optimized_workflow(workflow, plan)
        write_workflow(optimized_yaml, out_path, plan)
        proposed_text = out_path.read_text(encoding="utf-8")
        try:
            yaml_valid = "jobs" in yaml.safe_load(proposed_text)
        except yaml.YAMLError:
            yaml_valid = False
        diff = generate_diff(wf_path, optimized_yaml, plan=plan)
        results["step10_output"] = {
            "yaml_valid": yaml_valid, "output_path": str(out_path), "diff_preview": (diff or "(no diff)")[:20000],
            "original_yaml": wf_path.read_text(encoding="utf-8"), "proposed_yaml": proposed_text,
            "file_name": f"{wf_path.stem}.optimized.yml",
        }
        results["summary"] = {
            "files_analyzed": len(snap.source_files), "source_deps": source_graph.number_of_edges(), "ci_deps": ci_graph.number_of_edges(),
            "fused_nodes": fused_graph.number_of_nodes(), "fused_edges": fused_graph.number_of_edges(), "jobs": len(workflow.jobs),
            "models_trained": len(trained), **_plan_numbers(plan), "estimated": True,
            "elapsed": round(time.time() - t0, 2),
        }
        results["suggestions"] = ctx.suggestions
        return results, ctx

    # ------------------------------------------------------------------ scoring (pipeline and what-if share this)
    def _score(self, ctx: RepoContext, changed: list[str]) -> dict[str, Any]:
        infer_df = build_inference_frame(ctx.simple_graph, ctx.job_ids, changed, ctx.stats, as_of=ctx.as_of)
        infer_df.index = infer_df["job_id"]
        cache = ctx.models["cache_miss"].predict_proba(infer_df).to_dict()
        fail = ctx.models["failure"].predict_proba(infer_df).to_dict()
        dur = ctx.models["duration"].predict(infer_df).to_dict()
        plan = build_schedule(ctx.dag, fail, cache, dur)
        predictions = {}
        for j in ctx.job_ids:
            lookup = ctx.calibrator.lookup(fail[j]) if ctx.calibrator is not None else {"available": False}
            predictions[j] = {
                "cache_miss": round(cache[j], 4), "failure": round(fail[j], 4), "duration": round(dur[j], 1),
                "failure_base_rate": _nan_to_none(round(float(infer_df.loc[j, "historical_failure_rate"]), 4)),
                "failure_seen_like_this": lookup,
                "structurally_impacted": _nan_to_none(infer_df.loc[j, "job_structurally_impacted"]),
            }
        jobs = [{"job_id": jp.job_id, "order": jp.order, "runner": jp.runner, "dag_level": jp.dag_level,
                 "failure_prob": round(jp.predicted_failure_prob, 4), "est_duration": round(jp.estimated_duration_seconds, 1),
                 "est_cost": round(jp.estimated_cost_usd, 5), "skip": jp.skip_recommended} for jp in plan.jobs]
        return {"predictions": predictions, "schedule_jobs": jobs, "_plan": plan}

    # ------------------------------------------------------------------ what-if
    def files(self, repo: str, query: str = "", limit: int = 40) -> dict:
        ctx = self._contexts.get(repo)
        if ctx is None:
            return {"available": False, "files": [], "suggestions": []}
        q = query.strip().lower()
        matches = [f for f in ctx.tracked_files if q in f.lower()] if q else ctx.tracked_files[:limit]
        return {"available": True, "total": len(ctx.tracked_files), "matches": len(matches), "files": matches[:limit],
                "real_diff": ctx.real_diff[:200], "suggestions": ctx.suggestions}

    def what_if(self, repo: str, changed_files: list[str]) -> dict:
        ctx = self._contexts.get(repo)
        if ctx is None:
            raise KeyError("no in-memory context: run the repository first (POST /run?need_context=1)")
        known = set(ctx.tracked_files)
        files = [f for f in dict.fromkeys(changed_files) if f]
        unknown = [f for f in files if f not in known]
        if not files:
            raise ValueError("pick at least one changed file")
        base = self._score(ctx, ctx.real_diff)
        alt = self._score(ctx, files)
        impacted = sorted(impacted_jobs_for_diff(ctx.full_graph, files))
        jobs = []
        for j in ctx.job_ids:
            b, a = base["predictions"][j], alt["predictions"][j]
            jobs.append({"job_id": j, "impacted": j in impacted, "base": b, "whatif": a,
                         "delta_failure": round(a["failure"] - b["failure"], 4), "delta_duration": round(a["duration"] - b["duration"], 1)})
        return {"repo": repo, "changed_files": files, "unknown_files": unknown, "real_diff_files": len(ctx.real_diff),
                "impacted_jobs": impacted, "cache_keys_touched": sorted(cache_keys_touched_by(ctx.full_graph, files)),
                "jobs": jobs, "schedule": alt["schedule_jobs"], "summary": _plan_numbers(alt["_plan"]),
                "base_summary": _plan_numbers(base["_plan"]),
                "note": ("The structural features (job impacted, cache key touched, graph distance) respond to the files you pick; "
                         "they are informative only where the workflow states a file-to-job relationship (docs/GRAPH_FEATURES.md). "
                         "The other features come from the repository's history and do not change with the files.")}

    # ------------------------------------------------------------------ evidence and documents
    def evidence(self, repo: str) -> dict:
        return {"repo": repo, "reports": evidence.evidence_for_repo(repo, self.docs_dir), "docs": evidence.list_docs(self.docs_dir)}

    def doc(self, name: str) -> str | None:
        return evidence.read_doc(name, self.docs_dir)

    def live(self) -> dict:
        """Hash-chain verified summary of every live-scoring ledger in ``.gha_optimizer/live/``."""
        from gha_optimizer.live import read_ledger

        out = []
        for path in sorted((Path(".gha_optimizer") / "live").glob("*.jsonl")):
            try:
                rows = read_ledger(path)
                verified = True
            except ValueError:
                rows, verified = read_ledger(path, verify=False), False
            scored = [r for r in rows if r.get("kind") == "scored"]
            out.append({"ledger": path.name, "entries": len(rows), "scored_runs": len(scored),
                        "skipped_runs": len(rows) - len(scored), "hash_chain_verified": verified,
                        "first": scored[0].get("scored_at") if scored else None, "last": scored[-1].get("scored_at") if scored else None})
        return {"ledgers": out}


# ---------------------------------------------------------------------------------------------------------------
def _plan_numbers(plan: Any) -> dict[str, Any]:
    return {
        "baseline_wall_clock": round(plan.baseline_wall_clock_seconds, 1), "optimized_wall_clock": round(plan.estimated_wall_clock_seconds, 1),
        "time_saved_pct": round(plan.time_saved_pct, 1), "baseline_cost": round(plan.baseline_cost_usd, 5),
        "optimized_cost": round(plan.estimated_cost_usd, 5), "cost_saved_pct": round(plan.cost_saved_pct, 1),
        "baseline_energy": round(plan.baseline_energy_wh, 2), "optimized_energy": round(plan.estimated_energy_wh, 2),
        "energy_saved_pct": round(plan.energy_saved_pct, 1), "baseline_co2": round(plan.baseline_co2_grams, 2),
        "optimized_co2": round(plan.estimated_co2_grams, 2), "co2_saved_pct": round(plan.co2_saved_pct, 1),
    }


_DEPENDENCY_FILES = re.compile(r"(^|/)(pyproject\.toml|poetry\.lock|uv\.lock|requirements[^/]*\.txt|package(-lock)?\.json|go\.(mod|sum)|Cargo\.(toml|lock)|pom\.xml|build\.gradle(\.kts)?)$")
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]+\.|_test\.(py|go)$|\.test\.(js|ts)$")


def _suggest_changes(tracked: list[str]) -> list[dict[str, Any]]:
    """One-click scenarios for the presenter, built from files that really exist in the repository."""
    def first(pred) -> str | None:
        return next((f for f in tracked if pred(f)), None)

    candidates = [
        ("Docs only", first(lambda f: f.lower().endswith(".md") and ("docs/" in f.lower() or "/" not in f))),
        ("A dependency file", first(lambda f: bool(_DEPENDENCY_FILES.search(f)))),
        ("CI configuration", first(lambda f: f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml")))),
        ("A test file", first(lambda f: bool(_TEST_FILE.search(f)) and f.endswith((".py", ".go", ".java", ".js", ".ts", ".rs")))),
        ("A source file", first(lambda f: f.endswith((".py", ".go", ".java", ".js", ".ts", ".rs")) and not _TEST_FILE.search(f)
                                and not f.startswith((".github/", "docs/")))),
    ]
    return [{"label": label, "files": [f]} for label, f in candidates if f]
