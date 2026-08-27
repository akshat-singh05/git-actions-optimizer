"""
Runs the full six-layer pipeline end to end on a synthetic example:

    python examples/demo.py

Produces:
  - examples/sample_repo/.github/workflows/ci.optimized.yml
  - a printed explanation of the schedule + model metrics

Run from the project root so ``gha_optimizer`` is importable
(or ``pip install -e .`` first).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1252 can't handle emoji)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.synthetic_history import generate_history
from gha_optimizer.pipeline import run_pipeline

REPO_ROOT = Path(__file__).resolve().parent / "sample_repo"
WORKFLOW_PATH = Path(__file__).resolve().parent / "sample_workflow.yml"
OUT_PATH = REPO_ROOT / ".github" / "workflows" / "ci.optimized.yml"

TRACKED_FILES = [
    "src/app.py",
    "src/utils.py",
    "src/api/routes.py",
    "tests/unit/test_app.py",
    "tests/integration/test_api.py",
    "requirements.txt",
]

# Simulate a developer's current, not-yet-merged diff: they touched the
# integration-relevant route file AND the lockfile, which should (a) mark
# integration_tests as structurally impacted, and (b) predict a cache miss
# for every job downstream of the requirements.txt change.
CHANGED_FILES = ["src/api/routes.py", "requirements.txt"]


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    history = generate_history(n_runs=150)

    plan, explanation = run_pipeline(
        workflow_path=WORKFLOW_PATH,
        repo_root=REPO_ROOT,
        tracked_files=TRACKED_FILES,
        history=history,
        changed_files=CHANGED_FILES,
        out_path=OUT_PATH,
    )

    print(explanation)
    print(f"\n✅ Optimized workflow written to: {OUT_PATH}")
    print(f"\n📊 Schedule summary:")
    print(f"   ⏱  Wall-clock: {plan.baseline_wall_clock_seconds:.0f}s → "
          f"{plan.estimated_wall_clock_seconds:.0f}s "
          f"({plan.time_saved_pct:+.1f}%)")
    print(f"   💰 Cost: ${plan.baseline_cost_usd:.5f} → "
          f"${plan.estimated_cost_usd:.5f} "
          f"({plan.cost_saved_pct:+.1f}%)")
    print(f"   ⚡ Energy: {plan.baseline_energy_wh:.2f}Wh → "
          f"{plan.estimated_energy_wh:.2f}Wh "
          f"({plan.energy_saved_pct:+.1f}%)")
    print(f"   🌿 CO₂: {plan.baseline_co2_grams:.2f}g → "
          f"{plan.estimated_co2_grams:.2f}g "
          f"({plan.co2_saved_pct:+.1f}%)")


if __name__ == "__main__":
    main()
