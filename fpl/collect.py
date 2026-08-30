"""Phase 1 collector: snapshot the game state and record price movement.

Runs unattended on a schedule. Needs no credentials — everything the projection
model reads is public. Authentication only affects the optional squad check at
the end, which degrades to a warning rather than failing the run.
"""

from __future__ import annotations

import logging
import os

from .client import AuthExpired, Client, SchemaDrift
from .store import Store

log = logging.getLogger(__name__)


def collect(entry_id: int | None = None, db_path=None) -> dict:
    """Take one snapshot. Returns a summary dict for the caller to report."""
    client = Client()
    store = Store(db_path) if db_path else Store()
    summary: dict = {"ok": False, "price_changes": [], "authenticated": False}

    try:
        bootstrap = client.bootstrap_static()
        fixtures = client.fixtures()
    except SchemaDrift as exc:
        # Fail loudly: projecting from misread fields is worse than not running.
        store.log_run("collect", "schema_drift", str(exc))
        store.close()
        raise
    except Exception as exc:
        store.log_run("collect", "error", str(exc))
        store.close()
        raise

    sid = store.save_bootstrap(bootstrap)
    n_fixtures = store.save_fixtures(fixtures)
    changes = store.detect_price_changes(sid)

    summary.update(
        snapshot_id=sid,
        players=len(bootstrap["elements"]),
        fixtures=n_fixtures,
        gw_next=next((e["id"] for e in bootstrap["events"] if e.get("is_next")), None),
        price_changes=changes,
        snapshots_held=store.snapshot_count(),
        ok=True,
    )

    # Optional: prove the bearer token still works, so a dead session is noticed
    # before it matters rather than at a deadline.
    if client.has_token and entry_id:
        try:
            if client.is_authenticated():
                team = client.my_team(entry_id)
                summary["authenticated"] = True
                summary["bank"] = team.get("transfers", {}).get("bank")
                summary["free_transfers"] = team.get("transfers", {}).get("limit")
            else:
                summary["auth_warning"] = "token present but not accepted — it has probably expired (8h lifetime)"
        except (AuthExpired, RuntimeError) as exc:
            summary["auth_warning"] = str(exc)

    store.log_run(
        "collect", "ok",
        f"snapshot={sid} players={summary['players']} price_changes={len(changes)}",
    )
    store.close()
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    entry_id = os.environ.get("FPL_ENTRY_ID")
    result = collect(int(entry_id) if entry_id else None)

    print(f"snapshot #{result['snapshot_id']} "
          f"({result['snapshots_held']} held) — "
          f"{result['players']} players, {result['fixtures']} fixtures, "
          f"next GW {result['gw_next']}")

    if result["price_changes"]:
        print(f"\n{len(result['price_changes'])} price change(s):")
        for c in result["price_changes"]:
            direction = "rise" if c["new_cost"] > c["old_cost"] else "fall"
            print(f"  {c['web_name']:<18} "
                  f"{c['old_cost']/10:.1f} -> {c['new_cost']/10:.1f}  ({direction})")
    else:
        print("no price changes since last snapshot")

    if result.get("auth_warning"):
        print(f"\nAUTH: {result['auth_warning']}")
    elif result["authenticated"]:
        print(f"\nauth ok — bank {result['bank']}, free transfers {result['free_transfers']}")


if __name__ == "__main__":
    main()
