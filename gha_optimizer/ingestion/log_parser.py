"""
Cache-outcome extraction from real GitHub Actions job logs.

GitHub's REST API does not expose cache hit/miss.  It appears only in the job
log (``/actions/jobs/{id}/logs``).  This module turns a log (an iterable of
lines) into :class:`CacheEvent` records and derives a job-level label.

Recognised lines (formats verified against live logs, 2026-10-07):

========================= ================================================ ============
line                      emitted by                                       kind
========================= ================================================ ============
``Cache restored from key: K``                actions/cache, setup-python/node/go/java/uv   restore_hit
``Cache not found for input keys: A, B``      actions/cache                                 restore_miss
``No GitHub Actions cache found for key: K``  astral-sh/setup-uv                            restore_miss
``<pm> cache is not found`` / ``Cache is not found``  setup-node / setup-go / setup-java    restore_miss
``Cache saved with key: K``                   actions/cache post step                       saved
``Cache hit occurred on the primary key K, not saving cache.``  post step                   primary_hit_not_saved
========================= ================================================ ============

A ``restore-keys`` partial hit prints the same ``Cache restored from key:`` line as
an exact hit (with the *matched* key), so exact vs partial is resolved by comparing
the matched key with the step's ``key:`` input (read from the ``##[group]Run ...``
header block) and with the post-step lines.

Nothing here guesses.  A log with no cache line yields no events and therefore a
NULL label, never ``False``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

PARSER_VERSION = "2"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
_GROUP = re.compile(r"^##\[group\](.*)$")
_ACTION = re.compile(r"^(?:Post )?Run (\S+?)(?:@\S*)?$")
_HEADER_KEY = re.compile(r"^\s+key:\s*(.+?)\s*$")

_RESTORE_HIT = re.compile(r"^Cache restored from key: (.+?)\s*$")
_RESTORE_MISS_KEYS = re.compile(r"^Cache not found for input keys?: (.+?)\s*$")
_RESTORE_MISS_UV = re.compile(r"^No GitHub Actions cache found for key: (.+?)\s*$")
_RESTORE_MISS_SETUP = re.compile(r"^(?:[\w.-]+ )?[Cc]ache is not found\.?\s*$")
# Swatinem/rust-cache (used by uv): the restore step prints whether the matched key was the requested one.
_RESTORE_RUST = re.compile(r'^Restored from cache key "(.+?)" full match: (true|false)\.?\s*$')
# Printed by the same action when nothing matched (taken from the action's source; not yet seen in an ingested log).
_RESTORE_MISS_RUST = re.compile(r"^No cache found\.\s*$")
_SAVED = re.compile(r"^Cache saved with key: (.+?)\s*$")
_PRIMARY_HIT = re.compile(r"^Cache hit occurred on the primary key (.+?), not saving cache\.?\s*$")


@dataclass
class CacheEvent:
    seq: int
    line_no: int
    kind: str  # restore_hit | restore_miss | saved | primary_hit_not_saved
    cache_key: str
    step_name: str | None
    action: str | None
    primary_key: str | None = None   # 'key:' input of the step header, when printed
    restore_kind: str | None = None  # exact | partial | unresolved (restore_hit only)
    full_match: bool | None = None   # the log itself states whether the matched key was the requested one
    raw_line: str = ""


@dataclass
class ParsedLog:
    events: list[CacheEvent] = field(default_factory=list)
    n_lines: int = 0


@dataclass(frozen=True)
class JobCacheLabel:
    hit: bool | None       # None => no label (no cache restore line in the log)
    outcome: str | None    # miss | partial_hit | exact_hit | hit_unresolved | None


def clean_line(line: str) -> str:
    """Strip the ISO timestamp prefix, BOM and ANSI colour codes."""
    return _ANSI.sub("", _TS.sub("", line)).rstrip("\r\n")


def parse_log_lines(lines: Iterable[str]) -> ParsedLog:
    """Parse a job log (any iterable of raw lines) into cache events."""
    parsed = ParsedLog()
    step_title: str | None = None
    action: str | None = None
    header_key: str | None = None
    in_header = False
    seq = 0

    for line_no, raw in enumerate(lines, start=1):
        parsed.n_lines = line_no
        # Cheap pre-filter: only group markers, step headers and cache lines matter.
        if not in_header and "ache" not in raw and "##[" not in raw:
            continue
        line = clean_line(raw)

        m = _GROUP.match(line)
        if m:
            title = m.group(1).strip()
            if title.startswith("Run ") or title.startswith("Post Run ") or title in ("Set up job", "Post job cleanup."):
                step_title = title
                am = _ACTION.match(title)
                action = am.group(1) if am else None
                header_key = None
                in_header = True
            continue
        if line.startswith("##[endgroup]"):
            in_header = False
            continue
        if in_header:
            km = _HEADER_KEY.match(line)
            if km and header_key is None:
                header_key = km.group(1)
            continue

        kind: str | None = None
        key = ""
        full_match: bool | None = None
        rm = _RESTORE_RUST.match(line)
        if rm:
            kind, key, full_match = "restore_hit", rm.group(1), rm.group(2) == "true"
        for rx, k in (
            (_RESTORE_HIT, "restore_hit"),
            (_RESTORE_MISS_KEYS, "restore_miss"),
            (_RESTORE_MISS_UV, "restore_miss"),
            (_SAVED, "saved"),
            (_PRIMARY_HIT, "primary_hit_not_saved"),
        ):
            if kind is not None:
                break
            mm = rx.match(line)
            if mm:
                kind, key = k, mm.group(1)
                break
        if kind is None and (_RESTORE_MISS_SETUP.match(line) or _RESTORE_MISS_RUST.match(line)):
            kind, key = "restore_miss", ""
        if kind is None:
            continue

        seq += 1
        parsed.events.append(
            CacheEvent(
                seq=seq,
                line_no=line_no,
                kind=kind,
                cache_key=key,
                step_name=step_title,
                action=action,
                primary_key=header_key,
                full_match=full_match,
                raw_line=line[:500],
            )
        )

    _resolve_restore_kinds(parsed.events)
    return parsed


def _attribute_post_events(events: list[CacheEvent]) -> None:
    """Post-step lines (``Cache saved with key`` / ``... not saving cache``) are not preceded by
    their own ``##[group]`` header in the log, so they inherit the *previous* step.  Re-attribute
    them to the restore event that used the same key (its action), as ``Post <step>``."""
    restores = [e for e in events if e.kind in ("restore_hit", "restore_miss")]
    for e in events:
        if e.kind not in ("saved", "primary_hit_not_saved"):
            continue
        for r in restores:
            first_input_key = r.cache_key.split(",")[0].strip() if r.kind == "restore_miss" else None
            if e.cache_key in (r.primary_key, first_input_key) or (r.kind == "restore_hit" and e.cache_key == r.cache_key):
                e.action = r.action
                e.step_name = f"Post {r.step_name}" if r.step_name and not r.step_name.startswith("Post ") else r.step_name
                e.primary_key = r.primary_key
                break


def _resolve_restore_kinds(events: list[CacheEvent]) -> None:
    """Decide exact / partial / unresolved for every restore_hit event."""
    _attribute_post_events(events)
    primary_hits = {e.cache_key for e in events if e.kind == "primary_hit_not_saved"}
    saved = {e.cache_key for e in events if e.kind == "saved"}
    for e in events:
        if e.kind != "restore_hit":
            continue
        matched = e.cache_key
        if e.full_match is not None:
            e.restore_kind = "exact" if e.full_match else "partial"
        elif e.primary_key is not None:
            e.restore_kind = "exact" if matched == e.primary_key else "partial"
        elif matched in primary_hits:
            e.restore_kind = "exact"
        elif saved and matched not in saved:
            e.restore_kind = "partial"  # the post step saved a different (primary) key
        else:
            e.restore_kind = "unresolved"


def derive_job_label(events: list[CacheEvent]) -> JobCacheLabel:
    """Job-level cache label from its restore events.

    * no restore event            -> NULL (``hit=None``)
    * any restore_miss            -> miss  (``hit=False``)
    * only hits, any partial      -> partial_hit (``hit=True``)
    * only hits, all exact        -> exact_hit
    * only hits, some unresolved  -> hit_unresolved
    """
    restores = [e for e in events if e.kind in ("restore_hit", "restore_miss")]
    if not restores:
        return JobCacheLabel(None, None)
    if any(e.kind == "restore_miss" for e in restores):
        return JobCacheLabel(False, "miss")
    kinds = {e.restore_kind for e in restores}
    if "partial" in kinds:
        return JobCacheLabel(True, "partial_hit")
    if kinds == {"exact"}:
        return JobCacheLabel(True, "exact_hit")
    return JobCacheLabel(True, "hit_unresolved")
