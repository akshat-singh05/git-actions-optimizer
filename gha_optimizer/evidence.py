"""
Read the project's generated reports (``docs/*.md``) for the dashboard.

Every number the dashboard shows as *evidence* (model vs baseline, triage, gate-relaxation replay, calibration, live test ...)
is a figure that a CLI command computed from real data and wrote into ``docs/``.  This module only locates and parses
those files: it computes nothing, so a row shown for a repository is exactly the row in the generated report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+\.md$")
_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


@dataclass
class Table:
    heading: str
    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(markdown: str) -> list[Table]:
    """Every pipe table of ``markdown`` with the nearest preceding heading."""
    lines = markdown.splitlines()
    heading = ""
    tables: list[Table] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif line.startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1].strip()):
            table = Table(heading=heading, headers=_cells(line))
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                table.rows.append(_cells(lines[i]))
                i += 1
            tables.append(table)
            continue
        i += 1
    return tables


def _mentions(cell: str, repo: str) -> bool:
    cell = cell.strip("* `")
    return cell == repo or cell.startswith(repo + " ") or cell.startswith(repo + "(")


def rows_for_repo(markdown: str, repo: str) -> list[Table]:
    """The tables of ``markdown`` that have a row for ``repo`` (first cell), reduced to that row."""
    out = []
    for t in parse_tables(markdown):
        rows = [r for r in t.rows if r and _mentions(r[0], repo)]
        if rows:
            out.append(Table(heading=t.heading, headers=t.headers, rows=rows))
    return out


def list_docs(docs_dir: Path = DOCS_DIR) -> list[str]:
    return sorted(p.name for p in docs_dir.glob("*.md") if _SAFE_NAME.match(p.name)) if docs_dir.is_dir() else []


def read_doc(name: str, docs_dir: Path = DOCS_DIR) -> str | None:
    """Text of ``docs/<name>`` or None; only plain ``NAME.md`` files directly in ``docs/`` are readable."""
    if not _SAFE_NAME.match(name):
        return None
    path = docs_dir / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


# the generated reports that carry one row per repository, in the order the dashboard shows them
REPORTS = [
    ("MODEL_EVAL.md", "Model vs baseline (rolling-origin, original protocol)"),
    ("IMPROVE_EVAL_DEV.md", "Failure model v2 vs baseline (development repositories)"),
    ("IMPROVE_EVAL_TEST.md", "Failure model v2 vs baseline (test repositories)"),
    ("POOLED_EVAL.md", "Pooled failure model (exploratory)"),
    ("CALIBRATION.md", "Calibration of the failure probabilities"),
    ("FAILURE_TRIAGE.md", "Failure triage"),
    ("GATE_REPLAY.md", "Safe gate relaxation replayed on history"),
    ("IMPACT_REPLAY.md", "Graph-based skipping replayed on history"),
    ("DATASET_REPORT.md", "Dataset"),
]


def evidence_for_repo(repo: str, docs_dir: Path = DOCS_DIR) -> list[dict]:
    """``[{report, title, tables: [{heading, headers, rows}]}]`` for the reports that mention ``repo``."""
    out = []
    for name, title in REPORTS:
        text = read_doc(name, docs_dir)
        if text is None:
            continue
        tables = rows_for_repo(text, repo)
        if tables:
            out.append({"report": name, "title": title,
                        "tables": [{"heading": t.heading, "headers": t.headers, "rows": t.rows} for t in tables]})
    return out
