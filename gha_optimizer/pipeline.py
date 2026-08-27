"""
End-to-end orchestration: wires together all six layers described in the
architecture.  This is the module a CLI or GitHub App webhook handler
would call.

Supports two model backends:
  - ``"tabular"`` (default): GBT/XGBoost models on flat features
  - ``"gnn"``: Graph Neural Network over the dependency graph

Uses the Heterogeneous Graph Engine (GraphBuilder + GraphFusion) as
the canonical graph implementation.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

import networkx as nx

from gha_optimizer.features.feature_extraction import (
    build_inference_frame,
    build_training_frame,
)
from gha_optimizer.generator.workflow_writer import (
    explain_changes,
    generate_diff,
    generate_optimized_workflow,
    write_workflow,
)
from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.fusion import GraphFusion
from gha_optimizer.ingestion.github_client import WorkflowRunRecord
from gha_optimizer.ingestion.workflow_parser import WorkflowSpec, parse_workflow
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.scheduler.optimizer import SchedulePlan, build_schedule
from gha_optimizer.schemas import (
    DecisionTraceSchema,
    PredictionResult,
    PredictionStatus,
)

logger = logging.getLogger(__name__)


def build_dependency_graph(
    workflow: WorkflowSpec,
    repo_root: Path,
    tracked_files: list[str] | None = None,
) -> nx.MultiDiGraph:
    """
    Build the unified heterogeneous dependency graph by:
    1. Parsing source files with language-specific AST parsers.
    2. Parsing the workflow YAML into CI/CD nodes.
    3. Fusing the two domains with cross-domain edges.

    This is the SINGLE canonical graph construction function.
    """
    builder = GraphBuilder(repo_root)

    # Build source code subgraph
    if tracked_files:
        source_paths = [repo_root / f for f in tracked_files if (repo_root / f).exists()]
        builder.build_from_source(source_paths)

    source_graph = builder.get_graph()

    # Build CI/CD subgraph
    ci_builder = GraphBuilder(repo_root)
    ci_builder.build_from_workflow(workflow)
    ci_graph = ci_builder.get_graph()

    # Fuse the two domains
    fused = GraphFusion.fuse(source_graph, ci_graph)
    return fused


def job_only_dag(workflow: WorkflowSpec) -> nx.DiGraph:
    """Extract the pure job-dependency DAG from a workflow spec."""
    dag = nx.DiGraph()
    for job_id in workflow.jobs:
        dag.add_node(job_id)
    for upstream, downstream in workflow.job_dag_edges():
        dag.add_edge(upstream, downstream)
    return dag


def run_pipeline(
    workflow_path: str | Path,
    repo_root: str | Path,
    tracked_files: list[str],
    history: list[WorkflowRunRecord],
    changed_files: list[str],
    out_path: str | Path,
    model_backend: str = "tabular",
    alpha: float | None = None,
    beta: float | None = None,
    gamma: float | None = None,
) -> tuple[SchedulePlan, str]:
    """
    Run the full optimization pipeline end to end.

    Parameters
    ----------
    workflow_path : Path to the GitHub Actions workflow YAML.
    repo_root : Path to the repository root.
    tracked_files : List of source files to include in the graph.
    history : Historical workflow run records for training.
    changed_files : Files changed in the current diff.
    out_path : Where to write the optimized workflow YAML.
    model_backend : ``"tabular"`` (default) or ``"gnn"``.
    alpha, beta, gamma : Scheduler objective weights (time, cost, energy).

    Returns
    -------
    (SchedulePlan, explanation_string)
    """
    trace_id = str(uuid.uuid4())[:8]
    repo_root = Path(repo_root)

    # ──────────────────────────────────────────────────────────────
    # Layer 1 — Data Ingestion
    # ──────────────────────────────────────────────────────────────
    workflow = parse_workflow(workflow_path)
    logger.info(
        "[%s] Parsed workflow '%s': %d jobs, %d DAG edges",
        trace_id, workflow.name,
        len(workflow.jobs),
        len(workflow.job_dag_edges()),
    )

    # ──────────────────────────────────────────────────────────────
    # Layer 2 — Graph Construction + Feature Extraction
    # ──────────────────────────────────────────────────────────────
    graph = build_dependency_graph(workflow, repo_root, tracked_files)
    logger.info(
        "[%s] Dependency graph: %d nodes, %d edges",
        trace_id, graph.number_of_nodes(), graph.number_of_edges(),
    )

    # For the training frame, we need a simple DiGraph (not MultiDiGraph)
    # The feature extraction module uses the graph for file->job mapping
    simple_graph = nx.DiGraph(graph)
    train_df = build_training_frame(simple_graph, history)
    logger.info(
        "[%s] Training frame: %d rows × %d features",
        trace_id, len(train_df), train_df.shape[1],
    )

    # ──────────────────────────────────────────────────────────────
    # Layer 3 — Train models & score current diff
    # ──────────────────────────────────────────────────────────────
    cache_model = CacheMissPredictor()
    failure_model = FailurePredictor()
    duration_model = DurationEstimator()

    cache_metrics = cache_model.fit(train_df)
    failure_metrics = failure_model.fit(train_df)
    duration_metrics = duration_model.fit(train_df)

    logger.info("[%s] Cache-miss predictor: %s", trace_id, cache_metrics)
    logger.info("[%s] Failure predictor: %s", trace_id, failure_metrics)
    logger.info("[%s] Duration estimator: %s", trace_id, duration_metrics)

    # Build per-job history stats for the inference frame
    history_stats = {}
    for job_id, grp in train_df.groupby("job_id"):
        fail_list = grp["label_failed"].tolist()
        cache_list = grp["label_cache_hit"].tolist()
        history_stats[job_id] = {
            "failure_rate": grp["historical_failure_rate"].iloc[-1]
            if len(grp) > 0
            else 0.1,
            "mean_duration": grp["historical_mean_duration"].iloc[-1]
            if len(grp) > 0
            else 60.0,
            "cache_miss_rate": 1 - (sum(cache_list) / len(cache_list))
            if cache_list
            else 0.15,
            "recent_failure_streak": _streak(fail_list, 1),
            "recent_success_streak": _streak(fail_list, 0),
            "cache_key_stability": (sum(cache_list) / len(cache_list))
            if cache_list
            else 0.85,
        }

    infer_df = build_inference_frame(
        simple_graph, list(workflow.jobs.keys()), changed_files, history_stats
    )
    infer_df.index = infer_df["job_id"]

    # Score with tabular models
    cache_miss_prob = cache_model.predict_proba(infer_df).to_dict()
    failure_prob = failure_model.predict_proba(infer_df).to_dict()
    duration_pred = duration_model.predict(infer_df).to_dict()

    # Build prediction results for the decision trace
    predictions = []
    for job_id in workflow.jobs:
        predictions.append(PredictionResult(
            job_id=job_id,
            cache_miss_probability=cache_miss_prob.get(job_id, 0.0),
            failure_probability=failure_prob.get(job_id, 0.0),
            predicted_duration_seconds=duration_pred.get(job_id, 60.0),
            status=PredictionStatus.PREDICTED,
            model_version=trace_id,
        ))

    # Optionally override cache-miss with GNN predictions
    gnn_metrics = None
    if model_backend == "gnn":
        gnn_metrics = _try_gnn_prediction(
            simple_graph, history, changed_files, cache_miss_prob
        )

    # ──────────────────────────────────────────────────────────────
    # Layer 4 — Optimization / Scheduling
    # ──────────────────────────────────────────────────────────────
    dag = job_only_dag(workflow)

    schedule_kwargs = {}
    if alpha is not None:
        schedule_kwargs["alpha"] = alpha
    if beta is not None:
        schedule_kwargs["beta"] = beta
    if gamma is not None:
        schedule_kwargs["gamma"] = gamma

    plan = build_schedule(dag, failure_prob, cache_miss_prob, duration_pred, **schedule_kwargs)

    # ──────────────────────────────────────────────────────────────
    # Layer 5 — Workflow Generation
    # ──────────────────────────────────────────────────────────────
    optimized_yaml = generate_optimized_workflow(workflow, plan)
    write_workflow(optimized_yaml, out_path)
    explanation = explain_changes(plan)

    # Generate diff if original file exists
    diff = generate_diff(workflow_path, optimized_yaml)

    # Model metrics summary
    model_lines = [
        "",
        "### Model validation metrics",
        f"- Cache-miss predictor: {cache_metrics}",
        f"- Failure predictor: {failure_metrics}",
        f"- Duration estimator: {duration_metrics}",
    ]
    if gnn_metrics:
        model_lines.append(f"- GNN cache predictor: {gnn_metrics}")

    # Feature importance
    model_lines.append("")
    model_lines.append("### Top feature importances (cache-miss)")
    for feat, imp in list(cache_model.feature_importance.items())[:5]:
        model_lines.append(f"  - {feat}: {imp:.4f}")

    full_explanation = explanation + "\n".join(model_lines)

    if diff:
        full_explanation += "\n\n### Workflow diff\n```diff\n" + diff + "\n```"

    # ──────────────────────────────────────────────────────────────
    # Layer 6 — Decision Trace
    # ──────────────────────────────────────────────────────────────
    trace = DecisionTraceSchema(
        trace_id=trace_id,
        repo=str(repo_root),
        commit_sha="local",
        predictions=predictions,
    )
    logger.info(
        "[%s] Pipeline complete. Time saved: %.1f%%, Cost saved: %.1f%%",
        trace_id, plan.time_saved_pct, plan.cost_saved_pct,
    )

    return plan, full_explanation


def _streak(values: list, target) -> int:
    """Count consecutive trailing occurrences of ``target``."""
    count = 0
    for v in reversed(values):
        if v == target:
            count += 1
        else:
            break
    return count


def _try_gnn_prediction(
    graph: nx.DiGraph,
    history: list[WorkflowRunRecord],
    changed_files: list[str],
    cache_miss_prob: dict[str, float],
) -> dict | None:
    """
    Attempt GNN-based cache-miss prediction.  If torch/torch_geometric
    are not installed, log a warning and fall back silently.
    """
    try:
        from gha_optimizer.models.gnn_cache_predictor import GNNCachePredictor
    except ImportError:
        logger.warning(
            "GNN backend requested but torch/torch-geometric not available. "
            "Falling back to tabular predictions."
        )
        return None

    try:
        # Build training data for the GNN
        graphs = []
        changed_list = []
        labels_list = []

        # Group history by run_id
        runs: dict[str, list[WorkflowRunRecord]] = {}
        for rec in history:
            runs.setdefault(rec.run_id, []).append(rec)

        for run_id, run_records in runs.items():
            graphs.append(graph)
            changed_list.append(run_records[0].files_changed)
            labels = {
                rec.job_id: int(not rec.cache_hit) for rec in run_records
            }
            labels_list.append(labels)

        gnn = GNNCachePredictor(epochs=30, hidden_channels=32, num_layers=2)
        metrics = gnn.fit(graphs, changed_list, labels_list)

        # Override cache_miss_prob with GNN predictions
        gnn_probs = gnn.predict_proba_graph(graph, changed_files)
        cache_miss_prob.update(gnn_probs)

        logger.info("GNN cache predictor: %s", metrics)
        return metrics

    except Exception as exc:
        logger.warning("GNN prediction failed: %s. Using tabular fallback.", exc)
        return None
