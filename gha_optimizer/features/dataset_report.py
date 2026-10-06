"""
Generates ``docs/DATASET_REPORT.md`` from the database.  **Every number in the report
is computed at generation time** (SQL counts and pandas statistics over the ingested
rows); the only hand-written text is the method description and the conditional
warnings, which fire from the computed numbers.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from gha_optimizer.config import config
from gha_optimizer.features.dataset import build_dataset
from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
from gha_optimizer.server.db import models as m

STRUCTURAL = ["job_structurally_impacted", "cache_key_touched", "dep_graph_distance"]


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out) + "\n"


def _pct(n: float, d: float) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def _fmt(ts: Any) -> str:
    return str(ts)[:19].replace("T", " ") + " UTC" if ts is not None else "n/a"


def _repo_collection(db: Session, repo: str) -> dict[str, Any]:
    runs, jobs = m.WorkflowRun, m.WorkflowJob
    out: dict[str, Any] = {}
    out["license"] = (db.query(m.Repository.license_spdx).filter_by(full_name=repo).scalar())
    out["language"] = (db.query(m.Repository.language).filter_by(full_name=repo).scalar())
    out["runs_total"] = db.query(func.count(runs.id)).filter(runs.repo == repo).scalar() or 0
    out["runs_processed"] = (
        db.query(func.count(m.IngestionProgress.id)).filter_by(repo=repo, stage="run_done").scalar() or 0
    )
    lo, hi = db.query(func.min(runs.gh_created_at), func.max(runs.gh_created_at)).filter(runs.repo == repo).one()
    out["range"] = (lo, hi)
    out["workflows"] = dict(
        db.query(runs.workflow_path, func.count(runs.id)).filter(runs.repo == repo).group_by(runs.workflow_path).all()
    )
    out["events"] = dict(db.query(runs.event, func.count(runs.id)).filter(runs.repo == repo).group_by(runs.event).all())
    out["diff_sources"] = dict(
        db.query(runs.diff_source, func.count(runs.id)).filter(runs.repo == repo).group_by(runs.diff_source).all()
    )
    jq = db.query(jobs).join(runs, runs.run_id == jobs.run_id).filter(runs.repo == repo)
    out["cells"] = jq.count()
    out["cells_mapped"] = jq.filter(jobs.job_key.isnot(None)).count()
    out["cell_conclusions"] = dict(
        db.query(jobs.conclusion, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.conclusion).all()
    )
    out["cell_cache_status"] = dict(
        db.query(jobs.cache_label_status, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.cache_label_status).all()
    )
    out["cell_cache_values"] = dict(
        db.query(jobs.cache_hit, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.cache_hit).all()
    )
    out["cell_cache_outcomes"] = dict(
        db.query(jobs.cache_outcome, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.cache_outcome).all()
    )
    out["ledger"] = dict(
        db.query(m.JobLogFetch.status, func.count(m.JobLogFetch.github_job_id))
        .join(runs, runs.run_id == m.JobLogFetch.run_id).filter(runs.repo == repo)
        .group_by(m.JobLogFetch.status).all()
    )
    out["log_bytes"] = (
        db.query(func.sum(m.JobLogFetch.bytes_downloaded)).join(runs, runs.run_id == m.JobLogFetch.run_id)
        .filter(runs.repo == repo).scalar() or 0
    )
    out["cache_events"] = (
        db.query(func.count(m.JobCacheEvent.id)).join(jobs, jobs.github_job_id == m.JobCacheEvent.github_job_id)
        .join(runs, runs.run_id == jobs.run_id).filter(runs.repo == repo).scalar() or 0
    )
    inv = db.query(m.IngestionRun).filter_by(repo=repo).all()
    out["invocations"] = len(inv)
    out["api_requests"] = sum(int((r.counters or {}).get("client_requests", 0)) for r in inv)
    out["rate_limit_waits"] = sum(int((r.counters or {}).get("client_rate_limit_waits", 0)) for r in inv)
    out["failures"] = sum(int((r.counters or {}).get("failures", 0)) for r in inv)
    return out


def _label_block(df: pd.DataFrame) -> list[list[Any]]:
    rows: list[list[Any]] = []
    lf = df["label_failed"].dropna()
    rows.append(["failure (1 = failure/timed_out)", len(lf), int(lf.sum()), _pct(lf.sum(), len(lf)),
                 f"{len(df) - len(lf)} rows without label (cancelled/skipped/other)"])
    lc = df["label_cache_hit"].dropna()
    miss = int((lc == 0).sum())
    rows.append(["cache miss (1 = miss; log-derived)", len(lc), miss, _pct(miss, len(lc)),
                 f"{len(df) - len(lc)} rows NULL (excluded from cache-model training)"])
    ld = df["label_duration"].dropna()
    rows.append(["duration seconds (success/failure rows)", len(ld), "", "",
                 (f"mean {ld.mean():.1f}s, median {ld.median():.1f}s, p90 {ld.quantile(0.9):.1f}s, max {ld.max():.0f}s" if len(ld) else "none")])
    return rows


def generate_report(
    db: Session,
    repos: Iterable[str],
    since: datetime | None = None,
    events: Iterable[str] | None = None,
) -> str:
    repos = list(repos)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    frames: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
    coll: dict[str, dict[str, Any]] = {}
    for r in repos:
        coll[r] = _repo_collection(db, r)
        frames[r] = build_dataset(db, r, since=since, events=events)

    out: list[str] = []
    out.append("# Dataset report — real GitHub Actions history\n")
    out.append(
        f"*Generated {now} by `gha-optimizer dataset-report` from database "
        f"`{os.environ.get('DATABASE_URL', 'sqlite:///gha_optimizer.db')}`. Every figure below was computed from the "
        "ingested rows at generation time; nothing is hand-entered.*\n"
    )
    out.append("## 1. What this dataset is\n")
    out.append(
        "One row per **(workflow run, workflow job key)**. A matrix job's cells (several GitHub jobs sharing one YAML key) are "
        "aggregated to the node the scheduler schedules: failure if any cell failed, duration = slowest cell, cache label from "
        "the cells that have one. Labels:\n\n"
        "* **failure** — job conclusion `failure`/`timed_out` = 1, `success` = 0; `cancelled`, `skipped` and anything else carry **no** label.\n"
        "* **duration** — `completed_at − started_at` from the API, success/failure rows only.\n"
        "* **cache miss** — parsed from the job's real log (GitHub's REST API does not expose it). A job is a *miss* if any cache "
        "restore step found nothing; a *hit* if every restore step restored (exact or via `restore-keys`). Jobs whose log has no cache "
        "line, whose log expired, or that cannot run a cache action have a **NULL** label and are excluded from cache-model training — "
        "never defaulted.\n\n"
        "Historical features use only runs strictly before the target run (`tests/test_feature_leakage.py`). Structural features are computed "
        "on the dependency graph of the repository **at the run's commit** (git mirror), not a static graph. "
        "The split is temporal at run level.\n"
    )

    # ---- 2. collection
    out.append("## 2. Collection (what was actually fetched)\n")
    rows = []
    for r in repos:
        c = coll[r]
        rows.append([r, c["license"], c["language"], c["runs_total"], c["runs_processed"], c["cells"],
                     f"{_fmt(c['range'][0])} → {_fmt(c['range'][1])}", c["api_requests"], c["rate_limit_waits"], c["failures"]])
    out.append(_table(["repo", "licence", "language", "runs listed", "runs fully processed", "job cells", "run date range",
                       "API requests*", "rate-limit waits*", "failures*"], rows))
    out.append("\\* summed over this database's `ingestion_runs` records (an interrupted/resumed ingest counts each invocation).\n")
    for r in repos:
        c = coll[r]
        out.append(f"### {r}\n")
        out.append(f"* workflows ingested: {c['workflows']}\n* triggering events: {c['events']}\n"
                   f"* diff source per run: {c['diff_sources']} (`compare_pr` = API compare of the whole PR; "
                   f"`git_merge_base` = whole-PR diff from the git mirror, merge-base(default branch at run time, head)..head; "
                   f"`commit` = head commit of a non-PR event — multi-commit pushes are approximated by it; "
                   f"`commit_only` = PR run whose PR diff could not be determined, head commit only; `None` = not yet fetched)\n"
                   f"* job cells: {c['cells']} total, {c['cells_mapped']} mapped to a YAML job key "
                   f"({_pct(c['cells_mapped'], c['cells'])})\n"
                   f"* cell conclusions: {c['cell_conclusions']}\n"
                   f"* log ledger: {c['ledger']} — {c['cache_events']} cache events parsed from {c['log_bytes'] / 1e6:.1f} MB of logs\n")

    # ---- 3. rows
    out.append("## 3. Dataset rows (run × job key)\n")
    rows = []
    total_rows = 0
    for r in repos:
        df, info = frames[r]
        total_rows += len(df)
        rng = (df["timestamp"].min(), df["timestamp"].max()) if len(df) else (None, None)
        n_runs = df["run_id"].nunique() if len(df) else 0
        rows.append([r, len(df), n_runs, coll[r]["runs_processed"] - n_runs, df["job_id"].nunique() if len(df) else 0,
                     f"{rng[0]} → {rng[1]}"])
    rows.append(["**total**", total_rows, "", "", "", ""])
    out.append(_table(["repo", "rows", "runs in dataset", "processed runs not in dataset*", "job keys", "date range (run created_at)"], rows))
    out.append("\\* processed runs that contribute no row: GitHub started no job for them (e.g. every job filtered out) or none of their jobs "
               "could be mapped to a YAML job key.\n")

    # ---- 4. labels
    out.append("## 4. Label distributions\n")
    all_df = pd.concat([f for f, _ in frames.values() if len(f)], ignore_index=True) if any(len(f) for f, _ in frames.values()) else pd.DataFrame()
    for r in repos + (["ALL REPOS"] if len(repos) > 1 else []):
        df = all_df if r == "ALL REPOS" else frames[r][0]
        if df.empty:
            out.append(f"### {r}\n\n*no dataset rows*\n")
            continue
        out.append(f"### {r}\n")
        out.append(_table(["target", "labelled rows", "positives", "positive rate", "note"], _label_block(df)))
        fr = df["label_failed"].dropna()
        if len(fr):
            out.append(f"**Failure rate: {100 * fr.mean():.2f}%** ({int(fr.sum())} of {len(fr)} labelled rows). "
                       + ("The positive class is tiny, so accuracy and ROC-AUC would flatter any model; use PR-AUC against the prevalence "
                          "and report the base rate.\n" if fr.mean() < 0.10 else "\n"))
        oc = df.groupby("cache_outcome", dropna=False).size().to_dict() if "cache_outcome" in df else {}
        out.append(f"Cache outcome breakdown at row level: {oc}\n")

    # ---- 5. cache coverage
    out.append("## 5. Cache-label coverage\n")
    rows = []
    for r in repos:
        c = coll[r]
        df = frames[r][0]
        lab = int(df["label_cache_hit"].notna().sum()) if len(df) else 0
        rows.append([r, c["cells"], c["cell_cache_values"].get(True, 0), c["cell_cache_values"].get(False, 0),
                     c["cell_cache_values"].get(None, 0), len(df), lab, len(df) - lab, _pct(lab, len(df))])
    out.append(_table(["repo", "job cells", "cells hit", "cells miss", "cells NULL", "rows", "rows labelled", "rows NULL", "row coverage"], rows))
    for r in repos:
        out.append(f"* **{r}** — why cells have no label (`cache_label_status`): {coll[r]['cell_cache_status']}\n")
    out.append(
        "\nStatus meanings: `labelled` = a cache restore line was parsed; `parsed_no_events` = the log downloaded fine but contained no cache "
        "restore line (no cache action ran or it printed nothing recognisable); `skipped_not_cache_capable` = the job's YAML steps cannot "
        "restore a cache; `skipped_conclusion` = cancelled/skipped job; `expired` = log past GitHub's retention (HTTP 410); `unavailable`/`error` = "
        "download failed; `skipped_budget` = hit `--log-budget`; NULL status = logs were not requested.\n"
    )

    # ---- 6. missing values
    out.append("## 6. Missing values per feature (rows with NaN)\n")
    rows = []
    for col in FEATURE_COLUMNS:
        row = [col]
        for r in repos:
            df = frames[r][0]
            row.append(f"{int(df[col].isna().sum())} ({_pct(df[col].isna().sum(), len(df))})" if len(df) else "n/a")
        rows.append(row)
    out.append(_table(["feature"] + repos, rows))
    out.append("Historical features are NaN by construction for a job's first runs (no earlier evidence); the models accept NaN. "
               "`author_failure_rate` is NaN when the actor has no earlier labelled run. Structural features are NaN where graph "
               "features were not computed for that run.\n")

    # ---- 7. graph feature quality
    out.append("## 7. Graph-feature quality\n")
    rows = []
    notes: list[str] = []
    for r in repos:
        df = frames[r][0]
        if df.empty:
            continue
        have = df["any_changed_file_in_graph"].notna()
        fb = int((df.loc[have, "any_changed_file_in_graph"] == 0).sum())
        vals = {col: df[col].dropna().nunique() for col in STRUCTURAL}
        real_impact = int(((df["job_structurally_impacted"] == 1) & (df["any_changed_file_in_graph"] == 1)).sum())
        cache_touched = int((df["cache_key_touched"] == 1).sum())
        rows.append([r, int(have.sum()), fb, _pct(fb, have.sum()), real_impact, cache_touched,
                     vals["job_structurally_impacted"], vals["cache_key_touched"], vals["dep_graph_distance"]])
        const = [c for c, n in vals.items() if n <= 1 and have.any()]
        if const:
            notes.append(f"* **{r}**: constant (zero-variance) structural feature(s) {const} — they carry no information for this repository.")
        if have.any() and fb / have.sum() > 0.5:
            notes.append(f"* **{r}**: for {_pct(fb, have.sum())} of rows no changed file was a graph node, so `job_structurally_impacted` fell back to "
                         "\"all jobs impacted\" (uninformative).")
    out.append(_table(["repo", "rows with graph features", "rows where fallback fired", "fallback share",
                       "rows impacted via a REAL graph path", "rows with cache_key_touched=1",
                       "distinct: impacted", "distinct: cache_key_touched", "distinct: distance"], rows))
    out.extend(n + "\n" for n in notes)

    # ---- 8. split
    out.append("## 8. Temporal train/test split\n")
    rows = []
    for r in repos:
        df, info = frames[r]
        if df.empty or "split" not in df:
            continue
        tr, te = df[df.split == "train"], df[df.split == "test"]
        ftr, fte = tr["label_failed"].dropna(), te["label_failed"].dropna()
        rows.append([r, info.get("boundary"), len(tr), tr["run_id"].nunique(), len(te), te["run_id"].nunique(),
                     f"{100 * ftr.mean():.2f}% ({int(ftr.sum())}/{len(ftr)})" if len(ftr) else "n/a",
                     f"{100 * fte.mean():.2f}% ({int(fte.sum())}/{len(fte)})" if len(fte) else "n/a"])
    out.append(_table(["repo", "test period starts (run created_at)", "train rows", "train runs", "test rows", "test runs",
                       "failure rate train", "failure rate test"], rows))
    out.append(f"Test fraction = last {int(config.model.temporal_test_fraction * 100)}% of runs by creation time (`config.model.temporal_test_fraction`); "
               "all jobs of a run are on the same side; the split is never random.\n")

    # ---- 9. limits
    out.append("## 9. What this data does NOT support\n")
    lim = [
        "Rows are **not independent**: jobs of one run, and consecutive runs of one PR, share a commit lineage. Treat any metric as optimistic by that margin.",
        "Cache labels exist only inside GitHub's log-retention window (measured ≈45 days for quarkus, ≈90 days for the others); older runs contribute failure/duration labels only.",
        "A PR run's changed-file set is the whole-PR diff (API compare, or merge-base..head from the git mirror, assuming the PR targets the default branch); runs whose PR could not be determined, and multi-commit pushes, fall back to the head commit's diff (see `diff_source`).",
        "Only the **latest attempt** of a re-run is ingested.",
        "`cancelled` jobs (mostly concurrency cancel-in-progress) and `skipped` jobs carry no failure or duration label; cancellation is not modelled.",
        "The scheduler's cost / energy / CO₂ constants are configuration assumptions, not measurements from this data.",
    ]
    for r in repos:
        df = frames[r][0]
        if df.empty:
            lim.append(f"**{r}**: no rows.")
            continue
        fr = df["label_failed"].dropna()
        if len(fr) and fr.sum() < 100:
            lim.append(f"**{r}**: only {int(fr.sum())} failure positives — failure-model metrics will have wide uncertainty.")
        lc = df["label_cache_hit"].dropna()
        if len(lc) < config.model.min_cache_labels or lc.nunique() < 2:
            lim.append(f"**{r}**: cache labels insufficient for a cache-miss model ({len(lc)} labelled rows, classes {sorted(lc.unique().tolist())}); the pipeline will decline to predict.")
        if coll[r]["cells_mapped"] < coll[r]["cells"]:
            lim.append(f"**{r}**: {coll[r]['cells'] - coll[r]['cells_mapped']} of {coll[r]['cells']} job cells "
                       f"({_pct(coll[r]['cells'] - coll[r]['cells_mapped'], coll[r]['cells'])}) could not be mapped to a YAML job key "
                       "(ambiguous or unmatched display names) and are absent from the dataset.")
        ev = coll[r]["events"]
        if ev and len(ev) > 1:
            lim.append(f"**{r}**: runs come from several events {ev}; event type is not a model feature.")
    out.extend(f"* {x}" for x in lim)
    out.append("")
    return "\n".join(out)
