"""
Lightweight Demo Server — serves the real pipeline results to a browser UI.

No Redis, Postgres, or Celery required. Runs entirely locally on REAL ingested data:

    gha-optimizer ingest --repo owner/repo --since 40d     # once
    python demo_server.py --repo owner/repo [--workflow .github/workflows/ci.yml]

Then open http://localhost:8501 in Chrome.  With too little ingested history the API
returns an explicit "declined" payload — there is no synthetic fallback.  Improvement
figures are ESTIMATED from predicted durations, not measured executions.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.WARNING)

import networkx as nx  # noqa: E402
import uvicorn  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from gha_optimizer.config import config  # noqa: E402
from gha_optimizer.features.feature_extraction import (  # noqa: E402
    FEATURE_COLUMNS,
    build_inference_frame,
    build_training_frame,
    compute_history_stats,
)
from gha_optimizer.generator.workflow_writer import generate_diff, generate_optimized_workflow, write_workflow  # noqa: E402
from gha_optimizer.graph.builder import GraphBuilder  # noqa: E402
from gha_optimizer.graph.dependency_graph import (  # noqa: E402
    build_dependency_graph,
    cache_keys_touched_by,
    graph_summary,
    impacted_jobs_for_diff,
)
from gha_optimizer.graph.fusion import GraphFusion  # noqa: E402
from gha_optimizer.ingestion.history import InsufficientHistoryError, require_history  # noqa: E402
from gha_optimizer.ingestion.workflow_parser import parse_workflow  # noqa: E402
from gha_optimizer.ingestion.workspace import prepare_repo_snapshot  # noqa: E402
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor  # noqa: E402
from gha_optimizer.models.duration_estimator import DurationEstimator  # noqa: E402
from gha_optimizer.models.failure_predictor import FailurePredictor  # noqa: E402
from gha_optimizer.pipeline import job_only_dag  # noqa: E402
from gha_optimizer.scheduler.optimizer import build_schedule  # noqa: E402
from gha_optimizer.server.db.session import SessionLocal, init_db  # noqa: E402

# Selected on the command line (see __main__)
TARGET: dict = {"repo": None, "workflow": None}


def _num(v):
    return v if isinstance(v, (int, float)) and v == v else (None if isinstance(v, float) else str(v))


# ─── Run the full pipeline on real data and return structured results ───
def run_demo_pipeline():
    t0 = time.time()
    init_db()
    db = SessionLocal()
    try:
        snap = prepare_repo_snapshot(db, TARGET["repo"], TARGET["workflow"])
    except (InsufficientHistoryError, LookupError) as exc:
        return {"declined": True, "reason": str(exc),
                "hint": f"gha-optimizer ingest --repo {TARGET['repo']}  (no synthetic data is substituted)"}
    finally:
        db.close()

    results: dict = {"declined": False}
    repo_root, wf_path = snap.root, snap.workflow_path
    out_path = repo_root / ".github" / "workflows" / f"{wf_path.stem}.optimized.yml"
    changed, alt_changed = snap.changed_files, snap.alt_changed_files
    primary = changed[0] if changed else ""
    comparison = (alt_changed[0] if alt_changed else "")

    workflow = parse_workflow(wf_path)
    dag_edges = workflow.job_dag_edges()
    results["data_source"] = {
        "repo": snap.repo, "commit": snap.sha[:10], "workflow": snap.workflow_repo_path,
        "history_rows": len(snap.history), "history_runs": len({r.run_id for r in snap.history}),
        "diff_from_run": snap.changed_from_run,
    }
    results["step1_repository"] = {
        "files_discovered": snap.source_files[:50],
        "file_count": len(snap.source_files),
        "workflow_name": workflow.name,
        "job_count": len(workflow.jobs),
        "dag_edge_count": len(dag_edges),
        "triggers": list(workflow.triggers.keys()),
        "jobs": {jid: {"runs_on": str(js.runs_on), "steps": len(js.steps), "needs": js.needs,
                       "uses_cache": js.uses_cache_action} for jid, js in workflow.jobs.items()},
        "dag_edges": [{"from": u, "to": d} for u, d in dag_edges],
    }

    source_builder = GraphBuilder(repo_root)
    n_parsed = source_builder.build_from_source([repo_root / f for f in snap.source_files])
    source_graph = source_builder.get_graph()
    results["step2_source_graph"] = {
        "files_parsed": n_parsed,
        "node_count": source_graph.number_of_nodes(),
        "edge_count": source_graph.number_of_edges(),
        "node_types": dict(Counter(d.get("node_type", "unknown") for _, d in source_graph.nodes(data=True))),
        "edges": [
            {"from": u.split("::")[-1], "to": v.split("::")[-1], "type": d.get("edge_type", "?")}
            for u, v, d in list(source_graph.edges(data=True))[:200]
        ],
    }

    ci_builder = GraphBuilder(repo_root)
    ci_builder.build_from_workflow(workflow)
    ci_graph = ci_builder.get_graph()
    results["step3_ci_graph"] = {
        "node_count": ci_graph.number_of_nodes(),
        "edge_count": ci_graph.number_of_edges(),
        "node_types": dict(Counter(d.get("node_type", "unknown") for _, d in ci_graph.nodes(data=True))),
    }

    fused_graph = GraphFusion.fuse(source_graph, ci_graph)
    fusion_edges = []
    for u, v, d in fused_graph.edges(data=True):
        prov = d.get("provenance", {})
        if isinstance(prov, dict) and prov.get("origin_parser") == "GraphFusion":
            fusion_edges.append({"from": u.split("::")[-1], "to": v.split("::")[-1],
                                 "type": d.get("edge_type", "?"), "confidence": d.get("confidence", 0)})
    results["step4_fusion"] = {
        "node_count": fused_graph.number_of_nodes(), "edge_count": fused_graph.number_of_edges(),
        "cross_domain_edges": len(fusion_edges), "fusion_edges": fusion_edges[:20],
        "summary": graph_summary(fused_graph),
    }

    full_graph = build_dependency_graph(workflow, repo_root, snap.tracked_files)
    results["step5_impact"] = {
        "primary_change": primary,
        "primary_impacted_jobs": sorted(impacted_jobs_for_diff(full_graph, changed)),
        "primary_cache_touched": sorted(cache_keys_touched_by(full_graph, changed)),
        "comparison_change": comparison,
        "comparison_impacted_jobs": sorted(impacted_jobs_for_diff(full_graph, alt_changed)) if alt_changed else [],
        "comparison_cache_touched": sorted(cache_keys_touched_by(full_graph, alt_changed)) if alt_changed else [],
    }

    try:
        require_history(snap.history, workflow.jobs.keys(), config.model.min_history_runs, config.model.min_cache_labels)
    except InsufficientHistoryError as exc:
        results["declined"] = True
        results["reason"] = str(exc)
        results["hint"] = "ingest more real history; no synthetic data is substituted"
        return results

    simple_graph = nx.DiGraph(full_graph)
    train_df = build_training_frame(simple_graph, snap.history)
    stats, _ = compute_history_stats(snap.history, workflow_path=snap.workflow_repo_path)
    infer_df = build_inference_frame(simple_graph, list(workflow.jobs.keys()), changed, stats)
    results["step6_features"] = {
        "history_records": len(snap.history),
        "training_shape": f"{train_df.shape[0]} x {train_df.shape[1]}",
        "feature_columns": FEATURE_COLUMNS,
        "per_job_features": {
            row["job_id"]: {c: _num(round(float(row[c]), 4)) for c in FEATURE_COLUMNS} for _, row in infer_df.iterrows()
        },
    }

    models = {"cache_miss": CacheMissPredictor(), "failure": FailurePredictor(), "duration": DurationEstimator()}
    step7: dict = {}
    trained = {}
    for name, model in models.items():
        try:
            metrics = model.fit(train_df)
            trained[name] = model
            step7[name] = {**{k: _num(v) if isinstance(v, (int, float)) else str(v) for k, v in metrics.items()}, "status": "TRAINED"}
        except ValueError as exc:
            step7[name] = {"status": f"NOT TRAINED — {exc}"}
    if len(trained) < 3:
        results.update({"declined": True, "reason": "not all models could be trained from the real labels", "step7_models": step7})
        return results

    infer_df.index = infer_df["job_id"]
    cache_miss_prob = trained["cache_miss"].predict_proba(infer_df).to_dict()
    failure_prob = trained["failure"].predict_proba(infer_df).to_dict()
    duration_pred = trained["duration"].predict(infer_df).to_dict()
    step7["predictions"] = {
        j: {"cache_miss": round(cache_miss_prob[j], 4), "failure": round(failure_prob[j], 4),
            "duration": round(duration_pred[j], 1)} for j in workflow.jobs
    }
    step7["feature_importance"] = [
        {"name": n, "value": round(v, 4)} for n, v in list(trained["cache_miss"].feature_importance.items())[:7]
    ]
    results["step7_models"] = step7

    dag = job_only_dag(workflow)
    plan = build_schedule(dag, failure_prob, cache_miss_prob, duration_pred)
    results["step8_schedule"] = {
        "jobs": [
            {"job_id": jp.job_id, "order": jp.order, "runner": jp.runner, "dag_level": jp.dag_level,
             "failure_prob": round(jp.predicted_failure_prob, 4), "est_duration": round(jp.estimated_duration_seconds, 1),
             "est_cost": round(jp.estimated_cost_usd, 5), "skip": jp.skip_recommended}
            for jp in plan.jobs
        ],
        "original_order": list(workflow.jobs.keys()),
    }

    level = {jp.job_id: jp.dag_level for jp in plan.jobs}
    results["step9_validation"] = {
        "all_jobs_present": set(workflow.jobs) == set(level),
        "no_cycles": nx.is_directed_acyclic_graph(dag),
        "needs_preserved": all(level[dep] < level[jid] for jid, js in workflow.jobs.items() for dep in js.needs if dep in level),
        "workflow_executable": True,
    }
    results["step9_validation"]["workflow_executable"] = all(
        results["step9_validation"][k] for k in ("all_jobs_present", "no_cycles", "needs_preserved")
    )

    optimized_yaml = generate_optimized_workflow(workflow, plan)
    write_workflow(optimized_yaml, out_path, plan)
    try:
        yaml_valid = "jobs" in yaml.safe_load(out_path.read_text(encoding="utf-8"))
    except Exception:
        yaml_valid = False
    diff = generate_diff(wf_path, optimized_yaml, plan=plan)
    results["step10_output"] = {"yaml_valid": yaml_valid, "output_path": str(out_path),
                                "diff_preview": diff[:2000] if diff else "(no diff)"}

    results["summary"] = {
        "files_analyzed": len(snap.source_files),
        "source_deps": source_graph.number_of_edges(),
        "ci_deps": ci_graph.number_of_edges(),
        "fused_nodes": fused_graph.number_of_nodes(),
        "fused_edges": fused_graph.number_of_edges(),
        "jobs": len(workflow.jobs),
        "models_trained": len(trained),
        "baseline_wall_clock": round(plan.baseline_wall_clock_seconds, 1),
        "optimized_wall_clock": round(plan.estimated_wall_clock_seconds, 1),
        "time_saved_pct": round(plan.time_saved_pct, 1),
        "baseline_cost": round(plan.baseline_cost_usd, 5),
        "optimized_cost": round(plan.estimated_cost_usd, 5),
        "cost_saved_pct": round(plan.cost_saved_pct, 1),
        "baseline_energy": round(plan.baseline_energy_wh, 2),
        "optimized_energy": round(plan.estimated_energy_wh, 2),
        "energy_saved_pct": round(plan.energy_saved_pct, 1),
        "baseline_co2": round(plan.baseline_co2_grams, 2),
        "optimized_co2": round(plan.estimated_co2_grams, 2),
        "co2_saved_pct": round(plan.co2_saved_pct, 1),
        "estimated": True,
        "elapsed": round(time.time() - t0, 2),
    }
    return results


# ─── FastAPI app ─────────────────────────────────────────────────────
demo_app = FastAPI(title="GHA Optimizer Demo")
demo_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cached_results = None


@demo_app.get("/api/demo")
def get_demo_results():
    global _cached_results
    if _cached_results is None:
        _cached_results = run_demo_pipeline()
    return JSONResponse(content=_cached_results)


@demo_app.get("/api/demo/refresh")
def refresh_demo():
    global _cached_results
    _cached_results = run_demo_pipeline()
    return JSONResponse(content=_cached_results)


@demo_app.get("/", response_class=HTMLResponse)
def serve_ui():
    return HTMLResponse(content=(PROJECT_ROOT / "demo_ui.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo UI server on a real ingested repository")
    parser.add_argument("--repo", required=True, help="owner/repo (must have been ingested)")
    parser.add_argument("--workflow", help="repo-relative workflow path (default: the most-ingested workflow)")
    parser.add_argument("--db", help="database URL")
    cli_args = parser.parse_args()
    if cli_args.db:
        os.environ["DATABASE_URL"] = cli_args.db
    TARGET.update(repo=cli_args.repo, workflow=cli_args.workflow)
    print("=" * 60)
    print("  GHA Optimizer — Demo UI Server (real ingested data)")
    print(f"  Repository: {cli_args.repo}")
    print("  Open http://localhost:8501 in Chrome")
    print("=" * 60)
    uvicorn.run(demo_app, host="127.0.0.1", port=8501, log_level="warning")
