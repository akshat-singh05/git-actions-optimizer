"""Add ``rank_permutation_p`` to the summary.json of finished measurements (written before that statistic existed).

    python scripts/recompute_summary.py docs/speedup_run docs/speedup_run_bhagatshivam ...

Reads timings.json, leaves every other field untouched."""
import importlib.util
import json
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("measure_workflows", Path(__file__).with_name("measure_workflows.py"))
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)

for d in map(Path, sys.argv[1:]):
    timings = json.loads((d / "timings.json").read_text(encoding="utf-8"))
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    meas = [r for r in timings if not r["warmup"]]
    for key in ("critical_path_s", "run_s", "billable_job_s"):
        a = [r[key] for r in meas if r["arm"] == "A"]
        b = [r[key] for r in meas if r["arm"] == "B"]
        summary[key]["rank_permutation_p"] = round(mw.rank_permutation_p(a, b), 4)
    (d / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(d, {k: summary[k]["permutation_p"] for k in ("critical_path_s",)}, {k: summary[k]["rank_permutation_p"] for k in ("critical_path_s",)})
