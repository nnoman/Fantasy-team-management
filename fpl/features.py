"""Feature assembly: one row per player, plus their fixture horizon.

Everything here is derived from the latest snapshot and the fixture list, both
public. No credentials, no network — it reads the database the collector fills.

A note on what the numbers mean before GW1. bootstrap-static carries *last
season's* aggregates until the new season starts (Haaland shows 2953 minutes and
27 goals in a season that has not kicked off). So the per-90 rates below are real
and usable, but they describe last season's player at last season's club. 195 of
595 players have zero minutes — promoted-club players, new signings, youth — and
for those we have no history at all and fall back to a price-based prior. That is
the single largest source of error in a GW1 projection and it is honestly
unfixable until real minutes exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from .store import Store

# Squad rules read from the API would be better, but these are stable and the
# optimiser needs them as plain numbers.
POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# How many games a player could have appeared in. Before a ball is kicked,
# bootstrap-static carries LAST season's aggregates and this is a full season;
# once the season starts it resets and counts only what has been played. Getting
# this wrong is silent and severe: dividing 2 starts by 38 games reads a
# nailed-on starter as a 5% one.
SEASON_GAMES = 38

# Empirical-Bayes shrinkage strength for per-90 rates, in minutes. A player is
# trusted on his own numbers roughly in proportion to minutes/(minutes + K), so
# at K minutes he is weighted half his own rate and half his position's.
SHRINK_MINUTES = 600

# Only players with real game time define what "normal" looks like for a
# position: 60% of the minutes available so far. In August that is one full
# match, not nine.
PRIOR_MINUTES_SHARE = 0.60

# If that leaves too few players to average over, the threshold is dropped
# rather than left to produce a meaningless or empty baseline.
PRIOR_MIN_PLAYERS = 20

# Rates that are meaningless in tiny samples and so get shrunk.
SHRUNK_RATES = ("xg90", "xa90", "xgc90", "defcon90", "bps90", "saves90", "yellow90")

# How much of last season to carry into this one. Last season is strong evidence
# about a player's *rates* -- how often he shoots, how many defensive actions he
# racks up -- and much weaker evidence about his *current role*, which a transfer
# or a new manager can change overnight. So the two are weighted differently:
# rates pool last season's minutes at a discount, while start probability lets
# last season stand in for only a few matches, and current-season evidence
# overtakes it quickly.
PRIOR_SEASON_RATE_WEIGHT = 0.6
PRIOR_SEASON_GAMES_EQUIV = 2


@dataclass(frozen=True)
class Fixture:
    """One upcoming match from one team's point of view."""
    gw: int
    opponent: int
    is_home: bool
    difficulty: int


def _raw_num(raw: dict, key: str, default: float = 0.0) -> float:
    v = raw.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fixture_horizon(store: Store, first_gw: int, horizon: int) -> dict[int, list[Fixture]]:
    """Map team id -> its fixtures over the horizon, ordered by gameweek.

    A team can appear twice in one gameweek (double gameweek) or not at all
    (blank), and both cases matter enormously for planning, so this returns a
    list rather than one fixture per week.
    """
    out: dict[int, list[Fixture]] = {}
    for f in store.fixtures(first_gw, first_gw + horizon - 1):
        if f["event"] is None:
            continue
        out.setdefault(f["team_h"], []).append(
            Fixture(f["event"], f["team_a"], True, f["team_h_difficulty"] or 3)
        )
        out.setdefault(f["team_a"], []).append(
            Fixture(f["event"], f["team_h"], False, f["team_a_difficulty"] or 3)
        )
    for fixtures in out.values():
        fixtures.sort(key=lambda x: x.gw)
    return out


def availability(status: str | None, chance: float | None) -> float:
    """Probability the player is available to be selected at all.

    `chance_of_playing_next_round` is authoritative when FPL sets it, but it is
    null for the ~95% of players with no news, so status carries the rest.
    Most FPL points are lost to fielding someone who was never going to play.
    """
    if chance is not None:
        return max(0.0, min(1.0, chance / 100.0))
    return {
        "a": 1.00,   # available
        "d": 0.60,   # doubtful, but FPL did not quantify it
        "i": 0.0,    # injured
        "s": 0.0,    # suspended
        "u": 0.0,    # unavailable (left the club, not registered)
        "n": 0.0,    # on loan / ineligible
    }.get((status or "a").lower(), 0.5)


def start_probability(minutes: float, starts: float, cost: int, avail: float,
                      games_played: int = SEASON_GAMES,
                      prior_start_rate: float | None = None) -> float:
    """P(starts) from workload so far, shrunk toward a weak prior.

    `games_played` is how many matches this player's club has actually played,
    so the rate means the same thing in August as in April. Shrinkage matters
    because 3 starts in 38 games is not an 8% starter and 2 starts in 2 games is
    not a certainty — both samples are read against how much they could prove.
    The pull is toward 0.35, roughly a squad player.
    """
    games = max(1, games_played)
    if games_played <= 0:
        # Nothing has been played yet, so zero minutes really does mean "no
        # evidence". Price is the only signal, and a weak one — FPL prices new
        # arrivals on reputation. Capped low so the optimiser cannot load up on
        # unknowns it has no case for.
        prior = 0.30 + min(0.35, max(0.0, (cost - 45) / 100.0))
        return prior * avail

    # Once games have been played, zero minutes is evidence, not the absence of
    # it: a fit player with no minutes across his club's matches is being left
    # out, and FPL's own ep_next reads exactly 0.0 for those players. Treating
    # that as "unknown, fall back to price" is how a £7.9m benched forward ends
    # up captained.
    observed = min(1.0, starts / games)

    if prior_start_rate is not None:
        # Last season stands in for a handful of matches, no more. Two games of
        # being dropped should outweigh a season of starting, because that is
        # usually what a changed role looks like from the outside.
        # No second shrink toward the 0.35 prior here: the blend is already a
        # shrunk estimate, and applying both compressed the whole pool toward
        # the middle — which is how a dropped ex-starter came out ahead of
        # someone actually in the team.
        w = games / (games + PRIOR_SEASON_GAMES_EQUIV)
        blended = w * observed + (1 - w) * min(1.0, prior_start_rate)
        return min(1.0, blended) * avail

    # No prior season on file: weight on what we could have observed, not on
    # what the player played, so a benched player reads as benched rather than
    # merely uncertain.
    weight = min(1.0, (games * 90) / (90 * 9))
    shrunk = weight * observed + (1 - weight) * 0.35
    return min(1.0, shrunk) * avail


def blend_prior_season(rows: list[dict], prev: dict) -> None:
    """Pool last season's totals into this season's rates, in place.

    Each rate is a minutes-weighted average of the two seasons, so a player with
    180 minutes this year and 3000 last year is read almost entirely off last
    year, and by spring the current season dominates on its own. `minutes_sample`
    grows to match, which is what stops shrink_rates from flattening everyone to
    the position baseline in August.
    """
    for row in rows:
        past = prev.get(row["code"])
        if past is None or not past["minutes"]:
            continue
        prev_min = float(past["minutes"])
        weight = prev_min * PRIOR_SEASON_RATE_WEIGHT
        cur_min = row["minutes_sample"]
        total = cur_min + weight
        if total <= 0:
            continue

        def per90(field, scale=1.0):
            value = past[field]
            return (float(value) * scale * 90.0 / prev_min) if value else 0.0

        for col, field in (("xg90", "expected_goals"),
                           ("xa90", "expected_assists"),
                           ("xgc90", "expected_goals_conceded"),
                           ("defcon90", "defensive_contribution"),
                           ("bps90", "bps"),
                           ("saves90", "saves"),
                           ("yellow90", "yellow_cards")):
            row[col] = (cur_min * row[col] + weight * per90(field)) / total

        row["minutes_sample"] = total
        row["prior_minutes"] = prev_min


def _prior_pool(df: pd.DataFrame) -> pd.DataFrame:
    """The players whose rates define a position baseline.

    Falls back down the sample-size ladder rather than returning an empty frame:
    early in a season nobody has many minutes, and an empty baseline turns every
    projection into a crash or a zero.
    """
    ceiling = df.minutes_sample.max()
    if ceiling <= 0:
        return df
    for share in (PRIOR_MINUTES_SHARE, 0.3, 0.0):
        pool = df[df.minutes_sample >= ceiling * share]
        pool = pool[pool.minutes_sample > 0]
        if len(pool) >= PRIOR_MIN_PLAYERS:
            return pool
    return df[df.minutes_sample > 0] if (df.minutes_sample > 0).any() else df


def shrink_rates(df: pd.DataFrame, k: float = SHRINK_MINUTES) -> pd.DataFrame:
    """Pull every per-90 rate toward its position's baseline, by sample size.

    This is not a refinement, it is load-bearing. FPL computes per-90 figures by
    dividing a season total by minutes played with no regard for sample size, so
    a midfielder who played two minutes and won a tackle shows 45 defensive
    contributions per 90 and 225 BPS per 90. Projected naively he outscores
    Haaland. Weighting each player's own rate against his position's baseline in
    proportion to minutes played fixes it, and leaves anyone with a real season
    behind them essentially untouched.
    """
    established = _prior_pool(df)
    for col in SHRUNK_RATES:
        # Minutes-weighted position baseline: the rate of a typical starter.
        priors = established.groupby("pos").apply(
            lambda g, c=col: (
                (g[c] * g.minutes_sample).sum() / g.minutes_sample.sum()
                if g.minutes_sample.sum() > 0 else 0.0
            ),
            include_groups=False,
        )
        if isinstance(priors, pd.DataFrame):        # empty groupby yields a frame
            priors = pd.Series(dtype=float)
        prior = df.pos.map(priors).astype(float).fillna(0.0)
        m = df.minutes_sample
        df[col + "_raw"] = df[col]
        df[col] = (m * df[col] + k * prior) / (m + k)
    return df


def build(store: Store, snapshot_id: int | None = None) -> pd.DataFrame:
    """One row per player with everything the projection model reads."""
    sid = snapshot_id or store.latest_snapshot_id()
    teams = {t["team_id"]: t for t in store.teams(sid)}
    # Zero once the season resets and before any fixture finishes; in that window
    # bootstrap still carries last season's totals, so a full season is right.
    games = store.team_games_played()
    default_games = max(games.values()) if games else SEASON_GAMES
    prev = store.previous_season()

    rows = []
    for p in store.players(sid):
        raw = json.loads(p["raw"])
        past = prev.get(raw["code"])
        prior_starts = (
            past["starts"] / SEASON_GAMES
            if past and past["starts"] is not None and past["minutes"]
            else None
        )
        minutes = float(p["minutes"] or 0)
        starts = float(p["starts"] or 0)
        avail = availability(p["status"], p["chance_next"])
        per90 = (90.0 / minutes) if minutes > 0 else 0.0

        rows.append({
            "id": p["element_id"],
            "code": raw["code"],
            "name": p["web_name"],
            "team": p["team"],
            "team_short": teams[p["team"]]["short_name"] if p["team"] in teams else "?",
            "pos": POS.get(p["element_type"], "?"),
            "element_type": p["element_type"],
            "cost": p["now_cost"],                       # tenths of a million
            "status": p["status"],
            "news": p["news"] or "",
            "avail": avail,
            "p_start": start_probability(
                minutes, starts, p["now_cost"], avail,
                games.get(p["team"], default_games),
                prior_start_rate=prior_starts,
            ),
            "games_played": games.get(p["team"], default_games),
            "minutes_sample": minutes,
            "prior_minutes": 0.0,
            "ep_next": p["ep_next"] or 0.0,              # the baseline to beat
            # FPL writing a player off entirely. It knows things `status` does
            # not: these players show minutes played alongside total_points of
            # 0, which is arithmetically impossible under FPL's own scoring
            # (82 minutes is worth at least 2 appearance points), so something
            # has changed about their eligibility that the status letter has not
            # caught up with. Used as a safety rail in the optimiser, never as an
            # input to the projection -- ep_next is the benchmark, and feeding it
            # into the model being benchmarked would make the comparison
            # meaningless.
            "fpl_writeoff": (p["ep_next"] or 0.0) <= 0.0,
            "owned_pct": p["selected_by_percent"] or 0.0,
            # Rates. Per-90 fields come straight from FPL where they exist;
            # bps and saves are totals, so they are converted here.
            "xg90": p["xg90"] or 0.0,
            "xa90": p["xa90"] or 0.0,
            "xgc90": p["xgc90"] or 0.0,
            "defcon90": p["defcon90"] or 0.0,
            "bps90": (p["bps"] or 0) * per90,
            "saves90": _raw_num(raw, "saves") * per90,
            "yellow90": _raw_num(raw, "yellow_cards") * per90,
            "ppg_last": _raw_num(raw, "points_per_game"),
            # Set-piece duty: durable, and worth real points.
            "pens": _raw_num(raw, "penalties_order", 0) or 0,
            "corners": _raw_num(raw, "corners_and_indirect_freekicks_order", 0) or 0,
            "freekicks": _raw_num(raw, "direct_freekicks_order", 0) or 0,
            "strength_home": teams[p["team"]]["strength_overall_home"] if p["team"] in teams else 3,
            "strength_away": teams[p["team"]]["strength_overall_away"] if p["team"] in teams else 3,
        })

    blend_prior_season(rows, prev)
    df = pd.DataFrame(rows).set_index("id", drop=False)
    return shrink_rates(df)
