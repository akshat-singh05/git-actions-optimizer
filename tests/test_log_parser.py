"""Cache-outcome extraction from job logs.

Two layers of evidence:
* ``TestRealLogExcerpts`` parses verbatim excerpts captured from real GitHub
  Actions logs (``tests/fixtures/real_logs``; provenance in ``SOURCES.json``).
* ``TestLineFormats`` pins each recognised line format with a one-line input.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gha_optimizer.ingestion.log_parser import derive_job_label, parse_log_lines

FIXTURES = Path(__file__).parent / "fixtures" / "real_logs"
TS = "2026-10-06T18:37:36.7756485Z "


def parse(*lines: str):
    return parse_log_lines([TS + ln for ln in lines])


def kinds(parsed):
    return [(e.kind, e.restore_kind) for e in parsed.events]


class TestLineFormats:
    def test_actions_cache_miss_lists_all_input_keys(self):
        p = parse("Cache not found for input keys: pip-abc, pip-")
        assert kinds(p) == [("restore_miss", None)]
        assert p.events[0].cache_key == "pip-abc, pip-"
        assert derive_job_label(p.events).hit is False

    def test_setup_uv_miss(self):
        p = parse("No GitHub Actions cache found for key: setup-uv-2-x86_64-ubuntu-24.04-3.11")
        assert kinds(p) == [("restore_miss", None)]

    @pytest.mark.parametrize("line", ["pnpm cache is not found", "Cache is not found", "npm cache is not found."])
    def test_setup_action_miss_lines(self, line):
        assert kinds(parse(line)) == [("restore_miss", None)]

    def test_exact_hit_resolved_by_header_key(self):
        p = parse_log_lines([
            TS + "##[group]Run actions/cache@v4",
            TS + "with:",
            TS + "  key: pip-abc",
            TS + "  path: ~/.cache/pip",
            TS + "##[endgroup]",
            TS + "Cache restored successfully",
            TS + "Cache restored from key: pip-abc",
        ])
        assert p.events[0].restore_kind == "exact" and p.events[0].primary_key == "pip-abc"
        assert p.events[0].action == "actions/cache"
        lab = derive_job_label(p.events)
        assert (lab.hit, lab.outcome) == (True, "exact_hit")

    def test_partial_hit_when_matched_key_differs_from_header_key(self):
        p = parse_log_lines([
            TS + "##[group]Run actions/cache@v4",
            TS + "  key: pip-new-hash",
            TS + "##[endgroup]",
            TS + "Cache restored from key: pip-old-hash",
        ])
        assert p.events[0].restore_kind == "partial"
        assert derive_job_label(p.events).outcome == "partial_hit"

    def test_setup_action_hit_resolved_by_post_step_line(self):
        p = parse(
            "Cache restored from key: setup-go-Linux-x64-go-1.27.1-abc",
            "Cache hit occurred on the primary key setup-go-Linux-x64-go-1.27.1-abc, not saving cache.",
        )
        assert [e.restore_kind for e in p.events if e.kind == "restore_hit"] == ["exact"]

    def test_unresolvable_hit_is_reported_as_such_not_guessed(self):
        p = parse("Cache restored from key: some-key")
        assert p.events[0].restore_kind == "unresolved"
        assert derive_job_label(p.events).outcome == "hit_unresolved"

    def test_any_miss_makes_the_job_a_miss(self):
        p = parse("Cache restored from key: a", "Cache not found for input keys: b")
        assert derive_job_label(p.events).hit is False

    def test_no_cache_lines_means_null_label_not_false(self):
        p = parse("##[group]Run pytest", "collected 12 items", "Cache hit rate improved")  # prose, not a cache line
        assert p.events == []
        lab = derive_job_label(p.events)
        assert lab.hit is None and lab.outcome is None

    def test_only_a_save_line_is_not_a_restore_label(self):
        p = parse("Cache saved with key: k")
        assert kinds(p) == [("saved", None)]
        assert derive_job_label(p.events).hit is None

    def test_ansi_bom_and_timestamps_are_stripped(self):
        p = parse_log_lines(["﻿2026-10-06T18:37:36.7756485Z \x1b[36;1mCache restored from key: k\x1b[0m"])
        assert p.events and p.events[0].cache_key == "k"

    def test_step_attribution_follows_group_headers(self):
        p = parse_log_lines([
            TS + "##[group]Run actions/setup-python@v5",
            TS + "##[endgroup]",
            TS + "pip cache is not found",
            TS + "##[group]Post Run actions/cache@v4",
            TS + "##[endgroup]",
            TS + "Cache saved with key: k2",
        ])
        assert [(e.step_name, e.action) for e in p.events] == [
            ("Run actions/setup-python@v5", "actions/setup-python"),
            ("Post Run actions/cache@v4", "actions/cache"),
        ]


def test_post_step_events_are_attributed_to_the_restore_step_that_used_the_key():
    p = parse_log_lines([
        TS + "##[group]Run actions/cache@v4",
        TS + "  key: pip-abc",
        TS + "##[endgroup]",
        TS + "Cache not found for input keys: pip-abc, pip-",
        TS + "##[group]Run pytest",          # a later, unrelated step
        TS + "##[endgroup]",
        TS + "Cache saved with key: pip-abc",   # post-step line: no group header of its own
    ])
    saved = next(e for e in p.events if e.kind == "saved")
    assert saved.action == "actions/cache" and saved.step_name == "Post Run actions/cache@v4"


@pytest.mark.skipif(not (FIXTURES / "SOURCES.json").exists(), reason="real log fixtures not captured")
class TestRealLogExcerpts:
    sources = json.loads((FIXTURES / "SOURCES.json").read_text(encoding="utf-8")) if (FIXTURES / "SOURCES.json").exists() else {}

    def _parse(self, name):
        lines = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8").splitlines()
        return parse_log_lines(lines)

    @pytest.mark.parametrize("name", sorted(sources))
    def test_fixture_reproduces_the_outcome_recorded_at_capture_time(self, name):
        meta = self.sources[name]
        parsed = self._parse(name)
        assert parsed.events, f"{name}: no cache events parsed from a real log excerpt"
        assert [(e.kind, e.restore_kind) for e in parsed.events] == [tuple(k) for k in meta["kinds"]]
        assert derive_job_label(parsed.events).outcome == meta["outcome"]
