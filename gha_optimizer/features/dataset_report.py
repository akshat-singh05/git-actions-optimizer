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


def graph_quality(db: Session, repo: str) -> dict[str, Any]:
    """Coverage of the stored (current-version) graph features and their association with whether the
    job was actually executed or skipped.  Everything is counted from ``run_graph_features`` and
    ``workflow_jobs``; nothing is estimated."""
    from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION

    gf, runs, jobs = m.RunGraphFeatures, m.WorkflowRun, m.WorkflowJob
    rows = (
        db.query(gf).join(runs, runs.run_id == gf.run_id)
        .filter(runs.repo == repo, gf.feature_version == GRAPH_FEATURE_VERSION).all()
    )
    stale = (
        db.query(func.count(gf.id)).join(runs, runs.run_id == gf.run_id)
        .filter(runs.repo == repo, gf.feature_version != GRAPH_FEATURE_VERSION).scalar() or 0
    )
    conclusions: dict[tuple[str, str], list[str | None]] = {}
    for run_id, key, concl in (
        db.query(jobs.run_id, jobs.job_key, jobs.conclusion).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo, jobs.job_key.isnot(None)).all()
    ):
        conclusions.setdefault((run_id, key), []).append(concl)

    q: dict[str, Any] = {"rows": len(rows), "stale_rows": stale}
    q["no_file_in_graph"] = sum(1 for r in rows if r.any_changed_file_in_graph == 0)
    q["with_file_links"] = sum(1 for r in rows if r.job_has_file_links == 1)
    q["impacted_1"] = sum(1 for r in rows if r.job_structurally_impacted == 1)
    q["impacted_0"] = sum(1 for r in rows if r.job_structurally_impacted == 0)
    q["impacted_unknown"] = sum(1 for r in rows if r.job_structurally_impacted is None)
    q["touched_1"] = sum(1 for r in rows if r.cache_key_touched == 1)
    q["touched_0"] = sum(1 for r in rows if r.cache_key_touched == 0)
    q["touched_unknown"] = sum(1 for r in rows if r.cache_key_touched is None)
    q["distinct_distance"] = len({r.dep_graph_distance for r in rows if r.dep_graph_distance is not None})

    # executed vs skipped: a (run, job key) is skipped when every matrix cell was skipped
    def outcome(r: Any) -> str | None:
        cs = conclusions.get((r.run_id, r.job_key)) or []
        if not cs:
            return None
        return "skipped" if all(c == "skipped" for c in cs) else "executed"

    labelled = [(r, outcome(r)) for r in rows if r.job_structurally_impacted is not None]
    labelled = [(r, o) for r, o in labelled if o is not None]
    gated_keys = {
        key for key in {r.job_key for r, _ in labelled}
        if {o for r, o in labelled if r.job_key == key} == {"skipped", "executed"}
    }

    def table(subset: list[tuple[Any, str]]) -> dict[str, Any]:
        t = {(i, o): 0 for i in (0, 1) for o in ("skipped", "executed")}
        for r, o in subset:
            t[(int(r.job_structurally_impacted), o)] += 1
        out: dict[str, Any] = {f"i{i}_{o}": n for (i, o), n in t.items()}
        try:
            from scipy.stats import fisher_exact
            out["fisher_p"] = float(fisher_exact([[t[(1, "skipped")], t[(1, "executed")]],
                                                  [t[(0, "skipped")], t[(0, "executed")]]])[1]) if subset else None
        except Exception:  # scipy missing or degenerate table
            out["fisher_p"] = None
        return out

    q["assoc_all"] = table(labelled)
    q["assoc_gated"] = table([(r, o) for r, o in labelled if r.job_key in gated_keys])
    q["gated_jobs"] = sorted(gated_keys)
    return q


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
    from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION

    qual = {r: graph_quality(db, r) for r in repos}
    rows = []
    for r in repos:
        q = qual[r]
        if not q["rows"]:
            continue
        rows.append([r, q["rows"], f"{q['no_file_in_graph']} ({_pct(q['no_file_in_graph'], q['rows'])})",
                     f"{q['with_file_links']} ({_pct(q['with_file_links'], q['rows'])})",
                     f"{q['impacted_1']} ({_pct(q['impacted_1'], q['rows'])})",
                     f"{q['impacted_0']} ({_pct(q['impacted_0'], q['rows'])})",
                     f"{q['impacted_unknown']} ({_pct(q['impacted_unknown'], q['rows'])})",
                     q["touched_1"], q["touched_0"], q["touched_unknown"], q["distinct_distance"]])
    out.append(f"Feature definition version {GRAPH_FEATURE_VERSION}. `impacted` = a changed file reaches the job through "
               "File -> (Directory | BuildTarget) -> Job edges and `needs`; **unknown** = the job has no file-level evidence in its graph "
               "(e.g. it only runs `make` targets or remote actions), which is NOT the same as unaffected.\n")
    out.append(_table(["repo", "(run, job) rows", "no changed file in graph", "job has file evidence", "impacted = 1",
                       "impacted = 0", "impacted unknown", "cache touched = 1", "cache touched = 0", "cache unknown",
                       "distinct distances"], rows))
    for r in repos:
        q = qual[r]
        if q["stale_rows"]:
            out.append(f"* **{r}**: {q['stale_rows']} stored rows are from an older feature definition and are ignored "
                       "(rerun `build-dataset`).\n")
        if q["rows"] and q["impacted_unknown"] / q["rows"] > 0.5:
            out.append(f"* **{r}**: `job_structurally_impacted` is unknown for {_pct(q['impacted_unknown'], q['rows'])} of rows.\n")
        if q["rows"] and q["impacted_1"] == 0:
            out.append(f"* **{r}**: no row is impacted via a real graph path - the feature carries no information here.\n")

    out.append("### 7.1 Does `impacted` agree with what the workflow actually did?\n")
    out.append("Ground truth available without any model: whether a job was **skipped** (every matrix cell skipped) or "
               "**executed**.  A job that runs on every change is never skipped, so the all-jobs row is diluted by jobs the "
               "workflow does not gate; the second row restricts to jobs that were both skipped and executed in this repository "
               "(selected after the fact, so read it as a consistency check, not as a test of the rules).  "
               "Fisher exact p-values assume independent rows, which the data does not satisfy - they are optimistic.\n")
    rows = []
    for r in repos:
        q = qual[r]
        for scope, key in (("all jobs", "assoc_all"), (f"jobs that skip at least once ({len(q['gated_jobs'])})", "assoc_gated")):
            t = q[key]
            n1, n0 = t["i1_skipped"] + t["i1_executed"], t["i0_skipped"] + t["i0_executed"]
            if not (n1 or n0):
                continue
            rows.append([r, scope, n1, _pct(t["i1_skipped"], n1), n0, _pct(t["i0_skipped"], n0),
                         "n/a" if t["fisher_p"] is None else f"{t['fisher_p']:.2g}"])
    out.append(_table(["repo", "scope", "impacted=1 rows", "of which skipped", "impacted=0 rows", "of which skipped",
                       "Fisher p"], rows))

    out.append("### 7.2 Do the structural features associate with the labels the models predict?\n")
    out.append("Descriptive only (no model): the label rate of all labelled rows split by the feature value.  "
               "Rows are not independent (jobs of a run share a commit; consecutive runs share a PR), so differences are "
               "optimistic, and failure positives are few - read the counts, not just the percentages.\n")
    rows = []
    for r in repos:
        df = frames[r][0]
        if df.empty:
            continue
        for feat, label, what, invert in (
            ("job_structurally_impacted", "label_failed", "failure rate", False),
            ("cache_key_touched", "label_cache_hit", "cache-miss rate", True),
        ):
            cells = []
            for val, name in ((1.0, "=1"), (0.0, "=0")):
                lab = df.loc[df[feat] == val, label].dropna()
                rate = (1 - lab.mean()) if invert else lab.mean()
                cells.append(f"{100 * rate:.1f}% (n={len(lab)})" if len(lab) else "n/a")
            unk = df.loc[df[feat].isna(), label].dropna()
            urate = (1 - unk.mean()) if invert else unk.mean()
            cells.append(f"{100 * urate:.1f}% (n={len(unk)})" if len(unk) else "n/a")
            rows.append([r, what, f"`{feat}`"] + cells)
        # the exact cache key is what `cache_key_touched` describes; a restore-keys prefix fallback ("partial hit")
        # counts as a HIT in label_cache_hit, so also show the share of rows whose exact key was not restored
        known = df[df["cache_outcome"].isin(["miss", "partial_hit", "exact_hit"])]
        cells = []
        for sel in (known["cache_key_touched"] == 1, known["cache_key_touched"] == 0, known["cache_key_touched"].isna()):
            sub = known.loc[sel, "cache_outcome"]
            cells.append(f"{100 * (sub != 'exact_hit').mean():.1f}% (n={len(sub)})" if len(sub) else "n/a")
        rows.append([r, "exact key not restored (miss or partial hit)", "`cache_key_touched`"] + cells)
    out.append(_table(["repo", "label", "feature", "feature = 1", "feature = 0", "feature unknown"], rows))

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
