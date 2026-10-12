"""
Command-line interface for gha-optimizer.

Real-data workflow::

    gha-optimizer ingest --repo owner/repo [--since 40d] [--workflow ci.yml] [--max-runs N]
    gha-optimizer ingest-status --repo owner/repo
    gha-optimizer build-dataset --repo owner/repo        # per-commit graph features + dataset
    gha-optimizer dataset-report --repo owner/repo       # writes docs/DATASET_REPORT.md
    gha-optimizer eval-models --repo owner/repo          # rolling-origin eval + ablation -> docs/MODEL_EVAL.md
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


def _replay_stats(repo: str, workflow_repo_path: str):
    """Replay figures for the advisory section, or None (with a warning) if they cannot be computed."""
    from gha_optimizer.advisory import advisory_stats
    from gha_optimizer.server.db.session import SessionLocal

    db = SessionLocal()
    try:
        return advisory_stats(db, repo, workflow_repo_path)
    except Exception as exc:  # the advisory is optional; never block the analysis on it
        logger.warning("Advisory replay figures unavailable: %s", exc)
        return None
    finally:
        db.close()


def _llm_client(requested: bool):
    """The explanation-layer client if requested AND configured; otherwise None (the analysis is unchanged)."""
    if not requested:
        return None
    from gha_optimizer.llm_summary import client_from_env

    client = client_from_env()
    if client is None:
        print("--llm-summary requested but GEMINI_API_KEY and GHA_LLM_MODEL are not both set; continuing without it.")
    return client


def _triage_profile(repo: str, workflow_repo_path: str):
    """Per-job profile of the repository's real past failures for the explanation, or None if it cannot be computed."""
    from gha_optimizer.server.db.session import SessionLocal
    from gha_optimizer.triage import job_failure_profile, triage_repo

    db = SessionLocal()
    try:
        return job_failure_profile(triage_repo(db, repo, workflow_repo_path))
    except Exception as exc:  # optional context; never block the analysis on it
        logger.warning("Failure triage unavailable: %s", exc)
        return None
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

    logger.info("Analyzing %s (history: %s :: %s)", workflow_path, args.repo, wf_repo_path)
    history = _load_real_history(args.repo, wf_repo_path)
    try:
        plan, explanation = run_pipeline(
            workflow_path=workflow_path,
            repo_root=repo_root,
            tracked_files=tracked,
            history=history,
            changed_files=changed,
            out_path=out_path,
            workflow_repo_path=wf_repo_path,
            allow_runner_upsizing=args.larger_runners or None,
            speedup_exponent=args.speedup_exponent,
            allow_skipping=args.enable_skips or None,
            model_dir=None if args.no_model_store else args.model_dir,
            replay_stats=_replay_stats(args.repo, wf_repo_path),
            llm_client=_llm_client(args.llm_summary),
            relax_gates=args.relax_gates or None,
            triage_profile=_triage_profile(args.repo, wf_repo_path),
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

    unknown = [t for t in (args.target or []) if t not in TARGETS]
    if unknown:
        print(f"unknown target(s) {unknown}; choose from {sorted(TARGETS)}")
        sys.exit(2)
    init_db()
    db = SessionLocal()
    results: dict[str, dict] = {}
    try:
        for repo in args.repo:
            df, info = build_dataset(db, repo, args.workflow_path, completion_aware=args.protocol == "v2")
            logger.info("%s: %d rows, %d runs", repo, len(df), df["run_id"].nunique() if not df.empty else 0)
            if df.empty:
                continue
            results[repo] = evaluate_frame(
                df, args.target or list(TARGETS), n_folds=args.folds, n_boot=args.bootstrap,
                initial_fraction=args.initial_fraction, min_train_runs=args.min_train_runs, cutoff=args.after,
            )
    finally:
        db.close()
    if not results:
        _decline(RuntimeError("no ingested data for the requested repositories"))
    text = render_markdown(results, args.folds, args.bootstrap, cutoff=args.after)
    out = Path(args.out or ("docs/MODEL_EVAL_FRESH.md" if args.after else "docs/MODEL_EVAL.md"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def _github_client(repo: str):
    from gha_optimizer.config import config
    from gha_optimizer.ingestion.github_client import GithubClient

    if not config.github.token:
        print("GITHUB_TOKEN is required (set it or put it in .env).")
        sys.exit(2)
    return GithubClient(token=config.github.token, repo=repo)


def cmd_live_score(args: argparse.Namespace) -> None:
    """Score newly queued runs of one workflow into a hash-chained ledger BEFORE their jobs complete (prospective test)."""
    _use_db(args)
    from gha_optimizer.config import config
    from gha_optimizer.ingestion.mirror import DEFAULT_MIRROR_ROOT, RepoMirror
    from gha_optimizer.live import LiveScorer, run_live

    client = _github_client(args.repo)
    wf = next((w for w in client.list_workflows() if w.get("path") == args.workflow_path), None)
    if wf is None:
        print(f"workflow {args.workflow_path} not found in {args.repo}")
        sys.exit(2)
    history = _load_real_history(args.repo, args.workflow_path)
    mirror = RepoMirror(args.repo, DEFAULT_MIRROR_ROOT, token=config.github.token)
    mirror.ensure(refresh=True)
    ledger = Path(args.ledger or f".gha_optimizer/live/{args.repo.replace('/', '__')}.jsonl")
    scorer = LiveScorer(args.repo, args.workflow_path, history, client, mirror, ledger)
    print(f"models fitted on {len(history)} job rows up to {scorer.history_until}; model hash {scorer.model_hash}; "
          f"ledger {ledger}; polling every {args.poll_seconds:.0f}s for {args.minutes:.0f} min")
    print(run_live(scorer, wf["id"], args.minutes, args.poll_seconds))


def cmd_live_evaluate(args: argparse.Namespace) -> None:
    """Read the real outcomes of the ledger's runs and score the predictions -> docs/LIVE_TEST.md."""
    _use_db(args)
    from datetime import datetime, timezone

    from gha_optimizer.ingestion.history import load_history
    from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
    from gha_optimizer.live import evaluate_ledger, read_ledger, render_live
    from gha_optimizer.server.db.session import SessionLocal, init_db

    ledger = Path(args.ledger)
    rows = read_ledger(ledger)                      # raises if the ledger was edited after it was written
    scored = [r for r in rows if r.get("kind") == "scored"]
    client = _github_client(args.repo)
    init_db()
    db = SessionLocal()
    try:
        opts = IngestOptions(since=datetime.now(timezone.utc).replace(tzinfo=None), workflows=[args.workflow_path],
                             fetch_logs=False, use_mirror=True)
        pending = 0
        for r in scored:
            if client.get_run(r["run_id"]).get("status") != "completed":
                pending += 1
                continue
            RepoIngestor(args.repo, client, db, opts).ingest_single_run(r["run_id"])
        history = load_history(db, args.repo, args.workflow_path)
    finally:
        db.close()
    outcomes = {(r.run_id, r.job_id): (1 if r.conclusion in ("failure", "timed_out") else 0 if r.conclusion == "success" else None)
                for r in history}
    result = evaluate_ledger(rows, outcomes)
    result["runs_not_completed_yet"] = pending
    text = render_live(result, str(ledger), scored[0]["scored_at"] if scored else None, scored[-1]["scored_at"] if scored else None)
    if pending:
        text += f"\n{pending} scored run(s) had not completed when this was evaluated and are not included.\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {out}")


def cmd_eval_pooled(args: argparse.Namespace) -> None:
    """EXPLORATORY leave-one-repository-out evaluation of the v2 failure model -> docs/POOLED_EVAL.md."""
    _use_db(args)
    from gha_optimizer.features.dataset import build_dataset
    from gha_optimizer.models.pooled_eval import evaluate_pooled_repo, render_pooled
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    frames = {}
    try:
        for repo in args.repo:
            df, _ = build_dataset(db, repo, None)
            logger.info("%s: %d rows", repo, len(df))
            if not df.empty:
                frames[repo] = df
    finally:
        db.close()
    if len(frames) < 2:
        _decline(RuntimeError("need at least two repositories with ingested data"))
    results = [evaluate_pooled_repo(frames, repo, n_folds=args.folds, n_boot=args.bootstrap, cutoff=args.after) for repo in frames]
    text = render_pooled(results, args.bootstrap, args.after)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_replay_gates(args: argparse.Namespace) -> None:
    """Replay relaxing safe `needs` gates on real history -> docs/GATE_REPLAY.md."""
    _use_db(args)
    from gha_optimizer.gate_replay import gate_replay_repo, render_gate_replay
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        results = [gate_replay_repo(db, repo, args.workflow_path) for repo in args.repo]
    finally:
        db.close()
    if not any("scored_runs" in r for r in results):
        _decline(RuntimeError("no repository with enough ingested runs"))
    text = render_gate_replay(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_triage(args: argparse.Namespace) -> None:
    """Classify the real failures in the ingested history and recommend actions -> docs/FAILURE_TRIAGE.md."""
    _use_db(args)
    from gha_optimizer.server.db.session import SessionLocal, init_db
    from gha_optimizer.triage import render_triage, triage_repo

    init_db()
    db = SessionLocal()
    try:
        results = [triage_repo(db, repo, args.workflow_path) for repo in args.repo]
    finally:
        db.close()
    if not any("classes" in r for r in results):
        _decline(RuntimeError("no ingested runs with jobs for the requested repositories"))
    text = render_triage(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_replay_impact(args: argparse.Namespace) -> None:
    """Counterfactual replay of "skip jobs the graph says the diff cannot reach" -> docs/IMPACT_REPLAY.md."""
    _use_db(args)
    from gha_optimizer.replay import deferral_repo, render_deferral, render_replay, replay_repo
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        replay = deferral_repo if args.deferral else replay_repo
        results = [replay(db, repo, args.workflow_path, args.bootstrap) for repo in args.repo]
    finally:
        db.close()
    if not any("dag_safe" in r for r in results):
        _decline(RuntimeError("no ingested runs with current graph features for the requested repositories"))
    text = (render_deferral if args.deferral else render_replay)(results, args.bootstrap)
    out = Path(args.out or ("docs/DEFERRAL_REPLAY.md" if args.deferral else "docs/IMPACT_REPLAY.md"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


def cmd_llm_check(args: argparse.Namespace) -> None:
    """Connectivity check for the optional LLM layer: one tiny request, no repository data is sent."""
    import time

    from gha_optimizer.llm_summary import LLMError, client_from_env

    client = client_from_env()
    if client is None:
        print("Not configured: set GEMINI_API_KEY and GHA_LLM_MODEL (a .env file in the repo root works; see .env.example).")
        sys.exit(2)
    t0 = time.time()
    try:
        reply = client.complete("Reply with exactly the single word OK.", "ping")
    except LLMError as exc:
        print(f"FAILED: {exc}")
        print("  HTTP 400/404 usually means a wrong model id; 401/403 a wrong or disabled key; 429 a rate or quota limit.")
        sys.exit(1)
    print(f"OK: model '{client.model}' answered in {time.time() - t0:.1f}s: {reply.strip()[:80]!r}")
    print("The request format works. Next: python demo.py --repo python-poetry/poetry --llm-summary")


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
    from gha_optimizer.config import config

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
    p_analyze.add_argument("--model-dir", default=str(config.model.model_dir),
                           help="persisted models are loaded from / saved to here when their data fingerprint matches")
    p_analyze.add_argument("--no-model-store", action="store_true", help="always refit; never read or write saved models")
    p_analyze.add_argument("--enable-skips", action="store_true",
                           help="apply skip recommendations (`if: ${{ false }}` guards); off by default - see docs/SCHEDULER_ASSUMPTIONS.md")
    p_analyze.add_argument("--relax-gates", action="store_true",
                           help="opt-in: drop `needs` gates proven safe (no data dependency, no secrets/deploy) that shorten the "
                                "estimated critical path; the job then also runs when its former upstream fails (see docs/IMPROVEMENT_PLAN.md C)")
    p_analyze.add_argument("--llm-summary", action="store_true",
                           help="append a plain-language summary written by an LLM (needs GEMINI_API_KEY and GHA_LLM_MODEL). "
                                "Sends only job ids, runner labels and computed numbers to the provider; never affects the plan")
    p_analyze.add_argument("--larger-runners", action="store_true",
                           help="let the plan relabel base-tier jobs to larger-runner labels (they must exist in your org)")
    p_analyze.add_argument("--speedup-exponent", type=float,
                           help="ASSUMED runner speed-up law exponent (default config.scheduler.speedup_exponent)")

    p_eval = subparsers.add_parser("evaluate", help="Scheduler-weight ablation from ingested history")
    _add_analysis_args(p_eval)

    subparsers.add_parser("llm-check", help="Check the optional LLM layer (GEMINI_API_KEY, GHA_LLM_MODEL) with one tiny request")

    p_serve = subparsers.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

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
    p_ev.add_argument("--target", action="append",
                      help="target to evaluate (repeatable; default: all): failure, failure_tree, failure_regularized_tree, "
                           "failure_v2_logistic, failure_v2_hgb, failure_v2_blend, cache_any_miss, cache_exact_miss, "
                           "duration, duration_ratio")
    p_ev.add_argument("--protocol", choices=["v1", "v2"], default="v2",
                      help="v2 (default): a run's outcome is visible to later runs only once it has finished; "
                           "v1: every earlier-started run is visible (the original behaviour)")
    p_ev.add_argument("--folds", type=int, default=5)
    p_ev.add_argument("--initial-fraction", type=float, default=0.5, help="share of runs used only as the first training prefix")
    p_ev.add_argument("--min-train-runs", type=int, default=20)
    p_ev.add_argument("--bootstrap", type=int, default=1000)
    p_ev.add_argument("--after", metavar="ISO_TIMESTAMP",
                      help="fresh-data check: train on runs at or before this timestamp (e.g. 2026-10-06T23:31:13Z), "
                           "test once on every later run; writes docs/MODEL_EVAL_FRESH.md unless --out is given")
    p_ev.add_argument("--out", default=None, help="default: docs/MODEL_EVAL.md (docs/MODEL_EVAL_FRESH.md with --after)")
    _add_db(p_ev)

    p_ls = subparsers.add_parser("live-score", help="Prospective test: score newly queued runs into a hash-chained ledger before they finish")
    p_ls.add_argument("--repo", required=True, help="owner/repo")
    p_ls.add_argument("--workflow-path", required=True, help="repo-relative workflow path, e.g. .github/workflows/ci.yml")
    p_ls.add_argument("--ledger", help="default: .gha_optimizer/live/<owner>__<repo>.jsonl")
    p_ls.add_argument("--minutes", type=float, default=120.0, help="how long to watch for new runs")
    p_ls.add_argument("--poll-seconds", type=float, default=120.0)
    _add_db(p_ls)

    p_le = subparsers.add_parser("live-evaluate", help="Score a live ledger against the real outcomes -> docs/LIVE_TEST.md")
    p_le.add_argument("--repo", required=True, help="owner/repo")
    p_le.add_argument("--workflow-path", required=True)
    p_le.add_argument("--ledger", required=True)
    p_le.add_argument("--out", default="docs/LIVE_TEST.md")
    _add_db(p_le)

    p_ep = subparsers.add_parser("eval-pooled", help="EXPLORATORY: leave-one-repository-out failure model -> docs/POOLED_EVAL.md")
    p_ep.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable; all are pooled)")
    p_ep.add_argument("--folds", type=int, default=5)
    p_ep.add_argument("--bootstrap", type=int, default=1000)
    p_ep.add_argument("--after", metavar="ISO_TIMESTAMP", help="fresh-data check: one split at this timestamp")
    p_ep.add_argument("--out", default="docs/POOLED_EVAL.md")
    _add_db(p_ep)

    p_rg = subparsers.add_parser("replay-gates", help="Replay relaxing safe `needs` gates on real history -> docs/GATE_REPLAY.md")
    p_rg.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_rg.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_rg.add_argument("--out", default="docs/GATE_REPLAY.md")
    _add_db(p_rg)

    p_tr = subparsers.add_parser("triage", help="Classify real failures (flaky / pre-existing / persistent ...) -> docs/FAILURE_TRIAGE.md")
    p_tr.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_tr.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_tr.add_argument("--out", default="docs/FAILURE_TRIAGE.md")
    _add_db(p_tr)

    p_ri = subparsers.add_parser(
        "replay-impact", help="Replay 'skip jobs the graph says the diff cannot reach' on real history -> docs/IMPACT_REPLAY.md")
    p_ri.add_argument("--repo", action="append", required=True, help="owner/repo (repeatable)")
    p_ri.add_argument("--workflow-path", help="repo-relative workflow path (default: all ingested workflows)")
    p_ri.add_argument("--bootstrap", type=int, default=1000)
    p_ri.add_argument("--deferral", action="store_true",
                      help="evaluate deferring the unreached jobs on pull_request runs instead (docs/DEFERRAL_PLAN.md)")
    p_ri.add_argument("--out", help="default: docs/IMPACT_REPLAY.md, or docs/DEFERRAL_REPLAY.md with --deferral")
    _add_db(p_ri)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "analyze": cmd_analyze,
        "evaluate": cmd_evaluate,
        "serve": cmd_serve,
        "llm-check": cmd_llm_check,
        "ingest": cmd_ingest,
        "ingest-status": cmd_ingest_status,
        "build-dataset": cmd_build_dataset,
        "dataset-report": cmd_dataset_report,
        "eval-models": cmd_eval_models,
        "replay-impact": cmd_replay_impact,
        "triage": cmd_triage,
        "replay-gates": cmd_replay_gates,
        "eval-pooled": cmd_eval_pooled,
        "live-score": cmd_live_score,
        "live-evaluate": cmd_live_evaluate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
