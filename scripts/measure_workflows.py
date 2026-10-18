"""
Measure two workflow files against each other on REAL GitHub Actions runs (before / after), alternating the arms.

    python scripts/measure_workflows.py --repo owner/repo --a docs/speedup_run/arm_a_original_ci.yml \
        --b docs/speedup_run/arm_b_proposed_ci.yml --rounds 7 --out docs/speedup_run

What it does (all of it is visible in the output; nothing is simulated):
  1. creates two throw-away branches from ``origin/<default branch>`` (never from local, unpushed commits), one per arm, each with
     the arm's file installed as ``.github/workflows/<name of the file the arms replace>`` and a draft pull request to the default
     branch (the workflow under test is triggered by pull_request);
  2. per round pushes one empty commit to each arm's branch (arm order alternates A,B / B,A / ...), waits for the run to finish and
     records it; the first round per arm is a warm-up (pip caches) and is excluded;
  3. reads every run's jobs from the GitHub API: critical path (first job start -> last job end), run time, summed job time
     (billable proxy), per-job times, queue times, step conclusions, and checks that every run and job succeeded and that each ``needs``
     of the arm's own file was respected;
  4. reports medians, the difference and an exact permutation p-value (difference of medians) per metric;
  5. closes the pull requests and deletes the branches, also when something fails.

Needs the GitHub CLI (``gh auth login``) with push rights on the repository.  It consumes GitHub Actions minutes
(about 2 x (rounds + 1) runs) and creates temporary branches / draft PRs in the repository named below.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

GH = next((g for g in ("gh", r"C:\Program Files\GitHub CLI\gh.exe") if subprocess.run([g, "--version"], capture_output=True).returncode == 0), "gh") \
    if os.name == "nt" else "gh"


EXTRA_ENV: dict[str, str] = {}      # extra environment for subprocesses (none by default)


def run(cmd: list[str], cwd: str | None = None, check: bool = True, tries: int = 6) -> str:
    """Run a command; a transient network failure (gh / git cannot reach GitHub) is retried with back-off, anything else is an error."""
    for attempt in range(tries):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env={**os.environ, **EXTRA_ENV})
        if r.returncode == 0 or not check:
            return r.stdout
        transient = cmd[0] == GH or re.search(r"Could not resolve|timed out|Connection (reset|refused)|unable to access|HTTP/2|early EOF", r.stderr, re.I)   # gh calls are idempotent reads / PR calls: retry any failure
        if not transient or attempt == tries - 1:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {r.stderr.strip()[:400]}")
        time.sleep(15 * (attempt + 1))
    return ""


def gh_json(*args: str):
    return json.loads(run([GH, *args]))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ----------------------------------------------------------------------------- statistics (unit-tested)
def permutation_p(a: list[float], b: list[float]) -> float:
    """Exact two-sided permutation p-value of |median(b) - median(a)| over all splits of the pooled values."""
    pooled = list(a) + list(b)
    n, k = len(pooled), len(a)
    observed = abs(statistics.median(b) - statistics.median(a))
    hits = total = 0
    for idx in itertools.combinations(range(n), k):
        chosen = set(idx)
        ga = [pooled[i] for i in idx]
        gb = [pooled[i] for i in range(n) if i not in chosen]
        total += 1
        hits += abs(statistics.median(gb) - statistics.median(ga)) >= observed - 1e-12
    return hits / total


def rank_permutation_p(a: list[float], b: list[float]) -> float:
    """Exact two-sided permutation p-value of the difference of MEAN RANKS (the Mann-Whitney statistic; ties share the average rank).

    ADDED AFTER the first experiments had been read: the pre-set statistic (difference of medians, ``permutation_p``) is conservative for
    small groups - with fully separated groups it still gives p of about 0.1 because many splits share the same median.  Both are reported."""
    pooled = list(a) + list(b)
    order = sorted(range(len(pooled)), key=lambda i: pooled[i])
    ranks = [0.0] * len(pooled)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pooled[order[j + 1]] == pooled[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    n, k = len(pooled), len(a)
    observed = abs(statistics.mean(ranks[:k]) - statistics.mean(ranks[k:]))
    hits = total = 0
    for idx in itertools.combinations(range(n), k):
        chosen = set(idx)
        total += 1
        hits += abs(statistics.mean(ranks[i] for i in idx) - statistics.mean(ranks[i] for i in range(n) if i not in chosen)) >= observed - 1e-9
    return hits / total


def summarize_metric(a: list[float], b: list[float]) -> dict:
    ma, mb = statistics.median(a), statistics.median(b)
    return {
        "A_original": {"n": len(a), "median": ma, "mean": round(statistics.mean(a), 1), "min": min(a), "max": max(a),
                       "sd": round(statistics.pstdev(a), 1)},
        "B_proposed": {"n": len(b), "median": mb, "mean": round(statistics.mean(b), 1), "min": min(b), "max": max(b),
                       "sd": round(statistics.pstdev(b), 1)},
        "median_diff_B_minus_A_s": mb - ma,
        "median_diff_pct": round(100 * (mb - ma) / ma, 2) if ma else None,
        "permutation_p": round(permutation_p(a, b), 4),
        "rank_permutation_p": round(rank_permutation_p(a, b), 4),
    }


def run_metrics(jobs: list[dict], run_obj: dict) -> dict:
    """Timings of one finished run from the API objects."""
    starts = [parse_ts(j["started_at"]) for j in jobs if j.get("started_at")]
    ends = [parse_ts(j["completed_at"]) for j in jobs if j.get("completed_at")]
    created = parse_ts(run_obj["created_at"])
    return {
        "critical_path_s": (max(ends) - min(starts)).total_seconds(),
        "run_s": (max(ends) - created).total_seconds(),
        "billable_job_s": sum((parse_ts(j["completed_at"]) - parse_ts(j["started_at"])).total_seconds() for j in jobs if j.get("started_at") and j.get("completed_at")),
        "queue_s": {j["name"]: (parse_ts(j["started_at"]) - created).total_seconds() for j in jobs if j.get("started_at")},
        "job_s": {j["name"]: (parse_ts(j["completed_at"]) - parse_ts(j["started_at"])).total_seconds() for j in jobs if j.get("started_at") and j.get("completed_at")},
        "job_conclusion": {j["name"]: j["conclusion"] for j in jobs},
        "steps": sorted((j["name"], s["name"], s["conclusion"]) for j in jobs for s in j.get("steps", [])),
    }


def build_summary(records: list[dict], repo: str) -> dict:
    """summary.json content from the per-run records (warm-up rounds excluded from every statistic)."""
    def valid(r: dict) -> bool:
        return r["conclusion"] == "success" and all(c in ("success", "skipped") for c in r["job_conclusion"].values())

    # a cancelled / failed run (e.g. cancelled by the workflow's own concurrency group) is not a timing of the workflow: it is excluded and listed
    measured = [r for r in records if not r["warmup"]]
    meas = [r for r in measured if valid(r)]
    excluded = [{"arm": r["arm"], "round": r["round"], "run_id": r["run_id"], "conclusion": r["conclusion"],
                 "job_conclusion": {j: c for j, c in r["job_conclusion"].items() if c not in ("success", "skipped")}} for r in measured if not valid(r)]
    A = [r for r in meas if r["arm"] == "A"]
    B = [r for r in meas if r["arm"] == "B"]
    summary = {
        "repo": repo, "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_measured_runs": len(meas), "n_per_arm": {"A_original": len(A), "B_proposed": len(B)}, "excluded_invalid_runs": excluded,
        "all_success": all(valid(r) for r in meas),
        "needs_ok_all": all(r["needs_ok"] for r in records),
    }
    for key in ("critical_path_s", "run_s", "billable_job_s"):
        summary[key] = summarize_metric([r[key] for r in A], [r[key] for r in B])
    jobs = sorted(set.intersection(*(set(r["job_s"]) for r in meas)))     # jobs present in every measured run
    summary["job_median_s"] = {"A_original": {j: statistics.median(r["job_s"][j] for r in A) for j in jobs},
                               "B_proposed": {j: statistics.median(r["job_s"][j] for r in B) for j in jobs}}
    return summary


def needs_respected(jobs: list[dict], needs: dict[str, list[str]]) -> bool:
    by = {j["name"]: j for j in jobs}
    for down, ups in needs.items():
        for up in ups:
            if down in by and up in by and parse_ts(by[down]["started_at"]) < parse_ts(by[up]["completed_at"]):
                return False
    return True


# ----------------------------------------------------------------------------- the experiment
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--a", required=True, help="arm A (original) workflow file")
    ap.add_argument("--b", required=True, help="arm B (proposed) workflow file")
    ap.add_argument("--target", default=".github/workflows/ci.yml", help="path both arms install their file at")
    ap.add_argument("--rounds", type=int, default=7, help="measured runs per arm (one extra warm-up run per arm is added)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout-min", type=float, default=25)
    ap.add_argument("--trigger", choices=["pr", "dispatch"], default="pr",
                    help="pr (default): one empty commit per run on a throw-away draft PR; dispatch: start the arm's workflow with workflow_dispatch on its branch (for workflows that only run when certain paths change, so an empty commit would not trigger them; the workflow needs a workflow_dispatch trigger)")
    ap.add_argument("--touch-file", metavar="PATH", help="pr mode, for a workflow that only runs when certain paths change: instead of an empty commit, append one comment line (prefix --touch-comment) to this repository file in every round, so the push matches the workflow's paths filter without changing any behaviour (use a file whose comments are inert, e.g. ruff.toml)")
    ap.add_argument("--touch-comment", default="#", help="comment prefix of the touched file (default #)")
    ap.add_argument("--resume", metavar="STAMP", help="continue an interrupted measurement: the timestamp in the branch names measure-<STAMP>-a / -b")
    args = ap.parse_args()

    import shutil

    import yaml
    needs = {}
    for arm, f in (("A", args.a), ("B", args.b)):
        spec = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
        needs[arm] = {k: ([v["needs"]] if isinstance(v.get("needs"), str) else list(v.get("needs") or [])) for k, v in spec["jobs"].items()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = args.resume or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    default_branch = gh_json("repo", "view", args.repo, "--json", "defaultBranchRef")["defaultBranchRef"]["name"]
    tmp = tempfile.mkdtemp(prefix="gha-measure-")
    url = f"https://github.com/{args.repo}.git"
    env_git: list[str] = []     # git uses the credentials already configured on this machine (a gh token lacks the `workflow` scope needed to push workflow files)
    wt = os.path.join(tmp, "wt")
    run(["git", *env_git, "clone", "--quiet", "--depth", "1", "--branch", default_branch, url, wt])
    branches = {"A": f"measure-{stamp}-a", "B": f"measure-{stamp}-b"}
    prs: dict[str, int] = {}
    records: list[dict] = []
    finished = False
    pushed_branches: list[str] = []      # dispatch mode: branches to delete at the end
    seen_run_ids: set[int] = set()
    GIT = ["git", "-c", "user.name=gha-measure", "-c", "user.email=gha-measure@example.invalid"]

    def record_run(arm: str, rnd: int, sha: str, run_obj: dict) -> dict:
        jobs = gh_json("api", f"repos/{args.repo}/actions/runs/{run_obj['id']}/jobs?per_page=100")["jobs"]
        rec = {"arm": arm, "round": rnd, "warmup": rnd == 0, "sha": sha, "run_id": run_obj["id"], "conclusion": run_obj["conclusion"],
               **run_metrics(jobs, run_obj), "needs_ok": needs_respected(jobs, needs[arm])}
        records.append(rec)
        (out / "timings.partial.json").write_text(json.dumps(records, indent=1), encoding="utf-8")      # survives a crash
        print(f"arm {arm} round {rnd}{' (warm-up)' if rec['warmup'] else ''}: {run_obj['conclusion']}, critical path {rec['critical_path_s']:.0f}s, "
              f"jobs {rec['job_s']}", flush=True)
        return rec

    def find_finished_run(sha: str) -> dict | None:
        runs = gh_json("api", f"repos/{args.repo}/actions/runs?head_sha={sha}&event=pull_request").get("workflow_runs", [])
        runs = [r for r in runs if (r.get("path") or "").split("@")[0] == args.target]      # other workflows (e.g. CodeQL) run on the same commit
        return runs[0] if runs and runs[0]["status"] == "completed" else None

    try:
        if args.resume:
            # re-attach to the existing branches and re-read the rounds that already finished (from GitHub, not from memory)
            for arm in ("A", "B"):
                run(["git", *env_git, "fetch", "-q", "origin", f"{branches[arm]}:{branches[arm]}"], cwd=wt)
                prs[arm] = gh_json("pr", "view", branches[arm], "--repo", args.repo, "--json", "number")["number"]
                commits = gh_json("api", f"repos/{args.repo}/commits?sha={branches[arm]}&per_page=100")
                for c in commits:
                    m = re.match(rf"measure: arm {arm} round (\d+)$", c["commit"]["message"].strip())
                    if m:
                        run_obj = find_finished_run(c["sha"])
                        if run_obj is not None:
                            record_run(arm, int(m.group(1)), c["sha"], run_obj)
                print(f"arm {arm}: resumed branch {branches[arm]}, PR #{prs[arm]}", flush=True)
        else:
            for arm, f in (("A", args.a), ("B", args.b)):
                run(["git", "checkout", "-q", "-b", branches[arm], f"origin/{default_branch}"], cwd=wt)
                target = Path(wt) / args.target
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(Path(f).read_bytes())
                run(["git", "add", "-A"], cwd=wt)
                run([*GIT, "commit", "-q", "--allow-empty", "-m", f"measure: arm {arm} workflow"], cwd=wt)
                run(["git", *env_git, "push", "-q", "origin", branches[arm]], cwd=wt)
                if args.trigger == "dispatch":
                    pushed_branches.append(branches[arm])
                    print(f"arm {arm}: branch {branches[arm]} (workflow_dispatch trigger)", flush=True)
                    continue
                run([GH, "pr", "create", "--repo", args.repo, "--draft", "--base", default_branch, "--head", branches[arm],
                     "--title", f"[throwaway] workflow measurement arm {arm}", "--body", "Temporary pull request created by scripts/measure_workflows.py; it is closed and its branch deleted when the measurement ends."])
                prs[arm] = gh_json("pr", "view", branches[arm], "--repo", args.repo, "--json", "number")["number"]
                print(f"arm {arm}: branch {branches[arm]}, PR #{prs[arm]}", flush=True)

        done = {(r["arm"], r["round"]) for r in records}

        def trigger_dispatch_and_wait(arm: str, rnd: int) -> None:
            """Start the arm's workflow with workflow_dispatch on its branch and wait for the new run (found by branch, event and creation time)."""
            wf_name = Path(args.target).name
            t0 = datetime.now(timezone.utc)
            run([GH, "workflow", "run", wf_name, "--repo", args.repo, "--ref", branches[arm]])
            deadline = time.time() + 60 * args.timeout_min
            run_obj = None
            while time.time() < deadline:
                runs = gh_json("api", f"repos/{args.repo}/actions/workflows/{wf_name}/runs?branch={branches[arm]}&event=workflow_dispatch&per_page=10").get("workflow_runs", [])
                fresh = [r for r in runs if r["id"] not in seen_run_ids and parse_ts(r["created_at"]) >= t0 - timedelta(seconds=20)]
                if fresh and fresh[0]["status"] == "completed":
                    run_obj = fresh[0]
                    break
                time.sleep(10)
            if run_obj is None:
                raise TimeoutError(f"arm {arm} round {rnd}: the dispatched run did not finish in {args.timeout_min} min")
            seen_run_ids.add(run_obj["id"])
            record_run(arm, rnd, run_obj["head_sha"], run_obj)

        def trigger_and_wait(arm: str, rnd: int) -> None:
            if args.trigger == "dispatch":
                return trigger_dispatch_and_wait(arm, rnd)
            run(["git", "checkout", "-q", branches[arm]], cwd=wt)
            if args.touch_file:
                tf = Path(wt) / args.touch_file
                with open(tf, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"\n{args.touch_comment} measure: arm {arm} round {rnd}\n")
                run(["git", "add", args.touch_file], cwd=wt)
            run([*GIT, "commit", "-q", "--allow-empty", "-m", f"measure: arm {arm} round {rnd}"], cwd=wt)
            run(["git", *env_git, "push", "-q", "origin", branches[arm]], cwd=wt)
            sha = run(["git", "rev-parse", "HEAD"], cwd=wt).strip()
            deadline = time.time() + 60 * args.timeout_min
            run_obj = None
            while time.time() < deadline:
                run_obj = find_finished_run(sha)
                if run_obj is not None:
                    break
                time.sleep(10)
            if run_obj is None:
                raise TimeoutError(f"arm {arm} round {rnd}: the run did not finish in {args.timeout_min} min")
            record_run(arm, rnd, sha, run_obj)

        for rnd in range(0, args.rounds + 1):
            order = ["A", "B"] if rnd % 2 == 0 else ["B", "A"]
            for arm in order:
                if (arm, rnd) not in done:
                    trigger_and_wait(arm, rnd)
        finished = True
    finally:
        if finished and args.trigger == "dispatch":
            for b in pushed_branches:
                subprocess.run([GH, "api", "-X", "DELETE", f"repos/{args.repo}/git/refs/heads/{b}"], capture_output=True, env={**os.environ, **EXTRA_ENV})
            print("deleted the throw-away branches", flush=True)
        elif finished:
            for arm, number in prs.items():
                subprocess.run([GH, "pr", "close", str(number), "--repo", args.repo, "--delete-branch"], capture_output=True, env={**os.environ, **EXTRA_ENV})
            print("closed the throw-away pull requests and deleted their branches", flush=True)
        else:
            print(f"NOT finished: the throw-away pull requests {sorted(prs.values())} / branches {pushed_branches} are left in place" + ("" if args.trigger == "dispatch" else f"; continue with --resume {stamp}"), flush=True)
        shutil.rmtree(tmp, ignore_errors=True)

    records.sort(key=lambda r: (r["round"], r["arm"]))
    (out / "timings.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    (out / "runs.json").write_text(json.dumps([{k: r[k] for k in ("arm", "round", "warmup", "sha", "run_id", "conclusion")} for r in records], indent=1), encoding="utf-8")
    summary = build_summary(records, args.repo)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
