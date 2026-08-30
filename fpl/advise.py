"""Phase 2 advisory report: projections, a squad, an XI and a captain.

Read-only and unauthenticated. It touches nothing on the FPL account — it reads
the snapshot database, projects, solves and prints. Run it as often as you like.

    python -m fpl.advise                 # suggest a squad from scratch
    python -m fpl.advise --shortlist     # also print the best options by position
    python -m fpl.advise --own 411,381   # rank an XI from a squad you own

The suggested squad is advice, not a plan of record. Before GW1 in particular,
read the caveats it prints at the end — 195 of 595 players have no Premier League
history at all, and no model can bluff its way past that.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import features, optimise, project
from .store import Store

HORIZON = 5


def _deadline_note(store: Store) -> str:
    gw = store.next_gameweek()
    if gw is None or not gw["deadline_time"]:
        return "next gameweek unknown — the snapshot may predate the gameweek table"
    deadline = datetime.fromisoformat(gw["deadline_time"].replace("Z", "+00:00"))
    left = deadline - datetime.now(timezone.utc)
    hours = left.total_seconds() / 3600
    when = "PASSED" if hours < 0 else f"{int(hours)}h {int(abs(hours) % 1 * 60)}m away"
    return f"{gw['name']} deadline {deadline:%a %d %b %H:%M} UTC — {when}"


def _row(df, pid, tag=""):
    p = df.loc[pid]
    return (f"  {tag:<4}{p['name'][:16]:<17}{p.team_short:<5}{p.pos:<5}"
            f"{p.cost/10:>5.1f}  {p.p_start:>5.0%}  {p.xp_next:>5.2f}  "
            f"{p.xp_total:>6.2f}  {p.ep_next:>5.1f}")


HEADER = (f"  {'':<4}{'player':<17}{'team':<5}{'pos':<5}{'£m':>5}  "
          f"{'strt':>5}  {'xP':>5}  {'xP5':>6}  {'FPL':>5}")


def report(store: Store, shortlist: bool = False, own: list[int] | None = None,
           record: bool = True) -> None:
    gw_row = store.next_gameweek()
    gw = gw_row["id"] if gw_row else 1
    rules = store.rules()

    df = features.build(store)
    horizon = features.fixture_horizon(store, gw, HORIZON)
    df = project.project(df, horizon, gw, HORIZON)

    # Write the forecast down before the gameweek is played. Like the snapshots,
    # this cannot be back-filled: once the gameweek has happened there is no way
    # to recover what the model would have said beforehand, and without that
    # there is nothing to score the model against later.
    snap = store.latest_snapshot()
    if record:
        n = store.save_predictions(
            gw, snap["id"],
            ((pid, row.xp_next, row.ep_next, row.p_start) for pid, row in df.iterrows()),
        )
        log_note = f"recorded {n} predictions for GW{gw}"
    else:
        log_note = "predictions NOT recorded (--no-record)"

    print(f"\nFPL Autopilot — advisory report")
    print(f"{_deadline_note(store)}")
    print(f"snapshot #{snap['id']} taken {snap['taken_at']} · "
          f"{len(df)} players · horizon GW{gw}-{gw + HORIZON - 1}\n")

    if own:
        squad = optimise.best_xi(df, own, rules)
        title = "BEST XI FROM YOUR SQUAD"
    else:
        squad = optimise.best_squad(df, rules)
        title = f"SUGGESTED SQUAD — £{squad.cost / 10:.1f}m of £{rules['budget'] / 10:.1f}m"

    print(title)
    print(HEADER)
    starting = squad.starting.sort_values(
        ["element_type", "xp_next"], ascending=[True, False]
    )
    for pid in starting.index:
        tag = "(C)" if pid == squad.captain else "(V)" if pid == squad.vice else ""
        print(_row(df, pid, tag))

    print(f"  {'-' * 62}")
    for n, pid in enumerate(squad.bench_order, 1):
        print(_row(df, pid, f"{n}."))

    print(f"\n  XI expected points (captain doubled): {squad.xp_next:.1f}")
    print(f"  captain {df.loc[squad.captain, 'name']} · "
          f"vice {df.loc[squad.vice, 'name']}")
    if not own:
        print(f"  budget left: £{(rules['budget'] - squad.cost) / 10:.1f}m · "
              f"clubs used: {squad.players.team_short.nunique()}")

    if shortlist:
        print("\nBEST BY POSITION (xP for the next gameweek)")
        for pos in ("GKP", "DEF", "MID", "FWD"):
            print(f"\n  {pos}")
            print(HEADER)
            top = df[(df.pos == pos) & (df.avail > 0)].nlargest(6, "xp_next")
            for pid in top.index:
                print(_row(df, pid))

    # Benchmark: if the model cannot beat FPL's own ep_next it is not worth
    # trusting with transfers. Pre-season that number is heavily compressed, so
    # correlation is all this can honestly report until real results land.
    corr = df.xp_next.corr(df.ep_next)
    print(f"\nMODEL vs FPL BASELINE")
    print(f"  correlation with ep_next: {corr:.3f}  "
          f"(mean xP {df.xp_next.mean():.2f} vs ep_next {df.ep_next.mean():.2f})")
    print("  ep_next is capped at 4.0 for every premium pre-season, so it is a weak")
    print("  benchmark right now. Real scoring against it starts after GW1.")

    unknown = int((df.minutes_sample == 0).sum())
    in_squad = int((squad.players.minutes_sample == 0).sum())
    print(f"\nREAD THIS BEFORE TRUSTING IT")
    print(f"  · {unknown} of {len(df)} players have no Premier League minutes to")
    print(f"    project from — promoted clubs, new signings, academy. {in_squad} of them")
    print(f"    are in the squad above, priced on reputation rather than evidence.")
    print("  · Rates come from LAST season, at last season's club, before any")
    print("    transfer or tactical change. Nothing here has seen 2026/27 football.")
    print("  · No transfer planning, no chips, no price modelling yet — that is the")
    print("    next phase and it needs the authenticated my-team read.")
    print("  · Advisory only. Nothing in this command can write to your team.")
    print(f"  · {log_note} — `python -m fpl.reconcile` scores them once the")
    print("    gameweek finishes.\n")


def main(argv: list[str] | None = None) -> int:
    # Player names carry accents and the report prints £ and ·. A Windows console
    # defaults to cp1252 and raises UnicodeEncodeError on all three, so force
    # UTF-8 and degrade to replacement characters rather than dying mid-table.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="FPL advisory report (read-only)")
    ap.add_argument("--shortlist", action="store_true",
                    help="also print the best options per position")
    ap.add_argument("--own", help="comma-separated element ids you already own; "
                                  "ranks an XI from them instead of building a squad")
    ap.add_argument("--no-record", action="store_true",
                    help="do not write this forecast to the prediction log")
    args = ap.parse_args(argv)

    own = [int(x) for x in args.own.split(",")] if args.own else None
    store = Store()
    try:
        report(store, shortlist=args.shortlist, own=own, record=not args.no_record)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
