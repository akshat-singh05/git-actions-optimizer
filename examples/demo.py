"""
Runs the full six-layer pipeline end to end on a REAL ingested repository:

    gha-optimizer ingest --repo owner/repo --since 40d     # once: fetch real history
    python examples/demo.py --repo owner/repo [--workflow .github/workflows/ci.yml]

The repository tree is exported from the local git mirror (default-branch tip), the
changed files are the real diff of the most recent ingested run of the workflow, and
the models are trained on the real ingested history.  With too little history the demo
says so and exits — there is no synthetic fallback.

Run from the project root so ``gha_optimizer`` is importable (or ``pip install -e .``).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1252 can't handle emoji)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end demo on a real ingested repository")
    parser.add_argument("--repo", required=True, help="owner/repo (must have been ingested)")
    parser.add_argument("--workflow", help="repo-relative workflow path (default: the most-ingested workflow)")
    parser.add_argument("--db", help="database URL (default: DATABASE_URL or sqlite:///gha_optimizer.db)")
    args = parser.parse_args()
    if args.db:
        os.environ["DATABASE_URL"] = args.db

    from gha_optimizer.ingestion.history import InsufficientHistoryError
    from gha_optimizer.ingestion.workspace import prepare_repo_snapshot
    from gha_optimizer.pipeline import run_pipeline
    from gha_optimizer.server.db.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        snap = prepare_repo_snapshot(db, args.repo, args.workflow)
    except (InsufficientHistoryError, LookupError) as exc:
        print(f"Cannot run the demo: {exc}\nIngest real history first: gha-optimizer ingest --repo {args.repo}")
        return 2
    finally:
        db.close()

    out_path = snap.root / ".github" / "workflows" / f"{snap.workflow_path.stem}.optimized.yml"
    print(f"Repository: {snap.repo} @ {snap.sha[:10]}   workflow: {snap.workflow_repo_path}")
    print(f"Current diff (real files of ingested run {snap.changed_from_run}): {snap.changed_files[:8]}"
          + (" ..." if len(snap.changed_files) > 8 else ""))
    try:
        plan, explanation = run_pipeline(
            workflow_path=snap.workflow_path,
            repo_root=snap.root,
            tracked_files=snap.tracked_files,
            history=snap.history,
            changed_files=snap.changed_files,
            out_path=out_path,
            workflow_repo_path=snap.workflow_repo_path,
        )
    except InsufficientHistoryError as exc:
        print(f"Declining to predict: {exc}")
        return 2

    print(explanation)
    print(f"\nProposed workflow written to: {out_path}")
    print("\nSchedule summary (ESTIMATED from predicted durations — not measured executions):")
    print(f"   Wall-clock: {plan.baseline_wall_clock_seconds:.0f}s -> {plan.estimated_wall_clock_seconds:.0f}s "
          f"({plan.time_saved_pct:+.1f}%)")
    print(f"   Cost:   ${plan.baseline_cost_usd:.5f} -> ${plan.estimated_cost_usd:.5f} ({plan.cost_saved_pct:+.1f}%)")
    print(f"   Energy: {plan.baseline_energy_wh:.2f}Wh -> {plan.estimated_energy_wh:.2f}Wh ({plan.energy_saved_pct:+.1f}%)")
    print(f"   CO2:    {plan.baseline_co2_grams:.2f}g -> {plan.estimated_co2_grams:.2f}g ({plan.co2_saved_pct:+.1f}%)")
    print("   (cost / energy / CO2 use configuration constants in gha_optimizer/config.py, not measurements)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
