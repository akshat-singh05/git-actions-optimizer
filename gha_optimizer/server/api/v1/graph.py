"""
Graph REST API — visualization, knowledge, and analysis endpoints.

Endpoints:
  GET  /graph/{repo_id}/{commit_sha}              — Full graph snapshot
  GET  /graph/{repo_id}/{commit_sha}/subgraph      — File-impact subgraph
  GET  /graph/provenance/{node_id}                 — Node provenance record
  GET  /graph/history/{repo_id}                    — Graph version history
  GET  /graph/diff/{repo_id}/{commit_a}/{commit_b} — Structural diff
  GET  /graph/risk/{repo_id}/{commit_sha}          — Node risk scores
  GET  /graph/hotspots/{repo_id}/{commit_sha}      — Repository hotspots
  GET  /graph/critical-path/{repo_id}/{commit_sha} — Workflow critical path
  GET  /graph/events/{repo_id}                     — Recent graph events
  POST /graph/explain                              — Explainability trace
  GET  /graph/quality/{repo_id}/{commit_sha}       — Graph quality report
  GET  /graph/benchmark/{repo_id}/{commit_sha}     — Graph benchmark report
"""
from __future__ import annotations

from typing import List, Optional

import networkx as nx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gha_optimizer.graph.analytics import GraphAnalytics
from gha_optimizer.graph.benchmark import GraphBenchmarker
from gha_optimizer.graph.explain import GraphExplainer
from gha_optimizer.graph.quality import GraphQualityEvaluator
from gha_optimizer.graph.storage import GraphStorage
from gha_optimizer.graph.versioning import GraphVersioning
from gha_optimizer.server.db.session import get_db
from gha_optimizer.server.db import models as db_models

router = APIRouter(prefix="/graph", tags=["graph"])


# ─── Helper ─────────────────────────────────────────────────────────

def _load_graph(db: Session, repo_id: int, commit_sha: str) -> nx.MultiDiGraph:
    """Load a graph snapshot or raise 404."""
    graph = GraphStorage.get_snapshot(db, repo_id, commit_sha)
    if graph is None:
        raise HTTPException(status_code=404, detail="Graph snapshot not found")
    return graph


# ─── Request / Response Models ──────────────────────────────────────

class ExplainRequest(BaseModel):
    repo_id: int
    commit_sha: str
    source_file: str
    target_job: str


class BulkExplainRequest(BaseModel):
    repo_id: int
    commit_sha: str
    changed_files: List[str]


# ─── Existing endpoints (preserved) ────────────────────────────────

@router.get("/{repo_id}/{commit_sha}")
async def get_graph_snapshot(
    repo_id: int, commit_sha: str, db: Session = Depends(get_db)
):
    """Retrieve a serialized graph snapshot for frontend visualization."""
    graph = _load_graph(db, repo_id, commit_sha)
    return GraphStorage.serialize_to_json(graph)


@router.get("/{repo_id}/{commit_sha}/subgraph")
async def get_impacted_subgraph(
    repo_id: int,
    commit_sha: str,
    file_path: str = Query(..., description="Relative file path"),
    db: Session = Depends(get_db),
):
    """Extract a subgraph highlighting the impact of a specific file."""
    graph = _load_graph(db, repo_id, commit_sha)

    node_id = f"File::{file_path}"
    if node_id not in graph:
        raise HTTPException(status_code=404, detail="File node not found in graph")

    reachable = nx.descendants(graph, node_id)
    reachable.add(node_id)
    subgraph = graph.subgraph(reachable)
    return GraphStorage.serialize_to_json(subgraph)


# ─── Part 9: New Knowledge Layer endpoints ──────────────────────────

@router.get("/provenance/{node_id}")
async def get_provenance(
    node_id: str,
    repo_id: int = Query(...),
    commit_sha: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return the provenance record for a specific node."""
    graph = _load_graph(db, repo_id, commit_sha)
    if node_id not in graph:
        raise HTTPException(status_code=404, detail="Node not found")

    data = dict(graph.nodes[node_id])
    provenance = data.get("provenance", {})
    return {
        "node_id": node_id,
        "node_type": data.get("node_type", "unknown"),
        "provenance": provenance,
    }


@router.get("/history/{repo_id}")
async def get_graph_history(
    repo_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return graph snapshot version history for a repository."""
    snapshots = (
        db.query(db_models.GraphSnapshot)
        .filter(db_models.GraphSnapshot.repo_id == repo_id)
        .order_by(db_models.GraphSnapshot.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": s.id,
            "commit_sha": s.commit_sha,
            "created_at": str(s.created_at),
        }
        for s in snapshots
    ]


@router.get("/diff/{repo_id}/{commit_a}/{commit_b}")
async def get_graph_diff(
    repo_id: int,
    commit_a: str,
    commit_b: str,
    db: Session = Depends(get_db),
):
    """Structural diff between two graph snapshots."""
    graph_a = _load_graph(db, repo_id, commit_a)
    graph_b = _load_graph(db, repo_id, commit_b)
    diff = GraphVersioning.generate_diff(graph_a, graph_b)
    return diff


@router.get("/risk/{repo_id}/{commit_sha}")
async def get_risk_scores(
    repo_id: int,
    commit_sha: str,
    top_k: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return nodes sorted by risk score (highest first)."""
    graph = _load_graph(db, repo_id, commit_sha)

    scored = []
    for node, data in graph.nodes(data=True):
        scored.append({
            "node_id": node,
            "node_type": data.get("node_type", "unknown"),
            "risk_score": data.get("risk_score", 0.0),
            "failure_score": data.get("failure_score", 0.0),
            "importance_score": data.get("importance_score", 0.0),
        })
    scored.sort(key=lambda x: x["risk_score"], reverse=True)
    return scored[:top_k]


@router.get("/hotspots/{repo_id}/{commit_sha}")
async def get_hotspots(
    repo_id: int,
    commit_sha: str,
    top_k: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Identify repository hotspots (high centrality + high failure)."""
    graph = _load_graph(db, repo_id, commit_sha)
    # Ensure analytics are computed
    GraphAnalytics.compute_all_metrics(graph)
    hotspots = GraphAnalytics.identify_hotspots(graph, top_k=top_k)
    return hotspots


@router.get("/critical-path/{repo_id}/{commit_sha}")
async def get_critical_path(
    repo_id: int,
    commit_sha: str,
    db: Session = Depends(get_db),
):
    """Find the workflow critical path (longest execution chain)."""
    graph = _load_graph(db, repo_id, commit_sha)
    path = GraphAnalytics.find_workflow_critical_path(graph)
    return {"critical_path": path, "length": len(path)}


@router.get("/events/{repo_id}")
async def get_graph_events(
    repo_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Return recent graph-related events stored in the
    continuous_learning_events table.
    """
    events = (
        db.query(db_models.ContinuousLearningEvent)
        .order_by(db_models.ContinuousLearningEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "details": e.details,
            "created_at": str(e.created_at),
        }
        for e in events
    ]


@router.post("/explain")
async def explain_impact(
    req: ExplainRequest,
    db: Session = Depends(get_db),
):
    """
    Trace the structural dependency path from a changed file to a
    target workflow job and produce human-readable reasoning.
    """
    graph = _load_graph(db, req.repo_id, req.commit_sha)
    explainer = GraphExplainer(graph)
    trace = explainer.explain_impact(req.source_file, req.target_job)
    return trace.model_dump()


@router.post("/explain/bulk")
async def explain_bulk(
    req: BulkExplainRequest,
    db: Session = Depends(get_db),
):
    """
    For every changed file, explain the impact on all reachable jobs.
    """
    graph = _load_graph(db, req.repo_id, req.commit_sha)
    explainer = GraphExplainer(graph)
    traces = explainer.explain_all_impacts(req.changed_files)
    return [t.model_dump() for t in traces]


@router.get("/quality/{repo_id}/{commit_sha}")
async def get_quality_report(
    repo_id: int,
    commit_sha: str,
    db: Session = Depends(get_db),
):
    """Evaluate graph structural health and return a quality report."""
    graph = _load_graph(db, repo_id, commit_sha)
    evaluator = GraphQualityEvaluator(graph)
    report = evaluator.evaluate()
    return report.model_dump()


@router.get("/benchmark/{repo_id}/{commit_sha}")
async def get_benchmark_report(
    repo_id: int,
    commit_sha: str,
    db: Session = Depends(get_db),
):
    """Measure and return graph performance benchmarks."""
    graph = _load_graph(db, repo_id, commit_sha)
    benchmarker = GraphBenchmarker()

    # Benchmark traversal
    benchmarker.time_operation(
        "traversal",
        lambda: list(nx.bfs_edges(graph, list(graph.nodes())[0]))
        if graph.number_of_nodes() > 0
        else [],
    )

    benchmarker.measure_memory(graph)
    benchmarker.measure_snapshot_size(graph)

    report = benchmarker.generate_report(graph)
    return report.model_dump()


@router.get("/summary/{repo_id}/{commit_sha}")
async def get_graph_summary(
    repo_id: int,
    commit_sha: str,
    db: Session = Depends(get_db),
):
    """Quick structural summary of the graph."""
    graph = _load_graph(db, repo_id, commit_sha)
    return GraphAnalytics.graph_summary(graph)
