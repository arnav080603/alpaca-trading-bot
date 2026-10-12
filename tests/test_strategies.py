import pandas as pd

from bot.data import session_buckets
from bot.strategies import CommodityTrend, MeanReversion

MR = dict(timeframe_minutes=15, lookback=20, z_entry=2.0, atr_period=14, stop_atr=1.5,
          entry_start="09:45", entry_end="15:30", flatten_time="15:50")


def ts(hhmm):
    return pd.Timestamp(f"2024-01-02 {hhmm}", tz="America/New_York")


def test_mean_reversion_entry_window_uses_bar_close_time():
    leg = MeanReversion(MR)
    row = pd.Series(dict(z=-3.0, last_of_day=False))
    row = type("R", (), dict(row))()
    assert leg.entry_signal(ts("09:15"), row, True) == 0   # closes 09:30
    assert leg.entry_signal(ts("09:30"), row, True) == 1   # closes 09:45
    assert leg.entry_signal(ts("15:15"), row, True) == 1   # closes 15:30
    assert leg.entry_signal(ts("15:30"), row, True) == 0   # closes 15:45


def test_mean_reversion_shorts_only_when_allowed():
    leg = MeanReversion(MR)
    row = type("R", (), dict(z=2.5, last_of_day=False))()
    assert leg.entry_signal(ts("11:00"), row, True) == -1
    assert leg.entry_signal(ts("11:00"), row, False) == 0


def test_flatten_bar_is_1545():
    leg = MeanReversion(MR)
    assert not leg.flatten_at_open(ts("15:30")) and leg.flatten_at_open(ts("15:45"))


def test_session_buckets():
    idx = pd.date_range("2024-01-02 09:30", "2024-01-02 15:45", freq="15min", tz="America/New_York")
    df = pd.DataFrame(dict(open=range(len(idx)), high=range(len(idx)), low=range(len(idx)),
                           close=range(len(idx)), volume=1), index=idx, dtype=float)
    out = session_buckets(df)
    assert [t.strftime("%H:%M") for t in out.index] == ["09:30", "13:30"]
    assert out.iloc[0]["open"] == 0 and out.iloc[0]["close"] == 15 and out.iloc[1]["close"] == 25
    assert out.iloc[0]["volume"] == 16 and out.iloc[1]["volume"] == 10


def test_commodity_crosses_fire_once_each_in_order():
    close = [100.0 - i for i in range(40)] + [61.0 + i for i in range(30)] + [90.0 - i for i in range(30)]
    df = pd.DataFrame(dict(open=close, high=close, low=close, close=close),
                      index=pd.date_range("2024-01-02", periods=len(close), freq="h", tz="America/New_York"))
    p = dict(timeframe_minutes=240, ema_fast=3, ema_slow=6, atr_period=3, stop_atr=2.0)
    out = CommodityTrend(p).prepare(df)
    ups, dns = out.index[out["cross_up"]], out.index[out["cross_dn"]]
    assert len(ups) == 1 and len(dns) == 1 and ups[0] < dns[0]
