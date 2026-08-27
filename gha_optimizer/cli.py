"""
Command-line interface for gha-optimizer.

Usage:
    gha-optimizer analyze <workflow> [--repo-root <path>] [--model <tabular|gnn>]
    gha-optimizer serve [--host <host>] [--port <port>]
    gha-optimizer train --repo <owner/repo> [--limit <n>]
    gha-optimizer evaluate [--workflow <path>]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gha-optimizer")


def cmd_analyze(args: argparse.Namespace) -> None:
    """Run the optimization pipeline on a local workflow file."""
    from examples.synthetic_history import generate_history
    from gha_optimizer.pipeline import run_pipeline

    workflow_path = Path(args.workflow)
    repo_root = Path(args.repo_root) if args.repo_root else workflow_path.parent.parent
    out_path = (
        repo_root / ".github" / "workflows" / f"{workflow_path.stem}.optimized.yml"
    )

    tracked_files = args.files.split(",") if args.files else [
        "src/app.py",
        "src/utils.py",
        "src/api/routes.py",
        "tests/unit/test_app.py",
        "tests/integration/test_api.py",
        "requirements.txt",
    ]

    changed_files = args.changed.split(",") if args.changed else [
        "src/api/routes.py",
        "requirements.txt",
    ]

    logger.info("Analyzing workflow: %s", workflow_path)
    logger.info("Model backend: %s", args.model)

    history = generate_history(n_runs=args.history_size)

    plan, explanation = run_pipeline(
        workflow_path=workflow_path,
        repo_root=repo_root,
        tracked_files=tracked_files,
        history=history,
        changed_files=changed_files,
        out_path=out_path,
        model_backend=args.model,
    )

    print(explanation)
    print(f"\n✅ Optimized workflow written to: {out_path}")
    print(f"⏱  Time saved: {plan.time_saved_pct:.1f}%")
    print(f"💰 Cost saved: {plan.cost_saved_pct:.1f}%")
    print(f"⚡ Energy saved: {plan.energy_saved_pct:.1f}%")


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


def cmd_train(args: argparse.Namespace) -> None:
    """Fetch history from a GitHub repo and train models."""
    from gha_optimizer.config import config
    from gha_optimizer.ingestion.github_client import GithubClient

    token = args.token or config.github.token
    if not token:
        print(
            "Error: GitHub token required. Set GITHUB_TOKEN env var "
            "or pass --token."
        )
        sys.exit(1)

    logger.info("Fetching history from %s (limit=%d)", args.repo, args.limit)
    client = GithubClient(token=token, repo=args.repo)
    records = client.fetch_recent_runs(limit=args.limit)

    if not records:
        print("No workflow run records found.")
        sys.exit(1)

    logger.info("Fetched %d run-job records. Training models...", len(records))

    # Build a minimal workflow + graph for training
    from examples.synthetic_history import generate_history
    from gha_optimizer.ingestion.workflow_parser import parse_workflow
    from gha_optimizer.pipeline import build_dependency_graph
    from gha_optimizer.features.feature_extraction import build_training_frame
    from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
    from gha_optimizer.models.failure_predictor import FailurePredictor
    from gha_optimizer.models.duration_estimator import DurationEstimator

    # Use sample workflow as template
    sample_wf = Path(__file__).resolve().parent.parent / "examples" / "sample_workflow.yml"
    sample_repo = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"

    if sample_wf.exists():
        workflow = parse_workflow(sample_wf)
        graph = build_dependency_graph(workflow, sample_repo)
    else:
        print("Warning: sample workflow not found, using minimal graph")
        import networkx as nx
        graph = nx.DiGraph()

    train_df = build_training_frame(graph, records)
    logger.info("Training frame: %d rows", len(train_df))

    model_dir = Path(args.output)
    model_dir.mkdir(parents=True, exist_ok=True)

    cache_model = CacheMissPredictor()
    cache_metrics = cache_model.fit(train_df)
    cache_model.save(model_dir / "cache_miss.joblib")
    print(f"Cache-miss predictor: {cache_metrics}")

    failure_model = FailurePredictor()
    failure_metrics = failure_model.fit(train_df)
    failure_model.save(model_dir / "failure.joblib")
    print(f"Failure predictor: {failure_metrics}")

    duration_model = DurationEstimator()
    duration_metrics = duration_model.fit(train_df)
    duration_model.save(model_dir / "duration.joblib")
    print(f"Duration estimator: {duration_metrics}")

    print(f"\n✅ Models saved to: {model_dir}")


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Run an ablation study comparing pipeline configurations."""
    from examples.synthetic_history import generate_history
    from gha_optimizer.pipeline import run_pipeline

    workflow = Path(args.workflow)
    repo_root = workflow.parent.parent if args.repo_root is None else Path(args.repo_root)

    tracked_files = [
        "src/app.py", "src/utils.py", "src/api/routes.py",
        "tests/unit/test_app.py", "tests/integration/test_api.py",
        "requirements.txt",
    ]
    changed_files = ["src/api/routes.py", "requirements.txt"]

    history = generate_history(n_runs=200)

    configs = [
        ("α=0.7, β=0.3, γ=0.0 (no energy)", 0.7, 0.3, 0.0),
        ("α=0.7, β=0.25, γ=0.05 (balanced)", 0.7, 0.25, 0.05),
        ("α=0.9, β=0.1, γ=0.0 (speed-first)", 0.9, 0.1, 0.0),
        ("α=0.3, β=0.7, γ=0.0 (cost-first)", 0.3, 0.7, 0.0),
    ]

    print("=" * 70)
    print("Ablation Study: Scheduler Weight Configurations")
    print("=" * 70)

    for label, alpha, beta, gamma in configs:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            out_path = f.name

        plan, _ = run_pipeline(
            workflow_path=workflow,
            repo_root=repo_root,
            tracked_files=tracked_files,
            history=history,
            changed_files=changed_files,
            out_path=out_path,
            alpha=alpha,
            beta=beta,
            gamma=gamma,
        )

        print(f"\n{label}:")
        print(f"  Wall-clock: {plan.baseline_wall_clock_seconds:.0f}s → "
              f"{plan.estimated_wall_clock_seconds:.0f}s "
              f"({plan.time_saved_pct:+.1f}%)")
        print(f"  Cost: ${plan.baseline_cost_usd:.5f} → "
              f"${plan.estimated_cost_usd:.5f} "
              f"({plan.cost_saved_pct:+.1f}%)")
        print(f"  Energy: {plan.baseline_energy_wh:.2f}Wh → "
              f"{plan.estimated_energy_wh:.2f}Wh "
              f"({plan.energy_saved_pct:+.1f}%)")
        print(f"  Jobs skipped: "
              f"{sum(1 for j in plan.jobs if j.skip_recommended)}/{len(plan.jobs)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gha-optimizer",
        description="ML-based predictive scheduler for GitHub Actions CI/CD pipelines",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- analyze ---
    p_analyze = subparsers.add_parser("analyze", help="Analyze and optimize a workflow")
    p_analyze.add_argument("workflow", help="Path to the workflow YAML file")
    p_analyze.add_argument("--repo-root", help="Repository root directory")
    p_analyze.add_argument("--model", default="tabular", choices=["tabular", "gnn"])
    p_analyze.add_argument("--files", help="Comma-separated tracked files")
    p_analyze.add_argument("--changed", help="Comma-separated changed files")
    p_analyze.add_argument("--history-size", type=int, default=150)

    # --- serve ---
    p_serve = subparsers.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    # --- train ---
    p_train = subparsers.add_parser("train", help="Fetch data and train models")
    p_train.add_argument("--repo", required=True, help="owner/repo")
    p_train.add_argument("--token", help="GitHub personal access token")
    p_train.add_argument("--limit", type=int, default=200)
    p_train.add_argument("--output", default=".gha_optimizer/models")

    # --- evaluate ---
    p_eval = subparsers.add_parser("evaluate", help="Run ablation study")
    p_eval.add_argument(
        "--workflow",
        default=str(Path(__file__).resolve().parent.parent / "examples" / "sample_workflow.yml"),
    )
    p_eval.add_argument("--repo-root", default=None)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "analyze": cmd_analyze,
        "serve": cmd_serve,
        "train": cmd_train,
        "evaluate": cmd_evaluate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
