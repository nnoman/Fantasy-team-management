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
    data_checked    INTEGER,
    updated_at      TEXT NOT NULL
);

-- Per-season totals from element-summary, the only place a player's prior
-- seasons survive once the new season resets bootstrap-static's aggregates.
-- Keyed on element_code, which is stable across seasons; the per-season `id`
-- is not. Not append-only: a completed season is a fixed fact.
CREATE TABLE IF NOT EXISTS player_history_past (
    element_code    INTEGER NOT NULL,
    season_name     TEXT NOT NULL,
    minutes         INTEGER,
    starts          INTEGER,
    total_points    INTEGER,
    goals_scored    INTEGER,
    assists         INTEGER,
    expected_goals          REAL,
    expected_assists        REAL,
    expected_goals_conceded REAL,
    defensive_contribution  INTEGER,
    bps             INTEGER,
    saves           INTEGER,
    yellow_cards    INTEGER,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (element_code, season_name)
);

-- Which players element-summary has been asked about, and when. Needed because
-- a player who has never appeared in the Premier League has no history_past at
-- all, so "did we already fetch this one?" cannot be answered from
-- player_history_past -- and without it the weekly job re-fetches every
-- historyless player forever, on an API we do not own.
CREATE TABLE IF NOT EXISTS history_fetch (
    element_code    INTEGER PRIMARY KEY,
    seasons         INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL
);

-- What the model said, recorded before kickoff. Append-only and, like the
-- snapshots, impossible to back-fill: once a gameweek is played there is no way
-- to recover what we would have predicted beforehand. Every projection run
-- writes here, so a D-26h forecast and a D-4h one are both kept and can be
-- compared.
CREATE TABLE IF NOT EXISTS prediction (
    gw              INTEGER NOT NULL,
    element_id      INTEGER NOT NULL,
    made_at         TEXT NOT NULL,
    snapshot_id     INTEGER REFERENCES snapshot(id),
    xp              REAL NOT NULL,
    ep_next         REAL,                    -- FPL's own number, the baseline
    p_start         REAL,
    PRIMARY KEY (gw, element_id, made_at)
);
CREATE INDEX IF NOT EXISTS idx_prediction_gw ON prediction(gw);

-- What actually happened, pulled from event/{gw}/live once the gameweek is
-- finished and bonus points have been confirmed.
CREATE TABLE IF NOT EXISTS actual (
    gw              INTEGER NOT NULL,
    element_id      INTEGER NOT NULL,
    minutes         INTEGER,
    total_points    INTEGER,
    scored_at       TEXT NOT NULL,
    PRIMARY KEY (gw, element_id)
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
    finished_provisional INTEGER,
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
    def __init__(self, path: Path | str | None = None):
        # Resolved at call time, not bound as a default argument: a default is
        # evaluated once at import, so overriding DEFAULT_DB later would leave
        # Store() silently opening the original file.
        self.path = Path(path) if path is not None else DEFAULT_DB
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
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(gameweek)")}
        if "data_checked" not in have:
            self.conn.execute("ALTER TABLE gameweek ADD COLUMN data_checked INTEGER")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(fixture)")}
        if "finished_provisional" not in have:
            self.conn.execute(
                "ALTER TABLE fixture ADD COLUMN finished_provisional INTEGER")

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
            """INSERT INTO gameweek
                 (id, name, deadline_time, is_current, is_next, finished,
                  data_checked, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, deadline_time=excluded.deadline_time,
                 is_current=excluded.is_current, is_next=excluded.is_next,
                 finished=excluded.finished, data_checked=excluded.data_checked,
                 updated_at=excluded.updated_at""",
            [
                (
                    e["id"], e.get("name"), e.get("deadline_time"),
                    int(bool(e.get("is_current"))), int(bool(e.get("is_next"))),
                    int(bool(e.get("finished"))), int(bool(e.get("data_checked"))), now,
                )
                for e in events
            ],
        )
        return len(events)

    def save_fixtures(self, fixtures: list) -> int:
        now = utcnow()
        self.conn.executemany(
            """INSERT INTO fixture
                 (id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
                  kickoff_time, finished, finished_provisional, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 event=excluded.event, team_h_difficulty=excluded.team_h_difficulty,
                 team_a_difficulty=excluded.team_a_difficulty,
                 kickoff_time=excluded.kickoff_time, finished=excluded.finished,
                 finished_provisional=excluded.finished_provisional,
                 updated_at=excluded.updated_at""",
            [
                (
                    f["id"], f.get("event"), f["team_h"], f["team_a"],
                    f.get("team_h_difficulty"), f.get("team_a_difficulty"),
                    f.get("kickoff_time"), int(bool(f.get("finished"))),
                    int(bool(f.get("finished_provisional"))), now,
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

    def save_predictions(self, gw: int, snapshot_id: int, rows,
                         made_at: str | None = None) -> int:
        """Record what the model expects, before the gameweek is played.

        `rows` is an iterable of (element_id, xp, ep_next, p_start). One
        `made_at` stamps the whole run, so a single forecast is one readable set
        and re-running within the same second replaces it rather than
        accumulating duplicates.
        """
        made_at = made_at or utcnow()
        rows = list(rows)
        self.conn.executemany(
            "INSERT OR REPLACE INTO prediction VALUES (?,?,?,?,?,?,?)",
            [(gw, int(eid), made_at, snapshot_id, float(xp),
              None if ep is None else float(ep),
              None if ps is None else float(ps))
             for eid, xp, ep, ps in rows],
        )
        self.conn.commit()
        return len(rows)

    def save_actuals(self, gw: int, rows) -> int:
        """Record what really happened. `rows` is (element_id, minutes, points)."""
        now = utcnow()
        rows = list(rows)
        self.conn.executemany(
            "INSERT OR REPLACE INTO actual VALUES (?,?,?,?,?)",
            [(gw, int(eid), int(mins), int(pts), now) for eid, mins, pts in rows],
        )
        self.conn.commit()
        return len(rows)

    def save_history_past(self, element_code: int, seasons: list) -> int:
        now = utcnow()
        self.conn.executemany(
            """INSERT INTO player_history_past VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(element_code, season_name) DO UPDATE SET
                 minutes=excluded.minutes, starts=excluded.starts,
                 total_points=excluded.total_points,
                 goals_scored=excluded.goals_scored, assists=excluded.assists,
                 expected_goals=excluded.expected_goals,
                 expected_assists=excluded.expected_assists,
                 expected_goals_conceded=excluded.expected_goals_conceded,
                 defensive_contribution=excluded.defensive_contribution,
                 bps=excluded.bps, saves=excluded.saves,
                 yellow_cards=excluded.yellow_cards, updated_at=excluded.updated_at""",
            [
                (
                    element_code, h["season_name"], h.get("minutes"), h.get("starts"),
                    h.get("total_points"), h.get("goals_scored"), h.get("assists"),
                    _num(h.get("expected_goals")), _num(h.get("expected_assists")),
                    _num(h.get("expected_goals_conceded")),
                    h.get("defensive_contribution"), h.get("bps"), h.get("saves"),
                    h.get("yellow_cards"), now,
                )
                for h in seasons
            ],
        )
        self.conn.commit()
        return len(seasons)

    def mark_history_fetched(self, element_code: int, seasons: int) -> None:
        self.conn.execute(
            """INSERT INTO history_fetch VALUES (?,?,?)
               ON CONFLICT(element_code) DO UPDATE SET
                 seasons=excluded.seasons, fetched_at=excluded.fetched_at""",
            (element_code, seasons, utcnow()),
        )
        self.conn.commit()

    def history_fetched(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT element_code FROM history_fetch")}

    def previous_season(self) -> dict[int, sqlite3.Row]:
        """Each player's most recent completed season, keyed by element_code."""
        return {
            r["element_code"]: r
            for r in self.conn.execute(
                """SELECT h.* FROM player_history_past h
                   JOIN (SELECT element_code, MAX(season_name) AS s
                         FROM player_history_past GROUP BY element_code) m
                     ON m.element_code = h.element_code AND m.s = h.season_name"""
            )
        }

    def history_coverage(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT element_code) FROM player_history_past"
        ).fetchone()[0]

    def latest_predictions(self, gw: int) -> list[sqlite3.Row]:
        """The most recent forecast for a gameweek — the one that would have been
        acted on, so the one worth scoring."""
        return self.conn.execute(
            """SELECT p.* FROM prediction p
               WHERE p.gw = ? AND p.made_at = (
                   SELECT MAX(made_at) FROM prediction WHERE gw = ?
               )""",
            (gw, gw),
        ).fetchall()

    def team_games_played(self) -> dict[int, int]:
        """Finished fixtures per team.

        Not the finished-gameweek count: teams diverge through double and blank
        gameweeks and postponements, and every rate in the feature layer is
        divided by this.
        """
        # `finished` only flips once FPL has confirmed bonus points, which can
        # be days after the match. `finished_provisional` marks a match that has
        # actually been played. Counting only `finished` reported one game when
        # two had been played, which inflated every start rate -- two starts
        # divided by one game reads as a certainty.
        played: dict[int, int] = {}
        for f in self.conn.execute(
            """SELECT team_h, team_a FROM fixture
               WHERE finished = 1 OR finished_provisional = 1"""
        ):
            played[f["team_h"]] = played.get(f["team_h"], 0) + 1
            played[f["team_a"]] = played.get(f["team_a"], 0) + 1
        return played

    def gameweek(self, gw: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM gameweek WHERE id = ?", (gw,)
        ).fetchone()

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
