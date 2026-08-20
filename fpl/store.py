"""Append-only SQLite store for FPL snapshots.

bootstrap-static is a snapshot, not a history: it says what a player costs today
and gives you no way to learn what they cost last week. Price trajectory,
ownership momentum and form decay only exist if we have been recording them, and
they cannot be back-filled. Hence: append-only, from day one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "fpl.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    gw_current  INTEGER,
    gw_next     INTEGER,
    n_players   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS player_snapshot (
    snapshot_id             INTEGER NOT NULL REFERENCES snapshot(id),
    element_id              INTEGER NOT NULL,
    web_name                TEXT,
    team                    INTEGER,
    element_type            INTEGER,
    now_cost                INTEGER,
    status                  TEXT,
    news                    TEXT,
    chance_next             INTEGER,
    minutes                 INTEGER,
    starts                  INTEGER,
    total_points            INTEGER,
    form                    REAL,
    ep_next                 REAL,
    selected_by_percent     REAL,
    transfers_in_event      INTEGER,
    transfers_out_event     INTEGER,
    xg90                    REAL,
    xa90                    REAL,
    xgc90                   REAL,
    defcon                  INTEGER,
    defcon90                REAL,
    bps                     INTEGER,
    penalties_order         INTEGER,
    raw                     TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, element_id)
);
CREATE INDEX IF NOT EXISTS idx_player_element ON player_snapshot(element_id);

CREATE TABLE IF NOT EXISTS team_snapshot (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id),
    team_id         INTEGER NOT NULL,
    name            TEXT,
    short_name      TEXT,
    strength_attack_home    INTEGER,
    strength_attack_away    INTEGER,
    strength_defence_home   INTEGER,
    strength_defence_away   INTEGER,
    PRIMARY KEY (snapshot_id, team_id)
);

CREATE TABLE IF NOT EXISTS fixture (
    id              INTEGER PRIMARY KEY,
    event           INTEGER,
    team_h          INTEGER,
    team_a          INTEGER,
    team_h_difficulty INTEGER,
    team_a_difficulty INTEGER,
    kickoff_time    TEXT,
    finished        INTEGER,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_change (
    element_id      INTEGER NOT NULL,
    detected_at     TEXT NOT NULL,
    old_cost        INTEGER NOT NULL,
    new_cost        INTEGER NOT NULL,
    PRIMARY KEY (element_id, detected_at)
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT NOT NULL,
    job         TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT
);
"""


def _num(value, cast=float):
    """FPL returns numerics as strings, nulls as None and blanks as ''."""
    if value in (None, ""):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- writes ----------

    def save_bootstrap(self, data: dict) -> int:
        """Insert one snapshot of the whole game state. Returns snapshot id."""
        events = data["events"]
        gw_current = next((e["id"] for e in events if e.get("is_current")), None)
        gw_next = next((e["id"] for e in events if e.get("is_next")), None)

        cur = self.conn.execute(
            "INSERT INTO snapshot (taken_at, gw_current, gw_next, n_players) VALUES (?,?,?,?)",
            (utcnow(), gw_current, gw_next, len(data["elements"])),
        )
        sid = cur.lastrowid

        self.conn.executemany(
            """INSERT INTO player_snapshot VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    sid, e["id"], e["web_name"], e["team"], e["element_type"],
                    e["now_cost"], e["status"], e.get("news") or None,
                    _num(e.get("chance_of_playing_next_round"), int),
                    e.get("minutes"), e.get("starts"), e.get("total_points"),
                    _num(e.get("form")), _num(e.get("ep_next")),
                    _num(e.get("selected_by_percent")),
                    e.get("transfers_in_event"), e.get("transfers_out_event"),
                    _num(e.get("expected_goals_per_90")),
                    _num(e.get("expected_assists_per_90")),
                    _num(e.get("expected_goals_conceded_per_90")),
                    e.get("defensive_contribution"),
                    _num(e.get("defensive_contribution_per_90")),
                    e.get("bps"), _num(e.get("penalties_order"), int),
                    json.dumps(e, separators=(",", ":")),
                )
                for e in data["elements"]
            ],
        )

        self.conn.executemany(
            "INSERT INTO team_snapshot VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    sid, t["id"], t["name"], t["short_name"],
                    t.get("strength_attack_home"), t.get("strength_attack_away"),
                    t.get("strength_defence_home"), t.get("strength_defence_away"),
                )
                for t in data["teams"]
            ],
        )
        self.conn.commit()
        return sid

    def save_fixtures(self, fixtures: list) -> int:
        now = utcnow()
        self.conn.executemany(
            """INSERT INTO fixture VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 event=excluded.event, team_h_difficulty=excluded.team_h_difficulty,
                 team_a_difficulty=excluded.team_a_difficulty,
                 kickoff_time=excluded.kickoff_time, finished=excluded.finished,
                 updated_at=excluded.updated_at""",
            [
                (
                    f["id"], f.get("event"), f["team_h"], f["team_a"],
                    f.get("team_h_difficulty"), f.get("team_a_difficulty"),
                    f.get("kickoff_time"), int(bool(f.get("finished"))), now,
                )
                for f in fixtures
            ],
        )
        self.conn.commit()
        return len(fixtures)

    def detect_price_changes(self, snapshot_id: int) -> list[dict]:
        """Diff this snapshot's prices against the previous one."""
        prev = self.conn.execute(
            "SELECT id FROM snapshot WHERE id < ? ORDER BY id DESC LIMIT 1", (snapshot_id,)
        ).fetchone()
        if prev is None:
            return []

        rows = self.conn.execute(
            """SELECT c.element_id, c.web_name, p.now_cost AS old_cost, c.now_cost AS new_cost
               FROM player_snapshot c
               JOIN player_snapshot p
                 ON p.element_id = c.element_id AND p.snapshot_id = ?
               WHERE c.snapshot_id = ? AND c.now_cost <> p.now_cost""",
            (prev["id"], snapshot_id),
        ).fetchall()

        now = utcnow()
        self.conn.executemany(
            "INSERT OR IGNORE INTO price_change VALUES (?,?,?,?)",
            [(r["element_id"], now, r["old_cost"], r["new_cost"]) for r in rows],
        )
        self.conn.commit()
        return [dict(r) for r in rows]

    def log_run(self, job: str, status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO run_log (ran_at, job, status, detail) VALUES (?,?,?,?)",
            (utcnow(), job, status, detail),
        )
        self.conn.commit()

    # ---------- reads ----------

    def snapshot_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]

    def latest_snapshot(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM snapshot ORDER BY id DESC LIMIT 1"
        ).fetchone()
