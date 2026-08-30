"""Render the advisory report as a static HTML page for GitHub Pages.

The pipeline writes this to `site/index.html` and Actions publishes it, so the
squad, the model's standing and the pipeline's health are readable from any
browser without a GitHub login or a Python install.

    python -m fpl.dashboard --out site/index.html

Everything is inlined — no external CSS, fonts or scripts — so the page renders
from a single file and cannot break because a CDN moved. The only JavaScript is
a deadline countdown, and the page states the deadline in text as well, so it
still reads correctly with scripting disabled.
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import features, optimise, project
from .store import Store

HORIZON = 5


def _esc(value) -> str:
    return html.escape(str(value))


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


CSS = """
:root{
  --ground:#f4f3f7; --surface:#fff; --sunk:#ebe8f0; --ink:#1b1020;
  --ink-soft:#584b60; --ink-faint:#8a7d92; --line:#dcd6e4; --line-hard:#c4bad0;
  --accent:#4a0b52; --accent-2:#0b7c74; --warn:#9c4a12; --crit:#a0151f; --ok:#1b6d46;
  --chip:#e7e2ee;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#130a17; --surface:#1c1122; --sunk:#170d1c; --ink:#f1ebf4;
    --ink-soft:#b4a6bc; --ink-faint:#867890; --line:#31243a; --line-hard:#463453;
    --accent:#dca9f0; --accent-2:#3fd3c3; --warn:#e9a160; --crit:#ff8e96;
    --ok:#5fd79c; --chip:#2a1b33;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:0 20px 80px;background:var(--ground);color:var(--ink);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto}
header{padding:48px 0 26px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:.7rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
  color:var(--accent-2);margin:0 0 .7em}
h1{font-size:clamp(2rem,6vw,3rem);font-weight:800;letter-spacing:-.03em;line-height:1;margin:0}
h2{font-size:1.15rem;font-weight:700;letter-spacing:-.01em;margin:0 0 14px}
section{padding:38px 0 0}
.strip{display:flex;flex-wrap:wrap;gap:1px;background:var(--line-hard);
  border:1px solid var(--line-hard);border-radius:6px;overflow:hidden;margin-top:26px}
.strip>div{flex:1 1 150px;background:var(--surface);padding:14px 16px}
.strip dt{font-size:.63rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink-faint);margin:0 0 5px}
.strip dd{margin:0;font-size:1.3rem;font-weight:700;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.hot{color:var(--crit)}
.tscroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:560px;font-size:.88rem}
th{text-align:left;font-size:.63rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-faint);padding:10px 14px;border-bottom:1px solid var(--line-hard);
  background:var(--sunk);white-space:nowrap}
td{padding:9px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr.bench td{color:var(--ink-soft);background:var(--sunk)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:.6rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;padding:2px 6px;border-radius:3px;background:var(--chip);
  color:var(--ink-soft);margin-left:6px}
.tag.c{background:color-mix(in srgb,var(--accent-2) 20%,transparent);color:var(--accent-2)}
.ok{color:var(--ok)} .warn{color:var(--warn)} .fail{color:var(--crit)}
.note{border-left:3px solid var(--warn);background:var(--surface);padding:14px 18px;
  border-radius:0 6px 6px 0;margin:0 0 18px;font-size:.9rem;color:var(--ink-soft)}
.note strong{color:var(--ink)}
ul{margin:0;padding-left:1.15em;color:var(--ink-soft);font-size:.9rem}
li{margin-bottom:.35em}
footer{margin-top:52px;padding-top:20px;border-top:1px solid var(--line);
  font-size:.78rem;color:var(--ink-faint)}
code{background:var(--sunk);padding:.1em .35em;border-radius:3px;font-size:.85em}
@media (max-width:600px){body{padding:0 14px 56px}header{padding-top:32px}}
"""

COUNTDOWN_JS = """
(function(){
  var el=document.getElementById('cd'), iso=el&&el.dataset.deadline;
  if(!el||!iso)return;
  function tick(){
    var left=new Date(iso)-new Date();
    if(left<=0){el.textContent='PASSED';return;}
    var d=Math.floor(left/864e5), h=Math.floor(left/36e5)%24, m=Math.floor(left/6e4)%60;
    el.textContent=(d?d+'d ':'')+h+'h '+m+'m';
  }
  tick(); setInterval(tick,30000);
})();
"""


def _squad_table(df, squad) -> str:
    rows = []
    starting = squad.starting.sort_values(
        ["element_type", "xp_next"], ascending=[True, False])
    for pid in list(starting.index) + list(squad.bench_order):
        p = df.loc[pid]
        bench = pid in squad.bench_order
        tag = ""
        if pid == squad.captain:
            tag = '<span class="tag c">C</span>'
        elif pid == squad.vice:
            tag = '<span class="tag">V</span>'
        rows.append(
            f'<tr class="{"bench" if bench else ""}">'
            f'<td>{_esc(p["name"])}{tag}</td>'
            f'<td>{_esc(p.team_short)}</td><td>{_esc(p.pos)}</td>'
            f'<td class="num">{p.cost / 10:.1f}</td>'
            f'<td class="num">{p.p_start:.0%}</td>'
            f'<td class="num">{p.xp_next:.2f}</td>'
            f'<td class="num">{p.xp_total:.1f}</td>'
            f'<td class="num">{p.ep_next:.1f}</td></tr>'
        )
    return (
        '<div class="tscroll"><table><thead><tr>'
        '<th>Player</th><th>Team</th><th>Pos</th>'
        '<th class="num">£m</th><th class="num">Start</th>'
        '<th class="num">xP</th><th class="num">xP5</th><th class="num">FPL</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )


def _price_table(store) -> str:
    rows = store.conn.execute(
        """SELECT p.web_name, c.old_cost, c.new_cost, c.detected_at
           FROM price_change c
           JOIN player_snapshot p ON p.element_id = c.element_id
             AND p.snapshot_id = (SELECT MAX(id) FROM snapshot)
           ORDER BY c.detected_at DESC LIMIT 12"""
    ).fetchall()
    if not rows:
        return '<p class="note">No price changes recorded yet — this needs two ' \
               'snapshots to compare, and it is the one signal FPL does not ' \
               'publish a history for.</p>'
    out = []
    for r in rows:
        rise = r["new_cost"] > r["old_cost"]
        out.append(
            f'<tr><td>{_esc(r["web_name"])}</td>'
            f'<td class="num">{r["old_cost"] / 10:.1f}</td>'
            f'<td class="num">{r["new_cost"] / 10:.1f}</td>'
            f'<td class="{"ok" if rise else "fail"}">{"rise" if rise else "fall"}</td>'
            f'<td>{_esc((r["detected_at"] or "")[:16].replace("T", " "))}</td></tr>'
        )
    return ('<div class="tscroll"><table><thead><tr><th>Player</th>'
            '<th class="num">From</th><th class="num">To</th><th>Move</th>'
            '<th>Detected (UTC)</th></tr></thead><tbody>'
            + "".join(out) + "</tbody></table></div>")


def render(store: Store) -> str:
    gw_row = store.next_gameweek()
    gw = gw_row["id"] if gw_row else 1
    rules = store.rules()

    df = features.build(store)
    df = project.project(df, features.fixture_horizon(store, gw, HORIZON), gw, HORIZON)
    squad = optimise.best_squad(df, rules)

    snap = store.latest_snapshot()
    deadline = _iso(gw_row["deadline_time"]) if gw_row else None
    corr = df.xp_next.corr(df.ep_next)
    covered = store.history_coverage()
    now = datetime.now(timezone.utc)

    snap_age = now - (_iso(snap["taken_at"]) or now)
    stale = snap_age.total_seconds() > 30 * 3600
    recorded = len(store.latest_predictions(gw))

    deadline_text = deadline.strftime("%a %d %b %H:%M UTC") if deadline else "unknown"
    cd_attr = f' data-deadline="{_esc(deadline.isoformat())}"' if deadline else ""

    checks = [
        ("Snapshot", f"{snap_age.total_seconds() / 3600:.0f}h old",
         "fail" if stale else "ok"),
        ("Player history", f"{covered} players", "ok" if covered > 400 else "warn"),
        ("Predictions on file", f"{recorded} for GW{gw}", "ok" if recorded else "warn"),
    ]
    check_html = "".join(
        f'<li><strong>{_esc(n)}:</strong> <span class="{c}">{_esc(v)}</span></li>'
        for n, v, c in checks
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>FPL Autopilot — GW{gw}</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header>
  <p class="eyebrow">Advisory only · nothing here writes to the team</p>
  <h1>FPL Autopilot</h1>
  <dl class="strip">
    <div><dt>Next deadline</dt><dd class="hot" id="cd"{cd_attr}>{_esc(deadline_text)}</dd></div>
    <div><dt>Gameweek</dt><dd>GW{gw}</dd></div>
    <div><dt>XI expected pts</dt><dd>{squad.xp_next:.1f}</dd></div>
    <div><dt>vs FPL baseline</dt><dd>{corr:.3f}</dd></div>
  </dl>
</header>

<section>
  <h2>Suggested squad — £{squad.cost / 10:.1f}m of £{rules['budget'] / 10:.1f}m</h2>
  <p class="note"><strong>This is a shortlist to argue with, not an answer.</strong>
  The model correlates {corr:.2f} with FPL's own <code>ep_next</code>, which is not yet
  good enough to trust with transfers. Its real accuracy is unknown until predictions
  are scored against played gameweeks.</p>
  {_squad_table(df, squad)}
  <p class="note" style="border-left-color:var(--line-hard)">
  Shaded rows are the bench, in substitution order. <strong>xP</strong> is expected
  points for GW{gw}, <strong>xP5</strong> over the next {HORIZON} gameweeks,
  <strong>FPL</strong> is their own estimate for comparison.</p>
</section>

<section>
  <h2>Recent price moves</h2>
  {_price_table(store)}
</section>

<section>
  <h2>Pipeline health</h2>
  <ul>{check_html}</ul>
</section>

<footer>
  Built {now:%Y-%m-%d %H:%M} UTC from snapshot #{snap['id']} ·
  {len(df)} players · deadline {_esc(deadline_text)}.
  Regenerated by the scheduled pipeline; run <code>python -m fpl.status</code> locally
  for the full health check.
</footer>

</div>
<script>{COUNTDOWN_JS}</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Render the dashboard to HTML")
    ap.add_argument("--out", default="site/index.html", help="output path")
    args = ap.parse_args(argv)

    store = Store()
    try:
        page = render(store)
    finally:
        store.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
