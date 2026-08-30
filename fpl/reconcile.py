"""Score the model's own predictions against what actually happened.

This is the phase that decides whether any of the rest is worth running. A model
that cannot beat FPL's own `ep_next` over a real backtest has no business
recommending transfers, and the only way to know is to write down the forecast
before kickoff and grade it afterwards.

    python -m fpl.reconcile           # score the most recent finished gameweek
    python -m fpl.reconcile --gw 3    # score a specific one
    python -m fpl.reconcile --history # every gameweek scored so far

One trap worth naming. `event/{gw}/live/` does not return an empty payload for a
gameweek that has not been played -- before the 2026/27 season started it served
last season's GW1 in full, 610 players with 90-minute performances, against a
current pool of 595. Scoring that would have produced a confident, entirely
fictitious accuracy report. So nothing is scored unless FPL has marked the
gameweek `finished` and `data_checked`, the latter because bonus points are not
final until it flips.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .client import Client
from .store import Store

log = logging.getLogger(__name__)


def _mean_abs_error(pairs: list[tuple[float, float]]) -> float:
    return sum(abs(a - b) for a, b in pairs) / len(pairs) if pairs else float("nan")


def _rmse(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return float("nan")
    return (sum((a - b) ** 2 for a, b in pairs) / len(pairs)) ** 0.5


def fetch_actuals(store: Store, gw: int, client: Client | None = None) -> int:
    """Pull and store the real per-player result for a finished gameweek."""
    week = store.gameweek(gw)
    if week is None:
        raise RuntimeError(f"gameweek {gw} is not in the database — run the collector")
    if not week["finished"]:
        raise RuntimeError(
            f"gameweek {gw} is not finished. event/{gw}/live/ still returns data "
            f"(it serves last season's results before a season starts), so scoring "
            f"it now would grade the model against the wrong football."
        )
    if not week["data_checked"]:
        log.warning("gameweek %d finished but not data_checked — bonus points may "
                    "still change; scoring anyway", gw)

    client = client or Client()
    live = client.event_live(gw)
    elements = live.get("elements") or []
    if not elements:
        raise RuntimeError(f"event/{gw}/live/ returned no players")

    return store.save_actuals(gw, (
        (e["id"], e["stats"].get("minutes", 0), e["stats"].get("total_points", 0))
        for e in elements
    ))


def score(store: Store, gw: int) -> dict:
    """Compare the last forecast made for a gameweek against the result."""
    predictions = store.latest_predictions(gw)
    if not predictions:
        raise RuntimeError(
            f"no predictions recorded for GW{gw}. They cannot be back-filled — "
            f"`python -m fpl.advise` must run before the deadline to record one."
        )

    actuals = {
        r["element_id"]: r
        for r in store.conn.execute("SELECT * FROM actual WHERE gw = ?", (gw,))
    }
    positions = {
        r["element_id"]: r["element_type"]
        for r in store.players(predictions[0]["snapshot_id"])
    }
    names = {
        r["element_id"]: r["web_name"]
        for r in store.players(predictions[0]["snapshot_id"])
    }

    model: list[tuple[float, float]] = []
    baseline: list[tuple[float, float]] = []
    by_pos: dict[int, list[tuple[float, float]]] = {}
    rows = []

    for p in predictions:
        actual = actuals.get(p["element_id"])
        if actual is None:
            continue
        got = float(actual["total_points"])
        model.append((p["xp"], got))
        if p["ep_next"] is not None:
            baseline.append((p["ep_next"], got))
        by_pos.setdefault(positions.get(p["element_id"], 0), []).append((p["xp"], got))
        rows.append({
            "id": p["element_id"],
            "name": names.get(p["element_id"], "?"),
            "xp": p["xp"],
            "actual": got,
            "error": got - p["xp"],
        })

    return {
        "gw": gw,
        "made_at": predictions[0]["made_at"],
        "scored": len(model),
        "model_mae": _mean_abs_error(model),
        "model_rmse": _rmse(model),
        "baseline_mae": _mean_abs_error(baseline),
        "baseline_rmse": _rmse(baseline),
        "by_pos": {k: _mean_abs_error(v) for k, v in by_pos.items()},
        "rows": rows,
    }


def _print(result: dict) -> None:
    pos_names = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    beat = result["model_mae"] < result["baseline_mae"]

    print(f"\nGW{result['gw']} reconciliation — {result['scored']} players scored")
    print(f"forecast made {result['made_at']}\n")
    print(f"  {'':<12}{'MAE':>8}{'RMSE':>9}")
    print(f"  {'our model':<12}{result['model_mae']:>8.3f}{result['model_rmse']:>9.3f}")
    print(f"  {'FPL ep_next':<12}{result['baseline_mae']:>8.3f}{result['baseline_rmse']:>9.3f}")
    delta = result["baseline_mae"] - result["model_mae"]
    verdict = "BEATS baseline" if beat else "LOSES to baseline"
    print(f"\n  {verdict} by {abs(delta):.3f} points of mean absolute error")
    if not beat:
        print("  A model that cannot beat ep_next should not be trusted with transfers.")

    print("\n  mean absolute error by position")
    for code, mae in sorted(result["by_pos"].items()):
        print(f"    {pos_names.get(code, code):<5}{mae:>8.3f}")

    worst = sorted(result["rows"], key=lambda r: -abs(r["error"]))[:8]
    print("\n  biggest misses")
    print(f"    {'player':<18}{'xP':>6}{'actual':>8}{'error':>8}")
    for r in worst:
        print(f"    {r['name'][:17]:<18}{r['xp']:>6.2f}{r['actual']:>8.0f}{r['error']:>+8.1f}")
    print()


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Score predictions against results")
    ap.add_argument("--gw", type=int, help="gameweek to score (default: latest finished)")
    ap.add_argument("--history", action="store_true", help="score every gameweek with data")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    store = Store()
    try:
        if args.history:
            done = [r["id"] for r in store.conn.execute(
                "SELECT id FROM gameweek WHERE finished = 1 ORDER BY id")]
            if not done:
                print("no finished gameweeks yet — nothing to score")
                return 0
            for gw in done:
                try:
                    fetch_actuals(store, gw)
                    _print(score(store, gw))
                except RuntimeError as exc:
                    print(f"GW{gw}: {exc}")
            return 0

        gw = args.gw
        if gw is None:
            row = store.conn.execute(
                "SELECT MAX(id) AS gw FROM gameweek WHERE finished = 1").fetchone()
            gw = row["gw"] if row else None
            if gw is None:
                print("no finished gameweek yet — nothing to score. "
                      "The collector keeps the deadline table up to date.")
                return 0

        n = fetch_actuals(store, gw)
        result = score(store, gw)
        _print(result)
        store.log_run("reconcile", "ok",
                      f"gw={gw} scored={result['scored']} of {n} "
                      f"mae={result['model_mae']:.3f} baseline={result['baseline_mae']:.3f}")
    except RuntimeError as exc:
        store.log_run("reconcile", "error", str(exc))
        print(f"reconcile: {exc}")
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
