"""Checks the backtest fill model on hand-built bars, so the results table can be trusted."""
import pandas as pd
import pytest

from backtest import simulate
from bot.risk import position_size
from bot.strategies import Leg

RISK = dict(risk_per_trade=0.01, max_position_pct=0.25, max_open_positions=4, max_gross_exposure=1.0,
            correlation_lookback_days=60, correlation_threshold=0.8, daily_kill_switch_pct=0.05, stop_file="STOP")
CFG = dict(account=dict(starting_equity=10000, allow_shorts=True, cash_account_mode=False), risk=RISK, legs={},
           costs=dict(equity_slippage=0.0, crypto_slippage=0.0, crypto_fee=0.0))


class Scripted(Leg):
    """Enters long on bar 0's close with a fixed $2 stop; optional exit signal on a given bar."""
    name = "scripted"

    def __init__(self, exit_on=None):
        super().__init__(dict(timeframe_minutes=60))
        self.exit_on = exit_on

    def entry_signal(self, ts, row, allow_shorts):
        return 1 if row.i == 0 else 0

    def initial_stop(self, side, entry_price, row):
        return entry_price - 2.0

    def exit_signal(self, row, pos):
        return "signal" if row.i == self.exit_on else None


def bars(ohlc):
    idx = pd.date_range("2024-01-02 10:00", periods=len(ohlc), freq="h", tz="America/New_York")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["i"] = range(len(df))
    return df


def run(leg, df, cfg=CFG):
    return simulate(leg, {"X": df}, df.index[0], df.index[-1] + pd.Timedelta(hours=1), cfg, True)


def test_signal_fills_next_open_and_exit_signal_fills_following_open():
    df = bars([(100, 101, 99, 100), (101, 102, 100, 101), (103, 104, 102, 103), (105, 106, 104, 105)])
    trades, _, _ = run(Scripted(exit_on=2), df)
    t = trades[0]
    assert (t["entry"], t["exit"], t["exit_reason"]) == (101, 105, "signal")


def test_stop_fills_at_stop_price():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (99, 99.5, 97, 98)])
    t = run(Scripted(), df)[0][0]
    assert (t["exit"], t["exit_reason"], t["slippage"]) == (98, "stop", 0)


def test_gap_through_stop_fills_at_open_and_logs_slippage():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (95, 96, 94, 95)])
    t = run(Scripted(), df)[0][0]
    assert t["exit"] == 95 and t["exit_reason"] == "stop"
    assert t["slippage"] == pytest.approx(3 * t["qty"])  # intended 98, got 95


def test_costs_reduce_pnl():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (110, 110, 110, 110)])
    cfg = {**CFG, "costs": dict(equity_slippage=0.001, crypto_slippage=0, crypto_fee=0)}
    t = run(Scripted(exit_on=1), df, cfg)[0][0]
    assert t["entry"] == pytest.approx(100.1) and t["exit"] == pytest.approx(110 * 0.999)


def test_open_position_closed_at_end_of_test():
    df = bars([(100, 100, 100, 100), (100, 101, 99.5, 101), (101, 102, 100.5, 102)])
    t = run(Scripted(), df)[0][0]
    assert t["exit_reason"] == "end_of_test" and t["exit"] == 102


def test_daily_kill_switch_flattens_in_backtest():
    # with 1% risk a single trade can only lose >5% by gapping through its stop:
    # X gaps from 100 to 70 (25 sh x -$30 = -7.5%), so Y must be flattened at the next open
    x = bars([(100, 100, 100, 100), (100, 100, 100, 100), (70, 70, 70, 70), (70, 70, 70, 70)])
    y = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 100), (101, 101, 101, 101)])
    trades, _, _ = simulate(Scripted(), {"X": x, "Y": y}, x.index[0], x.index[-1] + pd.Timedelta(hours=1), CFG, True)
    by = {t["symbol"]: t for t in trades}
    assert by["X"]["exit_reason"] == "stop" and by["X"]["exit"] == 70
    assert by["Y"]["exit_reason"] == "kill_switch" and by["Y"]["exit"] == 101


def test_cash_mode_backtest_never_spends_unsettled_cash():
    cfg = {**CFG, "account": dict(starting_equity=10000, allow_shorts=False, cash_account_mode=True)}
    leg = Scripted()
    leg.entry_signal = lambda ts, row, s: 1 if row.i in (0, 2) else 0
    leg.exit_signal = lambda row, pos: "signal" if row.i == 1 else None
    df = bars([(100, 101, 99, 100)] * 4)
    trades, _, _ = simulate(leg, {"X": df}, df.index[0], df.index[-1] + pd.Timedelta(hours=1), cfg, False)
    # day-1 sale proceeds are unsettled, but $7,500 settled cash still covers a 25% position
    assert len(trades) == 2 and trades[1]["qty"] == 25


def test_sizing_uses_tightest_limit():
    assert position_size(10000, 100, 98, 0, 0, RISK) == (25.0, "position_cap")  # risk says 50 shares
    assert position_size(10000, 100, 90, 0, 0, RISK) == (10.0, "risk")
    assert position_size(10000, 100, 98, 9000, 0, RISK) == (10.0, "gross_cap")
    assert position_size(10000, 100, 98, 0, 4, RISK)[0] == 0
    assert position_size(10000, 100, 97, 0, 0, RISK, fractional=False) == (25, "position_cap")
