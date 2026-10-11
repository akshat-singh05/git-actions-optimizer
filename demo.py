"""
========================================================================
GitHub Actions Performance Optimizer — Live Local Demo (real data)
========================================================================

Team:
  - Akshat Singh  — 23BKT0005
  - Aditya Choubey — 23BCE2274

Usage:
    gha-optimizer ingest --repo owner/repo --since 40d       # once
    gha-optimizer build-dataset --repo owner/repo            # once (per-commit graph features)
    python demo.py --repo owner/repo [--workflow .github/workflows/ci.yml]

Walks through every pipeline stage with detailed output on a REAL repository:

    Repository Analysis → Dependency Graph → Graph Fusion → Impact Analysis →
    Feature Extraction → ML Predictions → DAG-Aware Scheduler →
    Optimized Workflow → Validation

Every result is produced by the ACTUAL implementation from REAL ingested GitHub
Actions history.  There is no synthetic history anywhere in this script: with too
little ingested history it says so and exits.  Improvement figures are ESTIMATED
from predicted durations, not measured executions.
========================================================================
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

# Suppress noisy logs during demo — we print our own structured output
logging.basicConfig(level=logging.WARNING)

import networkx as nx  # noqa: E402
import yaml  # noqa: E402

SEP = "═" * 72
SUB_SEP = "─" * 72


def banner(title: str, step: int | None = None):
    print(f"\n{SEP}")
    print(f"{'  STEP ' + str(step) + ': ' if step is not None else '  '}{title}")
    print(SEP)


def section(title: str):
    print(f"\n{SUB_SEP}\n  {title}\n{SUB_SEP}")


def kv(key: str, value, indent: int = 2):
    print(f"{' ' * indent}{key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage-by-stage walkthrough on a real ingested repository")
    parser.add_argument("--repo", required=True, help="owner/repo (must have been ingested)")
    parser.add_argument("--workflow", help="repo-relative workflow path (default: the most-ingested workflow)")
    parser.add_argument("--db", help="database URL (default: DATABASE_URL or sqlite:///gha_optimizer.db)")
    args = parser.parse_args()
    if args.db:
        os.environ["DATABASE_URL"] = args.db

    from gha_optimizer.features.feature_extraction import (
        FEATURE_COLUMNS,
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
    from gha_optimizer.graph.builder import GraphBuilder
    from gha_optimizer.graph.dependency_graph import (
        build_dependency_graph,
        cache_keys_touched_by,
        graph_distance_to_job,
        graph_summary,
        impacted_jobs_for_diff,
    )
    from gha_optimizer.graph.fusion import GraphFusion
    from gha_optimizer.ingestion.history import InsufficientHistoryError, require_history
    from gha_optimizer.ingestion.workflow_parser import parse_workflow
    from gha_optimizer.ingestion.workspace import prepare_repo_snapshot
    from gha_optimizer.config import config
    from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
    from gha_optimizer.models.duration_estimator import DurationEstimator
    from gha_optimizer.models.failure_predictor import FailurePredictor
    from gha_optimizer.pipeline import job_only_dag
    from gha_optimizer.scheduler.optimizer import build_schedule
    from gha_optimizer.server.db.session import SessionLocal, init_db

    t0 = time.time()
    print(SEP)
    print("  GITHUB ACTIONS PERFORMANCE OPTIMIZER — LOCAL DEMO ON REAL DATA")
    print(SEP)

    init_db()
    db = SessionLocal()
    try:
        snap = prepare_repo_snapshot(db, args.repo, args.workflow)
    except (InsufficientHistoryError, LookupError) as exc:
        print(f"\n  Cannot run the demo: {exc}\n  Ingest real history first: gha-optimizer ingest --repo {args.repo}")
        return 2
    finally:
        db.close()

    repo_root, wf_path = snap.root, snap.workflow_path
    out_path = repo_root / ".github" / "workflows" / f"{wf_path.stem}.optimized.yml"
    changed, alt_changed = snap.changed_files, snap.alt_changed_files
    kv("Repository", f"{snap.repo} @ {snap.sha[:10]} (exported from the local git mirror)")
    kv("Workflow", snap.workflow_repo_path)
    kv("Ingested history", f"{len(snap.history)} (run, job) rows from {len({r.run_id for r in snap.history})} runs")
    kv("Current diff", f"{len(changed)} real changed files of ingested run {snap.changed_from_run}")

    # ── STEP 1 ─────────────────────────────────────────────────────
    banner("REPOSITORY & WORKFLOW ANALYSIS", step=1)
    workflow = parse_workflow(wf_path)
    dag_edges = workflow.job_dag_edges()
    kv("Source files analysed", f"{len(snap.source_files)} (of {len(snap.tracked_files)} tracked files)")
    kv("Workflow", f"{workflow.name}  ({len(workflow.jobs)} jobs, {len(dag_edges)} needs-edges)")
    kv("Triggers", list(workflow.triggers.keys()))
    for jid, js in workflow.jobs.items():
        print(f"    • {jid}: runs-on={js.runs_on} steps={len(js.steps)} needs={js.needs} cache_action={js.uses_cache_action}")

    # ── STEP 2-4 ───────────────────────────────────────────────────
    banner("DEPENDENCY GRAPH (source ⊕ CI, fused)", step=2)
    source_builder = GraphBuilder(repo_root)
    n_parsed = source_builder.build_from_source([repo_root / f for f in snap.source_files])
    source_graph = source_builder.get_graph()
    kv("Files parsed", f"{n_parsed}/{len(snap.source_files)}")
    kv("Source graph", f"{source_graph.number_of_nodes()} nodes, {source_graph.number_of_edges()} edges")
    kv("Node types", dict(Counter(d.get("node_type", "?") for _, d in source_graph.nodes(data=True))))
    ci_builder = GraphBuilder(repo_root)
    ci_builder.build_from_workflow(workflow)
    ci_graph = ci_builder.get_graph()
    kv("CI graph", f"{ci_graph.number_of_nodes()} nodes, {ci_graph.number_of_edges()} edges")
    fused = GraphFusion.fuse(source_graph, ci_graph)
    cross = [(u, v, d) for u, v, d in fused.edges(data=True)
             if isinstance(d.get("provenance"), dict) and d["provenance"].get("origin_parser") == "GraphFusion"]
    kv("Fused graph", f"{fused.number_of_nodes()} nodes, {fused.number_of_edges()} edges; summary {graph_summary(fused)}")
    kv("Cross-domain (fusion) edges inferred", len(cross))
    if not cross:
        print("    NOTE: fusion inferred no file→job / file→cache edges for this workflow (its commands/caches are not "
              "recognised by the fusion rules), so graph-derived features carry little information here.")

    # ── STEP 5 ─────────────────────────────────────────────────────
    banner("IMPACT ANALYSIS (real diffs)", step=5)
    full_graph = build_dependency_graph(workflow, repo_root, snap.tracked_files)
    simple_graph = nx.DiGraph(full_graph)
    for label, files in (("most recent ingested run", changed), ("previous, different run", alt_changed)):
        if not files:
            continue
        section(f"Impact of the real diff of the {label} ({len(files)} files)")
        impacted = impacted_jobs_for_diff(full_graph, files)
        kv("Changed files found in graph", sum(1 for f in files if f"File::{f}" in full_graph))
        kv("Impacted jobs", sorted(impacted) if impacted else "(none)")
        kv("Cache keys touched", sorted(cache_keys_touched_by(full_graph, files)) or "(none)")
        for job_id in workflow.jobs:
            dists = [graph_distance_to_job(full_graph, f, job_id) for f in files]
            reach = [d for d in dists if d >= 0]
            print(f"      → {job_id}: {min(reach) if reach else '∞ (no path)'}")

    # ── STEP 6 ─────────────────────────────────────────────────────
    banner("FEATURE EXTRACTION (strictly-prior history)", step=6)
    try:
        require_history(snap.history, workflow.jobs.keys(), config.model.min_history_runs, config.model.min_cache_labels)
    except InsufficientHistoryError as exc:
        print(f"  Declining to predict: {exc}")
        return 2
    train_df = build_training_frame(simple_graph, snap.history)
    kv("Training frame", f"{train_df.shape[0]} rows × {train_df.shape[1]} columns")
    kv("Rows with log-derived cache label", int(train_df["label_cache_hit"].notna().sum()))
    kv("Rows with failure label", f"{int(train_df['label_failed'].notna().sum())} "
       f"({int(train_df['label_failed'].fillna(0).sum())} failures)")
    kv("Missing values per feature", {c: int(train_df[c].isna().sum()) for c in FEATURE_COLUMNS if train_df[c].isna().any()})
    stats, _ = compute_history_stats(snap.history, workflow_path=snap.workflow_repo_path)
    infer_df = build_inference_frame(simple_graph, list(workflow.jobs.keys()), changed, stats)
    section("Inference frame for the current diff (NaN = no prior evidence)")
    print(infer_df.set_index("job_id")[FEATURE_COLUMNS].round(3).to_string())

    # ── STEP 7 ─────────────────────────────────────────────────────
    banner("ML MODEL TRAINING & PREDICTION (temporal holdout)", step=7)
    models = {"cache_miss": CacheMissPredictor(), "failure": FailurePredictor(), "duration": DurationEstimator()}
    trained = {}
    for name, model in models.items():
        try:
            metrics = model.fit(train_df)
        except ValueError as exc:
            print(f"  {name}: NOT TRAINED — {exc}")
            continue
        trained[name] = model
        print(f"  {name}: TRAINED")
        for k, v in metrics.items():
            print(f"      {k}: {v}")
    if len(trained) < 3:
        print("\n  Not all models could be trained from the real labels; declining to schedule.")
        return 2
    infer_df.index = infer_df["job_id"]
    cache_miss_prob = trained["cache_miss"].predict_proba(infer_df).to_dict()
    failure_prob = trained["failure"].predict_proba(infer_df).to_dict()
    duration_pred = trained["duration"].predict(infer_df).to_dict()
    print(f"\n    {'Job':<28} {'P(cache miss)*':<15} {'P(failure)':<14} {'Duration (s)':<14}")
    for job_id in workflow.jobs:
        print(f"    {job_id:<28} {cache_miss_prob[job_id]:<15.4f} {failure_prob[job_id]:<14.4f} {duration_pred[job_id]:<14.1f}")
    print("    * informational: the scheduler does not use the cache-miss probability")
    section("Top feature importances (cache-miss model, permutation on the temporal holdout)")
    for feat, imp in list(trained["cache_miss"].feature_importance.items())[:7]:
        print(f"      {feat:<30} {imp:+.4f}")

    # ── STEP 8-9 ───────────────────────────────────────────────────
    banner("DAG-AWARE SCHEDULING & SAFETY CHECKS", step=8)
    dag = job_only_dag(workflow)
    plan = build_schedule(dag, failure_prob, cache_miss_prob, duration_pred)
    print("    Original needs-edges:", [f"{u}→{v}" for u, v in dag_edges] or "(none)")
    for jp in plan.jobs:
        print(f"      {jp.order + 1}. {jp.job_id} (level={jp.dag_level}, runner={jp.runner}, "
              f"P(fail)={jp.predicted_failure_prob:.3f}, est={jp.estimated_duration_seconds:.1f}s)"
              + (" [SKIP RECOMMENDED]" if jp.skip_recommended else ""))
    level = {jp.job_id: jp.dag_level for jp in plan.jobs}
    needs_ok = all(level[dep] < level[jid] for jid, js in workflow.jobs.items() for dep in js.needs if dep in level)
    all_present = set(workflow.jobs) == set(level)
    print(f"    All jobs preserved: {'✓ PASS' if all_present else '✗ FAIL'}   "
          f"needs respected: {'✓ PASS' if needs_ok else '✗ FAIL'}   "
          f"acyclic: {'✓ PASS' if nx.is_directed_acyclic_graph(dag) else '✗ FAIL'}")

    # ── STEP 10 ────────────────────────────────────────────────────
    banner("OPTIMIZED WORKFLOW (proposal + reviewable diff; original untouched)", step=10)
    optimized_yaml = generate_optimized_workflow(workflow, plan)
    write_workflow(optimized_yaml, out_path, plan)
    kv("Proposed workflow written to", out_path)
    parsed_back = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    kv("YAML valid / job count preserved", f"{'jobs' in parsed_back} / {len(parsed_back.get('jobs', {})) == len(workflow.jobs)}")
    diff = generate_diff(wf_path, optimized_yaml, plan=plan)
    for line in (diff or "(no differences)").split("\n")[:40]:
        print(f"    {line}")
    print(explain_changes(plan))

    # ── STEP 11 ────────────────────────────────────────────────────
    banner("SUMMARY (all improvement figures ESTIMATED, not measured)", step=11)
    print(f"""
    Wall-clock: {plan.baseline_wall_clock_seconds:.0f}s → {plan.estimated_wall_clock_seconds:.0f}s ({plan.time_saved_pct:+.1f}%)
    Cost:       ${plan.baseline_cost_usd:.5f} → ${plan.estimated_cost_usd:.5f} ({plan.cost_saved_pct:+.1f}%)
    Energy:     {plan.baseline_energy_wh:.2f}Wh → {plan.estimated_energy_wh:.2f}Wh ({plan.energy_saved_pct:+.1f}%)
    CO₂:        {plan.baseline_co2_grams:.2f}g → {plan.estimated_co2_grams:.2f}g ({plan.co2_saved_pct:+.1f}%)
    (cost/energy/CO₂ use configuration constants, not measurements; see docs/DATASET_REPORT.md for data limits)
    Elapsed: {time.time() - t0:.1f}s
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
