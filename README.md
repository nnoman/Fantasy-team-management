# FPL Autopilot

Automated Fantasy Premier League squad management for entry `6643465`.

Snapshots the FPL API nightly, projects expected points per player per gameweek,
solves the squad as a multi-week optimisation, and applies the result — after you
approve it.

**Status: phase 1 (collector) is live. Nothing writes to the FPL account yet.**

## Quick start

```bash
pip install -r requirements.txt
FPL_ENTRY_ID=6643465 python -m fpl.collect
```

Writes to `data/fpl.sqlite3`. Safe to run repeatedly — snapshots are append-only.

## Why the database matters

`bootstrap-static` is a snapshot, not a history. It tells you what a player costs
today and gives you no way to learn what they cost last week. Price trajectory,
ownership momentum, form decay and injury-news recency only exist if we have been
recording them, and **they cannot be back-filled**. Every night this does not run
is a night of history permanently lost, which is why the collector shipped before
anything intelligent.

## Layout

| Path | Role |
| --- | --- |
| `fpl/client.py` | HTTP, retries, rate limiting, schema validation, auth health check |
| `fpl/store.py` | SQLite schema, append-only snapshots, price-change detection |
| `fpl/collect.py` | The nightly job |
| `.github/workflows/collect.yml` | Cron at 02:15 and 22:45 UTC |

## Configuration

Both are optional for the collector and required for the write path.

| Variable | Purpose |
| --- | --- |
| `FPL_ENTRY_ID` | `6643465` |
| `FPL_COOKIE` | Browser session cookie. Enables reading your squad and, later, writing to it. |

### Capturing `FPL_COOKIE`

FPL retired scripted password login — `users.premierleague.com` no longer resolves
at all, and `account.premierleague.com` sits behind bot protection. There is no way
to hand a script your password. Instead the bot borrows a session you create yourself:

1. Log in to <https://fantasy.premierleague.com> in Chrome.
2. DevTools (F12) → **Application** → **Storage** → **Cookies** → `https://fantasy.premierleague.com`.
3. Copy the values of **`pl_profile`** and **`sessionid`** — these are the ones that
   carry the session. `pl_guest_id` is an anonymous visitor ID and does **not**
   authenticate anything.
4. Set the variable as one string:
   `FPL_COOKIE="pl_profile=<value>; sessionid=<value>"`

Verify it:

```bash
FPL_COOKIE="pl_profile=...; sessionid=..." python -c \
  "from fpl.client import Client; print('authenticated:', Client().is_authenticated())"
```

`me/` returns `{"player": null}` when the cookie is not working, so a wrong value
fails clearly rather than silently.

The cookie expires periodically — expect to re-capture it roughly monthly. Every
scheduled run health-checks it and warns early, so it never surprises you at a
deadline. Treat it as equivalent to your password: it belongs in a GitHub secret,
never in a commit.

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Collector, store, nightly cron | **done** |
| 2 | Projection model, best XI, captain — advisory | next |
| 3 | Multi-gameweek transfer optimiser, chip scenarios | |
| 4 | Write path, approval gate, full schedule | |
| 5 | Backtesting and calibration | ongoing |

## Notes

FPL publishes no API contract and can change it without notice. The client
validates every response against the fields the pipeline depends on and raises
`SchemaDrift` rather than projecting from misread data. Requests are rate limited
to one per second.
