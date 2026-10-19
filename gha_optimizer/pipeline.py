"""
End-to-end orchestration: wires together all six layers described in the
architecture.  This is the module a CLI or GitHub App webhook handler
would call.

Models are gradient-boosted trees on flat features (the dependency graph reaches them through the structural
features computed in ``features/graph_features.py``).

Uses the Heterogeneous Graph Engine (GraphBuilder + GraphFusion) as
the canonical graph implementation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from gha_optimizer.advisory import advise_unreached_jobs, render_advisory
from gha_optimizer.config import config
from gha_optimizer.gates import plan_relaxation, render_relaxation, validate_relaxation
from gha_optimizer.install_pruning import plan_install_pruning, render_pruning
from gha_optimizer.pip_cache import plan_pip_cache, render_pip_cache, validate_step_edits
from gha_optimizer.triage import render_profile
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
from gha_optimizer.llm_summary import LLMClient, summarize
from gha_optimizer.graph.ci_semantics import workflow_cannot_restore_cache
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor, NoCacheModel
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.store import ModelStore, fingerprint
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.scheduler.optimizer import SchedulePlan, build_schedule, speedup_sensitivity
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
    alpha: float | None = None,
    beta: float | None = None,
    gamma: float | None = None,
    workflow_repo_path: str | None = None,
    allow_runner_upsizing: bool | None = None,
    speedup_exponent: float | None = None,
    model_dir: str | Path | None = None,
    allow_skipping: bool | None = None,
    replay_stats: dict | None = None,
    llm_client: LLMClient | None = None,
    relax_gates: bool | None = None,
    prune_installs: bool | None = None,
    add_pip_cache: bool | None = None,
    step_seconds: dict | None = None,
    triage_profile: dict | None = None,
    as_of: datetime | None = None,
) -> tuple[SchedulePlan, str]:
    """
    Run the full optimization pipeline end to end.

    Parameters
    ----------
    workflow_path : Path to the GitHub Actions workflow YAML.
    repo_root : Path to the repository root.
    tracked_files : Every tracked file of the repo (File nodes of the graph; only source files are parsed).
    history : **Real ingested** workflow run records for this workflow (see
        :func:`gha_optimizer.ingestion.history.load_history`).  There is no synthetic
        fallback: with too little history this raises
        :class:`~gha_optimizer.ingestion.history.InsufficientHistoryError`.
    changed_files : Files changed in the current diff.
    out_path : Where to write the optimized workflow YAML.
    alpha, beta, gamma : Scheduler objective weights (time, cost, energy).
    allow_runner_upsizing : Let the plan move base-tier jobs to larger-runner labels (default off; those labels must
        exist in the organisation).  The larger-runner case is always reported as an ASSUMED what-if.
    speedup_exponent : ASSUMED runner speed-up law exponent (default ``config.scheduler.speedup_exponent``).
    allow_skipping : Apply skip recommendations - `if: ${{ false }}` guards on jobs downstream of a job predicted to
        fail (default off; candidates are still reported).
    replay_stats : Result of :func:`gha_optimizer.advisory.advisory_stats` (counterfactual replay of the graph skip
        policy on this repository's history).  Only used to attach measured rates to the advisory section of the
        explanation; ``None`` says so there.  It never affects the schedule.
    triage_profile : ``triage.job_failure_profile`` of this repository's real history (optional): appended as a short
        "what the past failures looked like" section; never affects the plan.
    relax_gates : Opt-in (default off): remove ``needs`` gates that ``gates.py`` proves safe and that shorten the estimated
        critical path.  Rule 7 (CLAUDE.md) allows this exception only; the written workflow is validated and every
        removed edge is listed in the file header and the explanation.
    prune_installs : Opt-in (default off): remove project-dependency installs from jobs that only run stand-alone tools
        (``install_pruning.py``); validated, listed in the file header and the explanation.  ``step_seconds`` (recorded median
        seconds per ``(job key, step name)``, ``history.load_step_seconds``) only feeds the ESTIMATED saving.
    add_pip_cache : Opt-in (default off): add ``cache: pip`` to ``setup-python`` steps of jobs that install requirement files without a cache (``pip_cache.py``); validated, listed in the file
        header and the explanation; its saving is NOT estimated (only a real before/after run settles it).
    llm_client : Optional explanation-layer client (``llm_summary.py``).  When given, a validated plain-language
        summary is appended to the explanation; it never affects the plan, the predictions or the written workflow.
    model_dir : Directory of persisted models (``models/store.py``).  If it holds models trained on exactly this
        history/feature/hyper-parameter fingerprint they are loaded instead of refit; otherwise the models are fit
        and saved there.  ``None`` (default): always fit, never touch disk.
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
    # a workflow in which no step can restore a cache has no cache labels by construction: the (report-only) cache model is skipped, not faked
    no_cache_possible = workflow_cannot_restore_cache(workflow.raw.get("jobs") or {})
    require_history(
        history,
        workflow.jobs.keys(),
        min_runs=config.model.min_history_runs,
        need_cache_labels=0 if no_cache_possible else config.model.min_cache_labels,
        need_failure_labels=True,
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
    store = ModelStore(model_dir) if (model_dir is not None and not no_cache_possible) else None
    fp = fingerprint(history) if store is not None else None
    loaded = store.try_load(fp, workflow_repo_path) if store is not None else None
    if loaded is not None:
        models, metrics = loaded
        cache_model, failure_model, duration_model = models["cache_miss"], models["failure"], models["duration"]
        cache_metrics, failure_metrics, duration_metrics = metrics["cache_miss"], metrics["failure"], metrics["duration"]
        model_source = f"loaded from {model_dir} (same history, features and hyper-parameters as when they were fit)"
    else:
        cache_model = NoCacheModel() if no_cache_possible else CacheMissPredictor()
        failure_model = FailurePredictor()
        duration_model = DurationEstimator()

        cache_metrics = cache_model.fit(train_df)
        failure_metrics = failure_model.fit(train_df)
        duration_metrics = duration_model.fit(train_df)
        model_source = "fit on the ingested history just now"
        if store is not None:
            store.save(fp, workflow_repo_path,
                       {"cache_miss": cache_model, "failure": failure_model, "duration": duration_model},
                       {"cache_miss": cache_metrics, "failure": failure_metrics, "duration": duration_metrics})
            model_source += f" and saved to {model_dir}"

    logger.info("[%s] Cache-miss predictor: %s", trace_id, cache_metrics)
    logger.info("[%s] Failure predictor: %s", trace_id, failure_metrics)
    logger.info("[%s] Duration estimator: %s", trace_id, duration_metrics)

    # Per-job statistics over the FULL history (== strictly prior to the run being scored)
    scored_at = as_of if as_of is not None else datetime.now(timezone.utc)   # explicit, so the score is reproducible
    history_stats, _ = compute_history_stats(history, workflow_path=workflow_repo_path, as_of=scored_at)

    infer_df = build_inference_frame(
        simple_graph, list(workflow.jobs.keys()), changed_files, history_stats, as_of=scored_at
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
            cache_miss_probability=None if cache_miss_prob[job_id] != cache_miss_prob[job_id] else cache_miss_prob[job_id],   # NaN -> not predicted
            failure_probability=failure_prob[job_id],
            predicted_duration_seconds=duration_pred[job_id],
            status=PredictionStatus.PREDICTED,
            model_version=trace_id,
        ))

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
    if speedup_exponent is not None:
        schedule_kwargs["speedup_exponent"] = speedup_exponent
    sensitivity_kwargs = dict(schedule_kwargs)
    sensitivity_kwargs.pop("speedup_exponent", None)
    if allow_runner_upsizing is not None:
        schedule_kwargs["allow_upsizing"] = allow_runner_upsizing
    if allow_skipping is not None:
        schedule_kwargs["allow_skipping"] = allow_skipping

    plan = build_schedule(dag, failure_prob, cache_miss_prob, duration_pred, **schedule_kwargs)
    sensitivity = speedup_sensitivity(dag, failure_prob, cache_miss_prob, duration_pred, **sensitivity_kwargs)
    durations_for_gates = {jp.job_id: jp.estimated_duration_seconds for jp in plan.jobs}
    if prune_installs:
        # opt-in: a tool-only job does not need the project's dependencies; the shorter jobs feed the gate relaxation below
        plan.install_pruning = plan_install_pruning(workflow, repo_root, durations_for_gates, step_seconds)
        for job_id, saved in plan.install_pruning.seconds_by_job().items():
            durations_for_gates[job_id] = max(durations_for_gates[job_id] - saved, 0.0)
    if add_pip_cache:
        plan.pip_cache = plan_pip_cache(workflow, repo_root, {a.job for a in plan.install_pruning.actions} if plan.install_pruning is not None else set())
    if relax_gates:
        # opt-in: drop only the `needs` gates gates.py can prove safe AND that shorten the estimated critical path
        # the extra-cost estimate uses each job's own recent failure rate (a plain statistic of its history), not the
        # model's probability: with few recorded failures a fitted model extrapolates wildly
        plan.gate_relaxation = plan_relaxation(
            workflow, durations_for_gates,
            infer_df["historical_failure_rate"].fillna(0.0).to_dict())

    # ──────────────────────────────────────────────────────────────
    # Layer 5 — Workflow Generation
    # ──────────────────────────────────────────────────────────────
    optimized_yaml = generate_optimized_workflow(workflow, plan)
    problems = validate_relaxation(workflow, optimized_yaml)       # every job kept, no needs edge added or unsafely removed
    if plan.install_pruning is not None or plan.pip_cache is not None:
        problems += validate_step_edits(workflow, optimized_yaml, repo_root, prune=plan.install_pruning is not None, cache=plan.pip_cache is not None)   # steps unchanged except the provably safe edits
    if problems:                                                   # DAG correctness is sacred: never write such a file
        raise RuntimeError("generated workflow failed validation: " + "; ".join(problems))
    write_workflow(optimized_yaml, out_path, plan)
    explanation = explain_changes(plan, sensitivity)

    # Generate diff if original file exists
    diff = generate_diff(workflow_path, optimized_yaml, plan=plan)

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
        f"### Models: {model_source}",
        f"- scored at {scored_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC (the hour / weekday features use this instant; pass `as_of` / `--as-of` to reproduce a score)",
        "### Model validation metrics (temporal holdout)",
        f"- Cache-miss predictor: {cache_metrics}",
        f"- Failure predictor: {failure_metrics}",
        "- The failure score is a model output, NOT a calibrated probability (docs/CALIBRATION.md): read it next to the job's own failure rate, never alone",
        f"- Duration estimator: {duration_metrics}",
    ]
    labelled_jobs = {r.job_id for r in history if r.cache_hit is not None}
    unsupported = [j for j in workflow.jobs if j not in labelled_jobs]
    if no_cache_possible:
        model_lines.append("- P(cache miss) is not defined for this workflow: no step of it can restore a cache, so there are no cache labels (reported as n/a; it never entered the schedule)")
    elif unsupported:
        model_lines.append(
            f"- P(cache miss) for {unsupported} is NOT backed by any log-derived label for that job "
            "(the model extrapolates its overall base rate); it is informational only and is not used by the scheduler"
        )

    # Feature importance
    if not no_cache_possible:
        model_lines.append("")
        model_lines.append("### Top feature importances (cache-miss)")
        for feat, imp in list(cache_model.feature_importance.items())[:5]:
            model_lines.append(f"  - {feat}: {imp:.4f}")

    full_explanation = explanation + "\n".join(model_lines)

    # Advisory only: which jobs the graph says this diff cannot reach. Never changes the plan or the proposed YAML.
    advice, unknown_jobs = advise_unreached_jobs(workflow, infer_df["job_structurally_impacted"].to_dict())
    full_explanation += "\n\n" + render_advisory(advice, unknown_jobs, replay_stats)
    if plan.pip_cache is not None:
        full_explanation += "\n\n" + render_pip_cache(plan.pip_cache)
    if plan.install_pruning is not None:
        full_explanation += "\n\n" + render_pruning(plan.install_pruning)
    if plan.gate_relaxation is not None:
        full_explanation += "\n\n" + render_relaxation(plan.gate_relaxation)
    if triage_profile is not None:
        full_explanation += "\n\n" + render_profile(triage_profile)

    # Explanation layer (opt-in): plain-language wording of the facts above. It runs after the plan, the YAML and the
    # advisory are final and cannot change them; failures and invalid answers are reported in text (llm_summary.py).
    if llm_client is not None:
        full_explanation += "\n\n" + summarize(llm_client, plan, advice, unknown_jobs, replay_stats)

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

