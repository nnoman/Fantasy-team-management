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

The one genuine gap: FPL publishes picks per *completed* gameweek, so your squad
for the upcoming deadline is not public. `entry/{id}/event/{next}/picks/` returns
404 until that gameweek closes. Two things close the gap:

- Transfers already made for the upcoming gameweek appear in
  `entry/{id}/transfers/` with that gameweek's number, so they are replayed onto
  the last published squad. Whether FPL exposes them there before the deadline
  is not something this code can assume, so it is treated as best-effort.
- A manual `swap` override, for changes that are not visible either way. It
  takes web names or element ids, so a human can type it.

Neither is a workaround for authentication. With a token, `my-team/{id}/` would
answer this directly and exactly; without one, this is as close as public data
gets.
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
    # Transfers already made for the upcoming gameweek and replayed onto the
    # last published squad. Surfaced so the dashboard can say whether what it
    # shows is confirmed or reconstructed.
    pending_transfers: int = 0

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


def _replay(raw_picks: list[dict], transfers: list[dict]) -> list[dict]:
    """Apply transfers made after the last published gameweek, in order.

    Each swap keeps the outgoing player's squad position so the starting eleven
    and bench order stay meaningful; FPL would re-derive them at the deadline
    anyway, and the optimiser recomputes both regardless.
    """
    picks = {p["element"]: dict(p) for p in raw_picks}
    for t in sorted(transfers, key=lambda t: (t.get("event", 0), t.get("time", ""))):
        out_id, in_id = t.get("element_out"), t.get("element_in")
        if out_id not in picks:
            continue                      # already replaced by a later transfer
        slot = picks.pop(out_id)
        slot["element"] = in_id
        # Captaincy cannot follow a player who has been sold.
        if slot.get("is_captain") or slot.get("is_vice_captain"):
            slot["is_captain"] = slot["is_vice_captain"] = False
        picks[in_id] = slot
    return list(picks.values())


def apply_swaps(team: "Team", swaps: str, lookup: dict[str, int],
                costs: dict[int, int]) -> "Team":
    """Manually override the squad, for changes public data cannot show.

    `swaps` is "out>in" pairs separated by commas, using web names or element
    ids: "Thiago>Watkins, Isak>Calvert-Lewin". Names are matched case-insensitively
    and must be unambiguous -- several players share a web name, so an ambiguous
    one is rejected rather than guessed at.
    """
    if not swaps or not swaps.strip():
        return team

    def resolve(token: str) -> int:
        token = token.strip()
        if token.isdigit():
            return int(token)
        matches = lookup.get(token.casefold())
        if matches is None:
            raise ValueError(f"no player called {token!r}")
        if isinstance(matches, list):
            raise ValueError(
                f"{token!r} matches {len(matches)} players — use an element id"
            )
        return matches

    by_id = {p.element: p for p in team.picks}
    for pair in swaps.split(","):
        if not pair.strip():
            continue
        if ">" not in pair:
            raise ValueError(f"expected 'out>in', got {pair.strip()!r}")
        out_token, in_token = pair.split(">", 1)
        out_id, in_id = resolve(out_token), resolve(in_token)
        if out_id not in by_id:
            raise ValueError(f"{out_token.strip()!r} is not in the squad")
        old = by_id.pop(out_id)
        cost = costs.get(in_id, old.now_cost)
        by_id[in_id] = Pick(
            element=in_id, position=old.position,
            is_captain=False, is_vice=False,
            element_type=old.element_type,
            # A player just bought was bought at today's price, so there is no
            # profit to share and the sell price is what was paid.
            purchase_cost=cost, now_cost=cost, selling_price=cost,
        )
        # The sale funds the purchase; the bank absorbs the difference.
        team.bank += old.selling_price - cost

    team.picks = list(by_id.values())
    return team


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
         store: Store | None = None, cap: int = DEFAULT_FT_CAP,
         swaps: str = "") -> Team:
    """Fetch the manager's current squad and finances from public endpoints.

    `swaps` manually overrides the squad afterwards, for changes made before a
    deadline that FPL has not published. See apply_swaps().
    """
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

    # Transfers recorded for a gameweek later than the last published one have
    # been made but not yet reflected in any picks payload. Replay them so the
    # squad is current rather than a week stale.
    pending = [t for t in prices if (t.get("event") or 0) > gw]

    bootstrap = client.bootstrap_static()
    elements = {e["id"]: e for e in bootstrap["elements"]}

    if pending:
        raw_picks = _replay(raw_picks, pending)

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

    team = Team(
        entry_id=entry_id,
        gameweek=gw,
        picks=picks,
        bank=latest.get("bank") or 0,
        value=latest.get("value") or 0,
        free_transfers=free_transfers(history, cap),
        chips_used=[c.get("name") for c in (history.get("chips") or [])],
        total_points=latest.get("total_points") or 0,
        overall_rank=latest.get("overall_rank"),
        pending_transfers=len(pending),
    )

    if swaps:
        lookup: dict[str, int | list[int]] = {}
        for el in elements.values():
            key = el["web_name"].casefold()
            if key in lookup:
                existing = lookup[key]
                lookup[key] = (existing if isinstance(existing, list) else [existing])
                lookup[key].append(el["id"])
            else:
                lookup[key] = el["id"]
        team = apply_swaps(team, swaps,
                           lookup, {e["id"]: e["now_cost"] for e in elements.values()})
        # A manual swap is a transfer, so it spends the allowance.
        team.free_transfers = max(0, team.free_transfers - len(
            [p for p in swaps.split(",") if p.strip()]))
    return team


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
