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

# A full season of starts, used to turn last season's minutes into a rate.
SEASON_GAMES = 38
MINUTES_FULL = SEASON_GAMES * 90

# Empirical-Bayes shrinkage strength for per-90 rates, in minutes. A player is
# trusted on his own numbers roughly in proportion to minutes/(minutes + K), so
# at K minutes he is weighted half his own rate and half his position's.
SHRINK_MINUTES = 600

# Only established players define what "normal" looks like for a position.
PRIOR_MIN_MINUTES = 900

# Rates that are meaningless in tiny samples and so get shrunk.
SHRUNK_RATES = ("xg90", "xa90", "xgc90", "defcon90", "bps90", "saves90", "yellow90")


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


def start_probability(minutes: float, starts: float, cost: int, avail: float) -> float:
    """P(starts) from last season's workload, shrunk toward a weak prior.

    Shrinkage matters because a player with 3 starts in 38 games is not a 8%
    starter this season — the sample is tiny and the club may have changed. The
    pull is toward 0.35, roughly a squad player.
    """
    if minutes <= 0:
        # No history: promoted club, new signing or academy. Price is the only
        # signal we have, and it is a weak one — FPL prices new arrivals on
        # reputation. Deliberately capped low so the optimiser does not load up
        # on unknowns it cannot justify.
        prior = 0.30 + min(0.35, max(0.0, (cost - 45) / 100.0))
        return prior * avail

    observed = min(1.0, starts / SEASON_GAMES)
    weight = min(1.0, minutes / (MINUTES_FULL * 0.5))   # full trust at ~19 full games
    shrunk = weight * observed + (1 - weight) * 0.35
    return min(1.0, shrunk) * avail


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
    established = df[df.minutes_last >= PRIOR_MIN_MINUTES]
    for col in SHRUNK_RATES:
        # Minutes-weighted position baseline: the rate of a typical starter.
        priors = established.groupby("pos").apply(
            lambda g, c=col: (
                (g[c] * g.minutes_last).sum() / g.minutes_last.sum()
                if g.minutes_last.sum() > 0 else 0.0
            ),
            include_groups=False,
        )
        prior = df.pos.map(priors).fillna(0.0)
        m = df.minutes_last
        df[col + "_raw"] = df[col]
        df[col] = (m * df[col] + k * prior) / (m + k)
    return df


def build(store: Store, snapshot_id: int | None = None) -> pd.DataFrame:
    """One row per player with everything the projection model reads."""
    sid = snapshot_id or store.latest_snapshot_id()
    teams = {t["team_id"]: t for t in store.teams(sid)}

    rows = []
    for p in store.players(sid):
        raw = json.loads(p["raw"])
        minutes = float(p["minutes"] or 0)
        starts = float(p["starts"] or 0)
        avail = availability(p["status"], p["chance_next"])
        per90 = (90.0 / minutes) if minutes > 0 else 0.0

        rows.append({
            "id": p["element_id"],
            "name": p["web_name"],
            "team": p["team"],
            "team_short": teams[p["team"]]["short_name"] if p["team"] in teams else "?",
            "pos": POS.get(p["element_type"], "?"),
            "element_type": p["element_type"],
            "cost": p["now_cost"],                       # tenths of a million
            "status": p["status"],
            "news": p["news"] or "",
            "avail": avail,
            "p_start": start_probability(minutes, starts, p["now_cost"], avail),
            "minutes_last": minutes,
            "ep_next": p["ep_next"] or 0.0,              # the baseline to beat
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

    df = pd.DataFrame(rows).set_index("id", drop=False)
    return shrink_rates(df)
