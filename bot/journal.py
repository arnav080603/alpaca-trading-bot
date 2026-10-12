"""Obsidian dashboard (added 2026-10-11): the bot writes "Bot Dashboard.md" into the NEXUS Journal vault
so Arnav can see positions, every trade, daily results and AI decisions in Obsidian. Rebuilt from the
bot's own state and CSV logs; the file is overwritten each time, so nobody should edit it by hand."""
import csv
import json
import logging

import pandas as pd

log = logging.getLogger(__name__)


def _rows(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _money(x, sign=False):
    s = f"${abs(x):,.2f}"
    return (("+" if x >= 0 else "−") if sign else ("−" if x < 0 else "")) + s


def _when(ts):
    return pd.Timestamp(ts).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def render(now, st, equity, logs, decisions_path):
    """The whole note as markdown."""
    day, first = st.get("day") or {}, st.get("first_equity") or equity
    start = day.get("start_equity", equity)
    prices = (st.get("live") or {}).get("prices", {})
    pos = st.get("positions", {})
    trades = _rows(logs / "trades.csv")
    days = {r["date"]: r for r in _rows(logs / "daily_summary.csv")}  # last row per date wins
    vetoes = _rows(logs / "ai_vetoes.csv")
    try:
        dec = json.loads(decisions_path.read_text())
    except Exception:
        dec = None

    L = ["# 🤖 Bot Dashboard", "",
         f"*Written by the bot itself · last update {_when(now)} ET · don't edit this note: the bot overwrites it.*",
         "*How it all works: [[Trading Bot]] · Home: [[Home]]*", "",
         "## Right now", "",
         "| Equity | Today | Since start | Open positions | Kill switch |", "|---|---|---|---|---|",
         f"| **{_money(equity)}** | {_money(equity - start, True)} ({(equity - start) / start:+.2%}) "
         f"| {_money(equity - first, True)} ({(equity - first) / first:+.2%}) | {len(pos)} "
         f"| {'🛑 ' + day['halted'] if day.get('halted') else 'off ✅'} |", ""]

    L += ["## Open positions", ""]
    if pos:
        L += ["| Stock | Strategy | Shares | Bought at | Now | P&L | Stop-loss | Since |", "|---|---|---|---|---|---|---|---|"]
        for sym, p in sorted(pos.items(), key=lambda kv: kv[1]["entry_time"], reverse=True):
            px = prices.get(sym, p["entry"])
            upnl = p["side"] * p["qty"] * (px - p["entry"])
            L.append(f"| **{sym}** | {'🤖 AI pick' if p['leg'] == 'ai_pick' else p['leg']} | {p['qty']:g} | {_money(p['entry'])} "
                     f"| {_money(px)} | {_money(upnl, True)} | {_money(p['stop'])} | {_when(p['entry_time'])} |")
    else:
        L.append("_None right now. The bot buys when a stock's 10-day average crosses above its 100-day average "
                 "(checked at the 9:30 AM open), when Bitcoin breaks out, or when the AI picks a stock._")
    L.append("")

    L += ["## Closed trades", ""]
    if trades:
        pnl = [float(t["pnl"]) for t in trades]
        wins = sum(x > 0 for x in pnl)
        L += [f"**{len(trades)} trade{"s" if len(trades) != 1 else ""} · {wins} wins ({wins / len(trades):.0%}) · total {_money(sum(pnl), True)}**", "",
              "| Closed | Stock | Strategy | Shares | Bought | Sold | P&L | Why it closed |", "|---|---|---|---|---|---|---|---|"]
        for t in reversed(trades):
            L.append(f"| {_when(t['timestamp'])} | **{t['symbol']}** | {'🤖 AI pick' if t['leg'] == 'ai_pick' else t['leg']} "
                     f"| {float(t['qty']):g} | {_money(float(t['entry']))} | {_money(float(t['exit']))} "
                     f"| {'✅' if float(t['pnl']) > 0 else '❌'} {_money(float(t['pnl']), True)} | {t['exit_reason']} |")
    else:
        L.append("_No trades have closed yet._")
    L.append("")

    L += ["## Daily results", ""]
    if days:
        L += ["| Day | Start | End | P&L | Trades | Wins / losses | Kill switch |", "|---|---|---|---|---|---|---|"]
        for d in sorted(days.values(), key=lambda r: r["date"], reverse=True):
            L.append(f"| {d['date']} | {_money(float(d['start_equity']))} | {_money(float(d['end_equity']))} "
                     f"| {_money(float(d['pnl']), True)} | {d['trades']} | {d['wins']} / {d['losses']} "
                     f"| {'🛑 yes' if d['kill_switch_triggered'] == 'True' else 'no'} |")
    else:
        L.append("_The first day closes at midnight._")
    L.append("")

    L += ["## AI", ""]
    if dec:
        v, pk = dec.get("vetoes") or {}, dec.get("picks") or []
        L += [f"**Latest decisions:** {_when(dec['generated_at'])}" + (f" · _{dec['notes']}_" if dec.get("notes") else ""), ""]
        L += [f"- ⛔ Veto **{s}**: {r}" for s, r in v.items()] + [f"- 🟢 Pick **{p['symbol']}**: {p.get('reason', '')}" for p in pk]
        if not v and not pk:
            L.append("- No vetoes and no picks.")
    else:
        L.append("_No AI decisions yet._")
    L += ["", "**Trades the AI blocked** (so we can check later whether blocking them was right):", ""]
    if vetoes:
        L += ["| When | Stock | Price | Reason |", "|---|---|---|---|"]
        L += [f"| {_when(r['timestamp'])} | **{r['symbol']}** | {_money(float(r['price']))} | {r['reason']} |" for r in reversed(vetoes)]
    else:
        L.append("_None yet._")
    return "\n".join(L) + "\n"


def write(path, text):
    """Write only when something changed, so Obsidian doesn't see a new version every minute."""
    try:
        if path.exists() and path.read_text() == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return True
    except OSError as e:
        log.warning("could not write the Obsidian dashboard %s: %s", path, e)
        return False
