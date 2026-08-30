# FPL Autopilot

Automated Fantasy Premier League squad management for entry `6643465`.

Snapshots the FPL API nightly, projects expected points per player per gameweek,
solves the squad as a multi-week optimisation, and applies the result — after you
approve it.

**Status: phases 1-2 are live — collector plus advisory projections. Nothing writes
to the FPL account, and the write path is blocked on an auth change (see below).**

## Quick start

```bash
pip install -r requirements.txt

FPL_ENTRY_ID=6643465 python -m fpl.collect   # snapshot the game state
python -m fpl.advise --shortlist             # projections, squad, XI, captain
```

`collect` writes to `data/fpl.sqlite3` and is safe to run repeatedly — snapshots are
append-only. `advise` is read-only, needs no credentials and no network: it reads the
snapshot, projects expected points, solves the squad as an integer program and prints
the result. Neither command can touch your FPL team.

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
| `fpl/features.py` | Per-90 rates, minutes model, fixture horizon, availability |
| `fpl/project.py` | Expected points per player per gameweek |
| `fpl/optimise.py` | PuLP/CBC integer program: squad, XI, captain, bench |
| `fpl/advise.py` | The advisory report (`python -m fpl.advise`) |
| `tests/test_model.py` | `python -m pytest tests/` |
| `.github/workflows/collect.yml` | Cron at 02:15 and 22:45 UTC |

## How the projection works

FPL scoring is additive, so expected points decompose and each part is modelled
separately:

```
xP = P(plays) x [ appearance + goals + assists + clean_sheet
                  + defensive_contribution + saves + bonus ]
     - expected_deductions
```

Minutes dominate. A brilliant player with a 40% chance of starting is worth less than
a dull nailed-on one, and that is where most managers actually lose points. Attacking
returns come from xG and xA per 90 scaled by projected minutes and opponent strength,
never from raw goals. Clean sheets run team expected goals conceded through a Poisson.
Defensive contribution is a threshold crossing (10 for defenders, 12 for midfielders
and forwards), so it needs a distribution rather than a mean.

One thing is load-bearing and easy to miss: **FPL's per-90 fields are unusable raw**.
They divide a season total by minutes played with no regard for sample size, so a
midfielder who played two minutes and won one tackle reads as 45 defensive
contributions and 225 BPS per 90 — and naively projected, he outscores Haaland by
double. Every rate is therefore shrunk toward its position's baseline in proportion to
minutes played (`features.shrink_rates`). Established players are barely touched.

### What it cannot do yet

Before GW1, bootstrap-static carries *last season's* aggregates, so every projection
describes last season's player at last season's club. 195 of 595 players have no
Premier League minutes at all — promoted clubs, new signings, academy — and fall back
to a price-based prior, which is a guess dressed as a number. Treat GW1 output as a
shortlist to argue with, not an answer.

## Configuration

| Variable | Purpose |
| --- | --- |
| `FPL_ENTRY_ID` | `6643465`. Only needed by the collector's optional squad check. |
| `FPL_ACCESS_TOKEN` | Bearer token for reading your squad and, later, writing to it. Not yet wired up — see below. |

Neither is needed for `advise`, or for the snapshot itself. Everything the projection
model reads is public.

### Authentication: what actually works

Earlier revisions of this file told you to capture `pl_profile` and `sessionid` into
an `FPL_COOKIE`. **That is wrong and cannot work.** Probed live on 2026-08-20:

| Sent to `api/my-team/{id}/` | Response |
| --- | --- |
| Session cookies only | `403 {"detail":"Authentication credentials were not provided."}` |
| `x-api-authorization: Bearer <jwt>` | `401 {"detail": "Signature verification failed"}` on a bad token |

Cookies are not credentials here — FPL sees no auth at all. The real mechanism is an
OAuth bearer token in the `x-api-authorization` header, issued by PingOne:

```
issuer     https://account.premierleague.com/as
token      https://account.premierleague.com/as/token
client_id  bfcbaf69-aade-4c1b-8f00-c1cb8a193030
audience   https://api.pingone.eu
scope      openid profile email
lifetime   8 hours
```

Eight hours is the problem. A hand-pasted token is dead long before the next deadline
run, so pasting cannot support a cron schedule at all — which makes the original plan's
"re-paste a cookie monthly" chore not merely tedious but impossible.

The fix is to stop pasting. The discovery document advertises the `refresh_token`
grant and the `offline_access` scope, so a refresh token captured **once** would let
the bot mint its own access tokens indefinitely. That is the next piece of work on the
write path, and it ends up more robust than what was originally planned.

Whatever is captured is equivalent to your password: it belongs in a GitHub secret,
never in a commit, and never pasted into a chat window.

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Collector, store, nightly cron | **done** |
| 2 | Projection model, squad/XI/captain advice | **done** |
| 3 | Multi-gameweek transfer optimiser, chip scenarios | next — needs the authenticated `my-team` read |
| 4 | Write path, approval gate, full schedule | blocked on the refresh-token flow above |
| 5 | Backtesting and calibration | starts once GW1 results land |

## Notes

FPL publishes no API contract and can change it without notice. The client
validates every response against the fields the pipeline depends on and raises
`SchemaDrift` rather than projecting from misread data. Requests are rate limited
to one per second.
