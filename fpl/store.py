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
    n_players   INTEGER NOT NULL,
    rules       TEXT
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
    strength_overall_home   INTEGER,
    strength_overall_away   INTEGER,
    PRIMARY KEY (snapshot_id, team_id)
);

-- Deadlines, so the scheduler never has to guess when a gameweek closes.
CREATE TABLE IF NOT EXISTS gameweek (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    deadline_time   TEXT,
    is_current      INTEGER,
    is_next         INTEGER,
    finished        INTEGER,
    updated_at      TEXT NOT NULL
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
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently keeps an older table's columns, so
        new fields have to be ALTERed in explicitly or existing databases would
        keep reading NULL forever.
        """
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(team_snapshot)")}
        for col in ("strength_overall_home", "strength_overall_away"):
            if col not in have:
                self.conn.execute(f"ALTER TABLE team_snapshot ADD COLUMN {col} INTEGER")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(snapshot)")}
        if "rules" not in have:
            self.conn.execute("ALTER TABLE snapshot ADD COLUMN rules TEXT")

    def close(self) -> None:
        self.conn.close()

    # ---------- writes ----------

    def save_bootstrap(self, data: dict) -> int:
        """Insert one snapshot of the whole game state. Returns snapshot id."""
        events = data["events"]
        gw_current = next((e["id"] for e in events if e.get("is_current")), None)
        gw_next = next((e["id"] for e in events if e.get("is_next")), None)

        # Squad size, budget, team limit and the sell-on fee all come from the
        # API so the optimiser stays correct if FPL changes them mid-season.
        cur = self.conn.execute(
            """INSERT INTO snapshot (taken_at, gw_current, gw_next, n_players, rules)
               VALUES (?,?,?,?,?)""",
            (utcnow(), gw_current, gw_next, len(data["elements"]),
             json.dumps(data.get("game_settings") or {}, separators=(",", ":"))),
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

        # strength_attack_* and strength_defence_* are all zero until the season
        # starts; strength_overall_* is populated from day one, so both go in.
        self.conn.executemany(
            """INSERT INTO team_snapshot
                 (snapshot_id, team_id, name, short_name,
                  strength_attack_home, strength_attack_away,
                  strength_defence_home, strength_defence_away,
                  strength_overall_home, strength_overall_away)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    sid, t["id"], t["name"], t["short_name"],
                    t.get("strength_attack_home"), t.get("strength_attack_away"),
                    t.get("strength_defence_home"), t.get("strength_defence_away"),
                    t.get("strength_overall_home"), t.get("strength_overall_away"),
                )
                for t in data["teams"]
            ],
        )
        self.save_gameweeks(events)
        self.conn.commit()
        return sid

    def save_gameweeks(self, events: list) -> int:
        """Upsert gameweek deadlines. Not append-only: a deadline is a fact
        about the calendar, not a time series, and FPL does move them."""
        now = utcnow()
        self.conn.executemany(
            """INSERT INTO gameweek VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, deadline_time=excluded.deadline_time,
                 is_current=excluded.is_current, is_next=excluded.is_next,
                 finished=excluded.finished, updated_at=excluded.updated_at""",
            [
                (
                    e["id"], e.get("name"), e.get("deadline_time"),
                    int(bool(e.get("is_current"))), int(bool(e.get("is_next"))),
                    int(bool(e.get("finished"))), now,
                )
                for e in events
            ],
        )
        return len(events)

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

    def latest_snapshot_id(self) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM snapshot").fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("no snapshots yet — run `python -m fpl.collect` first")
        return int(row[0])

    def players(self, snapshot_id: int | None = None) -> list[sqlite3.Row]:
        sid = snapshot_id or self.latest_snapshot_id()
        return self.conn.execute(
            "SELECT * FROM player_snapshot WHERE snapshot_id = ?", (sid,)
        ).fetchall()

    def teams(self, snapshot_id: int | None = None) -> list[sqlite3.Row]:
        sid = snapshot_id or self.latest_snapshot_id()
        return self.conn.execute(
            "SELECT * FROM team_snapshot WHERE snapshot_id = ?", (sid,)
        ).fetchall()

    def fixtures(self, first_gw: int, last_gw: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM fixture
               WHERE event BETWEEN ? AND ? AND finished = 0
               ORDER BY event, kickoff_time""",
            (first_gw, last_gw),
        ).fetchall()

    def rules(self, snapshot_id: int | None = None) -> dict:
        """FPL's own squad rules from the latest snapshot, with fallbacks in case
        an older snapshot predates the column."""
        sid = snapshot_id or self.latest_snapshot_id()
        row = self.conn.execute(
            "SELECT rules FROM snapshot WHERE id = ?", (sid,)
        ).fetchone()
        raw = json.loads(row["rules"]) if row and row["rules"] else {}
        return {
            "squad_size": raw.get("squad_squadsize", 15),
            "starting": raw.get("squad_squadplay", 11),
            "team_limit": raw.get("squad_team_limit", 3),
            "budget": raw.get("squad_total_spend", 1000),
            "sell_on_fee": raw.get("transfers_sell_on_fee", 0.5),
            "transfers_cap": raw.get("transfers_cap", 20),
            "max_free_transfers": (raw.get("max_extra_free_transfers", 4) or 4) + 1,
        }

    def next_gameweek(self) -> sqlite3.Row | None:
        """The gameweek to plan for: the flagged next one, else the earliest
        unfinished one, so this still answers sensibly mid-season."""
        return self.conn.execute(
            """SELECT * FROM gameweek
               ORDER BY is_next DESC, (finished = 0) DESC, id
               LIMIT 1"""
        ).fetchone()
