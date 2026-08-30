"""Expected points per player per gameweek.

FPL scoring is additive, so expected points decompose cleanly and each component
can be modelled on its own and summed:

    xP = P(plays) x [ appearance + goals + assists + clean_sheet
                      + defensive_contribution + saves + bonus ]
         - expected_deductions

Ordered by how much each part actually matters, minutes first. A brilliant player
with a 40% chance of starting is worth less than a dull nailed-on one, and that is
where most managers really lose points.

The scoring values below are hardcoded because FPL does not publish them in
`game_settings` (squad rules *are* published, and those are read from the API in
`Store.rules()`). If the scoring changes, this table is the one place to edit.
"""

from __future__ import annotations

import math

import pandas as pd

from .features import Fixture

# --- scoring -----------------------------------------------------------------
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CS_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3

# 2 points for clearing a defensive-contribution threshold. Defenders are scored
# on CBIT alone and need 10; midfielders and forwards get tackles and recoveries
# counted too and need 12. FPL's own `defensive_contribution` field already uses
# the right basis per position, so only the threshold differs here.
DEFCON_POINTS = 2
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}

SAVES_PER_POINT = 3
CONCEDED_PER_PENALTY = 2          # GKP and DEF lose 1 point per 2 conceded
YELLOW_POINTS = -1

# --- match model -------------------------------------------------------------
LEAGUE_AVG_GOALS = 1.45           # goals per team per game
HOME_ADVANTAGE = 1.10             # multiplier on the home side's scoring
STRENGTH_ELASTICITY = 0.70        # how hard team strength bends the scoreline

# How much of the conceded rate comes from a team's own recorded expected goals
# conceded rather than from FPL's coarse 1-5 strength rating. The rating buckets
# twenty clubs into five levels; xGC per 90 separates the two ends of a bucket,
# and clean sheets are worth 4 points to a defender, so the difference is real.
XGC_WEIGHT = 0.5

# --- minutes model -----------------------------------------------------------
STARTER_MINUTES = 85
CAMEO_MINUTES = 22
P_STARTER_LASTS_60 = 0.85         # a starter usually sees 60 minutes
P_BENCH_APPEARS = 0.50            # an available non-starter often does not get on

# Near-term certainty should outweigh speculative long-range planning.
HORIZON_DECAY = 0.85


def team_goal_rates(own_strength: int, opp_strength: int,
                    is_home: bool) -> tuple[float, float]:
    """Expected goals scored and conceded by one team in one fixture.

    Driven by `strength_overall_home` / `strength_overall_away`, which FPL
    populates from day one -- unlike `strength_attack_*` and
    `strength_defence_*`, which sit at zero until the season is under way.
    """
    own = max(1, own_strength or 3)
    opp = max(1, opp_strength or 3)
    edge = (own / opp) ** STRENGTH_ELASTICITY
    home = HOME_ADVANTAGE if is_home else 1.0 / HOME_ADVANTAGE
    return LEAGUE_AVG_GOALS * edge * home, LEAGUE_AVG_GOALS / edge / home


def poisson_at_least(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam).

    Defensive contribution is a count crossing a line, not a rate, so it needs
    a distribution rather than a mean. k is small (10 or 12), so summing the
    lower tail directly is exact and cheap.
    """
    if lam <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def expected_bonus(bps90: float, minutes: float) -> float:
    """Bonus is small, but it decides captaincy ties.

    Modelled as a monotone map from BPS rate to expected bonus rather than a
    true top-three-in-match probability, which would need every other player in
    the fixture. 28 BPS per 90 is roughly where a player starts collecting bonus
    regularly; the cap reflects that even the best average well under 3.
    """
    if bps90 <= 0 or minutes <= 0:
        return 0.0
    share = max(0.0, min(1.0, (bps90 - 12.0) / 28.0))
    return share * 1.3 * (minutes / 90.0)


def project_fixture(row: pd.Series, fx: Fixture, opp_strength_home: int,
                    opp_strength_away: int) -> float:
    """Expected points for one player in one fixture."""
    own_strength = row.strength_home if fx.is_home else row.strength_away
    # The opponent's strength at *their* venue for this match: if we are home,
    # they are away.
    opp_strength = opp_strength_away if fx.is_home else opp_strength_home
    team_scored, team_conceded = team_goal_rates(own_strength, opp_strength, fx.is_home)
    attack_mult = team_scored / LEAGUE_AVG_GOALS

    # Temper the strength-derived rate with what this player's side has actually
    # conceded per 90, rescaled to this fixture. Without this, every club inside
    # a strength band gets an identical clean-sheet probability.
    if row.xgc90 > 0:
        observed = row.xgc90 * (team_conceded / LEAGUE_AVG_GOALS)
        team_conceded = (1 - XGC_WEIGHT) * team_conceded + XGC_WEIGHT * observed

    p_start = float(row.p_start)
    p_cameo = max(0.0, float(row.avail) - p_start) * P_BENCH_APPEARS
    minutes = p_start * STARTER_MINUTES + p_cameo * CAMEO_MINUTES
    if minutes <= 0:
        return 0.0
    p60 = p_start * P_STARTER_LASTS_60
    share = minutes / 90.0

    # Appearance: 2 points for 60+ minutes, 1 for anything less.
    pts = p_start * (P_STARTER_LASTS_60 * 2 + (1 - P_STARTER_LASTS_60)) + p_cameo

    # Attacking returns, scaled by projected minutes and the opponent. Never raw
    # goals -- a striker who scored twice from 0.3 xG is a sell signal.
    pts += row.xg90 * share * attack_mult * GOAL_POINTS.get(row.pos, 0)
    pts += row.xa90 * share * attack_mult * ASSIST_POINTS

    # Clean sheet needs 60 minutes, so it is gated on p60 rather than minutes.
    if CS_POINTS.get(row.pos):
        pts += math.exp(-team_conceded) * p60 * CS_POINTS[row.pos]

    # Defensive contribution: the newest scoring category and the least
    # efficiently priced by casual managers.
    threshold = DEFCON_THRESHOLD.get(row.pos)
    if threshold and row.defcon90 > 0:
        pts += DEFCON_POINTS * poisson_at_least(threshold, row.defcon90 * share)

    if row.pos == "GKP":
        pts += (row.saves90 * share) / SAVES_PER_POINT

    pts += expected_bonus(row.bps90, minutes)

    # Deductions.
    if row.pos in ("GKP", "DEF"):
        pts -= (team_conceded * share) / CONCEDED_PER_PENALTY
    pts += row.yellow90 * share * YELLOW_POINTS

    return max(0.0, pts)


def project(df: pd.DataFrame, horizon: dict[int, list[Fixture]],
            first_gw: int, weeks: int) -> pd.DataFrame:
    """Add per-gameweek xP columns plus a discounted horizon total.

    Blanks score zero and doubles score twice, because the horizon holds a list
    of fixtures per team rather than exactly one per week.
    """
    strengths = (
        df.groupby("team")[["strength_home", "strength_away"]].first().to_dict("index")
    )
    gws = list(range(first_gw, first_gw + weeks))
    columns: dict[int, list[float]] = {gw: [] for gw in gws}

    for _, row in df.iterrows():
        by_gw = {gw: 0.0 for gw in gws}
        for fx in horizon.get(row.team, []):
            if fx.gw not in by_gw:
                continue
            opp = strengths.get(fx.opponent, {"strength_home": 3, "strength_away": 3})
            by_gw[fx.gw] += project_fixture(
                row, fx, opp["strength_home"], opp["strength_away"]
            )
        for gw in gws:
            columns[gw].append(by_gw[gw])

    for gw in gws:
        df[f"xp_gw{gw}"] = columns[gw]

    cols = [f"xp_gw{gw}" for gw in gws]
    df["xp_next"] = df[cols[0]]
    df["xp_total"] = df[cols].sum(axis=1)
    df["xp_horizon"] = sum(df[c] * (HORIZON_DECAY ** i) for i, c in enumerate(cols))
    # Value per million, for reading a shortlist by eye -- not an objective.
    df["xp_per_m"] = df["xp_next"] / (df["cost"] / 10.0)
    return df
