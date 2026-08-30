"""Read the manager's actual squad — from public endpoints, no login.

The plan assumed reading your own team needed authentication. It does not.
`entry/{id}/event/{gw}/picks/` is public once that gameweek's deadline has
passed, and it carries the fifteen players, the captain and vice, the bank and
the squad value. `entry/{id}/history/` adds transfers made per gameweek and
chips used, and `entry/{id}/transfers/` lists every transfer with the prices
paid. Between them, everything the transfer optimiser needs is public.

Two consequences worth being explicit about:

- Transfer *advice* for your real team needs no credentials at all, so it works
  today and keeps working when a token expires.
- Only the write path — actually making the transfer — needs authentication.

The one thing genuinely not public is your squad *before* the upcoming deadline
if you have already made changes for it; picks are published per completed
gameweek. So this reads the most recent published gameweek, which is your
current squad unless you have already transferred for the next one.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from .client import Client
from .store import Store

# FPL gives one free transfer a gameweek and lets unused ones bank up to a cap.
# The cap comes from the API (`max_extra_free_transfers` + 1) via Store.rules().
DEFAULT_FT_CAP = 5


@dataclass
class Pick:
    element: int
    position: int              # 1-11 start, 12-15 bench, in substitution order
    is_captain: bool
    is_vice: bool
    element_type: int
    purchase_cost: int         # tenths of a million, what was paid
    now_cost: int
    selling_price: int

    @property
    def profit(self) -> int:
        return self.now_cost - self.purchase_cost


@dataclass
class Team:
    entry_id: int
    gameweek: int              # the gameweek these picks are from
    picks: list[Pick]
    bank: int                  # tenths of a million
    value: int                 # squad value excluding bank
    free_transfers: int
    chips_used: list[str] = field(default_factory=list)
    total_points: int = 0
    overall_rank: int | None = None

    @property
    def element_ids(self) -> list[int]:
        return [p.element for p in self.picks]

    @property
    def captain(self) -> int | None:
        return next((p.element for p in self.picks if p.is_captain), None)

    @property
    def vice(self) -> int | None:
        return next((p.element for p in self.picks if p.is_vice), None)

    @property
    def budget(self) -> int:
        """What a full rebuild could spend: everything sellable plus the bank."""
        return sum(p.selling_price for p in self.picks) + self.bank


def selling_price(purchase: int, now: int) -> int:
    """FPL's sell-on rule: you keep half your profit, rounded down.

    Losses are absorbed in full — a player who has dropped sells for his current
    price, not the higher one you paid. The rounding is per player and downward,
    which is why a £0.1m rise sells for nothing extra.
    """
    if now <= purchase:
        return now
    return purchase + (now - purchase) // 2


def free_transfers(history: dict, cap: int = DEFAULT_FT_CAP) -> int:
    """Free transfers available for the next deadline.

    One per gameweek, unused ones bank up to the cap, and you always have at
    least one. Derived from transfers actually made rather than assumed, because
    the count drives whether the optimiser is allowed to take a -4 hit.
    """
    played = sorted(history.get("current") or [], key=lambda c: c["event"])
    if not played:
        return 1

    # The opening gameweek builds the squad and consumes nothing, so the first
    # allowance to track is the one for gameweek two. Each played gameweek then
    # spends its transfers and rolls the remainder forward, which means after
    # the loop `available` is already the allowance for the next, unplayed
    # gameweek -- rolling forward again here would silently hand out a free
    # transfer that does not exist, and the optimiser would spend it on a -4 hit
    # it could not afford.
    available = 1
    for week in played[1:]:
        used = week.get("event_transfers") or 0
        available = min(cap, max(1, available - used + 1))
    return available


def load(entry_id: int, client: Client | None = None,
         store: Store | None = None, cap: int = DEFAULT_FT_CAP) -> Team:
    """Fetch the manager's current squad and finances from public endpoints."""
    client = client or Client()
    history = client.entry_history(entry_id)
    played = sorted(history.get("current") or [], key=lambda c: c["event"])
    if not played:
        raise RuntimeError(
            f"entry {entry_id} has no completed gameweeks yet — there is no "
            f"published squad to read"
        )
    latest = played[-1]
    gw = latest["event"]

    payload = client.get(f"entry/{entry_id}/event/{gw}/picks/")
    raw_picks = payload.get("picks") or []
    if not raw_picks:
        raise RuntimeError(f"entry/{entry_id}/event/{gw}/picks/ returned no players")

    # Prices actually paid. With no transfers every player was bought at the
    # season's starting price, which is now_cost minus the change since then.
    # Any transfer overrides that with the price recorded at the time.
    prices = client.get(f"entry/{entry_id}/transfers/") or []
    paid: dict[int, int] = {}
    for t in sorted(prices, key=lambda t: t.get("event", 0)):
        paid[t["element_in"]] = t["element_in_cost"]

    bootstrap = client.bootstrap_static()
    elements = {e["id"]: e for e in bootstrap["elements"]}

    picks = []
    for p in raw_picks:
        el = elements.get(p["element"])
        if el is None:                    # player removed from the game entirely
            continue
        now = el["now_cost"]
        purchase = paid.get(p["element"], now - (el.get("cost_change_start") or 0))
        picks.append(Pick(
            element=p["element"],
            position=p["position"],
            is_captain=bool(p.get("is_captain")),
            is_vice=bool(p.get("is_vice_captain")),
            element_type=p.get("element_type") or el["element_type"],
            purchase_cost=purchase,
            now_cost=now,
            selling_price=selling_price(purchase, now),
        ))

    return Team(
        entry_id=entry_id,
        gameweek=gw,
        picks=picks,
        bank=latest.get("bank") or 0,
        value=latest.get("value") or 0,
        free_transfers=free_transfers(history, cap),
        chips_used=[c.get("name") for c in (history.get("chips") or [])],
        total_points=latest.get("total_points") or 0,
        overall_rank=latest.get("overall_rank"),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Show a manager's squad (public data)")
    ap.add_argument("entry_id", type=int, nargs="?", default=6643465)
    args = ap.parse_args(argv)

    store = Store()
    try:
        rules = store.rules()
        team = load(args.entry_id, cap=rules["max_free_transfers"])
        names = {p["element_id"]: p["web_name"] for p in store.players()}
    finally:
        store.close()

    print(f"\nEntry {team.entry_id} — squad as of GW{team.gameweek}")
    print(f"{team.total_points} points, overall rank "
          f"{team.overall_rank:,}" if team.overall_rank else "")
    print(f"bank £{team.bank / 10:.1f}m · squad value £{team.value / 10:.1f}m · "
          f"{team.free_transfers} free transfer(s) · "
          f"chips used: {', '.join(team.chips_used) or 'none'}\n")

    print(f"  {'':<4}{'player':<18}{'bought':>8}{'now':>7}{'sell':>7}{'profit':>8}")
    for p in sorted(team.picks, key=lambda p: p.position):
        tag = "(C)" if p.is_captain else "(V)" if p.is_vice else ""
        bench = "  " if p.position <= 11 else "B "
        print(f"  {bench}{tag:<3}{names.get(p.element, p.element):<17}"
              f"{p.purchase_cost / 10:>8.1f}{p.now_cost / 10:>7.1f}"
              f"{p.selling_price / 10:>7.1f}{p.profit / 10:>+8.1f}")
    print(f"\n  full rebuild budget: £{team.budget / 10:.1f}m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
