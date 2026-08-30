"""Tests for the health check.

The point of `fpl.status` is to be trustworthy when something is broken, so the
cases worth testing are the broken ones.
"""

from __future__ import annotations

import pytest

from fpl import status, store as store_module


def test_missing_database_fails_with_a_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store_module, "DEFAULT_DB", tmp_path / "absent.sqlite3")
    report = status.Report()
    status.check(report)

    out = capsys.readouterr().out
    assert report.failed == 1
    assert "[FAIL]" in out
    assert "python -m fpl.collect" in out, "a failure must say what to run"


def test_empty_database_is_a_failure_not_a_pass(tmp_path, monkeypatch, capsys):
    """A file that exists but holds nothing must not read as healthy."""
    db = tmp_path / "empty.sqlite3"
    store_module.Store(db).close()          # creates the schema, no rows
    monkeypatch.setattr(store_module, "DEFAULT_DB", db)

    report = status.Report()
    status.check(report)

    assert report.failed >= 1
    assert "no snapshots" in capsys.readouterr().out


def test_exit_code_is_nonzero_only_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "DEFAULT_DB", tmp_path / "absent.sqlite3")
    assert status.main([]) == 1


def test_stale_snapshots_are_reported_as_a_failure():
    """Staleness is the signal that the schedule has stopped firing, which is
    otherwise invisible — the data still looks perfectly valid, just old."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    age = status._age(old)
    assert age is not None and age > status.STALE_AFTER

    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert status._age(fresh) < status.STALE_AFTER


def test_age_handles_the_z_suffix_and_junk():
    """FPL stamps deadlines with a Z suffix; the store writes +00:00 offsets."""
    assert status._age("2020-01-01T00:00:00Z") is not None
    assert status._age("not a date") is None
    assert status._age(None) is None


@pytest.mark.parametrize("delta,expected", [
    ((0, 30), "30m"),
    ((5, 0), "5h"),
    ((72, 0), "3.0d"),
])
def test_pretty_durations(delta, expected):
    from datetime import timedelta
    hours, minutes = delta
    assert status._pretty(timedelta(hours=hours, minutes=minutes)) == expected
