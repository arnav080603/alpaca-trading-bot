"""AI layer: decisions file loading, vetoes of rule entries, AI picks (entry, exit, caps) and the
rules-only fallback when the file is missing or stale. Fake broker, no network."""
import copy
import csv
import json

import pandas as pd
import pytest

import bot.live as live
from bot import ai
from bot.live import Bot
from test_live import CFG, FakeAlerts, FakeBroker, ScriptLeg, T, World, trades

AI = dict(enabled=True, decisions_file="ai/decisions.json", context_file="ai/context.json",
          max_age_minutes=120, max_picks=2, stop_atr=2.0, max_hold_days=20)


def write(root, at="2026-10-07 09:00", **d):
    (root / "ai").mkdir(exist_ok=True)
    (root / "ai" / "decisions.json").write_text(json.dumps(dict(generated_at=str(T(at)), **d)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    w, broker, alerts = World(), FakeBroker(), FakeAlerts()
    w.bars.update(NVDA=[], AMD=[], MU=[])
    monkeypatch.setattr(live, "leg_bars", lambda leg, sym, cfg, env, live=False, now=None: w.frame(sym))
    cfg = copy.deepcopy(CFG)
    cfg["ai"] = AI
    cfg["legs"]["stock_trend"] = dict(enabled=True, timeframe_minutes=1440, symbols=["NVDA", "AMD", "MU"], allow_shorts=False)

    def make():
        b = Bot(cfg, {}, root=tmp_path, broker=broker, alerts=alerts)
        b.legs = {"commodity_trend": ScriptLeg("commodity_trend", "equity", 240),
                  "btc_breakout": ScriptLeg("btc_breakout", "crypto", 1440),
                  "stock_trend": ScriptLeg("stock_trend", "equity", 1440)}
        return b
    for d in ("2026-10-05", "2026-10-06"):
        w.add("GLD", f"{d} 09:30", 100)
        w.add("GLD", f"{d} 13:30", 100)
        for s in ("NVDA", "AMD", "MU"):
            w.add(s, f"{d} 00:00", 50)
    for d in range(1, 7):
        w.add("BTC/USD", f"2026-10-0{d} 00:00", 60000)
    broker.prices = {"GLD": 100.0, "BTC/USD": 60000.0, "NVDA": 50.0, "AMD": 50.0, "MU": 50.0}
    return type("E", (), dict(w=w, broker=broker, alerts=alerts, make=make, root=tmp_path))


def sent(e):
    return [a[0] for k, a in e.alerts.log if k == "send"]


# ---- loading the decisions file ---------------------------------------------------
def test_load_fresh_stale_broken_missing(tmp_path):
    p, now = tmp_path / "d.json", T("2026-10-07 10:00")
    assert ai.load(p, now, 120) is None  # missing
    p.write_text(json.dumps(dict(generated_at=str(T("2026-10-07 09:00")), vetoes={"msft": "x"}, picks=[{"symbol": "nvda"}])))
    d = ai.load(p, now, 120)
    assert d["vetoes"] == {"MSFT": "x"} and d["picks"] == [dict(symbol="NVDA", reason="")] and d["exits"] == {}
    assert ai.load(p, T("2026-10-07 11:01"), 120) is None  # stale
    p.write_text("{not json")
    assert ai.load(p, now, 120) is None
    p.write_text(json.dumps(dict(generated_at="2026-10-07T09:00:00")))  # no timezone
    assert ai.load(p, now, 120) is None


# ---- vetoes -----------------------------------------------------------------------
def test_veto_blocks_rule_entry_and_is_logged(env):
    bot = env.make()
    write(env.root, at="2026-10-07 13:00", vetoes={"GLD": "earnings risk"})
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    assert env.broker.new("market") == []
    (row,) = csv.DictReader(open(env.root / "logs" / "ai_vetoes.csv"))
    assert row["symbol"] == "GLD" and row["reason"] == "earnings risk" and float(row["stop"]) == pytest.approx(96)
    assert any("AI vetoed **GLD**" in m for m in sent(env))


def test_stale_decisions_mean_rules_only(env):
    bot = env.make()
    write(env.root, at="2026-10-07 09:00", vetoes={"GLD": "old"})  # 4.5 h old at 13:35
    env.w.add("GLD", "2026-10-07 09:30", 100, sig=1)
    bot.tick(T("2026-10-07 13:35"))
    assert len(env.broker.new("market")) == 1


# ---- AI picks ---------------------------------------------------------------------
def test_pick_bought_with_stop_then_ai_exit_logged(env):
    bot = env.make()
    write(env.root, at="2026-10-07 10:00", picks=[{"symbol": "NVDA", "reason": "insider cluster buy"}])
    bot.tick(T("2026-10-07 10:01"))
    (oid,) = [k for k in env.broker.new("market") if env.broker.orders[k]["symbol"] == "NVDA"]
    env.broker.fill(oid, 50.0)
    bot.tick(T("2026-10-07 10:02"))
    pos = bot.st["positions"]["NVDA"]
    assert pos["leg"] == "ai_pick" and pos["stop"] == pytest.approx(46.0) and pos["stop_order"]
    assert any("AI is buying **NVDA**" in m for m in sent(env))
    write(env.root, at="2026-10-07 11:00", exits={"NVDA": "thesis broken"})
    bot.tick(T("2026-10-07 11:01"))
    (xid,) = [k for k in env.broker.new("market") if env.broker.orders[k]["symbol"] == "NVDA"]
    env.broker.fill(xid, 52.0)
    bot.tick(T("2026-10-07 11:02"))
    (t,) = trades(env.root)
    assert (t["leg"], t["symbol"], t["exit_reason"]) == ("ai_pick", "NVDA", "ai_exit") and float(t["pnl"]) > 0


def test_picks_capped_outside_universe_ignored_and_closed_market_skipped(env):
    bot = env.make()
    write(env.root, at="2026-10-07 10:00", picks=[{"symbol": "TSLA"}, {"symbol": "NVDA"}, {"symbol": "AMD"}, {"symbol": "MU"}])
    env.broker.is_open = False
    bot.tick(T("2026-10-07 10:01"))
    assert env.broker.new("market") == []
    env.broker.is_open = True
    bot.tick(T("2026-10-07 10:02"))
    assert sorted(env.broker.orders[k]["symbol"] for k in env.broker.new("market")) == ["AMD", "NVDA"]  # max_picks 2, no TSLA


def test_pick_sold_after_max_hold_days(env):
    bot = env.make()
    write(env.root, at="2026-10-07 10:00", picks=[{"symbol": "NVDA"}])
    bot.tick(T("2026-10-07 10:01"))
    env.broker.fill(env.broker.new("market")[0], 50.0)
    bot.tick(T("2026-10-07 10:02"))
    bot.tick(T("2026-10-27 10:05"))  # 20 days later, decisions long stale: the hold limit still applies
    (xid,) = env.broker.new("market")
    assert env.broker.orders[xid]["side"] == -1


def test_context_written_for_the_ai_task(env):
    bot = env.make()
    bot.tick(T("2026-10-07 10:01"))
    ctx = json.loads((env.root / "ai" / "context.json").read_text())
    assert ctx["equity"] == 10000.0 and ctx["positions"] == [] and ctx["ai_last_decisions_at"] is None
    assert any("No fresh AI decisions" in m for m in sent(env))
