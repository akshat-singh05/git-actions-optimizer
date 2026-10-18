"""How many of the runs the live scorers SAW did they manage to score before a job finished?  (docs/BACKGROUND_RUNBOOK.md, prometheus pass rule)

    python scripts/live_share.py [--since ISO_UTC] [--rule-min-runs 100] [--rule-min-share 0.5]

A run is *seen* when it is in the ledger as ``scored`` or ``skipped`` (skipped = a job had already completed when the scorer first looked, so no
prospective prediction was possible).  Runs that finish between two polls never appear in the ledger and are not counted here, so the share is an upper
bound of the true share of runs.  The prometheus rule fixed on 2026-10-18, before its weekday traffic: the faster poll (6 s, from 2026-10-17 22:49 IST) PASSES if at
least 50% of at least 100 seen runs were scored; with fewer than 100 seen runs the answer is "not enough runs yet".
"""
import argparse
import json
from collections import Counter
from pathlib import Path

LIVE = Path(__file__).resolve().parent.parent / ".gha_optimizer" / "live"
REPOS = ("astral-sh__uv", "astral-sh__ruff", "prometheus__prometheus")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-17T17:19:00Z", help="UTC; default = the restart that switched prometheus to the 6 s poll")
    ap.add_argument("--rule-min-runs", type=int, default=100)
    ap.add_argument("--rule-min-share", type=float, default=0.5)
    args = ap.parse_args()
    for repo in REPOS:
        path = LIVE / f"{repo}.jsonl"
        rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()] if path.exists() else []
        before = Counter(r["kind"] for r in rows if r["scored_at"] < args.since)
        after = Counter(r["kind"] for r in rows if r["scored_at"] >= args.since)
        def share(c):
            n = c["scored"] + c["skipped"]
            return f"{c['scored']} of {n} ({100 * c['scored'] / n:.0f}%)" if n else "no runs"
        line = f"{repo:24} before {args.since[:16]}: {share(before):22} since: {share(after)}"
        if repo.startswith("prometheus"):
            n = after["scored"] + after["skipped"]
            if n < args.rule_min_runs:
                verdict = f"NOT ENOUGH RUNS YET ({n} of {args.rule_min_runs} needed)"
            else:
                verdict = "PASS" if after["scored"] / n >= args.rule_min_share else "FAIL"
            line += f"   | rule (>= {args.rule_min_share:.0%} of >= {args.rule_min_runs} runs): {verdict}"
        print(line)


if __name__ == "__main__":
    main()
