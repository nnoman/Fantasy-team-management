"""Squad selection as an integer program.

Picking a squad is a textbook integer program, so it is solved as one rather than
with hand-written heuristics: PuLP with the bundled CBC solver does it in seconds,
with no paid solver and no cloud service.

Two entry points:

  best_squad()  builds a 15-man squad from scratch under the budget. That is what
                a new season needs, before any transfer accounting exists.
  best_xi()     picks the starting eleven, captain and bench order from a squad
                you already own. No transfers, so no way to lose value.

Transfer planning over a horizon -- hits, banked free transfers, chip scenarios --
is deliberately not here yet. It needs the authenticated `my-team` read to know
selling prices and free transfers, and it is the next phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pulp

# Squad shape. FPL publishes this in bootstrap-static's `element_types`
# (squad_select, squad_min_play, squad_max_play); size, budget and the per-club
# limit are read from the API in Store.rules(), and only the formation bounds are
# restated here.
SQUAD_SHAPE = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
MIN_PLAY = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
MAX_PLAY = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}

# A bench player only scores when a starter fails to play, so bench expected
# points are worth a fraction of a starter's -- but not zero, or the optimiser
# fills the bench with £4.0m players who can never cover an injury.
BENCH_VALUE = 0.15

# Weight on gameweeks beyond the next one. Less than 1 because transfers let you
# react later, more than 0 because a squad that falls apart in three weeks is a
# bad squad.
FUTURE_WEIGHT = 0.45


@dataclass
class Squad:
    """The solver's answer, in a form the report can print directly."""
    players: pd.DataFrame                 # all 15, with a `starting` flag
    captain: int
    vice: int
    bench_order: list[int] = field(default_factory=list)
    xp_next: float = 0.0                  # expected points for the XI, captain doubled
    cost: int = 0                         # tenths of a million

    @property
    def starting(self) -> pd.DataFrame:
        return self.players[self.players.starting]

    @property
    def bench(self) -> pd.DataFrame:
        return self.players.loc[self.bench_order]


def _future_xp(df: pd.DataFrame) -> pd.Series:
    """Discounted expected points from the gameweeks after the next one."""
    return (df.xp_horizon - df.xp_next).clip(lower=0)


def _solve(df: pd.DataFrame, rules: dict, forced: set[int] | None = None,
           banned: set[int] | None = None) -> Squad:
    """Shared model: choose 15, choose 11 of them, choose a captain."""
    ids = list(df.index)
    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    squad = pulp.LpVariable.dicts("squad", ids, cat="Binary")
    start = pulp.LpVariable.dicts("start", ids, cat="Binary")
    cap = pulp.LpVariable.dicts("cap", ids, cat="Binary")

    xp = df.xp_next.to_dict()
    future = _future_xp(df).to_dict()

    prob += pulp.lpSum(
        start[i] * xp[i]
        + cap[i] * xp[i]                       # the captain scores twice
        + (squad[i] - start[i]) * xp[i] * BENCH_VALUE
        + squad[i] * future[i] * FUTURE_WEIGHT
        for i in ids
    )

    prob += pulp.lpSum(squad[i] for i in ids) == rules["squad_size"]
    prob += pulp.lpSum(start[i] for i in ids) == rules["starting"]
    prob += pulp.lpSum(cap[i] for i in ids) == 1
    prob += pulp.lpSum(df.cost[i] * squad[i] for i in ids) <= rules["budget"]

    for i in ids:
        prob += start[i] <= squad[i]
        prob += cap[i] <= start[i]

    for pos, n in SQUAD_SHAPE.items():
        members = [i for i in ids if df.pos[i] == pos]
        prob += pulp.lpSum(squad[i] for i in members) == n
        prob += pulp.lpSum(start[i] for i in members) >= MIN_PLAY[pos]
        prob += pulp.lpSum(start[i] for i in members) <= MAX_PLAY[pos]

    for team in df.team.unique():
        members = [i for i in ids if df.team[i] == team]
        prob += pulp.lpSum(squad[i] for i in members) <= rules["team_limit"]

    for i in forced or ():
        prob += squad[i] == 1
    for i in banned or ():
        prob += squad[i] == 0

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"solver returned {pulp.LpStatus[status]} — constraints may conflict")

    chosen = [i for i in ids if squad[i].value() > 0.5]
    picked = df.loc[chosen].copy()
    picked["starting"] = [start[i].value() > 0.5 for i in chosen]

    captain = next(i for i in chosen if cap[i].value() > 0.5)
    xi = picked[picked.starting].sort_values("xp_next", ascending=False)
    vice = next((i for i in xi.index if i != captain), captain)

    # Bench order: goalkeeper last, since an outfield sub is far likelier to come
    # on, then by expected points.
    bench = picked[~picked.starting]
    bench_order = list(
        bench.assign(is_gk=(bench.pos == "GKP"))
        .sort_values(["is_gk", "xp_next"], ascending=[True, False])
        .index
    )

    return Squad(
        players=picked,
        captain=captain,
        vice=vice,
        bench_order=bench_order,
        xp_next=float(xi.xp_next.sum() + df.xp_next[captain]),
        cost=int(picked.cost.sum()),
    )


def best_squad(df: pd.DataFrame, rules: dict, forced: set[int] | None = None,
               banned: set[int] | None = None,
               exclude_unavailable: bool = True) -> Squad:
    """Build a 15-man squad from the whole player pool under the budget.

    Players FPL has flagged as injured, suspended or ineligible are dropped
    outright by default. Buying one as a cheap bench filler is a real tactic, but
    it needs a human decision, not a solver quietly doing it.
    """
    pool = df[df.avail > 0] if exclude_unavailable else df
    if banned:
        pool = pool.drop(index=[i for i in banned if i in pool.index], errors="ignore")
    return _solve(pool, rules, forced=forced)


def best_xi(df: pd.DataFrame, owned: list[int], rules: dict) -> Squad:
    """Pick the XI, captain, vice and bench order from a squad already owned.

    The safe half of the optimisation: it cannot spend money or lose value, so it
    is the right thing to run in shadow mode for a few gameweeks.
    """
    missing = [i for i in owned if i not in df.index]
    if missing:
        raise KeyError(f"owned players not in the snapshot: {missing}")
    expected = sum(SQUAD_SHAPE.values())
    if len(owned) != expected:
        # Otherwise the shape constraints are simply infeasible and the solver
        # reports "Infeasible", which says nothing useful about the real problem.
        raise ValueError(
            f"best_xi needs a full squad of {expected} players, got {len(owned)}"
        )
    pool = df.loc[owned]
    rules = {**rules, "squad_size": len(owned), "budget": int(pool.cost.sum()),
             "team_limit": len(owned)}
    return _solve(pool, rules)
