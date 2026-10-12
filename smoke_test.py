"""End-to-end check on the PAPER account: 1 share of SPY through the real live-bot code.

Forces an entry signal -> market buy -> fill -> server-side stop placed -> forced exit signal ->
stop canceled -> market sell -> fill -> trade logged. Discord pings say "smoke_test".
Uses its own folder (results/smoke/) so the real logs and state are untouched. Market hours only.

  .venv/bin/python smoke_test.py
"""
import copy
import time

import pandas as pd

import bot.live as live
from bot.config import ROOT, load_config, load_env
from bot.data import ET
from bot.live import Bot, setup_logging
from bot.strategies import Leg

SYM = "SPY"


class Forced(Leg):
    name = "smoke_test"

    def __init__(self):
        super().__init__(dict(timeframe_minutes=15))
        self.sig = 1

    def prepare(self, bars):
        df = bars.copy()
        df["atr"] = df["close"] * 0.01
        df["sig"] = self.sig
        return df

    def entry_signal(self, ts, row, allow_shorts):
        return 1 if row.sig == 1 else 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - side * 2 * row.atr  # 2% below: far enough not to trigger

    def exit_signal(self, row, pos):
        return "smoke_test_exit" if row.sig == -1 else None


def wait(bot, cond, what, secs=90):
    for _ in range(secs // 3):
        bot.tick()
        if cond():
            print(f"  ✓ {what}")
            return
        time.sleep(3)
    raise SystemExit(f"  ✗ timed out waiting for: {what}")


def main():
    root = ROOT / "results" / "smoke"
    root.mkdir(parents=True, exist_ok=True)
    for f in ("state.json", "logs/trades.csv"):
        (root / f).unlink(missing_ok=True)
    setup_logging(root)
    cfg, env = load_config(), load_env()
    bot0 = Bot(cfg, env, root=root)
    if not bot0.broker.market_open():
        raise SystemExit("market is closed; run this during market hours (9:30-16:00 ET, weekdays)")
    price = bot0.broker.latest_price(SYM)
    equity, _ = bot0.broker.account()
    c = copy.deepcopy(cfg)
    c["legs"] = {"commodity_trend": dict(enabled=True, symbols=[SYM], timeframe_minutes=15)}
    c["risk"]["max_position_pct"] = price * 1.5 / equity  # caps the size at exactly 1 share
    c["alerts"] = {k: v for k, v in cfg.get("alerts", {}).items() if k != "daily_update_time"}
    bot = Bot(c, env, root=root)
    leg = Forced()
    bot.legs = {"commodity_trend": leg}
    bar = [pd.Timestamp.now(tz=ET).floor("15min") - pd.Timedelta(minutes=15)]

    def bars(*a, **k):  # one fresh synthetic bar at the live price each call
        bar[0] += pd.Timedelta(minutes=15)
        return pd.DataFrame(dict(open=price, high=price, low=price, close=price, volume=1), index=[bar[0]])
    live.leg_bars = bars

    print(f"SPY ≈ ${price:.2f}; buying 1 share on paper")
    bot.reconcile()
    wait(bot, lambda: SYM in bot.st["positions"], "entry filled")
    pos = bot.st["positions"][SYM]
    open_stops = [o for o in bot.broker.open_orders() if o.symbol == SYM and o.order_type.value == "stop"]
    assert pos["qty"] == 1 and len(open_stops) == 1, (pos, open_stops)
    print(f"  ✓ server-side stop is live at Alpaca: ${pos['stop']:.2f}")

    leg.sig = -1
    bot.st["next_check"][SYM] = None
    wait(bot, lambda: SYM not in bot.st["positions"], "exit filled and trade logged")
    assert SYM not in bot.broker.positions(), "SPY still held at broker"
    assert not [o for o in bot.broker.open_orders() if o.symbol == SYM], "orders left open"
    t = pd.read_csv(root / "logs" / "trades.csv").iloc[-1]
    print(f"  ✓ broker flat, no orders left. Logged: {t.side} {t.qty:g} {t.symbol} {t.entry:.2f} → {t.exit:.2f}, "
          f"P&L ${t.pnl:.2f}, reason {t.exit_reason}")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
