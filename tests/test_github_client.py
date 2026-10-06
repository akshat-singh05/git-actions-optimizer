"""Unit tests for the real GitHub client: rate limits, retries, pagination, windowing.

The HTTP layer is a scripted fake (test double for the *transport*, not for any
value the models consume); live behaviour was verified separately against
api.github.com.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from gha_optimizer.ingestion.github_client import (
    LISTING_CAP,
    GithubAPIError,
    GithubClient,
    LogUnavailable,
    parse_ts,
    seconds_between,
)


class FakeResp:
    def __init__(self, status=200, data=None, headers=None, text="", links=None, lines=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text
        self.links = links or {}
        self._lines = lines or []
        self.closed = False

    def json(self):
        return self._data

    def iter_lines(self, chunk_size=0):
        yield from (ln.encode() for ln in self._lines)

    def close(self):
        self.closed = True


class ScriptedSession:
    """Returns responses (or raises exceptions) from a script, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.calls.append((url, dict(params or {})))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(script, **kw):
    sleeps = []
    session = ScriptedSession(script)
    client = GithubClient(
        token="t", repo="o/r", session=session, sleep_fn=sleeps.append, clock=lambda: 1000.0, **kw
    )
    return client, session, sleeps


# --------------------------------------------------------------------------- rate limits

def test_primary_rate_limit_sleeps_until_reset_then_succeeds():
    client, session, sleeps = make_client([
        FakeResp(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}, text="API rate limit exceeded"),
        FakeResp(200, data={"ok": 1}),
    ])
    assert client.get_json("/x") == {"ok": 1}
    assert len(sleeps) == 1 and 30 <= sleeps[0] <= 32          # reset(1030) - now(1000) + 1
    assert client.stats["rate_limit_waits_primary"] == 1


def test_secondary_rate_limit_uses_retry_after():
    client, _, sleeps = make_client([
        FakeResp(403, headers={"Retry-After": "17"}, text="secondary rate limit"),
        FakeResp(200, data={}),
    ])
    client.get_json("/x")
    assert sleeps == [18.0]
    assert client.stats["rate_limit_waits_secondary"] == 1


def test_secondary_rate_limit_without_headers_backs_off_exponentially():
    client, _, sleeps = make_client([
        FakeResp(403, text="You have exceeded a secondary rate limit"),
        FakeResp(403, text="You have exceeded a secondary rate limit"),
        FakeResp(200, data={}),
    ], secondary_backoff=10.0)
    client.get_json("/x")
    assert sleeps == [10.0, 20.0]


def test_proactive_sleep_when_budget_below_floor():
    client, _, sleeps = make_client([
        FakeResp(200, data={}, headers={"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "1060"}),
        FakeResp(200, data={}),
    ], rate_limit_floor=25)
    client.get_json("/a")
    assert sleeps == []
    client.get_json("/b")
    assert len(sleeps) == 1 and 60 <= sleeps[0] <= 62           # slept before the 2nd request


def test_plain_403_is_not_retried():
    client, session, _ = make_client([FakeResp(403, headers={"X-RateLimit-Remaining": "4000"}, text="Resource not accessible")])
    with pytest.raises(GithubAPIError) as ei:
        client.get_json("/x")
    assert ei.value.status == 403 and len(session.calls) == 1


# --------------------------------------------------------------------------- retries

def test_5xx_is_retried_with_backoff_then_succeeds():
    client, session, sleeps = make_client([FakeResp(502), FakeResp(503), FakeResp(200, data={"v": 1})])
    assert client.get_json("/x") == {"v": 1}
    assert sleeps == [2.0, 4.0] and client.stats["retries"] == 2


def test_5xx_gives_up_after_max_retries():
    client, _, _ = make_client([FakeResp(500) for _ in range(10)], max_retries=3)
    with pytest.raises(GithubAPIError):
        client.get_json("/x")


def test_network_errors_are_retried():
    class ConnectionError(Exception):  # name-matched, like requests.exceptions.ConnectionError
        pass

    client, _, sleeps = make_client([ConnectionError("dns"), FakeResp(200, data={"v": 2})])
    assert client.get_json("/x") == {"v": 2}
    assert sleeps == [2.0]


def test_404_in_ok_statuses_returns_none():
    client, _, _ = make_client([FakeResp(404)])
    assert client.get_json("/x", ok_statuses=(404,)) is None


# --------------------------------------------------------------------------- pagination

def test_paginate_follows_link_header_not_page_size():
    client, session, _ = make_client([
        FakeResp(200, data={"jobs": [1, 2]}, links={"next": {"url": "https://api.github.com/p2"}}),
        FakeResp(200, data={"jobs": [3]}),
    ])
    assert list(client.paginate("/p1", "jobs")) == [1, 2, 3]
    assert session.calls[1][0] == "https://api.github.com/p2"   # followed the Link url verbatim


def test_get_commit_merges_file_pages():
    client, _, _ = make_client([
        FakeResp(200, data={"sha": "abc", "files": [{"filename": "a"}]}, links={"next": {"url": "https://api.github.com/c2"}}),
        FakeResp(200, data={"files": [{"filename": "b"}]}),
    ])
    commit = client.get_commit("abc")
    assert [f["filename"] for f in commit["files"]] == ["a", "b"]
    assert commit["files_truncated"] is False


def test_get_commit_missing_returns_none():
    client, _, _ = make_client([FakeResp(404)])
    assert client.get_commit("deadbeef") is None


# --------------------------------------------------------------------------- run windows

class FakeRunServer:
    """Behaves like the measured API: created-window filter, per_page paging, and
    NO results beyond LISTING_CAP per query (page 11 is empty)."""

    def __init__(self, runs, drop_ids=()):
        self.runs = runs
        self.drop = set(drop_ids)
        self.queries = 0

    def get(self, url, params=None, timeout=None, stream=False):
        params = params or {}
        lo_s, hi_s = params["created"].split("..")
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        lo, hi = datetime.strptime(lo_s, fmt), datetime.strptime(hi_s, fmt)
        hits = [r for r in self.runs if lo <= datetime.strptime(r["created_at"], fmt) <= hi]
        total = len(hits)
        per_page, page = params.get("per_page", 30), params.get("page", 1)
        visible = [r for r in hits if r["id"] not in self.drop][:LISTING_CAP]
        chunk = visible[(page - 1) * per_page: page * per_page]
        self.queries += 1
        return FakeResp(200, data={"total_count": total, "workflow_runs": chunk})


def _runs(n, start=datetime(2026, 9, 1)):
    return [
        {"id": i, "created_at": (start + timedelta(seconds=i * 20)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for i in range(1, n + 1)
    ]


def test_collect_runs_bisects_past_the_listing_cap_and_is_complete():
    runs = _runs(2300)  # 2300 runs inside ~12.8 hours -> > 1000 in the whole-day window
    client = GithubClient(token="t", repo="o/r", session=FakeRunServer(runs), sleep_fn=lambda s: None)
    window = client.collect_runs(datetime(2026, 9, 1), datetime(2026, 9, 1, 23, 59, 59))
    assert window.total_count == 2300
    assert len({r["id"] for r in window.runs}) == 2300
    assert window.complete is True


def test_collect_runs_flags_an_inconsistent_listing_as_incomplete():
    runs = _runs(40)
    server = FakeRunServer(runs, drop_ids={5, 6})  # the API claims 40 but only serves 38
    client = GithubClient(token="t", repo="o/r", session=server, sleep_fn=lambda s: None)
    window = client.collect_runs(datetime(2026, 9, 1), datetime(2026, 9, 1, 23, 59, 59))
    assert window.complete is False and len(window.runs) == 38
    assert client.stats["incomplete_windows"] == 1 and client.stats["listing_retries"] >= 1


def test_collect_runs_empty_window():
    client = GithubClient(token="t", repo="o/r", session=FakeRunServer([]), sleep_fn=lambda s: None)
    window = client.collect_runs(datetime(2026, 9, 1), datetime(2026, 9, 2))
    assert window.runs == [] and window.complete is True and window.total_count == 0


# --------------------------------------------------------------------------- logs

def test_expired_log_raises_log_unavailable():
    client, _, _ = make_client([FakeResp(410)])
    with pytest.raises(LogUnavailable) as ei:
        client.open_job_log(123)
    assert ei.value.status == 410


def test_log_stream_yields_lines_and_counts_bytes():
    client, _, _ = make_client([FakeResp(200, lines=["alpha", "beta"])])
    stream = client.open_job_log(1)
    assert list(stream) == ["alpha", "beta"] and stream.bytes == len("alpha") + len("beta") + 2


# --------------------------------------------------------------------------- timestamps

def test_durations_come_from_api_timestamps_and_none_when_missing():
    assert seconds_between("2026-10-01T10:00:00Z", "2026-10-01T10:01:30Z") == 90.0
    assert seconds_between("2026-10-01T10:00:00Z", None) is None
    assert seconds_between(None, None) is None
    assert parse_ts("garbage") is None
