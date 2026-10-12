"""Deterministic signal logic for the three legs. Shared by backtest.py and the live bot.

Every leg has the same interface. The engine calls, per symbol per bar:
  at the open : flatten_at_open(ts)
  at the close: update_stop(pos, row) -> exit_signal(row, pos) / flatten_at_close(row) -> entry_signal(ts, row, allow_shorts)
`row` is one row of prepare(bars). `pos` has .side (+1/-1), .stop, .high_water, .low_water.
Signals are decided on a bar's close and filled at the next bar's open.
"""
import pandas as pd

from . import indicators as ta


def _hhmm(s):
    return int(s[:2]) * 60 + int(s[3:])


def _minute(ts):
    return ts.hour * 60 + ts.minute


class Leg:
    name = ""
    asset = "equity"  # equity or crypto (sets costs and fractional rules)
    timeframe_minutes = 60

    def __init__(self, p):
        self.p = p
        self.timeframe_minutes = p["timeframe_minutes"]

    def prepare(self, bars):
        raise NotImplementedError

    def entry_signal(self, ts, row, allow_shorts):
        return 0

    def initial_stop(self, side, entry_price, row):
        raise NotImplementedError

    def update_stop(self, pos, row):
        pass

    def exit_signal(self, row, pos):
        return None

    def flatten_at_open(self, ts):
        return False

    def flatten_at_close(self, row):
        return False


class MeanReversion(Leg):
    """z = (close - SMA20) / StdDev20 on 15m bars. Long z <= -2, short z >= +2. Exit when z crosses 0,
    at a 1.5 x ATR(14) stop, or flatten before the close. New trades only 09:45-15:30 ET."""
    name = "mean_reversion"

    def prepare(self, bars):
        p, df = self.p, bars.copy()
        df["z"] = (df["close"] - ta.sma(df["close"], p["lookback"])) / ta.stdev(df["close"], p["lookback"])
        df["atr"] = ta.atr(df, p["atr_period"])
        day = pd.Series(df.index.normalize(), index=df.index)
        df["last_of_day"] = day.ne(day.shift(-1))  # also catches half days
        return df.dropna(subset=["z", "atr"])

    def entry_signal(self, ts, row, allow_shorts):
        close_min = _minute(ts) + self.timeframe_minutes  # bar timestamps are bar starts
        if row.last_of_day or not (_hhmm(self.p["entry_start"]) <= close_min <= _hhmm(self.p["entry_end"])):
            return 0
        if row.z <= -self.p["z_entry"]:
            return 1
        if row.z >= self.p["z_entry"] and allow_shorts:
            return -1
        return 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - side * self.p["stop_atr"] * row.atr

    def exit_signal(self, row, pos):
        if (pos.side > 0 and row.z >= 0) or (pos.side < 0 and row.z <= 0):
            return "z_cross"
        return None

    def flatten_at_open(self, ts):
        # first bar that starts at/after flatten_time, floored to the bar size (15:50 -> 15:45 bar)
        tf = self.timeframe_minutes
        return _minute(ts) >= _hhmm(self.p["flatten_time"]) // tf * tf

    def flatten_at_close(self, row):
        return bool(row.last_of_day)  # half days end before flatten_time; never hold overnight


class BtcBreakout(Leg):
    """1h bars. Long when close breaks above the prior 20-bar high and close > EMA50. Long only.
    Exit on a 2 x ATR(14) trailing stop or a close below the prior 10-bar low."""
    name = "btc_breakout"
    asset = "crypto"

    def prepare(self, bars):
        p, df = self.p, bars.copy()
        df["hh"] = df["high"].rolling(p["breakout_lookback"]).max().shift(1)
        df["ll"] = df["low"].rolling(p["exit_low_lookback"]).min().shift(1)
        df["ema"] = ta.ema(df["close"], p["ema_period"])
        df["atr"] = ta.atr(df, p["atr_period"])
        return df.dropna(subset=["hh", "ll", "ema", "atr"])

    def entry_signal(self, ts, row, allow_shorts):
        return 1 if row.close > row.hh and row.close > row.ema else 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - self.p["trail_atr"] * row.atr

    def update_stop(self, pos, row):
        pos.high_water = max(pos.high_water, row.high)
        pos.stop = max(pos.stop, pos.high_water - self.p["trail_atr"] * row.atr)

    def exit_signal(self, row, pos):
        return "channel_low" if row.close < row.ll else None


class CommodityTrend(Leg):
    """4h session bars. Long on EMA20 crossing above EMA50, short on the opposite cross.
    Exit on the opposite cross or a 2 x ATR(14) stop."""
    name = "commodity_trend"

    def prepare(self, bars):
        p, df = self.p, bars.copy()
        fast, slow = ta.ema(df["close"], p["ema_fast"]), ta.ema(df["close"], p["ema_slow"])
        df["ema_f"], df["ema_s"] = fast, slow
        above = fast > slow
        valid = fast.notna() & slow.notna() & fast.shift(1).notna() & slow.shift(1).notna()
        df["cross_up"] = valid & above & ~above.shift(1, fill_value=False)
        df["cross_dn"] = valid & ~above & above.shift(1, fill_value=False)
        df["atr"] = ta.atr(df, p["atr_period"])
        return df[valid & df["atr"].notna()]

    def entry_signal(self, ts, row, allow_shorts):
        if row.cross_up:
            return 1
        if row.cross_dn and allow_shorts:
            return -1
        return 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - side * self.p["stop_atr"] * row.atr

    def exit_signal(self, row, pos):
        if (pos.side > 0 and row.cross_dn) or (pos.side < 0 and row.cross_up):
            return "opposite_cross"
        return None


class StockTrend(CommodityTrend):
    """Same EMA-cross trend rules as CommodityTrend, on daily bars of individual stocks."""
    name = "stock_trend"


LEGS = {c.name: c for c in (MeanReversion, BtcBreakout, CommodityTrend, StockTrend)}
