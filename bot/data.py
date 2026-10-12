"""Alpaca historical bars with a local CSV cache, plus session resampling."""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .config import ROOT

log = logging.getLogger(__name__)
ET = "America/New_York"
COLS = ["open", "high", "low", "close", "volume"]


def with_timeout(client, seconds=30):
    """alpaca-py sends requests with no timeout, so a connection that dies (e.g. while the Mac
    sleeps) can block forever. Give every request a time limit; the retry logic handles the error."""
    send = client._session.request
    client._session.request = lambda method, url, **kw: send(method, url, **{"timeout": seconds, **kw})
    return client


class MissingKeys(Exception):
    pass


def is_crypto(symbol):
    return "/" in symbol


def _fetch(symbol, minutes, start, end, env, feed):
    if minutes >= 1440:
        tf = TimeFrame(minutes // 1440, TimeFrameUnit.Day)
    elif minutes >= 60:
        tf = TimeFrame(minutes // 60, TimeFrameUnit.Hour)
    else:
        tf = TimeFrame(minutes, TimeFrameUnit.Minute)
    if is_crypto(symbol):
        client = with_timeout(CryptoHistoricalDataClient(env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")))
        df = client.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)).df
    else:
        if not (env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY")):
            raise MissingKeys(f"{symbol}: stock data needs ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        client = with_timeout(StockHistoricalDataClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]))
        df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end,
                                                    adjustment=Adjustment.SPLIT, feed=DataFeed(feed))).df
    if df.empty:
        return pd.DataFrame(columns=COLS)
    df = df.xs(symbol, level="symbol")[COLS]
    df.index = df.index.tz_convert(ET)
    return df


def get_bars(symbol, minutes, cfg, env):
    """Bars indexed by bar START time in US/Eastern. Stocks are trimmed to the regular session."""
    d = cfg["data"]
    start = datetime.fromisoformat(d["start"]).replace(tzinfo=timezone.utc)
    # free plan: SIP history must end > 15 min ago
    end = datetime.fromisoformat(d["end"]).replace(tzinfo=timezone.utc) if d["end"] else datetime.now(timezone.utc) - timedelta(minutes=20)
    cache = ROOT / d["cache_dir"] / f"{symbol.replace('/', '-')}_{minutes}m.csv"
    cache.parent.mkdir(exist_ok=True)
    df = None
    if cache.exists():
        df = pd.read_csv(cache, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
        if df.empty or df.index[0] > pd.Timestamp(start).tz_convert(ET) + timedelta(days=7):
            df = None  # cache doesn't reach back far enough, refetch everything
    if df is None:
        log.info("downloading %s %sm from %s", symbol, minutes, start.date())
        df = _fetch(symbol, minutes, start, end, env, d["feed"])
    elif df.index[-1] < pd.Timestamp(end) - timedelta(minutes=minutes):
        new = _fetch(symbol, minutes, df.index[-1].to_pydatetime() + timedelta(seconds=1), end, env, d["feed"])
        df = pd.concat([df, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(cache)
    df = df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))]
    if not is_crypto(symbol) and minutes < 1440:
        t = df.index.hour * 60 + df.index.minute
        df = df[(t >= 9 * 60 + 30) & (t < 16 * 60)]
    return df


def session_buckets(df, bucket_starts=("09:30", "13:30")):
    """Resample regular-session intraday bars into session-anchored buckets (4h-style bars:
    09:30-13:30 and 13:30-16:00). Alpaca's own hourly bars are clock-aligned (the 09:00 bar
    contains pre-market), so this builds session-aligned bars from smaller ones instead."""
    mins = [int(s[:2]) * 60 + int(s[3:]) for s in bucket_starts]
    t = df.index.hour * 60 + df.index.minute
    start_min = pd.Series(mins[0], index=df.index)
    for m in mins[1:]:
        start_min[t >= m] = m
    label = df.index.normalize() + pd.to_timedelta(start_min.values, unit="min")
    g = df.groupby(label)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()})
    out.index.name = "timestamp"
    return out


def _end(now, feed):
    """Free plan: SIP data must be over 15 minutes old (never ask past the real clock)."""
    real = datetime.now(timezone.utc)
    return min(now, real) - timedelta(minutes=20) if feed == "sip" else min(now, real)


def _sip_refused(e):
    return "subscription does not permit" in str(e)


def fetch_recent(symbol, minutes, days, env, feed="iex", now=None):
    """Live: the last `days` of bars up to now, no cache. Free plans get real-time stock data only
    from the IEX feed (SIP is 15 minutes delayed). Only completed bars are returned."""
    now = now or datetime.now(timezone.utc)
    try:
        df = _fetch(symbol, minutes, now - timedelta(days=days), _end(now, feed), env, feed)
    except Exception as e:
        if feed != "sip" or not _sip_refused(e):
            raise
        log.warning("SIP refused for %s, using IEX", symbol)
        df = _fetch(symbol, minutes, now - timedelta(days=days), _end(now, "iex"), env, "iex")
    if not is_crypto(symbol) and minutes < 1440:
        t = df.index.hour * 60 + df.index.minute
        df = df[(t >= 9 * 60 + 30) & (t < 16 * 60)]
    return df[df.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]


def fetch_recent_many(symbols, minutes, days, env, feed="sip", now=None, chunk=100):
    """Like fetch_recent for many stocks at once (one request per 100 symbols instead of one each)."""
    now = now or datetime.now(timezone.utc)
    tf = TimeFrame(minutes // 1440, TimeFrameUnit.Day) if minutes >= 1440 else TimeFrame(minutes, TimeFrameUnit.Minute)
    client = with_timeout(StockHistoricalDataClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]))
    out = {}

    def get(syms, f):
        return client.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=now - timedelta(days=days),
                                                      end=_end(now, f), adjustment=Adjustment.SPLIT, feed=DataFeed(f))).df
    for i in range(0, len(symbols), chunk):
        try:
            df = get(symbols[i:i + chunk], feed)
        except Exception as e:
            if feed != "sip" or not _sip_refused(e):
                raise
            log.warning("SIP refused, using IEX for this batch")
            df = get(symbols[i:i + chunk], "iex")
        for sym, g in df.groupby(level="symbol"):
            g = g.droplevel("symbol")[COLS]
            g.index = g.index.tz_convert(ET)
            out[sym] = g[g.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]
    return out


def bucket_end(ts):
    """End time of a session bucket that starts at `ts` (09:30 -> 13:30, 13:30 -> 16:00)."""
    return ts.normalize() + (pd.Timedelta(hours=13, minutes=30) if ts.hour < 13 else pd.Timedelta(hours=16))


def leg_bars(leg, symbol, cfg, env, live=False, now=None):
    """The bars a leg trades on. Commodity trend uses session-anchored 4h buckets built from 15m bars."""
    if leg.name == "commodity_trend":
        raw = fetch_recent(symbol, 15, cfg["data"]["live_lookback_days"], env, cfg["data"]["live_feed"], now) if live \
            else get_bars(symbol, 15, cfg, env)
        out = session_buckets(raw)
        if live and len(out):
            end = bucket_end(out.index[-1])
            if pd.Timestamp(now or datetime.now(timezone.utc)) < end and out.index[-1].normalize() == end.normalize():
                out = out.iloc[:-1]  # bucket still in progress
        return out
    if live:
        days = leg.p.get("live_lookback_days", cfg["data"]["live_lookback_days"])
        return fetch_recent(symbol, leg.timeframe_minutes, days, env, leg.p.get("live_feed", cfg["data"]["live_feed"]), now)
    return get_bars(symbol, leg.timeframe_minutes, cfg, env)
