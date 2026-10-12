"""Walk-forward backtester for all three legs.

Uses the exact signal code (bot.strategies) and sizing code (bot.risk) the live bot will use.
Fill model: signals on a bar's close fill at the next bar's open. Stops are checked against each
bar's high/low and fill at the stop, or at the open if the bar gaps through it. Slippage and fees
are charged on every fill.

Walk-forward: parameters are chosen on the first 70% of the data only; the last 30% is evaluated
once with the chosen parameters. Every out-of-sample evaluation is appended to
results/holdout_log.csv so it's visible how often the holdout has been looked at.

Usage: .venv/bin/python backtest.py [--legs mean_reversion btc_breakout commodity_trend]
"""
import argparse
import itertools
import logging
import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from bot.config import ROOT, load_config, load_env
from bot.data import MissingKeys, leg_bars
from bot.risk import Held, RiskEngine
from bot.strategies import LEGS

log = logging.getLogger("backtest")
RESULTS = ROOT / "results"


@dataclass
class Position:
    symbol: str
    side: int
    qty: float
    entry: float
    entry_time: object
    stop: float
    initial_stop: float
    high_water: float
    low_water: float
    fees: float
    sized_by: str


def daily_closes(raw):
    """Daily closes per symbol, for the risk engine's correlation filter."""
    return pd.DataFrame({s: df["close"].resample("1D").last() for s, df in raw.items()})


def simulate(leg, prepared, start, end, cfg, allow_shorts, closes=None, at_close=False):
    """Event-driven run of one leg over [start, end) through the live risk engine.
    at_close=True: signals fill at the same bar's close instead of the next open (models a bot that
    decides at 15:50 and trades just before the bell; slightly optimistic, it sees the true close).
    Returns (trades, equity_curve, blocked) where blocked counts blocked entries by reason."""
    c = cfg["costs"]
    risk = RiskEngine(cfg, check_stop_file=False, shorts=allow_shorts)
    crypto = leg.asset == "crypto"
    slip = c["crypto_slippage"] if crypto else c["equity_slippage"]
    fee = c["crypto_fee"] if crypto else 0.0
    rows = {s: {r.Index: r for r in df[(df.index >= start) & (df.index < end)].itertuples()} for s, df in prepared.items()}
    timeline = sorted(set().union(*rows.values()))
    cash = float(cfg["account"]["starting_equity"])
    pos, pending, last, trades, curve, blocked = {}, {}, {}, [], [], {}

    def equity():
        return cash + sum(p.side * p.qty * last[s] for s, p in pos.items())

    def open_pos(s, side, raw, ts, sig_row):
        nonlocal cash
        fill = raw * (1 + side * slip)
        stop = leg.initial_stop(side, fill, sig_row)
        held = [Held(x, p.side, p.qty, last[x]) for x, p in pos.items()]
        d = risk.evaluate(leg.name, s, side, fill, stop, equity(), held, closes, ts.normalize(), cash, crypto)
        if not d.ok:
            key = d.reason.split(" with ")[0]  # group "correlated 0.93 with open X" style reasons
            blocked[key] = blocked.get(key, 0) + 1
            return
        f = d.qty * fill * fee
        cash -= side * d.qty * fill + f
        pos[s] = Position(s, side, d.qty, fill, ts, stop, stop, fill, fill, f, d.reason)

    def close_pos(s, raw, intended, ts, reason):
        nonlocal cash
        p = pos.pop(s)
        fill = raw * (1 - p.side * slip)
        f = p.qty * fill * fee
        cash += p.side * p.qty * fill - f
        if p.side > 0:
            risk.record_sale(p.qty * fill - f, ts.date(), crypto)
        trades.append(dict(entry_time=p.entry_time, exit_time=ts, leg=leg.name, symbol=s,
                           side="long" if p.side > 0 else "short", qty=p.qty, entry=p.entry,
                           stop=p.initial_stop, exit=fill, pnl=p.side * p.qty * (fill - p.entry) - p.fees - f,
                           exit_reason=reason, slippage=p.side * (intended - fill) * p.qty, sized_by=p.sized_by))

    for ts in timeline:
        halted = risk.check(ts.date(), equity())  # kill switch decided on the last marked equity
        for s in rows:
            r = rows[s].get(ts)
            if r is None:
                continue
            last[s] = r.open
            acts = pending.pop(s, [])
            # --- at the open ---
            if halted and s in pos:
                close_pos(s, r.open, r.open, ts, "kill_switch")
            for a in acts:
                if a[0] == "exit" and s in pos:
                    close_pos(s, r.open, r.open, ts, a[1])
            if s in pos and leg.flatten_at_open(ts):
                close_pos(s, r.open, r.open, ts, "eod_flatten")
            for a in acts:
                if a[0] == "enter" and s not in pos and not leg.flatten_at_open(ts):
                    open_pos(s, a[1], r.open, ts, a[2])
            # --- during the bar: stop ---
            p = pos.get(s)
            if p and ((p.side > 0 and r.low <= p.stop) or (p.side < 0 and r.high >= p.stop)):
                raw = min(r.open, p.stop) if p.side > 0 else max(r.open, p.stop)
                close_pos(s, raw, p.stop, ts, "stop")
            # --- at the close ---
            last[s] = r.close
            p = pos.get(s)
            new = []
            if p:
                leg.update_stop(p, r)
                if leg.flatten_at_close(r):
                    close_pos(s, r.close, r.close, ts, "eod_flatten")
                elif why := leg.exit_signal(r, p):
                    new.append(("exit", why))
            p = pos.get(s)
            if not p or new:  # flat, or exiting at next open (allows a reversal on the same signal)
                side = leg.entry_signal(ts, r, allow_shorts)
                if side and (not p or side != p.side):
                    new.append(("enter", side, r))
            if new and at_close:
                for a in new:
                    if a[0] == "exit" and s in pos:
                        close_pos(s, r.close, r.close, ts, a[1])
                    elif a[0] == "enter" and s not in pos:
                        open_pos(s, a[1], r.close, ts, a[2])
            elif new:
                pending[s] = new
        curve.append((ts, equity()))

    for s in list(pos):
        close_pos(s, last[s], last[s], timeline[-1], "end_of_test")
    if curve:
        curve[-1] = (curve[-1][0], equity())
    return trades, curve, blocked


def metrics(trades, curve, start_equity, ann):
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    n = len(pnl)
    won, lost = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    eq = pd.Series([e for _, e in curve], index=pd.DatetimeIndex([t for t, _ in curve])) if curve else pd.Series([start_equity])
    daily = eq.resample("1D").last().dropna().pct_change().dropna() if curve else pd.Series(dtype=float)
    return dict(
        trades=n,
        win_rate=(pnl > 0).mean() if n else math.nan,
        profit_factor=won / lost if lost > 0 else (math.inf if won > 0 else math.nan),
        max_drawdown=(eq / eq.cummax() - 1).min(),
        sharpe=daily.mean() / daily.std() * math.sqrt(ann) if len(daily) > 1 and daily.std() > 0 else math.nan,
        avg_trade=pnl.mean() if n else math.nan,
        total_return=eq.iloc[-1] / start_equity - 1,
    )


def run_leg(name, cfg, env):
    base, Leg = cfg["legs"][name], LEGS[name]
    raw = {s: leg_bars(Leg(base), s, cfg, env) for s in base["symbols"]}
    t0 = min(df.index[0] for df in raw.values())
    t1 = max(df.index[-1] for df in raw.values())
    split = t0 + (t1 - t0) * cfg["backtest"]["in_sample_frac"]
    end = t1 + pd.Timedelta(minutes=1)
    eq0, ann = cfg["account"]["starting_equity"], 365 if Leg.asset == "crypto" else 252
    long_only_leg = name == "btc_breakout"  # Alpaca can't short crypto
    closes = daily_closes(raw)

    def run(params, start, stop_, shorts, with_curve=False):
        leg = Leg({**base, **params})
        prepared = {s: leg.prepare(df) for s, df in raw.items()}
        trades, curve, blocked = simulate(leg, prepared, start, stop_, cfg, shorts, closes)
        return (trades, metrics(trades, curve, eq0, ann), blocked) + ((curve,) if with_curve else ())

    # --- parameter selection on the in-sample 70% only ---
    grid = cfg["backtest"]["grids"][name]
    defaults = {k: base[k] for k in grid}
    select_shorts = cfg["backtest"]["select_mode"] == "both_sides" and not long_only_leg
    candidates = [defaults] + [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    best, best_score, scan = defaults, -math.inf, []
    for params in candidates:
        if scan and params == defaults:
            continue
        _, m, _ = run(params, t0, split, select_shorts)
        scan.append({**params, **m})
        score = m["sharpe"] if m["trades"] >= cfg["backtest"]["min_in_sample_trades"] and not math.isnan(m["sharpe"]) else -math.inf
        if score > best_score:  # strict: ties keep the earlier candidate (defaults first)
            best, best_score = params, score
    pd.DataFrame(scan).to_csv(RESULTS / f"grid_{name}.csv", index=False)

    rows = []
    modes = [("long_only", False)] + ([] if long_only_leg else [("both_sides", True)])
    for sample, (a, b) in (("in_sample", (t0, split)), ("out_of_sample", (split, end))):
        for mode, shorts in modes:
            trades, m, blocked, curve = run(best, a, b, shorts, with_curve=True)
            pd.DataFrame(trades).to_csv(RESULTS / f"trades_{name}_{sample}_{mode}.csv", index=False)
            pd.DataFrame(curve, columns=["time", "equity"]).to_csv(RESULTS / f"curve_{name}_{sample}_{mode}.csv", index=False)
            rows.append(dict(leg=name, sample=sample, mode=mode, blocked=sum(blocked.values()), buy_hold=buy_hold(raw, a, b), **m))
            if sample == "out_of_sample":
                with open(RESULTS / "holdout_log.csv", "a") as f:
                    f.write(f"{datetime.now().isoformat(timespec='seconds')},{name},{mode},\"{best}\"\n")
    info = dict(leg=name, start=t0, split=split, end=t1, defaults=defaults, chosen=best,
                eligible=best_score > -math.inf)
    return rows, info


def buy_hold(raw, start, end):
    """Benchmark: equal-weight buy-and-hold of the leg's symbols over [start, end) (symbols that
    started trading later are bought on their first day)."""
    rets = []
    for df in raw.values():
        c = df["close"][(df.index >= start) & (df.index < end)]
        if len(c) > 1:
            rets.append(c.iloc[-1] / c.iloc[0] - 1)
    return sum(rets) / len(rets) if rets else math.nan


def fmt_table(rows):
    def f(v, kind):
        if isinstance(v, float) and math.isnan(v):
            return "–"
        if kind == "pct":
            return f"{v * 100:.1f}%"
        if kind == "pf":
            return "∞" if math.isinf(v) else f"{v:.2f}"
        if kind == "usd":
            return f"${v:,.2f}"
        if kind == "num":
            return f"{v:.2f}"
        return str(v)
    cols = [("leg", ""), ("sample", ""), ("mode", ""), ("trades", ""), ("win_rate", "pct"), ("profit_factor", "pf"),
            ("max_drawdown", "pct"), ("sharpe", "num"), ("avg_trade", "usd"), ("total_return", "pct"), ("buy_hold", "pct"), ("blocked", "")]
    out = ["| " + " | ".join(c for c, _ in cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(f(r[c], k) for c, k in cols) + " |" for r in rows]
    return "\n".join(out)


def gates(rows):
    """Backtest half of the graduation criteria (1 and 2). Paper-trading criteria come later."""
    oos = {r["mode"]: r for r in rows if r["sample"] == "out_of_sample"}
    lo = oos.get("long_only")
    pf_ok = all(r["profit_factor"] > 1.2 for r in oos.values())
    lo_ok = lo is not None and lo["total_return"] > 0
    return pf_ok, lo_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", nargs="*", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg, env = load_config(), load_env()
    RESULTS.mkdir(exist_ok=True)
    names = args.legs or [n for n, p in cfg["legs"].items() if p["enabled"]]
    all_rows, md = [], [f"# Backtest results — {datetime.now():%Y-%m-%d %H:%M}\n"]
    for name in names:
        try:
            rows, info = run_leg(name, cfg, env)
        except MissingKeys as e:
            log.warning("skipping %s: %s", name, e)
            md.append(f"## {name}\nSkipped: {e}\n")
            continue
        all_rows += rows
        pf_ok, lo_ok = gates(rows)
        changed = {k: (info["defaults"][k], v) for k, v in info["chosen"].items() if info["defaults"][k] != v}
        md += [f"## {name}",
               f"Data {info['start']:%Y-%m-%d} → {info['end']:%Y-%m-%d}, in-sample/out-of-sample split at {info['split']:%Y-%m-%d}.",
               f"Parameters chosen in-sample: {info['chosen']}" + ("" if info["eligible"] else " (no grid point met the minimum trade count; kept defaults)"),
               "Changed from config defaults: " + (", ".join(f"{k} {a} → {b}" for k, (a, b) in changed.items()) or "none"),
               f"Graduation gate 1 (OOS profit factor > 1.2): {'PASS' if pf_ok else 'FAIL'} · "
               f"gate 2 (long-only profitable OOS): {'PASS' if lo_ok else 'FAIL'}\n",
               fmt_table(rows), ""]
    md += ["## All legs", fmt_table(all_rows)]
    (RESULTS / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
