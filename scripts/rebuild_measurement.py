"""Rebuild timings.json / summary.json of a finished measurement from GitHub, by the commit of every recorded round.

Used after a measurement picked the wrong workflow run for a commit (a repository with several workflows, e.g. CodeQL next to CI, before
``measure_workflows.py`` filtered by workflow path).  Every round's commit sha is in runs.json; the run of the workflow under test for that
commit is read again from the GitHub API, so nothing is re-executed and nothing is invented.

    python scripts/rebuild_measurement.py --repo owner/repo --dir docs/speedup_run_x --target .github/workflows/ci.yml --a arm_a.yml --b arm_b.yml
"""
import argparse
import importlib.util
import json
from pathlib import Path

import yaml

spec = importlib.util.spec_from_file_location("measure_workflows", Path(__file__).with_name("measure_workflows.py"))
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--dir", required=True)
ap.add_argument("--target", default=".github/workflows/ci.yml")
ap.add_argument("--a", required=True)
ap.add_argument("--b", required=True)
ap.add_argument("--expect-failed-job", help="failing-run measurement: the job that was made to fail (see measure_workflows.py --inject-failure)")
args = ap.parse_args()
d = Path(args.dir)
needs = {}
for arm, f in (("A", args.a), ("B", args.b)):
    spec_y = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
    needs[arm] = {k: ([v["needs"]] if isinstance(v.get("needs"), str) else list(v.get("needs") or [])) for k, v in spec_y["jobs"].items()}
records = []
for r in json.loads((d / "runs.json").read_text(encoding="utf-8")):
    runs = mw.gh_json("api", f"repos/{args.repo}/actions/runs?head_sha={r['sha']}&event=pull_request")["workflow_runs"]
    runs = [x for x in runs if (x.get("path") or "").split("@")[0] == args.target and x["status"] == "completed"]
    if not runs:
        raise SystemExit(f"no completed run of {args.target} for {r['arm']} round {r['round']} ({r['sha']})")
    run_obj = runs[0]
    jobs = mw.gh_json("api", f"repos/{args.repo}/actions/runs/{run_obj['id']}/jobs?per_page=100")["jobs"]
    records.append({"arm": r["arm"], "round": r["round"], "warmup": r["warmup"], "sha": r["sha"], "run_id": run_obj["id"], "conclusion": run_obj["conclusion"],
                    **mw.run_metrics(jobs, run_obj), "needs_ok": mw.needs_respected(jobs, needs[r["arm"]])})
records.sort(key=lambda x: (x["round"], x["arm"]))
(d / "timings.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
(d / "runs.json").write_text(json.dumps([{k: x[k] for k in ("arm", "round", "warmup", "sha", "run_id", "conclusion")} for x in records], indent=1), encoding="utf-8")
summary = mw.build_summary(records, args.repo, args.expect_failed_job)
(d / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
print(json.dumps(summary, indent=1))
