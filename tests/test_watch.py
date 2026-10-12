"""Watch alerts: exact trigger prices, setup scan, message, GIF, and the live schedule."""
import io

import pandas as pd
import pytest
from PIL import Image

from bot import watch
from bot.alerts import Discord
from bot.strategies import BtcBreakout, StockTrend
from bot import indicators as ta

ST = dict(timeframe_minutes=1440, ema_fast=10, ema_slow=100, atr_period=14, stop_atr=2.0)
BT = dict(timeframe_minutes=1440, breakout_lookback=20, ema_period=50, exit_low_lookback=10, atr_period=14, trail_atr=2.0)


def bars(closes, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D", tz="America/New_York")
    s = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(dict(open=s, high=s * 1.01, low=s * 0.99, close=s, volume=1))


def dip_then_recover():
    return [100 + i * 0.2 for i in range(150)] + [130 - i * 0.6 for i in range(40)] + [106 + i * 0.25 for i in range(37)]  # ~2.5% from a golden cross


def test_trigger_price_makes_emas_meet_exactly():
    s = bars(dip_then_recover())["close"]
    f, sl = ta.ema(s, 10).iloc[-1], ta.ema(s, 100).iloc[-1]
    p = watch.ema_cross_price(f, sl, 10, 100)
    s2 = pd.concat([s, pd.Series([p], index=[s.index[-1] + pd.Timedelta(days=1)])])
    assert ta.ema(s2, 10).iloc[-1] == pytest.approx(ta.ema(s2, 100).iloc[-1], rel=1e-9)


def test_scan_finds_buy_watch_and_ranks_by_distance():
    leg = StockTrend(ST)
    fr = leg.prepare(bars(dip_then_recover()))
    assert fr["ema_f"].iloc[-1] < fr["ema_s"].iloc[-1]
    (s,) = watch.scan({"X": fr}, {}, {}, {"X": leg}, max_dist=1)
    assert s.action == "buy" and s.trigger > 0 and "golden cross" in s.why
    assert s.dist == pytest.approx(s.trigger / fr["close"].iloc[-1] - 1)
    assert watch.scan({"X": fr}, {"X": s.trigger * 0.5}, {}, {"X": leg}, max_dist=0.05) == []  # too far: skipped


def test_scan_sell_watch_for_held_position_uses_nearer_of_cross_or_stop():
    leg = StockTrend(ST)
    fr = leg.prepare(bars([100 + i * 0.3 for i in range(200)]))  # uptrend: fast above slow
    price = fr["close"].iloc[-1]
    pos = dict(side=1, qty=5, entry=150.0, stop=price * 0.99)
    (s,) = watch.scan({"X": fr}, {}, {"X": pos}, {"X": leg}, max_dist=1)
    assert s.action == "sell" and s.trigger == pytest.approx(price * 0.99) and "stop" in s.why


def test_scan_btc_breakout_trigger_is_higher_of_high_and_average():
    leg = BtcBreakout(BT)
    fr = leg.prepare(bars([100 + i * 0.1 for i in range(80)] + [105] * 5))
    (s,) = watch.scan({"BTC/USD": fr}, {}, {}, {"BTC/USD": leg}, max_dist=1)
    assert s.trigger == pytest.approx(max(fr["high"].tail(20).max(), fr["ema"].iloc[-1]))


def test_message_and_gif():
    leg = StockTrend(ST)
    fr = leg.prepare(bars(dip_then_recover()))
    setups = watch.scan({"AAA": fr, "BBB": fr}, {}, {}, {"AAA": leg, "BBB": leg}, max_dist=1)
    msg = watch.message(setups, "Morning watchlist")
    assert "Morning watchlist" in msg and "AAA" in msg and "BBB" in msg and len(msg) < 2000
    im = Image.open(io.BytesIO(watch.gif(setups[0])))
    assert im.format == "GIF" and im.n_frames > 10 and im.size == (720, 405)


def test_discord_attaches_file_as_multipart():
    sent = []

    class R:
        status_code = 204
    d = Discord("https://x", post=lambda url, timeout, **kw: sent.append(kw) or R())
    assert d.send("hello", file=("a.gif", b"GIF89a"))
    assert "files" in sent[0] and '"hello"' in sent[0]["data"]["payload_json"]
