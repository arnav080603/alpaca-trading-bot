"""Risk engine: sizing, caps, shorts rules, correlation block, kill switches, cash-account mode."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.risk import Held, RiskEngine, SettlementLedger, correlation

D1, D2 = date(2026, 10, 7), date(2026, 10, 8)


def cfg(**acct):
    return dict(
        account={**dict(starting_equity=10000, allow_shorts=True, cash_account_mode=False), **acct},
        risk=dict(risk_per_trade=0.01, max_position_pct=0.25, max_open_positions=4, max_gross_exposure=1.0,
                  correlation_lookback_days=60, correlation_threshold=0.8, daily_kill_switch_pct=0.05, stop_file="STOP"),
        legs=dict(mean_reversion={}, commodity_trend=dict(allow_shorts=False), btc_breakout={}),
    )


class FakeAlerts:
    def __init__(self):
        self.kills, self.blocks = [], []

    def kill_switch(self, reason, equity):
        self.kills.append(reason)

    def blocked(self, leg, symbol, side, reason):
        self.blocks.append((symbol, reason))


@pytest.fixture
def make(tmp_path):
    def _make(equity=10000, day=D1, **acct):
        e = RiskEngine(cfg(**acct), root=tmp_path, alerts=FakeAlerts())
        e.check(day, equity)
        return e
    return _make


def ev(e, symbol="SPY", side=1, price=100.0, stop=98.0, equity=10000, held=(), leg="mean_reversion", **kw):
    return e.evaluate(leg, symbol, side, price, stop, equity, list(held), **kw)


@pytest.fixture
def closes():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2026-01-01", periods=120)
    a = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx)))
    b = a * (1 + rng.normal(0, 0.001, len(idx)))           # moves with a
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx)))  # independent
    return pd.DataFrame(dict(SPY=a, QQQ=b, GLD=c), index=idx)


# ---- sizing and caps ------------------------------------------------------------------
def test_size_is_one_percent_risk_when_that_is_tightest(make):
    d = ev(make(), price=100, stop=90)
    assert d.ok and (d.qty, d.reason) == (10, "risk")  # $100 risk / $10 stop distance


def test_position_cap_25pct(make):
    d = ev(make(), price=100, stop=99.5)  # risk alone would buy 200 shares = $20k
    assert (d.qty, d.reason) == (25, "position_cap")


def test_gross_exposure_cap_100pct(make):
    held = [Held("A", 1, 45, 100), Held("B", 1, 45, 100)]  # $9,000 already out
    d = ev(make(), held=held)
    assert (d.qty, d.reason) == (10, "gross_cap")


def test_gross_counts_shorts_as_exposure(make):
    held = [Held("A", -1, 90, 100)]
    assert ev(make(), held=held).qty == 10


def test_max_four_open_positions(make):
    held = [Held(s, 1, 1, 10) for s in "ABCD"]
    d = ev(make(), held=held)
    assert not d.ok and d.reason == "max open positions"


def test_no_second_position_in_same_symbol(make):
    d = ev(make(), held=[Held("SPY", 1, 1, 100)])
    assert not d.ok and "already holding" in d.reason


def test_stop_at_entry_price_is_blocked(make):
    d = ev(make(), price=100, stop=100)
    assert not d.ok and "invalid" in d.reason


def test_size_scales_with_current_equity(make):
    assert ev(make(equity=8000), equity=8000, price=100, stop=90).qty == 8


# ---- shorts ---------------------------------------------------------------------------
def test_shorts_whole_shares_only(make):
    d = ev(make(), side=-1, price=99, stop=102)  # cap allows 25.25
    assert d.ok and d.qty == 25


def test_shorts_disabled_globally(make):
    assert ev(make(allow_shorts=False), side=-1, stop=102).reason == "shorts disabled"


def test_shorts_disabled_per_leg(make):
    d = ev(make(), side=-1, stop=102, symbol="GLD", leg="commodity_trend")
    assert d.reason == "shorts disabled"


def test_crypto_never_shorted(make):
    d = ev(make(), side=-1, stop=102, symbol="BTC/USD", leg="btc_breakout", crypto=True)
    assert d.reason == "crypto can't be shorted"


def test_backtest_override_can_force_shorts(tmp_path):
    e = RiskEngine(cfg(), root=tmp_path, shorts=True)
    e.check(D1, 10000)
    assert ev(e, side=-1, stop=102, symbol="GLD", leg="commodity_trend").ok


# ---- correlation filter ---------------------------------------------------------------
def test_blocks_same_direction_when_correlated(make, closes):
    held = [Held("SPY", 1, 10, 100)]
    d = ev(make(), symbol="QQQ", held=held, closes=closes)
    assert not d.ok and "correlated" in d.reason and "SPY" in d.reason


def test_allows_opposite_direction_when_correlated(make, closes):
    held = [Held("SPY", 1, 10, 100)]
    assert ev(make(), symbol="QQQ", side=-1, stop=102, held=held, closes=closes).ok


def test_allows_uncorrelated(make, closes):
    held = [Held("SPY", 1, 10, 100)]
    assert ev(make(), symbol="GLD", held=held, closes=closes).ok


def test_correlation_unknown_with_little_data(make, closes):
    held = [Held("SPY", 1, 10, 100)]
    assert ev(make(), symbol="QQQ", held=held, closes=closes.head(10)).ok


def test_correlation_never_uses_data_on_or_after_asof(closes):
    c = closes.copy()
    cut = c.index[60]
    k = c["GLD"].iloc[59] / c["SPY"].iloc[59]
    c.loc[c.index >= cut, "GLD"] = c.loc[c.index >= cut, "SPY"] * k  # GLD copies SPY's moves only from `cut` on
    assert correlation(c, "SPY", "GLD", 60) > 0.99           # recent window sees the copy
    assert correlation(c, "SPY", "GLD", 60, asof=cut) < 0.5  # decision at `cut` must not


def test_correlation_window_is_lookback_days(closes):
    c = closes.copy()
    c.iloc[:50, c.columns.get_loc("GLD")] = c["SPY"].iloc[:50]  # correlated long ago only
    assert correlation(c, "SPY", "GLD", 60) < 0.5


# ---- daily kill switch ----------------------------------------------------------------
def test_daily_kill_switch_at_5pct(make):
    e = make(equity=10000)
    assert e.check(D1, 9501) is None
    assert e.check(D1, 9500) == "daily_loss"
    assert e.kill_switch_triggered and len(e.alerts.kills) == 1


def test_kill_switch_alerts_once_and_blocks_entries(make):
    e = make()
    e.check(D1, 9400)
    e.check(D1, 9300)
    assert len(e.alerts.kills) == 1
    d = ev(e)
    assert not d.ok and "halted" in d.reason


def test_kill_switch_stays_halted_if_equity_recovers_same_day(make):
    e = make()
    e.check(D1, 9400)
    assert e.check(D1, 10200) == "daily_loss"


def test_kill_switch_resets_next_session(make):
    e = make()
    e.check(D1, 9400)
    assert e.check(D2, 9400) is None and not e.kill_switch_triggered
    assert ev(e, equity=9400).ok
    assert e.day_start_equity == 9400  # new day's baseline


# ---- manual kill switch ---------------------------------------------------------------
def test_stop_file_halts_and_removal_resumes(make, tmp_path):
    e = make()
    (tmp_path / "STOP").touch()
    assert e.check(D1, 10000) == "manual" and len(e.alerts.kills) == 1
    assert not ev(e).ok
    assert e.check(D1, 10000) == "manual" and len(e.alerts.kills) == 1
    (tmp_path / "STOP").unlink()
    assert e.check(D1, 10000) is None and ev(e).ok


def test_stop_file_beats_daily_reset(make, tmp_path):
    e = make()
    (tmp_path / "STOP").touch()
    e.check(D1, 10000)
    assert e.check(D2, 10000) == "manual"


def test_stop_file_ignored_when_disabled(tmp_path):
    (tmp_path / "STOP").touch()
    e = RiskEngine(cfg(), root=tmp_path, check_stop_file=False)
    assert e.check(D1, 10000) is None


# ---- cash-account mode ----------------------------------------------------------------
def test_cash_mode_blocks_shorts(make):
    assert ev(make(cash_account_mode=True), side=-1, stop=102, cash=10000).reason == "shorts disabled"


def test_cash_mode_limits_to_cash(make):
    d = ev(make(cash_account_mode=True), cash=1000)
    assert (d.qty, d.reason) == (10, "settled_cash")


def test_cash_mode_cannot_spend_unsettled_sale(make):
    e = make(cash_account_mode=True)
    e.record_sale(2000, D1)
    d = ev(e, cash=2500)  # only $500 settled
    assert (d.qty, d.reason) == (5, "settled_cash")
    e.check(D2, 10000)    # T+1: settled
    assert ev(e, cash=2500).qty == 25


def test_cash_mode_crypto_sales_settle_immediately(make):
    e = make(cash_account_mode=True)
    e.record_sale(2000, D1, crypto=True)
    assert ev(e, cash=2500).qty == 25


def test_cash_mode_zero_cash_blocks(make):
    d = ev(make(cash_account_mode=True), cash=0)
    assert not d.ok and "settled_cash" in d.reason


def test_margin_mode_ignores_cash(make):
    assert ev(make(), cash=0).qty == 25


@pytest.mark.parametrize("trade, settles", [
    (date(2026, 10, 7), date(2026, 10, 8)),   # Wed -> Thu
    (date(2026, 10, 9), date(2026, 10, 13)),  # Fri -> Tue (Mon Oct 12 is Columbus Day, banks closed)
    (date(2026, 7, 2), date(2026, 7, 6)),     # Thu -> Mon (Fri Jul 3 = Independence Day observed)
])
def test_t_plus_1_settlement_calendar(trade, settles):
    led = SettlementLedger()
    led.record_sale(100, trade)
    assert led.pending[0][0] == settles
    assert led.unsettled(settles - pd.Timedelta(days=1)) == 100
    assert led.unsettled(settles) == 0


# ---- blocked-trade logging ------------------------------------------------------------
def test_blocked_trades_counted_once_per_symbol_reason_per_day(make):
    e = make(allow_shorts=False)
    for _ in range(3):
        ev(e, side=-1, stop=102)
    ev(e, symbol="QQQ", side=-1, stop=102)
    assert e.blocked_trades == 2 and e.alerts.blocks == [("SPY", "shorts disabled"), ("QQQ", "shorts disabled")]
    e.check(D2, 10000)
    assert e.blocked_trades == 0


def test_blocked_reason_change_counts_again(make):
    e = make()
    held = [Held(s, 1, 1, 10) for s in "ABCD"]
    ev(e, held=held)
    ev(e, held=held[:1] + [Held("SPY", 1, 1, 100)])
    assert e.blocked_trades == 2
