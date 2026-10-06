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

import networkx as nx

from gha_optimizer.config import config
from gha_optimizer.features.feature_extraction import (
    build_inference_frame,
    build_training_frame,
    compute_history_stats,
)
from gha_optimizer.generator.workflow_writer import (
    explain_changes,
    generate_diff,
    generate_optimized_workflow,
    write_workflow,
)
from gha_optimizer.graph.dependency_graph import build_dependency_graph
from gha_optimizer.ingestion.github_client import WorkflowRunRecord
from gha_optimizer.ingestion.history import require_history
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
    workflow_repo_path: str | None = None,
) -> tuple[SchedulePlan, str]:
    """
    Run the full optimization pipeline end to end.

    Parameters
    ----------
    workflow_path : Path to the GitHub Actions workflow YAML.
    repo_root : Path to the repository root.
    tracked_files : List of source files to include in the graph.
    history : **Real ingested** workflow run records for this workflow (see
        :func:`gha_optimizer.ingestion.history.load_history`).  There is no synthetic
        fallback: with too little history this raises
        :class:`~gha_optimizer.ingestion.history.InsufficientHistoryError`.
    changed_files : Files changed in the current diff.
    out_path : Where to write the optimized workflow YAML.
    model_backend : ``"tabular"`` (default) or ``"gnn"``.
    alpha, beta, gamma : Scheduler objective weights (time, cost, energy).
    workflow_repo_path : repo-relative path of the workflow (e.g. ``.github/workflows/ci.yml``),
        used to select that workflow's records when ``history`` mixes several.

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

    # Cold start: decline to predict rather than invent numbers.
    if workflow_repo_path:
        history = [r for r in history if r.workflow_path in (None, workflow_repo_path)]
    require_history(
        history,
        workflow.jobs.keys(),
        min_runs=config.model.min_history_runs,
        need_cache_labels=config.model.min_cache_labels,
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

    # Per-job statistics over the FULL history (== strictly prior to the run being scored)
    history_stats, _ = compute_history_stats(history, workflow_path=workflow_repo_path)

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
            cache_miss_probability=cache_miss_prob[job_id],
            failure_probability=failure_prob[job_id],
            predicted_duration_seconds=duration_pred[job_id],
            status=PredictionStatus.PREDICTED,
            model_version=trace_id,
        ))

    # Optionally override cache-miss with GNN predictions.  The GNN path degrades to
    # tabular on any import/training failure; that outcome is reported, never hidden.
    gnn_metrics = None
    gnn_note = None
    if model_backend == "gnn":
        gnn_metrics = _try_gnn_prediction(
            simple_graph, history, changed_files, cache_miss_prob
        )
        if gnn_metrics is None:
            gnn_note = (
                "GNN backend was requested but did NOT run (torch/torch-geometric unavailable or "
                "training failed) — all cache-miss probabilities above are TABULAR."
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

    # Data provenance + model metrics summary (all figures computed from the ingested history)
    n_runs = len({r.run_id for r in history})
    stamps = sorted(r.timestamp for r in history if r.timestamp)
    with_graph = sum(1 for r in history if r.graph_features is not None)
    model_lines = [
        "",
        "### Training data (real ingested history)",
        f"- {len(history)} (run, job) rows from {n_runs} runs, "
        f"{stamps[0] if stamps else 'n/a'} -> {stamps[-1] if stamps else 'n/a'}",
        f"- rows with per-commit graph features: {with_graph}/{len(history)}"
        + ("" if with_graph else "  (structural features are NaN: run `gha-optimizer build-dataset` first)"),
        "- improvement figures in this report are ESTIMATED from predicted durations; they are not measured executions",
        "",
        "### Model validation metrics (temporal holdout)",
        f"- Cache-miss predictor: {cache_metrics}",
        f"- Failure predictor: {failure_metrics}",
        f"- Duration estimator: {duration_metrics}",
    ]
    if gnn_metrics:
        model_lines.append(f"- GNN cache predictor: {gnn_metrics}")
    if gnn_note:
        model_lines.append(f"- {gnn_note}")
    labelled_jobs = {r.job_id for r in history if r.cache_hit is not None}
    unsupported = [j for j in workflow.jobs if j not in labelled_jobs]
    if unsupported:
        model_lines.append(
            f"- P(cache miss) for {unsupported} is NOT backed by any log-derived label for that job "
            "(the model extrapolates its overall base rate); it is informational only and is not used by the scheduler"
        )

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
    logger.debug("[%s] decision trace: %s", trace_id, trace.model_dump_json())
    logger.info(
        "[%s] Pipeline complete. Estimated time saved: %.1f%%, estimated cost saved: %.1f%%",
        trace_id, plan.time_saved_pct, plan.cost_saved_pct,
    )

    return plan, full_explanation


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
                rec.job_id: int(not rec.cache_hit) for rec in run_records if rec.cache_hit is not None
            }
            if not labels:  # no log-derived labels in this run: it cannot supervise the GNN
                graphs.pop()
                changed_list.pop()
                continue
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
