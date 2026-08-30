"""Backfill each player's completed seasons from element-summary.

Why this exists. Before a season starts, bootstrap-static carries last season's
aggregates, so the projection model has a full prior season to work from. The
moment the season begins those reset, and two gameweeks in every per-90 rate is
computed from at most 180 minutes. Shrinkage then pulls everything to the
position baseline, start probability goes flat across the entire player pool,
and the model loses badly to FPL's own ep_next.

`element-summary/{id}/` keeps `history_past`: per-season totals including
minutes, starts, xG, xA, xGC, defensive contribution and BPS. That is exactly
the discrimination the early season is missing, and it is the same data
bootstrap was serving in August, so the model itself needs no new concepts to
use it.

One request per player at the client's one-per-second rate limit, so a full pass
over ~620 players takes about ten minutes. Completed seasons do not change, so
this belongs on a weekly schedule rather than a nightly one.

    python -m fpl.enrich            # only players with no history yet
    python -m fpl.enrich --all      # refresh everyone
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .client import Client
from .store import Store

log = logging.getLogger(__name__)


def enrich(store: Store, client: Client | None = None, refresh_all: bool = False,
           limit: int | None = None) -> dict:
    client = client or Client()
    sid = store.latest_snapshot_id()

    # element_code is the join key that survives a season rollover; the
    # per-season element id does not.
    players = [
        (p["element_id"], json.loads(p["raw"])["code"], p["web_name"])
        for p in store.players(sid)
    ]
    # Keyed on what has been *fetched*, not what returned history: a player who
    # has never played in the Premier League has no history_past, and checking
    # the history table instead would re-request all of them every week.
    known = set() if refresh_all else store.history_fetched()
    todo = [p for p in players if p[1] not in known]
    already = len(players) - len(todo)      # count before --limit truncates
    if limit:
        todo = todo[:limit]

    done = seasons = 0
    failed: list[str] = []
    for element_id, code, name in todo:
        try:
            summary = client.element_summary(element_id)
        except Exception as exc:                    # one bad player must not
            failed.append(f"{name}: {exc}")         # abandon the whole pass
            log.warning("element-summary %s (%s) failed: %s", element_id, name, exc)
            continue
        past = summary.get("history_past") or []
        if past:
            seasons += store.save_history_past(code, past)
        store.mark_history_fetched(code, len(past))
        done += 1
        if done % 50 == 0:
            log.info("%d/%d players, %d seasons stored", done, len(todo), seasons)

    return {
        "considered": len(players),
        "fetched": done,
        "skipped_existing": already,
        "seasons": seasons,
        "failed": failed,
        "coverage": store.history_coverage(),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Backfill per-season player history")
    ap.add_argument("--all", action="store_true", help="refetch players already stored")
    ap.add_argument("--limit", type=int, help="stop after N players (for a quick check)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    store = Store()
    try:
        result = enrich(store, refresh_all=args.all, limit=args.limit)
        store.log_run("enrich", "ok",
                      f"fetched={result['fetched']} seasons={result['seasons']} "
                      f"coverage={result['coverage']}")
        print(f"\nfetched {result['fetched']} players "
              f"({result['skipped_existing']} already had history), "
              f"{result['seasons']} seasons stored")
        print(f"history now covers {result['coverage']} players")
        if result["failed"]:
            print(f"\n{len(result['failed'])} failed:")
            for f in result["failed"][:10]:
                print(f"  {f}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
