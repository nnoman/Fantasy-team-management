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


def test_start_probability_scales_with_games_played():
    # Two games is weak evidence either way, so everything stays near the prior.
    both = features.start_probability(180, 2, 70, 1.0, games_played=2)
    one = features.start_probability(77, 1, 46, 1.0, games_played=2)
    assert 0.40 < both < 0.60
    assert one < both

    # A full season of starts is strong evidence and comes through untouched...
    nailed = features.start_probability(3200, 36, 100, 1.0, games_played=38)
    assert nailed > 0.90
    # ...and so is a full season of not starting.
    fringe = features.start_probability(200, 3, 50, 1.0, games_played=38)
    assert fringe < 0.15

    # Availability gates everything.
    assert features.start_probability(3200, 36, 100, 0.0, games_played=38) == 0.0


def test_benched_player_is_not_treated_as_unknown():
    """Regression: two games in, zero minutes means benched, not unproven.

    Before this was fixed, `minutes == 0` fell through to a price-based prior
    regardless of whether any football had been played, so a fit £7.9m forward
    with no minutes in his club's two matches scored a 0.65 start probability
    and got captained. FPL's own ep_next read 0.0 for exactly those players.
    """
    benched = features.start_probability(0, 0, cost=79, avail=1.0, games_played=2)
    playing = features.start_probability(180, 2, cost=79, avail=1.0, games_played=2)
    assert benched < playing
    assert benched < 0.35

    # Before a ball is kicked, zero minutes genuinely is no evidence, and the
    # price prior is the right fallback.
    preseason = features.start_probability(0, 0, cost=79, avail=1.0, games_played=0)
    assert preseason > benched


def test_zero_history_players_get_a_capped_prior():
    cheap = features.start_probability(0, 0, cost=40, avail=1.0, games_played=0)
    premium = features.start_probability(0, 0, cost=120, avail=1.0, games_played=0)
    assert cheap < premium <= 0.65, "unknown players must never look nailed-on"


def test_prior_season_recentness_beats_reputation():
    """A season of starting must not outvote two games of being dropped.

    This is the failure the blend exists to avoid in both directions: without a
    prior season the model has no discrimination in August, but weighting it too
    heavily reinstates the benched-premium problem under a new name.
    """
    # Started every game last season, dropped for both games this season.
    dropped = features.start_probability(
        0, 0, cost=79, avail=1.0, games_played=2, prior_start_rate=34 / 38)
    # Started every game last season and both this season.
    playing = features.start_probability(
        180, 2, cost=79, avail=1.0, games_played=2, prior_start_rate=34 / 38)
    # Never started last season, starting now.
    risen = features.start_probability(
        180, 2, cost=50, avail=1.0, games_played=2, prior_start_rate=2 / 38)

    assert dropped < playing
    assert risen > dropped, "current form must be able to overtake reputation"
    assert playing > 0.85, "a proven starter still starting should read clearly"


def test_prior_season_blend_restores_sample_size():
    rows = [{
        "code": 1, "minutes_sample": 180.0, "prior_minutes": 0.0,
        "xg90": 0.10, "xa90": 0.0, "xgc90": 0.0,
        "defcon90": 2.0, "bps90": 10.0, "saves90": 0.0, "yellow90": 0.0,
    }]
    prev = {1: {
        "minutes": 2953, "starts": 34, "expected_goals": 25.5,
        "expected_assists": 2.67, "expected_goals_conceded": 38.6,
        "defensive_contribution": 104, "bps": 952, "saves": 0, "yellow_cards": 2,
    }}
    features.blend_prior_season(rows, prev)
    row = rows[0]

    # 180 minutes becomes a usable sample, which is what stops shrink_rates
    # flattening the whole pool to its position baseline.
    assert row["minutes_sample"] > 1500
    # Last season's 0.78 xG/90 dominates this season's 180-minute 0.10.
    assert 0.5 < row["xg90"] < 0.8
    assert row["prior_minutes"] == 2953

    # A player with no history on file is left exactly as found.
    untouched = [{"code": 2, "minutes_sample": 180.0, "prior_minutes": 0.0,
                  "xg90": 0.10, "xa90": 0.0, "xgc90": 0.0, "defcon90": 2.0,
                  "bps90": 10.0, "saves90": 0.0, "yellow90": 0.0}]
    features.blend_prior_season(untouched, prev)
    assert untouched[0]["xg90"] == 0.10
    assert untouched[0]["minutes_sample"] == 180.0


def test_prior_pool_never_comes_back_empty():
    """Early in a season nobody has many minutes; an empty baseline used to
    crash shrink_rates outright."""
    early = pd.DataFrame([
        {"pos": "MID", "minutes_sample": 90, "xg90": 0.4, "xa90": 0.1, "xgc90": 1.0,
         "defcon90": 5.0, "bps90": 20.0, "saves90": 0.0, "yellow90": 0.0},
    ] * 5)
    out = features.shrink_rates(early.copy())
    assert out.xg90.notna().all()

    # And with no minutes anywhere at all (pre-season reset).
    none_played = early.assign(minutes_sample=0)
    assert features.shrink_rates(none_played.copy()).xg90.notna().all()


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


def test_clean_sheets_use_the_team_defensive_record():
    """Two defenders of identical strength rating must not get identical clean
    sheet odds when their sides concede at different rates."""
    import pandas as pd

    def defender(xgc90):
        return pd.Series({
            "pos": "DEF", "p_start": 1.0, "avail": 1.0, "strength_home": 3,
            "strength_away": 3, "xg90": 0.0, "xa90": 0.0, "xgc90": xgc90,
            "defcon90": 0.0, "bps90": 0.0, "saves90": 0.0, "yellow90": 0.0,
        })

    fx = features.Fixture(gw=1, opponent=2, is_home=True, difficulty=3)
    tight = project.project_fixture(defender(0.7), fx, 3, 3)
    leaky = project.project_fixture(defender(2.2), fx, 3, 3)
    assert tight > leaky, "the meaner defence should project more points"

    # With no record on file it falls back to the strength rating alone.
    assert project.project_fixture(defender(0.0), fx, 3, 3) > 0


def test_shrink_rates_kills_two_minute_samples():
    # This is the bug the shrinkage exists for: FPL divides a season total by
    # minutes played, so one tackle in two minutes reads as 45 per 90.
    df = pd.DataFrame([
        {"pos": "MID", "minutes_sample": 2, "xg90": 3.6, "xa90": 0.0, "xgc90": 0.0,
         "defcon90": 45.0, "bps90": 225.0, "saves90": 0.0, "yellow90": 0.0},
        {"pos": "MID", "minutes_sample": 3000, "xg90": 0.30, "xa90": 0.25, "xgc90": 1.1,
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
    assert 3.0 < df.xp_next.max() < 12.0
    assert df.xp_next.mean() < 3.0


@pytest.mark.xfail(
    reason="Known gap, not a flake. With only two gameweeks of data every rate "
           "shrinks to its position baseline, so the model has little "
           "discrimination and trails FPL's ep_next (~0.39 correlation against "
           "0.80 pre-season, when bootstrap still carried a full prior season). "
           "The fix is enriching from element-summary history_past rather than "
           "loosening this bound. Until it passes, the model must not be "
           "trusted with transfers.",
    strict=False,
)
def test_model_agrees_with_the_fpl_baseline(projected):
    df, _ = projected
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
    # FPL rating a player at 0.0 expected points while calling him available is a
    # signal its status letter does not carry; those players stay out too.
    assert not squad.players.fpl_writeoff.any()


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

    # Constraining the solver can only ever cost objective value. Note this is
    # the objective, not xp_next: forcing a premium in can raise next week's XI
    # score while costing bench cover and horizon value, so xp_next is free to
    # go either way.
    free = optimise.best_squad(df, rules)
    assert forced.objective <= free.objective + 1e-6
    assert banned.objective <= free.objective + 1e-6


def test_best_xi_rejects_a_partial_squad(projected):
    df, rules = projected
    with pytest.raises(ValueError, match="full squad"):
        optimise.best_xi(df, list(df.index[:11]), rules)
