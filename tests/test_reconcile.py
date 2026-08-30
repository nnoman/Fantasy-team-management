"""Tests for scoring predictions against results.

The guard against scoring an unplayed gameweek gets the most attention here,
because that failure is silent and produces a confident wrong answer rather than
an error.
"""

from __future__ import annotations

import pytest

from fpl import reconcile
from fpl.store import Store


class FakeClient:
    """Stands in for the API so these tests never touch the network."""

    def __init__(self, elements=None):
        self.elements = elements if elements is not None else [
            {"id": 1, "stats": {"minutes": 90, "total_points": 8}},
            {"id": 2, "stats": {"minutes": 0, "total_points": 0}},
        ]
        self.calls = 0

    def event_live(self, gw):
        self.calls += 1
        return {"elements": self.elements}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite3")
    yield s
    s.close()


def _gameweek(store, gw, finished, data_checked=True):
    store.save_gameweeks([{
        "id": gw, "name": f"Gameweek {gw}", "deadline_time": "2026-09-04T17:30:00Z",
        "is_current": False, "is_next": False,
        "finished": finished, "data_checked": data_checked,
    }])
    store.conn.commit()


def test_refuses_to_score_an_unfinished_gameweek(store):
    """event/{gw}/live/ returns a full, plausible payload for a gameweek that has
    not happened -- before the 2026/27 season it served last season's GW1, 610
    players with 90-minute performances. Scoring that would have graded the model
    against the wrong football and reported a confident, fictitious accuracy."""
    _gameweek(store, 3, finished=False)
    client = FakeClient()

    with pytest.raises(RuntimeError, match="not finished"):
        reconcile.fetch_actuals(store, 3, client)

    assert client.calls == 0, "must not even fetch before checking the flag"


def test_refuses_a_gameweek_it_has_never_seen(store):
    with pytest.raises(RuntimeError, match="not in the database"):
        reconcile.fetch_actuals(store, 9, FakeClient())


def test_scores_a_finished_gameweek(store):
    _gameweek(store, 1, finished=True)
    assert reconcile.fetch_actuals(store, 1, FakeClient()) == 2

    rows = {r["element_id"]: r for r in
            store.conn.execute("SELECT * FROM actual WHERE gw = 1")}
    assert rows[1]["total_points"] == 8
    assert rows[2]["minutes"] == 0


def test_scoring_without_a_recorded_forecast_is_an_error(store):
    """Predictions cannot be back-filled, so this has to fail loudly rather than
    quietly reporting perfect or empty accuracy."""
    _gameweek(store, 1, finished=True)
    reconcile.fetch_actuals(store, 1, FakeClient())

    with pytest.raises(RuntimeError, match="cannot be back-filled"):
        reconcile.score(store, 1)


def test_error_metrics_and_baseline_comparison(store, monkeypatch):
    _gameweek(store, 1, finished=True)
    reconcile.fetch_actuals(store, 1, FakeClient())
    store.save_predictions(1, 1, [(1, 6.0, 4.0, 0.9), (2, 1.0, 3.0, 0.3)])

    # score() reads positions and names from the snapshot the forecast was made
    # against; there is no snapshot here, so stub those lookups out.
    monkeypatch.setattr(Store, "players", lambda self, sid: [])
    result = reconcile.score(store, 1)

    assert result["scored"] == 2
    # Ours: |6-8| + |1-0| = 3, over 2 players.
    assert result["model_mae"] == pytest.approx(1.5)
    # FPL's: |4-8| + |3-0| = 7, over 2.
    assert result["baseline_mae"] == pytest.approx(3.5)
    assert result["model_mae"] < result["baseline_mae"]

    worst = max(result["rows"], key=lambda r: abs(r["error"]))
    assert worst["id"] == 1 and worst["error"] == pytest.approx(2.0)


def test_only_the_latest_forecast_is_scored(store):
    """Both a D-26h and a D-4h forecast are kept; the later one is what would
    have been acted on."""
    _gameweek(store, 1, finished=True)
    store.save_predictions(1, 1, [(1, 2.0, 4.0, 0.5)], made_at="2026-09-03T15:30:00+00:00")
    store.save_predictions(1, 1, [(1, 6.0, 4.0, 0.9)], made_at="2026-09-04T13:30:00+00:00")

    latest = store.latest_predictions(1)
    assert len(latest) == 1
    assert latest[0]["xp"] == 6.0, "the D-4h forecast supersedes the D-26h one"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM prediction WHERE gw = 1").fetchone()[0] == 2,         "both forecasts stay on file so they can be compared later"


def test_rerunning_a_forecast_replaces_rather_than_duplicates(store):
    """Two runs in the same second are the same forecast run twice, not two
    forecasts, so the second replaces the first."""
    _gameweek(store, 1, finished=True)
    stamp = "2026-09-04T13:30:00+00:00"
    store.save_predictions(1, 1, [(1, 2.0, 4.0, 0.5)], made_at=stamp)
    store.save_predictions(1, 1, [(1, 6.0, 4.0, 0.9)], made_at=stamp)

    rows = store.latest_predictions(1)
    assert len(rows) == 1 and rows[0]["xp"] == 6.0
