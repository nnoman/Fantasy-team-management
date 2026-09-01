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
| `fpl/enrich.py` | Per-season player history from element-summary |
| `fpl/reconcile.py` | Scores predictions against what actually happened |
| `fpl/status.py` | Health check (`python -m fpl.status`) |
| `fpl/team.py` | Your real squad, bank and free transfers (public data) |
| `fpl/plan.py` | Transfer planner against the squad you own |
| `fpl/dashboard.py` | Static HTML page published to GitHub Pages |
| `tests/test_model.py` | `python -m pytest tests/` |
| `.github/workflows/collect.yml` | The scheduled pipeline |
| `.github/workflows/ci.yml` | Tests on push |

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

### The season rollover, and why it nearly broke everything

`bootstrap-static` serves *last* season's per-player aggregates right up until the new
season starts, then resets them to zero. Both states look identical to a reader — the
same fields with plausible numbers in them — and code written against one is silently
wrong against the other:

- Dividing starts by 38 is right in August and reads a nailed-on starter as a **5%**
  starter in September.
- `minutes == 0` means "no evidence" before a ball is kicked and "is being dropped"
  two games in. Treating the second as the first put a fit-but-benched £7.9m forward
  in the squad and captained him, while FPL's own `ep_next` read him at 0.0.
- Every per-90 rate computed from a 180-minute sample shrinks to its position
  baseline, so start probability goes flat across all 600-odd players and the model
  loses its discrimination entirely.

So nothing divides by a fixed season length any more. `Store.team_games_played()`
counts finished fixtures per club — not finished gameweeks, because clubs diverge
through doubles, blanks and postponements — and every rate is read against how much
could actually have been observed.

The sample-size problem is solved with `fpl/enrich.py`, which pulls each player's
completed seasons from `element-summary/{id}/history_past` and pools them into the
current season's rates, weighted by minutes. Rates and roles are blended differently
on purpose: last season is strong evidence about how often a player shoots or makes
defensive actions, and weak evidence about whether he is still in the team. Two games
of being dropped outweighs a season of starting, because from the outside that is what
a changed role looks like.

Players with no Premier League history at all — promoted clubs, new signings, academy
— still fall back to a price-based prior, which is a guess dressed as a number.

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

## The dashboard

The pipeline publishes a page to GitHub Pages on every run:

**<https://nnoman.github.io/Fantasy-team-management/>**

The recommended transfers for the next deadline, your actual squad with both
estimates side by side, the best XI and captain, recent price moves and pipeline
health — readable from any browser with no GitHub login and no Python install.

It is advisory. There is no write path in this project: you apply the transfers
yourself in the FPL app. A page on `github.io` could not do it anyway — the browser's
same-origin policy stops it reading a logged-in FPL tab, and FPL sends no CORS
headers, so it cannot call the API at all.

**Email**: the pipeline opens one GitHub issue per gameweek with the plan, and GitHub
emails the repo owner when an issue is opened. No SMTP credentials involved. Later
runs edit that issue silently; a comment — and therefore a second email — is sent only
when the recommendation actually changes, compared by a hash of the moves and captain
rather than the whole body. It is a single
self-contained file — no external CSS, fonts or scripts — so nothing about it can
break because a CDN moved.

> **This page is public.** GitHub Pages serves publicly on free plans even from a
> private repository, so the squad, the projections and the fact that this is entry
> 6643465 are visible to anyone with the link. It carries `noindex` so search engines
> should skip it, but that is a request, not a guarantee. Nothing secret is on the
> page — no tokens, no cookies — but if that visibility is unwanted, delete the
> `deploy` job from the pipeline and use the Actions job summary instead.

Enabling it is a one-time setting: **Settings → Pages → Source: GitHub Actions**.

## Checking it works

```bash
python -m fpl.status
```

Every check prints OK, WARN or FAIL and, when something is wrong, the command that
fixes it. Exit code is 0 unless something FAILed, so it works as a cron or CI guard,
and it runs as the last step of the scheduled pipeline.

```
  [OK  ] database                 19.4 MB, 8 snapshots
  [OK  ] snapshot freshness       newest is 4h old
  [OK  ] player history           520/623 players have prior seasons
  [OK  ] next gameweek            GW3, deadline in 5.3d
  [OK  ] predictions              623 for GW3, made 3h ago
```

Snapshot freshness is the check that matters most. Everything downstream still looks
perfectly valid when the schedule stops firing — the data is just quietly old — so
anything past 30 hours is treated as a failure rather than a warning.

## Running it on a second machine

The repository carries **no database** — `data/*.sqlite3` is gitignored, and the
season's history is the one thing that cannot be rebuilt from a clone. So a fresh
checkout needs the data from somewhere.

```bash
git clone https://github.com/nnoman/Fantasy-team-management.git
cd Fantasy-team-management
pip install -r requirements.txt
```

Then pick one:

**Copy the database from the cloud run.** Actions → the latest `pipeline` run →
Artifacts → `fpl-db` → unzip into `data/fpl.sqlite3`. This keeps one shared history
across machines and is the right choice if the pipeline has been running.

**Or build a fresh one locally**, which costs about eleven minutes of API calls and
starts the price and prediction history over from today:

```bash
python -m fpl.collect     # seconds
python -m fpl.enrich      # ~10 minutes, one request per player
python -m fpl.status      # confirm
```

Two machines writing their own copies will diverge — snapshots and predictions are
append-only per database, and there is no merge. Treat the cloud run as the source of
truth and copy from it, rather than collecting independently in two places.

You may not need a local copy at all: the scheduled run posts the advisory report,
the reconciliation and the health check into its job summary, readable from any
browser in the Actions tab.

## Automation

One scheduled workflow does everything, in order, over a single restored copy of the
database: snapshot, refresh history (Mondays), project and record predictions, then
score the last finished gameweek. It is deliberately one job rather than four
workflows — the database lives in the Actions cache, and separate workflows would each
restore, append and save their own copy, so whichever finished last would silently
overwrite the rest.

| When (UTC) | What |
| --- | --- |
| 02:15 daily | Snapshot, project, record predictions, reconcile |
| 22:45 daily | Same, before FPL's price-change deadline |
| 03:15 Mondays | Also refreshes per-season player history (~10 min) |

Each run writes the advisory report and the reconciliation into the workflow's job
summary, so the output is readable in the Actions tab with nothing to download.

Scheduled workflows only fire on the **default branch** — this repo's is `master`, so
that is where this has to live. GitHub also disables scheduled workflows after 60 days
without repository activity.

### Why predictions are recorded before kickoff

`fpl/reconcile.py` grades the model against reality and against FPL's own `ep_next`.
That only works if the forecast was written down *before* the gameweek was played, and
like the snapshots it cannot be back-filled: once a gameweek has happened there is no
way to recover what the model would have said beforehand. Every projection run records
one, which is why `advise` writes to the `prediction` table by default.

Reconciliation refuses to score a gameweek FPL has not marked `finished`. This is not
caution for its own sake: before the 2026/27 season started, `event/1/live/` served
*last* season's GW1 in full — 610 players with 90-minute performances against a current
pool of 623 — so scoring it would have produced a confident and entirely fictitious
accuracy report.

## Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Collector, store, nightly cron | **done** |
| 2 | Projection model, squad/XI/captain advice | **done** |
| 3 | Multi-gameweek transfer optimiser, chip scenarios | next — needs the authenticated `my-team` read |
| 4 | Write path, approval gate, full schedule | blocked on the refresh-token flow above |
| 5 | Backtesting and calibration | **wired** — predictions recorded, reconciliation scheduled |

### Is the model any good yet?

Not yet, and the test suite says so out loud rather than hiding it. Pre-season, with a
full prior season in `bootstrap-static`, projections correlated 0.80 with FPL's own
`ep_next`. After the rollover that collapsed to 0.39, which is why the history
enrichment exists. `test_model_agrees_with_the_fpl_baseline` is marked `xfail` with the
reason spelled out: until it passes, the model has no business recommending transfers.

The real answer comes from `python -m fpl.reconcile` once a few gameweeks have been
predicted and played — mean absolute error against reality, ours versus `ep_next`. That
number, not a correlation against a baseline, is what decides whether any of this was
worth building.

## Notes

FPL publishes no API contract and can change it without notice. The client
validates every response against the fields the pipeline depends on and raises
`SchemaDrift` rather than projecting from misread data. Requests are rate limited
to one per second.
