"""Tests for the projection and optimisation layer.

The solver tests run against the real snapshot database because that is the only
place a realistic 595-player pool exists, and the constraint checks are exactly
the ones an invalid squad would violate. They skip cleanly if no snapshot has
been collected yet.
"""

from __future__ import annotations

import math
from collections import Counter

import pandas as pd
import pytest

from fpl import features, optimise, project
from fpl.store import Store


# --- unit: no database needed -------------------------------------------------

def test_availability_reads_chance_before_status():
    # An explicit chance_of_playing overrides the status letter, because FPL only
    # sets it when it has something specific to say.
    assert features.availability("d", 25) == 0.25
    assert features.availability("a", None) == 1.0
    assert features.availability("i", None) == 0.0
    assert features.availability("s", None) == 0.0
    assert features.availability(None, None) == 1.0


def test_start_probability_shrinks_small_samples():
    # 3 starts in a season is not an 8% starter — the sample is too small to say.
    tiny = features.start_probability(minutes=200, starts=3, cost=50, avail=1.0)
    assert 0.30 < tiny < 0.40
    # A full season of starts should come through nearly untouched.
    nailed = features.start_probability(minutes=3200, starts=36, cost=100, avail=1.0)
    assert nailed > 0.90
    # Availability gates everything.
    assert features.start_probability(3200, 36, 100, avail=0.0) == 0.0


def test_zero_history_players_get_a_capped_prior():
    cheap = features.start_probability(0, 0, cost=40, avail=1.0)
    premium = features.start_probability(0, 0, cost=120, avail=1.0)
    assert cheap < premium <= 0.65, "unknown players must never look nailed-on"


def test_poisson_at_least_matches_hand_calculation():
    assert project.poisson_at_least(0, 5.0) == 1.0
    assert project.poisson_at_least(3, 0.0) == 0.0
    # P(X >= 1) = 1 - e^-lam
    assert project.poisson_at_least(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    # Monotone in lambda, and a 10-count threshold is genuinely unlikely at a
    # low rate — this is what stops cheap defenders inheriting free defcon points.
    assert project.poisson_at_least(10, 3.0) < project.poisson_at_least(10, 9.0)
    assert project.poisson_at_least(10, 3.0) < 0.01


def test_team_goal_rates_respect_strength_and_venue():
    strong_home, strong_conceded = project.team_goal_rates(5, 2, is_home=True)
    weak_home, weak_conceded = project.team_goal_rates(2, 5, is_home=True)
    assert strong_home > weak_home
    assert strong_conceded < weak_conceded
    # Same fixture, away instead of home, must score less.
    away, _ = project.team_goal_rates(5, 2, is_home=False)
    assert away < strong_home


def test_shrink_rates_kills_two_minute_samples():
    # This is the bug the shrinkage exists for: FPL divides a season total by
    # minutes played, so one tackle in two minutes reads as 45 per 90.
    df = pd.DataFrame([
        {"pos": "MID", "minutes_last": 2, "xg90": 3.6, "xa90": 0.0, "xgc90": 0.0,
         "defcon90": 45.0, "bps90": 225.0, "saves90": 0.0, "yellow90": 0.0},
        {"pos": "MID", "minutes_last": 3000, "xg90": 0.30, "xa90": 0.25, "xgc90": 1.1,
         "defcon90": 6.0, "bps90": 25.0, "saves90": 0.0, "yellow90": 0.1},
    ])
    out = features.shrink_rates(df.copy())
    fluke, real = out.iloc[0], out.iloc[1]
    assert fluke.xg90 < 0.5, "a 2-minute sample must not out-project a season"
    assert fluke.defcon90 < 10
    assert fluke.bps90 < 40
    # The established player is barely moved.
    assert real.xg90 == pytest.approx(0.30, abs=0.05)


# --- integration: needs a collected snapshot ---------------------------------

@pytest.fixture(scope="module")
def projected():
    store = Store()
    try:
        if store.snapshot_count() == 0:
            pytest.skip("no snapshots collected — run `python -m fpl.collect` first")
        gw = store.next_gameweek()
        first = gw["id"] if gw else 1
        df = features.build(store)
        horizon = features.fixture_horizon(store, first, 5)
        yield project.project(df, horizon, first, 5), store.rules()
    finally:
        store.close()


def test_projection_is_in_a_believable_range(projected):
    df, _ = projected
    assert (df.xp_next >= 0).all(), "negative expected points is never right"
    # The best player in the game is worth a handful of points, not dozens.
    assert 4.0 < df.xp_next.max() < 12.0
    assert df.xp_next.mean() < 3.0
    # And it should broadly agree with FPL's own estimate.
    assert df.xp_next.corr(df.ep_next) > 0.6


def test_suggested_squad_is_legal(projected):
    df, rules = projected
    squad = optimise.best_squad(df, rules)

    assert len(squad.players) == rules["squad_size"]
    assert Counter(squad.players.pos) == Counter(optimise.SQUAD_SHAPE)
    assert squad.cost <= rules["budget"]
    assert max(Counter(squad.players.team).values()) <= rules["team_limit"]

    xi = Counter(squad.starting.pos)
    assert sum(xi.values()) == rules["starting"]
    for pos, low in optimise.MIN_PLAY.items():
        assert low <= xi[pos] <= optimise.MAX_PLAY[pos]

    assert squad.captain in squad.starting.index
    assert squad.vice != squad.captain
    assert len(squad.bench_order) == rules["squad_size"] - rules["starting"]
    # A benched goalkeeper can only ever replace the other goalkeeper, so it goes last.
    assert df.loc[squad.bench_order[-1], "pos"] == "GKP"
    assert (squad.players.avail > 0).all(), "never buy a flagged-out player by default"


def test_forced_and_banned_players_are_honoured(projected):
    df, rules = projected
    haaland = df[df.name.str.contains("Haaland", case=False)].index
    if not len(haaland):
        pytest.skip("player pool has changed")
    pid = int(haaland[0])

    forced = optimise.best_squad(df, rules, forced={pid})
    assert pid in forced.players.index

    banned = optimise.best_squad(df, rules, banned={pid})
    assert pid not in banned.players.index
    # Forcing a specific player can only ever cost expected points.
    assert forced.xp_next <= optimise.best_squad(df, rules).xp_next + 1e-6


def test_best_xi_rejects_a_partial_squad(projected):
    df, rules = projected
    with pytest.raises(ValueError, match="full squad"):
        optimise.best_xi(df, list(df.index[:11]), rules)
