"""
Command-line interface for gha-optimizer.

Real-data workflow::

    gha-optimizer ingest --repo owner/repo [--since 40d] [--workflow ci.yml] [--max-runs N]
    gha-optimizer ingest-status --repo owner/repo
    gha-optimizer build-dataset --repo owner/repo        # per-commit graph features + dataset
    gha-optimizer dataset-report --repo owner/repo       # writes docs/DATASET_REPORT.md
    gha-optimizer eval-models --repo owner/repo          # rolling-origin eval + ablation -> docs/MODEL_EVAL.md
    gha-optimizer train --repo owner/repo [--workflow ci.yml]
    gha-optimizer analyze <workflow.yml> --repo owner/repo [--changed a,b]
    gha-optimizer evaluate <workflow.yml> --repo owner/repo
    gha-optimizer serve [--host H] [--port P]

``analyze`` / ``evaluate`` / ``train`` use **ingested history only**.  With too little
history they say so and exit with status 2 — there is no synthetic fallback.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gha-optimizer")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_since(value: str, now: datetime | None = None) -> datetime:
    """``40d`` (days back from now) or an ISO date/datetime (UTC) -> naive-UTC datetime."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    value = value.strip()
    if value.endswith("d") and value[:-1].isdigit():
        return now - timedelta(days=int(value[:-1]))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _use_db(args: argparse.Namespace) -> None:
    """Point the ORM session at ``--db`` (must run before the session module is imported)."""
    import os

    if getattr(args, "db", None):
        os.environ["DATABASE_URL"] = args.db


def _load_real_history(repo: str, workflow_repo_path: str):
    """Ingested history for one workflow; exits with status 2 (cold start) instead of inventing data."""
    from gha_optimizer.ingestion.history import load_history
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        return load_history(db, repo, workflow_repo_path)
    finally:
        db.close()


def _decline(exc: Exception) -> None:
    print(f"\nDeclining to predict: {exc}")
    print("No synthetic or default values are substituted. Ingest real history first, e.g.:\n"
          "  gha-optimizer ingest --repo <owner/repo> --since 40d\n"
          "  gha-optimizer build-dataset --repo <owner/repo>")
    sys.exit(2)


# ---------------------------------------------------------------------------
# analyze / evaluate
# ---------------------------------------------------------------------------

def _analysis_inputs(args: argparse.Namespace):
    from gha_optimizer.ingestion.workspace import (
        default_repo_root,
        tracked_files,
        workflow_repo_path,
        working_tree_changes,
    )

    workflow_path = Path(args.workflow)
    repo_root = Path(args.repo_root) if args.repo_root else default_repo_root(workflow_path)
    wf_repo_path = workflow_repo_path(workflow_path, repo_root, getattr(args, "workflow_path", None))
    tracked = args.files.split(",") if args.files else tracked_files(repo_root)
    changed = args.changed.split(",") if args.changed else working_tree_changes(repo_root)
    if not changed:
        print("No changed files: pass --changed a,b (or have uncommitted changes in --repo-root).")
        sys.exit(1)
    return workflow_path, repo_root, wf_repo_path, tracked, changed


def cmd_analyze(args: argparse.Namespace) -> None:
    """Run the optimization pipeline on a local workflow file using ingested history."""
    _use_db(args)
    from gha_optimizer.ingestion.history import InsufficientHistoryError
    from gha_optimizer.pipeline import run_pipeline

    workflow_path, repo_root, wf_repo_path, tracked, changed = _analysis_inputs(args)
    out_path = repo_root / ".github" / "workflows" / f"{workflow_path.stem}.optimized.yml"

    logger.info("Analyzing %s (history: %s :: %s), model backend: %s", workflow_path, args.repo, wf_repo_path, args.model)
    history = _load_real_history(args.repo, wf_repo_path)
    try:
        plan, explanation = run_pipeline(
            workflow_path=workflow_path,
            repo_root=repo_root,
            tracked_files=tracked,
            history=history,
            changed_files=changed,
            out_path=out_path,
            model_backend=args.model,
            workflow_repo_path=wf_repo_path,
            allow_runner_upsizing=args.larger_runners or None,
            speedup_exponent=args.speedup_exponent,
        )
    except InsufficientHistoryError as exc:
        _decline(exc)
        return

    print(explanation)
    print(f"\nProposed workflow written to: {out_path}  (your original file is untouched)")
    print(f"ESTIMATED time saved:   {plan.time_saved_pct:.1f}%   (from predicted durations, not measured)")
    print(f"ESTIMATED cost saved:   {plan.cost_saved_pct:.1f}%")
    print(f"ESTIMATED energy saved: {plan.energy_saved_pct:.1f}%")
    print(f"  of which skip recommendations alone: {plan.time_saved_by_skipping_pct:.1f}% time "
          f"(runner upsizing {'ON' if plan.runner_upsizing else 'off'}; larger-runner what-if under an ASSUMED "
          f"speed-up exponent {plan.speedup_exponent}: {plan.time_saved_with_upsizing_whatif_pct:.1f}% time, "
          f"{plan.cost_saved_with_upsizing_whatif_pct:+.1f}% cost)")


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Ablation over scheduler α/β/γ weights using ingested history (ESTIMATED figures)."""
    _use_db(args)
    import tempfile

    from gha_optimizer.ingestion.history import InsufficientHistoryError
    from gha_optimizer.pipeline import run_pipeline

    workflow_path, repo_root, wf_repo_path, tracked, changed = _analysis_inputs(args)
    history = _load_real_history(args.repo, wf_repo_path)

    configs = [
        ("α=0.7, β=0.3, γ=0.0 (no energy)", 0.7, 0.3, 0.0),
        ("α=0.7, β=0.25, γ=0.05 (balanced)", 0.7, 0.25, 0.05),
        ("α=0.9, β=0.1, γ=0.0 (speed-first)", 0.9, 0.1, 0.0),
        ("α=0.3, β=0.7, γ=0.0 (cost-first)", 0.3, 0.7, 0.0),
    ]
    print("=" * 70)
    print("Ablation: scheduler weight configurations (all figures ESTIMATED, not measured)")
    print("=" * 70)
    for label, alpha, beta, gamma in configs:
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            out_path = f.name
        try:
            plan, _ = run_pipeline(
                workflow_path=workflow_path, repo_root=repo_root, tracked_files=tracked, history=history,
                changed_files=changed, out_path=out_path, alpha=alpha, beta=beta, gamma=gamma,
                workflow_repo_path=wf_repo_path,
            )
        except InsufficientHistoryError as exc:
            _decline(exc)
            return
        print(f"\n{label}:")
        print(f"  est. wall-clock: {plan.baseline_wall_clock_seconds:.0f}s → {plan.estimated_wall_clock_seconds:.0f}s ({plan.time_saved_pct:+.1f}%)")
        print(f"  est. cost: ${plan.baseline_cost_usd:.5f} → ${plan.estimated_cost_usd:.5f} ({plan.cost_saved_pct:+.1f}%)")
        print(f"  est. energy: {plan.baseline_energy_wh:.2f}Wh → {plan.estimated_energy_wh:.2f}Wh ({plan.energy_saved_pct:+.1f}%)")
        print(f"  jobs skipped: {sum(1 for j in plan.jobs if j.skip_recommended)}/{len(plan.jobs)}")


# ---------------------------------------------------------------------------
# ingest / dataset / train
# ---------------------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest real GitHub Actions history (runs, jobs, steps, diffs, log-derived cache labels)."""
    _use_db(args)
    from gha_optimizer.config import config
    from gha_optimizer.ingestion.github_client import GithubClient
    from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
    from gha_optimizer.server.db.session import SessionLocal, init_db

    token = args.token or config.github.token
    if not token:
        print("Error: a GitHub token is required (set GITHUB_TOKEN or pass --token); "
              "job logs cannot be downloaded unauthenticated.")
        sys.exit(1)

    since = _parse_since(args.since)
    until = _parse_since(args.until) if args.until else None
    init_db()
    summary = {}
    for repo in args.repo:
        client = GithubClient(token=token, repo=repo)
        options = IngestOptions(
            since=since,
            until=until,
            workflows=args.workflow or None,
            max_runs=args.max_runs,
            fetch_logs=not args.no_logs,
            log_budget=args.log_budget,
            log_policy=args.log_policy,
            log_workers=args.log_workers,
            use_mirror=not args.no_mirror,
            mirror_root=Path(args.mirror_root),
        )
        db = SessionLocal()
        try:
            logger.info("=== ingesting %s since %s ===", repo, since.isoformat())
            summary[repo] = RepoIngestor(repo, client, db, options).run()
        finally:
            db.close()
    print("\nIngestion summary:")
    for repo, counters in summary.items():
        print(f"  {repo}: {dict(counters)}")


def cmd_ingest_status(args: argparse.Namespace) -> None:
    """Print real row counts for an ingested repository (SQL, not estimates)."""
    _use_db(args)
    import json

    from gha_optimizer.ingestion.ingest import ingestion_status
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        print(json.dumps(ingestion_status(db, args.repo), indent=2, default=str))
    finally:
        db.close()


def cmd_build_dataset(args: argparse.Namespace) -> None:
    """Compute per-commit graph features for ingested runs, then build + save the dataset."""
    _use_db(args)
    from gha_optimizer.features.dataset import build_dataset
    from gha_optimizer.features.graph_features import compute_repo_graph_features
    from gha_optimizer.ingestion.mirror import RepoMirror
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        if not args.no_graph:
            mirror = RepoMirror(args.repo, Path(args.mirror_root))
            mirror.ensure(refresh=False)
            owner_repo = args.repo.replace("/", "__")
            cache_path = Path(args.cache_dir) / f"graph_parse_{owner_repo}.sqlite"
            logger.info("computing per-commit graph features for %s ...", args.repo)
            counters = compute_repo_graph_features(db, args.repo, mirror, cache_path, args.workflow_path)
            print(f"graph features: {counters}")
        df, info = build_dataset(db, args.repo, args.workflow_path)
        print(f"dataset: {info}")
        if not df.empty:
            out = Path(args.out) if args.out else Path(".gha_optimizer/datasets") / f"{args.repo.replace('/', '__')}.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=False)
            print(f"saved {len(df)} rows to {out}")
    finally:
        db.close()


def cmd_dataset_report(args: argparse.Namespace) -> None:
    """Write docs/DATASET_REPORT.md from the database (all figures computed)."""
    _use_db(args)
    from gha_optimizer.features.dataset_report import generate_report
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        text = generate_report(db, args.repo)
    finally:
        db.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_eval_models(args: argparse.Namespace) -> None:
    """Rolling-origin evaluation with feature-set ablation + bootstrap intervals -> docs/MODEL_EVAL.md."""
    _use_db(args)
    from gha_optimizer.features.dataset import build_dataset
    from gha_optimizer.models.rolling_eval import TARGETS, evaluate_frame, render_markdown
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    results: dict[str, dict] = {}
    try:
        for repo in args.repo:
            df, info = build_dataset(db, repo, args.workflow_path)
            logger.info("%s: %d rows, %d runs", repo, len(df), df["run_id"].nunique() if not df.empty else 0)
            if df.empty:
                continue
            results[repo] = evaluate_frame(
                df, args.target or list(TARGETS), n_folds=args.folds, n_boot=args.bootstrap,
                initial_fraction=args.initial_fraction, min_train_runs=args.min_train_runs,
            )
    finally:
        db.close()
    if not results:
        _decline(RuntimeError("no ingested data for the requested repositories"))
    text = render_markdown(results, args.folds, args.bootstrap)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_train(args: argparse.Namespace) -> None:
    """Train the three models on the ingested dataset (temporal-holdout metrics) and save them."""
    _use_db(args)
    from gha_optimizer.config import config
    from gha_optimizer.features.dataset import build_dataset
    from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
    from gha_optimizer.models.duration_estimator import DurationEstimator
    from gha_optimizer.models.failure_predictor import FailurePredictor
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        df, info = build_dataset(db, args.repo, args.workflow_path)
    finally:
        db.close()
    if df.empty or df["run_id"].nunique() < config.model.min_history_runs:
        _decline(RuntimeError(f"only {df['run_id'].nunique() if not df.empty else 0} ingested runs "
                              f"(< {config.model.min_history_runs})"))
    logger.info("dataset: %s", info)
    model_dir = Path(args.output)
    model_dir.mkdir(parents=True, exist_ok=True)

    for name, model, fname in (
        ("Cache-miss predictor (any_miss)", CacheMissPredictor(), "cache_miss.joblib"),
        ("Cache-miss predictor (exact_miss)", CacheMissPredictor(target="exact_miss"), "cache_exact_miss.joblib"),
        ("Failure predictor", FailurePredictor(), "failure.joblib"),
        ("Duration estimator", DurationEstimator(), "duration.joblib"),
    ):
        try:
            metrics = model.fit(df)
        except ValueError as exc:
            print(f"{name}: NOT TRAINED — {exc}")
            continue
        model.save(model_dir / fname)
        print(f"{name}: {metrics}")
    print(f"\nModels saved to: {model_dir}")


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI server."""
    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required. Install with: pip install uvicorn")
        sys.exit(1)

    logger.info("Starting gha-optimizer server on %s:%d", args.host, args.port)
    uvicorn.run(
        "gha_optimizer.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", help="database URL (default: DATABASE_URL or sqlite:///gha_optimizer.db)")


def _add_analysis_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("workflow", help="Path to the workflow YAML file (a checkout of the ingested repo)")
    p.add_argument("--repo", required=True, help="owner/repo whose ingested history to use")
    p.add_argument("--repo-root", help="Repository checkout root (default: inferred from the workflow path)")
    p.add_argument("--workflow-path", help="repo-relative workflow path (default: relative to --repo-root)")
    p.add_argument("--files", help="Comma-separated tracked source files (default: git ls-files)")
    p.add_argument("--changed", help="Comma-separated changed files (default: git diff --name-only HEAD)")
    _add_db(p)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="gha-optimizer",
        description="ML-based predictive scheduler for GitHub Actions CI/CD pipelines",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    p_analyze = subparsers.add_parser("analyze", help="Analyze and optimize a workflow from ingested history")
    _add_analysis_args(p_analyze)
    p_analyze.add_argument("--larger-runners", action="store_true",
                           help="let the plan relabel base-tier jobs to larger-runner labels (they must exist in your org)")
    p_analyze.add_argument("--speedup-exponent", type=float,
                           help="ASSUMED runner speed-up law exponent (default config.scheduler.speedup_exponent)")
    p_analyze.add_argument("--model", default="tabular", choices=["tabular", "gnn"])

    p_eval = subparsers.add_parser("evaluate", help="Scheduler-weight ablation from ingested history")
    _add_analysis_args(p_eval)

    p_serve = subparsers.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    p_train = subparsers.add_parser("train", help="Train models on the ingested dataset")
    p_train.add_argument("--repo", required=True, help="owner/repo")
    p_train.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_train.add_argument("--output", default=".gha_optimizer/models")
    _add_db(p_train)

    p_ing = subparsers.add_parser("ingest", help="Ingest real GitHub Actions history into the database")
    p_ing.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_ing.add_argument("--since", default="40d", help="'40d' (days back) or an ISO date (UTC); default 40d")
    p_ing.add_argument("--until", help="ISO date (UTC); default now")
    p_ing.add_argument("--workflow", action="append",
                       help="workflow path, file name or name (repeatable); default: auto-select multi-job workflows with needs")
    p_ing.add_argument("--max-runs", type=int, help="stop after this many fully processed runs (newest first)")
    p_ing.add_argument("--no-logs", action="store_true", help="skip job-log download (cache labels stay NULL)")
    p_ing.add_argument("--log-budget", type=int, help="max job-log downloads in this invocation")
    p_ing.add_argument("--log-policy", default="cache-capable", choices=["cache-capable", "all"])
    p_ing.add_argument("--log-workers", type=int, default=6, help="concurrent log downloads within a run")
    p_ing.add_argument("--no-mirror", action="store_true",
                       help="do not clone a local git mirror (workflow YAML then costs one API call per run)")
    p_ing.add_argument("--mirror-root", default=".gha_optimizer/mirrors")
    p_ing.add_argument("--token", help="GitHub token (default: GITHUB_TOKEN)")
    _add_db(p_ing)

    p_ist = subparsers.add_parser("ingest-status", help="Show real row counts for an ingested repo")
    p_ist.add_argument("--repo", required=True)
    _add_db(p_ist)

    p_bd = subparsers.add_parser("build-dataset", help="Per-commit graph features + dataset for an ingested repo")
    p_bd.add_argument("--repo", required=True)
    p_bd.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_bd.add_argument("--no-graph", action="store_true", help="skip graph-feature computation (structural features stay NaN)")
    p_bd.add_argument("--mirror-root", default=".gha_optimizer/mirrors")
    p_bd.add_argument("--cache-dir", default=".gha_optimizer/cache")
    p_bd.add_argument("--out", help="CSV output path")
    _add_db(p_bd)

    p_dr = subparsers.add_parser("dataset-report", help="Write docs/DATASET_REPORT.md from the database")
    p_dr.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_dr.add_argument("--out", default="docs/DATASET_REPORT.md")
    _add_db(p_dr)

    p_ev = subparsers.add_parser(
        "eval-models", help="Rolling-origin evaluation, feature-set ablation and bootstrap intervals -> docs/MODEL_EVAL.md")
    p_ev.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_ev.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_ev.add_argument("--target", action="append", choices=["failure", "cache_any_miss", "cache_exact_miss", "duration", "duration_ratio"],
                      help="target to evaluate (repeatable; default: all)")
    p_ev.add_argument("--folds", type=int, default=5)
    p_ev.add_argument("--initial-fraction", type=float, default=0.5, help="share of runs used only as the first training prefix")
    p_ev.add_argument("--min-train-runs", type=int, default=20)
    p_ev.add_argument("--bootstrap", type=int, default=1000)
    p_ev.add_argument("--out", default="docs/MODEL_EVAL.md")
    _add_db(p_ev)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "analyze": cmd_analyze,
        "evaluate": cmd_evaluate,
        "serve": cmd_serve,
        "train": cmd_train,
        "ingest": cmd_ingest,
        "ingest-status": cmd_ingest_status,
        "build-dataset": cmd_build_dataset,
        "dataset-report": cmd_dataset_report,
        "eval-models": cmd_eval_models,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
