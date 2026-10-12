"""Live loop against a fake broker: entries, fills, server-side and bot-managed stops, exits,
kill switches, reconciliation, logging and restarts. No network."""
import csv
from types import SimpleNamespace

import pandas as pd
import pytest

import bot.live as live
from bot.broker import Broker, norm
from bot.live import Bot
from bot.strategies import Leg

ET = "America/New_York"


def T(s):
    return pd.Timestamp(s, tz=ET)


class FakeBroker:
    CRYPTO_STOP_LIMIT = 0.01

    def __init__(self, equity=100000.0):
        self.eq = self.cash = equity
        self.pos, self.orders, self.prices, self.is_open, self.n = {}, {}, {}, True, 0

    def account(self):
        return self.eq, self.cash

    def positions(self):
        return {k: dict(v) for k, v in self.pos.items()}

    def open_orders(self):
        return [SimpleNamespace(id=k, symbol=o["symbol"], side=SimpleNamespace(value="buy" if o["side"] > 0 else "sell"),
                                order_type=SimpleNamespace(value=o["type"])) for k, o in self.orders.items() if o["status"] == "new"]

    def _add(self, **o):
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = dict(status="new", filled_qty=0, price=None, **o)
        return oid

    def market(self, symbol, qty, side):
        return self._add(symbol=symbol, qty=qty, side=side, type="market")

    def stop(self, symbol, qty, side, stop_price):
        return self._add(symbol=symbol, qty=qty, side=side, type="stop", stop=stop_price)

    def cancel(self, oid):
        if self.orders[oid]["status"] == "new":
            self.orders[oid]["status"] = "canceled"

    def order(self, oid):
        o = self.orders[oid]
        return dict(status=o["status"], filled_qty=o["filled_qty"], price=o["price"])

    def fill(self, oid, price):
        o = self.orders[oid]
        o.update(status="filled", filled_qty=o["qty"], price=price)
        n = norm(o["symbol"])
        q = self.pos.get(n, {"qty": 0})["qty"] + o["side"] * o["qty"]
        if q == 0:
            del self.pos[n]
        else:
            self.pos[n] = dict(qty=q, entry=price, price=price)

    def new(self, type_):
        return [k for k, o in self.orders.items() if o["status"] == "new" and o["type"] == type_]

    def last_fill(self, symbol, side):
        fills = [o for o in self.orders.values() if o["symbol"] == symbol and o["side"] == side and o["status"] == "filled"]
        return fills[-1]["price"] if fills else None

    def latest_price(self, symbol):
        return self.prices[symbol]

    def market_open(self):
        return self.is_open


class FakeAlerts:
    def __init__(self):
        self.log = []

    def __getattr__(self, kind):
        return lambda *a, **k: self.log.append((kind, a))

    def kinds(self):
        return [k for k, _ in self.log]


class ScriptLeg(Leg):
    """Signals come from a `sig` column: +1 enter long, -1 exit. ATR fixed at 2, stop 2 x ATR."""

    def __init__(self, name, asset, tf):
        super().__init__(dict(timeframe_minutes=tf))
        self.name, self.asset = name, asset

    def prepare(self, bars):
        df = bars.copy()
        df["atr"] = 2.0
        return df

    def entry_signal(self, ts, row, allow_shorts):
        return 1 if row.sig == 1 else 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - side * 2 * row.atr

    def update_stop(self, pos, row):
        if self.asset == "crypto":  # trailing, like the BTC leg
            pos.high_water = max(pos.high_water, row.high)
            pos.stop = max(pos.stop, pos.high_water - 2 * row.atr)

    def exit_signal(self, row, pos):
        return "signal" if row.sig == -1 else None


CFG = dict(
    account=dict(starting_equity=10000, allow_shorts=True, cash_account_mode=False),
    risk=dict(risk_per_trade=0.01, max_position_pct=0.25, max_open_positions=4, max_gross_exposure=1.0,
              correlation_lookback_days=60, correlation_threshold=0.8, daily_kill_switch_pct=0.05, stop_file="STOP"),
    data=dict(live_feed="iex", live_lookback_days=400),
    legs=dict(mean_reversion=dict(enabled=False, timeframe_minutes=15, symbols=["SPY"]),
              commodity_trend=dict(enabled=True, timeframe_minutes=240, symbols=["GLD"], allow_shorts=False),
              btc_breakout=dict(enabled=True, timeframe_minutes=1440, symbols=["BTC/USD"])),
)


class World:
    """Bars the fake data feed returns, plus helpers to build them."""

    def __init__(self):
        self.bars = {"GLD": [], "BTC/USD": []}

    def add(self, sym, ts, close, sig=0, high=None):
        self.bars[sym].append(dict(ts=T(ts), open=close, high=high or close, low=close, close=close, volume=1, sig=sig))

    def frame(self, sym):
        rows = self.bars[sym]
        return pd.DataFrame(rows).set_index("ts") if rows else pd.DataFrame(columns=["open", "high", "low", "close", "volume", "sig"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    w, broker, alerts = World(), FakeBroker(), FakeAlerts()
    monkeypatch.setattr(live, "leg_bars", lambda leg, sym, cfg, env, live=False, now=None: w.frame(sym))

    def make():
        b = Bot(CFG, {}, root=tmp_path, broker=broker, alerts=alerts)
        b.legs = {"commodity_trend": ScriptLeg("commodity_trend", "equity", 240),
                  "btc_breakout": ScriptLeg("btc_breakout", "crypto", 1440)}
        return b
    for d in ("2026-10-05", "2026-10-06"):
        w.add("GLD", f"{d} 09:30", 100)
        w.add("GLD", f"{d} 13:30", 100)
    for d in range(1, 7):
        w.add("BTC/USD", f"2026-10-0{d} 00:00", 60000)
    broker.prices = {"GLD": 100.0, "BTC/USD": 60000.0}
    return SimpleNamespace(w=w, broker=broker, alerts=alerts, make=make, root=tmp_path)


def trades(root):
    p = root / "logs" / "trades.csv"
    return list(csv.DictReader(open(p))) if p.exists() else []


def enter_gld(e, bot, fill=100.5):
    e.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    (oid,) = e.broker.new("market")
    e.broker.fill(oid, fill)
    bot.tick(T("2026-10-07 13:36"))
    return bot.st["positions"]["GLD"]


# ---- the main flow ----------------------------------------------------------------
def test_entry_sized_as_10k_account_then_server_stop_placed(env):
    bot = env.make()
    pos = enter_gld(env, bot)
    o = env.broker.orders["o1"]
    assert (o["qty"], o["side"]) == (25, 1)  # 25% cap of $10k, not of the $100k paper balance
    assert pos["entry"] == 100.5 and pos["stop"] == pytest.approx(96.5)
    (sid,) = env.broker.new("stop")
    s = env.broker.orders[sid]
    assert (s["qty"], s["side"], s["stop"]) == (25, -1, pytest.approx(96.5))
    assert "entry" in env.alerts.kinds()


def test_server_stop_fill_logged_with_slippage(env):
    bot = env.make()
    enter_gld(env, bot)
    (sid,) = env.broker.new("stop")
    env.broker.fill(sid, 95.0)  # gapped below the 96.50 stop
    bot.tick(T("2026-10-07 13:37"))
    (t,) = trades(env.root)
    assert t["exit_reason"] == "stop" and float(t["exit"]) == 95.0
    assert float(t["slippage"]) == pytest.approx(1.5 * 25) and float(t["pnl"]) == pytest.approx(-5.5 * 25)
    assert list(t) == live.TRADE_COLS
    assert "GLD" not in bot.st["positions"] and "exit" in env.alerts.kinds()


def test_exit_signal_cancels_stop_first_then_sells(env):
    bot = env.make()
    enter_gld(env, bot)
    (sid,) = env.broker.new("stop")
    env.w.add("GLD", "2026-10-07 13:30", 103, sig=-1)
    bot.tick(T("2026-10-07 16:05"))
    assert env.broker.orders[sid]["status"] == "canceled"
    (mid,) = env.broker.new("market")
    assert env.broker.orders[mid]["side"] == -1
    env.broker.fill(mid, 103.2)
    bot.tick(T("2026-10-08 09:31"))
    (t,) = trades(env.root)
    assert t["exit_reason"] == "signal" and float(t["pnl"]) == pytest.approx(2.7 * 25)


def test_same_bar_is_never_traded_twice(env):
    bot = env.make()
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    bot.st["next_check"]["GLD"] = None  # force a re-check of the same bar
    bot.tick(T("2026-10-07 13:40"))
    assert len(env.broker.new("market")) == 1


def test_not_checked_before_bar_is_due(env):
    bot = env.make()
    bot.tick(T("2026-10-07 09:45"))  # sees yesterday's last bar; next bar due ~13:32
    bot.tick(T("2026-10-07 13:35"))  # due, but today's 09:30 bar isn't in the data yet -> retry in 15 min
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:40"))
    assert not env.broker.new("market")
    bot.tick(T("2026-10-07 13:51"))
    assert len(env.broker.new("market")) == 1


# ---- crypto: bot-managed trailing stop ---------------------------------------------
def test_crypto_trailing_stop_managed_by_bot(env):
    bot = env.make()
    env.w.add("BTC/USD", "2026-10-07 00:00", 60000, sig=1)
    bot.tick(T("2026-10-08 00:05"))
    (oid,) = env.broker.new("market")
    assert env.broker.orders[oid]["qty"] == pytest.approx(round(2500 / 60000, 6))  # fractional, capped at 25%
    env.broker.fill(oid, 60000)
    bot.tick(T("2026-10-08 00:06"))
    (s1,) = env.broker.new("stop")  # crypto now has a server-side stop(-limit) too
    assert env.broker.orders[s1]["stop"] == pytest.approx(59996)
    env.w.add("BTC/USD", "2026-10-08 00:00", 61000, high=62000)
    env.broker.prices["BTC/USD"] = 61000
    bot.tick(T("2026-10-09 00:05"))
    assert bot.st["positions"]["BTC/USD"]["stop"] == pytest.approx(61996)  # trailed up from 59996
    (s2,) = env.broker.new("stop")  # ...and the server-side order followed it
    assert env.broker.orders[s1]["status"] == "canceled" and env.broker.orders[s2]["stop"] == pytest.approx(61996)
    env.broker.prices["BTC/USD"] = 61990  # under the stop but above the 1% limit: the server order handles it
    bot.tick(T("2026-10-09 00:06"))
    assert not env.broker.new("market")
    env.broker.prices["BTC/USD"] = 60000  # crashed through the limit: the stop-limit can't fill, backup sells
    bot.tick(T("2026-10-09 00:07"))
    assert env.broker.orders[s2]["status"] == "canceled"
    (sell,) = env.broker.new("market")
    env.broker.fill(sell, 59990)
    bot.tick(T("2026-10-09 00:08"))
    (t,) = trades(env.root)
    assert t["exit_reason"] == "stop" and float(t["slippage"]) == pytest.approx(2006 * float(t["qty"]), abs=0.01)


# ---- kill switches -----------------------------------------------------------------
def test_daily_kill_switch_flattens_and_alerts(env):
    bot = env.make()
    enter_gld(env, bot)
    env.broker.eq = 100000 - 600  # bot equity 10,000 -> 9,400 (-6%)
    bot.tick(T("2026-10-07 13:40"))
    assert "kill_switch" in env.alerts.kinds()
    (mid,) = env.broker.new("market")
    env.broker.fill(mid, 98)
    bot.tick(T("2026-10-07 13:41"))
    (t,) = trades(env.root)
    assert t["exit_reason"] == "kill_switch"
    env.w.add("GLD", "2026-10-07 13:30", 100, sig=1)
    bot.tick(T("2026-10-07 16:05"))
    assert not env.broker.new("market")  # halted for the rest of the day


def test_stop_file_flattens_and_cancels_pending(env):
    bot = env.make()
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    (oid,) = env.broker.new("market")
    (env.root / "STOP").touch()
    bot.tick(T("2026-10-07 13:36"))
    assert env.broker.orders[oid]["status"] == "canceled" and not bot.st["pending"]
    assert "kill_switch" in env.alerts.kinds()


def test_kill_state_survives_restart(env):
    bot = env.make()
    bot.tick(T("2026-10-07 13:35"))
    env.broker.eq = 99000
    bot.tick(T("2026-10-07 13:36"))
    again = env.make()
    assert again.risk.halted == "daily_loss" and again.risk.day_start_equity == 10000


# ---- reconciliation ----------------------------------------------------------------
def test_reconcile_adopts_unknown_position_and_protects_it(env):
    env.broker.pos["GLD"] = dict(qty=10, entry=100, price=101)
    bot = env.make()
    bot.reconcile()
    pos = bot.st["positions"]["GLD"]
    assert pos["qty"] == 10 and pos["stop"] == 96
    (sid,) = env.broker.new("stop")
    assert env.broker.orders[sid]["qty"] == 10 and "error" in env.alerts.kinds()


def test_reconcile_reuses_existing_stop_after_restart(env):
    bot = env.make()
    enter_gld(env, bot)
    bot.st["positions"]["GLD"]["stop_order"] = None  # e.g. crashed before saving the id
    bot.save()
    again = env.make()
    again.reconcile()
    assert len(env.broker.new("stop")) == 1 and again.st["positions"]["GLD"]["stop_order"] == "o2"


def test_qty_mismatch_trusts_broker(env):
    bot = env.make()
    enter_gld(env, bot)
    env.broker.pos["GLD"]["qty"] = 20
    bot.tick(T("2026-10-07 13:40"))
    assert bot.st["positions"]["GLD"]["qty"] == 20


def test_unfilled_entry_is_dropped_with_alert(env):
    bot = env.make()
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    (oid,) = env.broker.new("market")
    env.broker.orders[oid]["status"] = "rejected"
    bot.tick(T("2026-10-07 13:36"))
    assert not bot.st["pending"] and "GLD" not in bot.st["positions"] and "error" in env.alerts.kinds()


# ---- daily summary -----------------------------------------------------------------
def test_daily_summary_written_on_new_day(env):
    bot = env.make()
    enter_gld(env, bot)
    (sid,) = env.broker.new("stop")
    env.broker.fill(sid, 96.5)
    env.broker.eq = 100000 - 100
    bot.tick(T("2026-10-07 15:00"))
    bot.tick(T("2026-10-08 09:31"))
    (row,) = list(csv.DictReader(open(env.root / "logs" / "daily_summary.csv")))
    assert list(row) == live.SUMMARY_COLS
    assert row["date"] == "2026-10-07" and row["trades"] == "1" and row["losses"] == "1"
    assert float(row["start_equity"]) == 10000 and float(row["end_equity"]) == 9900 and row["kill_switch_triggered"] == "False"


# ---- dry run -----------------------------------------------------------------------
def test_dry_run_broker_places_nothing():
    b = Broker(dict(ALPACA_API_KEY="x", ALPACA_SECRET_KEY="y"), dry_run=True)
    assert b.market("GLD", 5, 1) is None and b.stop("GLD", 5, -1, 90) is None


# ---- whole balance + daily update --------------------------------------------------
def test_set_aside_off_sizes_from_whole_balance(env, monkeypatch):
    monkeypatch.setitem(CFG, "account", {**CFG["account"], "set_aside_extra": False})
    bot = env.make()
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    (oid,) = env.broker.new("market")
    assert env.broker.orders[oid]["qty"] == 250  # 25% of $100k


def test_daily_update_once_a_day_after_update_time(env, monkeypatch):
    monkeypatch.setitem(CFG, "alerts", dict(daily_update_time="16:15"))
    bot = env.make()
    enter_gld(env, bot)
    env.broker.pos["GLD"]["price"] = 102.5
    bot.tick(T("2026-10-07 16:14"))
    assert not [a for k, a in env.alerts.log if k == "send"]
    bot.tick(T("2026-10-07 16:15"))
    bot.tick(T("2026-10-07 16:16"))
    (msg,) = [a[0] for k, a in env.alerts.log if k == "send"]
    assert "Daily update" in msg and "GLD long 25 @ $100.50" in msg and "+$50.00" in msg and "stop $96.50" in msg
    bot.tick(T("2026-10-08 16:20"))
    assert len([1 for k, _ in env.alerts.log if k == "send"]) == 2


# ---- hang protection -----------------------------------------------------------------
def test_every_alpaca_request_gets_a_timeout():
    from bot.data import with_timeout
    seen = []
    client = SimpleNamespace(_session=SimpleNamespace(request=lambda m, u, **kw: seen.append(kw)))
    with_timeout(client, 30)._session.request("GET", "https://x", params={})
    with_timeout(client, 30)._session.request("GET", "https://x", timeout=5)
    assert seen[0]["timeout"] == 30 and seen[0]["params"] == {} and seen[1]["timeout"] == 5


def test_watchdog_restarts_a_stuck_check(env, monkeypatch):
    bot = env.make()
    bot.WATCHDOG_SECS = 0
    bot._tick_started = live.time.monotonic() - 1
    exits = []
    monkeypatch.setattr(live.os, "_exit", lambda code: (exits.append(code), (_ for _ in ()).throw(SystemExit)))
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit):
        bot._watchdog()
    assert exits == [3] and "error" in env.alerts.kinds()


def test_watchdog_ignores_idle_bot(env, monkeypatch):
    bot = env.make()
    bot.WATCHDOG_SECS, bot._tick_started = 0, None
    calls = []
    def fake_sleep(s):
        calls.append(s)
        if len(calls) > 3:
            raise StopIteration
    monkeypatch.setattr(live.time, "sleep", fake_sleep)
    with pytest.raises(StopIteration):
        bot._watchdog()
    assert "error" not in env.alerts.kinds()


# ---- watch schedule ------------------------------------------------------------------
def test_watch_alert_sent_once_per_slot_with_chart(env, monkeypatch):
    from bot.strategies import StockTrend
    from tests.test_watch import bars, dip_then_recover
    monkeypatch.setitem(CFG, "alerts", dict(watch_times=["10:00", "14:30"]))
    bot = env.make()
    leg = StockTrend(dict(timeframe_minutes=1440, ema_fast=10, ema_slow=100, atr_period=14, stop_atr=2.0))
    bot.legs["commodity_trend"] = leg
    fr = leg.prepare(bars(dip_then_recover()))
    bot.frames = {"GLD": fr, "BTC/USD": fr.iloc[:0]}
    env.broker.latest_prices = lambda syms: {s: float(fr["close"].iloc[-1]) for s in syms}
    bot._watch(T("2026-10-07 09:59"))
    assert not [a for k, a in env.alerts.log if k == "send"]
    bot._watch(T("2026-10-07 10:01"))
    bot._watch(T("2026-10-07 10:30"))
    sends = [(a, kw) for k, a in env.alerts.log if k == "send" for kw in [None]]
    assert len(sends) == 1 and "Morning watchlist" in sends[0][0][0]


def test_weekend_watch_skips_stocks(env, monkeypatch):
    from bot.strategies import StockTrend
    from tests.test_watch import bars, dip_then_recover
    monkeypatch.setitem(CFG, "alerts", dict(watch_times=["10:00"]))
    bot = env.make()
    leg = StockTrend(dict(timeframe_minutes=1440, ema_fast=10, ema_slow=100, atr_period=14, stop_atr=2.0))
    bot.legs["commodity_trend"] = leg
    bot.frames = {"GLD": leg.prepare(bars(dip_then_recover())), "BTC/USD": pd.DataFrame()}
    env.broker.latest_prices = lambda syms: {}
    bot._watch(T("2026-10-10 10:05"))  # Saturday
    assert not [a for k, a in env.alerts.log if k == "send"]


def test_sip_requests_never_ask_past_the_real_clock():
    from datetime import datetime, timedelta, timezone
    from bot.data import _end
    real = datetime.now(timezone.utc)
    assert _end(real + timedelta(days=3), "sip") <= real - timedelta(minutes=19)  # rehearsing a future time
    assert _end(real - timedelta(days=1), "sip") == real - timedelta(days=1, minutes=20)
    assert _end(real + timedelta(days=3), "iex") <= datetime.now(timezone.utc)


def test_live_snapshot_for_orb(env):
    bot = env.make()
    enter_gld(env, bot)
    env.broker.pos["GLD"]["price"] = 103.0
    bot.tick(T("2026-10-07 13:40"))
    live_ = bot.st["live"]
    assert live_["prices"] == {"GLD": 103.0} and live_["equity"] == 10000.0 and live_["at"].startswith("2026-10-07 13:40")


def test_obsidian_dashboard_lists_trades(env, tmp_path):
    bot = env.make()
    bot.cfg = dict(bot.cfg, journal=dict(dashboard="vault/Bot Dashboard.md"))
    enter_gld(env, bot)
    note = (tmp_path / "vault" / "Bot Dashboard.md").read_text()
    assert "| **GLD** | commodity_trend | 25 |" in note and "No trades have closed yet" in note
    (sid,) = env.broker.new("stop")
    env.broker.fill(sid, 95.0)
    bot.tick(T("2026-10-07 13:37"))
    note = (tmp_path / "vault" / "Bot Dashboard.md").read_text()
    assert "1 trade · 0 wins" in note and "❌ −$137.50 | stop |" in note and "_None right now." in note
