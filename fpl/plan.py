"""Transfer planning against the squad you actually own.

Given the real fifteen, the real bank and the real free-transfer count, decide
which moves are worth making. Selling prices come from what was paid rather than
from current price, so the money freed up is exact.

Everything here is advisory. It proposes; you execute in the FPL UI. There is no
write path in this project.

The comparison that matters is not "is this XI good" but "is this XI enough
better than doing nothing to justify what it costs". So the planner always
solves the do-nothing case too, and reports the difference. A move that gains
less than the four points a hit costs is not a move.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pulp

from .optimise import BENCH_VALUE, FUTURE_WEIGHT, MAX_PLAY, MIN_PLAY, SQUAD_SHAPE
from .team import Team

# Each transfer beyond the free allowance costs four points. This is the number
# every recommendation has to clear.
HIT_COST = 4

# Solving over the entire pool with unlimited transfers is a wildcard, not a
# weekly plan. Beyond a handful of moves the answer stops being actionable.
DEFAULT_MAX_TRANSFERS = 3


@dataclass
class Move:
    out_id: int
    in_id: int
    out_name: str
    in_name: str
    out_sell: int              # tenths of a million actually received
    in_cost: int
    out_xp: float
    in_xp: float

    @property
    def xp_gain(self) -> float:
        return self.in_xp - self.out_xp


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    hits: int = 0
    squad: list[int] = field(default_factory=list)
    starting: list[int] = field(default_factory=list)
    captain: int | None = None
    vice: int | None = None
    bench_order: list[int] = field(default_factory=list)
    bank_after: int = 0
    objective: float = 0.0
    baseline: float = 0.0      # the same objective for doing nothing

    @property
    def hit_cost(self) -> int:
        return self.hits * HIT_COST

    @property
    def gain(self) -> float:
        """Net horizon value over doing nothing, hits already deducted."""
        return self.objective - self.baseline

    @property
    def worth_it(self) -> bool:
        return bool(self.moves) and self.gain > 0


def _solve(df: pd.DataFrame, team: Team, rules: dict, max_transfers: int,
           force_transfers: int | None = None) -> tuple[dict, float]:
    """One optimisation over keep/buy/start/captain. Returns chosen ids and value."""
    owned = [p.element for p in team.picks if p.element in df.index]
    sell = {p.element: p.selling_price for p in team.picks}

    # The buy pool excludes anyone already owned, anyone unavailable, and anyone
    # FPL has written off. Owned players stay eligible to be kept regardless, so
    # an injured player can be held rather than force-sold at a loss.
    pool = df[(df.avail > 0) & (~df.get("fpl_writeoff", False))]
    buyable = [i for i in pool.index if i not in set(owned)]
    ids = owned + buyable

    prob = pulp.LpProblem("fpl_transfers", pulp.LpMaximize)
    keep = pulp.LpVariable.dicts("keep", owned, cat="Binary")
    buy = pulp.LpVariable.dicts("buy", buyable, cat="Binary")
    start = pulp.LpVariable.dicts("start", ids, cat="Binary")
    cap = pulp.LpVariable.dicts("cap", ids, cat="Binary")
    hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")

    def held(i):
        return keep[i] if i in keep else buy[i]

    xp = df.xp_next.to_dict()
    future = (df.xp_horizon - df.xp_next).clip(lower=0).to_dict()

    prob += (
        pulp.lpSum(
            start[i] * xp[i]
            + cap[i] * xp[i]
            + (held(i) - start[i]) * xp[i] * BENCH_VALUE
            + held(i) * future[i] * FUTURE_WEIGHT
            for i in ids
        )
        - hits * HIT_COST
    )

    transfers = pulp.lpSum(buy[i] for i in buyable)
    prob += pulp.lpSum(held(i) for i in ids) == rules["squad_size"]
    prob += transfers == pulp.lpSum(1 - keep[i] for i in owned)   # one in, one out
    prob += transfers <= max_transfers
    if force_transfers is not None:
        prob += transfers == force_transfers

    # Hits are the transfers beyond the free allowance. `hits >= t - ft` with a
    # maximising objective and a negative coefficient pins it to exactly that.
    prob += hits >= transfers - team.free_transfers

    # Money: what is bought must be covered by the bank plus what is sold, using
    # selling prices rather than current prices.
    prob += (
        pulp.lpSum(df.cost[i] * buy[i] for i in buyable)
        <= team.bank + pulp.lpSum(sell[i] * (1 - keep[i]) for i in owned)
    )

    prob += pulp.lpSum(start[i] for i in ids) == rules["starting"]
    prob += pulp.lpSum(cap[i] for i in ids) == 1
    for i in ids:
        prob += start[i] <= held(i)
        prob += cap[i] <= start[i]

    for pos, n in SQUAD_SHAPE.items():
        members = [i for i in ids if df.pos[i] == pos]
        prob += pulp.lpSum(held(i) for i in members) == n
        prob += pulp.lpSum(start[i] for i in members) >= MIN_PLAY[pos]
        prob += pulp.lpSum(start[i] for i in members) <= MAX_PLAY[pos]

    for club in df.loc[ids].team.unique():
        members = [i for i in ids if df.team[i] == club]
        prob += pulp.lpSum(held(i) for i in members) <= rules["team_limit"]

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"transfer solver returned {pulp.LpStatus[status]}")

    chosen = {
        "kept": [i for i in owned if keep[i].value() > 0.5],
        "sold": [i for i in owned if keep[i].value() <= 0.5],
        "bought": [i for i in buyable if buy[i].value() > 0.5],
        "start": [i for i in ids if start[i].value() > 0.5],
        "captain": next(i for i in ids if cap[i].value() > 0.5),
        "hits": int(round(hits.value() or 0)),
    }
    return chosen, float(pulp.value(prob.objective))


def build(df: pd.DataFrame, team: Team, rules: dict,
          max_transfers: int = DEFAULT_MAX_TRANSFERS) -> Plan:
    """Best transfer plan, measured against doing nothing."""
    missing = [p.element for p in team.picks if p.element not in df.index]
    if len(missing) > 2:
        raise RuntimeError(
            f"{len(missing)} squad players are not in the snapshot — "
            f"the collector may be out of date"
        )

    # Do-nothing baseline: same objective, zero transfers, so the two numbers
    # are directly comparable.
    _, baseline = _solve(df, team, rules, max_transfers, force_transfers=0)
    chosen, objective = _solve(df, team, rules, max_transfers)

    sell = {p.element: p.selling_price for p in team.picks}
    names = df.name.to_dict()

    # Pair each sale with a purchase for readability. The solver does not think
    # in pairs -- the money is pooled -- so pairing by position keeps the report
    # honest where it can and falls back to order where it cannot.
    sold = sorted(chosen["sold"], key=lambda i: df.pos[i])
    bought = sorted(chosen["bought"], key=lambda i: df.pos[i])
    moves = [
        Move(out_id=o, in_id=n, out_name=names.get(o, str(o)),
             in_name=names.get(n, str(n)), out_sell=sell.get(o, 0),
             in_cost=int(df.cost[n]), out_xp=float(df.xp_next[o]),
             in_xp=float(df.xp_next[n]))
        for o, n in zip(sold, bought)
    ]

    squad = chosen["kept"] + chosen["bought"]
    starting = chosen["start"]
    bench = [i for i in squad if i not in set(starting)]
    bench_df = df.loc[bench]
    bench_order = list(
        bench_df.assign(is_gk=(bench_df.pos == "GKP"))
        .sort_values(["is_gk", "xp_next"], ascending=[True, False]).index
    )
    xi = df.loc[starting].sort_values("xp_next", ascending=False)
    captain = chosen["captain"]

    spent = sum(m.in_cost for m in moves)
    raised = sum(m.out_sell for m in moves)

    return Plan(
        moves=moves,
        hits=chosen["hits"],
        squad=squad,
        starting=starting,
        captain=captain,
        vice=next((i for i in xi.index if i != captain), captain),
        bench_order=bench_order,
        bank_after=team.bank + raised - spent,
        objective=objective,
        baseline=baseline,
    )
