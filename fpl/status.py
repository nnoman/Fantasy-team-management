"""Health check: is this install working, and is the data current?

Answers "does it work?" without needing to know anything about the schema. Run
it after cloning on a new machine, and any time the output looks wrong.

    python -m fpl.status

Every check prints OK, WARN or FAIL and says what to do about it. Exit code is
0 when nothing FAILed, so it doubles as a CI or cron guard.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

# Snapshots drive everything downstream, and the pipeline runs twice daily, so
# anything older than this means the schedule has stopped firing.
STALE_AFTER = timedelta(hours=30)


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def line(self, level: str, label: str, detail: str, fix: str = "") -> None:
        if level == "FAIL":
            self.failed += 1
        elif level == "WARN":
            self.warned += 1
        print(f"  [{level:4}] {label:<24} {detail}")
        if fix and level != "OK":
            print(f"         {'':<24} -> {fix}")


def _age(iso: str | None) -> timedelta | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp


def _pretty(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if abs(hours) < 1:
        return f"{int(delta.total_seconds() / 60)}m"
    if abs(hours) < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def check(report: Report) -> None:
    # --- dependencies ---------------------------------------------------------
    missing = []
    for module in ("requests", "pandas", "pulp"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        report.line("FAIL", "dependencies", f"missing {', '.join(missing)}",
                    "pip install -r requirements.txt")
        return
    report.line("OK", "dependencies", "requests, pandas, pulp present")

    from .store import DEFAULT_DB, Store

    # --- database -------------------------------------------------------------
    if not DEFAULT_DB.exists():
        report.line("FAIL", "database", f"not found at {DEFAULT_DB}",
                    "python -m fpl.collect  (then python -m fpl.enrich)")
        return

    store = Store()
    try:
        size_mb = DEFAULT_DB.stat().st_size / 1_048_576
        snapshots = store.snapshot_count()
        if snapshots == 0:
            report.line("FAIL", "database", f"{size_mb:.1f} MB but no snapshots",
                        "python -m fpl.collect")
            return
        report.line("OK", "database", f"{size_mb:.1f} MB, {snapshots} snapshots")

        # --- freshness --------------------------------------------------------
        latest = store.latest_snapshot()
        age = _age(latest["taken_at"])
        if age is None:
            report.line("WARN", "snapshot freshness", "cannot read timestamp")
        elif age > STALE_AFTER:
            report.line("FAIL", "snapshot freshness", f"newest is {_pretty(age)} old",
                        "the scheduled pipeline is not running — check the Actions tab")
        else:
            report.line("OK", "snapshot freshness", f"newest is {_pretty(age)} old")

        # --- the time series that cannot be back-filled -----------------------
        prices = store.conn.execute("SELECT COUNT(*) FROM price_change").fetchone()[0]
        if snapshots < 2:
            report.line("WARN", "price history", "needs 2+ snapshots to detect changes")
        else:
            report.line("OK", "price history", f"{prices} changes recorded")

        # --- player history ---------------------------------------------------
        covered = store.history_coverage()
        total = len(store.players())
        share = covered / total if total else 0
        if share < 0.5:
            report.line("FAIL", "player history", f"{covered}/{total} players",
                        "python -m fpl.enrich  (~10 min; the model is weak without it)")
        elif share < 0.75:
            report.line("WARN", "player history", f"{covered}/{total} players",
                        "python -m fpl.enrich to fill the rest")
        else:
            report.line("OK", "player history", f"{covered}/{total} players have prior seasons")

        # --- the gameweek we are planning for ---------------------------------
        gw = store.next_gameweek()
        if gw is None:
            report.line("FAIL", "next gameweek", "no gameweek table",
                        "python -m fpl.collect")
            return
        deadline = _age(gw["deadline_time"])
        when = f"in {_pretty(-deadline)}" if deadline and deadline.total_seconds() < 0 \
            else "PASSED"
        report.line("OK", "next gameweek", f"GW{gw['id']}, deadline {when}")

        # --- predictions, which cannot be back-filled -------------------------
        recorded = store.latest_predictions(gw["id"])
        if not recorded:
            report.line("WARN", "predictions", f"none recorded for GW{gw['id']}",
                        "python -m fpl.advise  (must run BEFORE the deadline or the "
                        "model can never be scored for this week)")
        else:
            made = _age(recorded[0]["made_at"])
            report.line("OK", "predictions",
                        f"{len(recorded)} for GW{gw['id']}, made {_pretty(made)} ago")

        # --- scoring ----------------------------------------------------------
        done = store.conn.execute(
            "SELECT MAX(id) FROM gameweek WHERE finished = 1").fetchone()[0]
        if done is None:
            report.line("OK", "reconciliation", "no finished gameweek yet")
        else:
            scored = store.conn.execute(
                "SELECT COUNT(*) FROM actual WHERE gw = ?", (done,)).fetchone()[0]
            had = store.conn.execute(
                "SELECT COUNT(*) FROM prediction WHERE gw = ?", (done,)).fetchone()[0]
            if scored and had:
                report.line("OK", "reconciliation", f"GW{done} scored",
                            "python -m fpl.reconcile for the numbers")
            elif scored:
                report.line("WARN", "reconciliation",
                            f"GW{done} results stored but no forecast was on file",
                            "expected until a full week runs before a deadline")
            else:
                report.line("WARN", "reconciliation", f"GW{done} finished, not scored",
                            "python -m fpl.reconcile")

        # --- auth, which only the write path needs ----------------------------
        from .client import Client
        client = Client()
        if not client.has_token:
            report.line("OK", "authentication", "no token — advisory mode",
                        "expected: the write path is not built yet")
        elif client.is_authenticated():
            report.line("OK", "authentication", "token accepted")
        else:
            report.line("WARN", "authentication", "token present but rejected",
                        "access tokens last 8 hours; recapture it")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    print("\nFPL Autopilot — health check\n")
    report = Report()
    check(report)

    print()
    if report.failed:
        print(f"  {report.failed} FAILED, {report.warned} warning(s). "
              f"Fix the FAIL lines above.\n")
        return 1
    if report.warned:
        print(f"  Working, with {report.warned} warning(s).\n")
    else:
        print("  All checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
