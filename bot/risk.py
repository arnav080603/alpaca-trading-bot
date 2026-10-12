"""Risk engine shared by every leg, the backtester and the live bot.

Deterministic and broker-agnostic: callers pass in equity, cash and held positions; the engine
answers "may I open this, and how big?" and "must everything be flattened right now?".
Every blocked trade is logged with its reason.
"""
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

log = logging.getLogger(__name__)
# Settlement follows the banking calendar: weekends and US federal holidays don't count
SETTLE_DAY = CustomBusinessDay(calendar=USFederalHolidayCalendar())
MIN_CORR_OBS = 20  # fewer common daily returns than this = correlation unknown, don't block


def position_size(equity, price, stop, gross_open, open_count, rcfg, fractional=True):
    """Quantity = risk_per_trade * equity / stop distance, cut down by the position cap, the gross
    exposure cap and the max-open-positions limit. Returns (qty, binding_rule)."""
    dist = abs(price - stop)
    if dist <= 0 or price <= 0 or equity <= 0:
        return 0.0, "invalid"
    if open_count >= rcfg["max_open_positions"]:
        return 0.0, "max_open_positions"
    limits = {
        "risk": rcfg["risk_per_trade"] * equity / dist,
        "position_cap": rcfg["max_position_pct"] * equity / price,
        "gross_cap": max(0.0, rcfg["max_gross_exposure"] * equity - gross_open) / price,
    }
    rule = min(limits, key=limits.get)
    qty = limits[rule]
    if not fractional:
        qty = math.floor(qty)
    return (qty, rule) if qty > 0 else (0.0, rule)


def correlation(closes, a, b, lookback, asof=None):
    """Correlation of daily returns of a and b over the last `lookback` common days strictly before
    `asof` (so a decision never sees today's or future data). NaN if too little overlap."""
    df = closes[[a, b]]
    if asof is not None:
        df = df[df.index < asof]
    r = df.dropna().pct_change().dropna().tail(lookback)
    return r[a].corr(r[b]) if len(r) >= MIN_CORR_OBS else math.nan


@dataclass
class Held:
    symbol: str
    side: int      # +1 long, -1 short
    qty: float
    price: float   # current price


@dataclass
class Decision:
    qty: float
    reason: str    # binding sizing rule when allowed, block reason when not

    @property
    def ok(self):
        return self.qty > 0


class SettlementLedger:
    """Cash account, T+1: proceeds from selling stocks/ETFs can't be spent until the next
    settlement day. Crypto settles immediately, so it never enters the ledger."""

    def __init__(self):
        self.pending = []  # (settle_date, amount)

    def record_sale(self, amount, trade_date):
        self.pending.append(((pd.Timestamp(trade_date) + SETTLE_DAY).date(), amount))

    def unsettled(self, today):
        self.pending = [(d, a) for d, a in self.pending if d > today]
        return sum(a for _, a in self.pending)


class RiskEngine:
    def __init__(self, cfg, root=".", alerts=None, check_stop_file=True, shorts=None):
        """`shorts` overrides every shorts toggle (backtest long-only vs both-sides runs only)."""
        self.r, self.acct = cfg["risk"], cfg["account"]
        self.leg_shorts = {n: p.get("allow_shorts", True) for n, p in cfg["legs"].items()}
        self.shorts_override = shorts
        self.stop_path = Path(root) / self.r["stop_file"]
        self.check_stop_file, self.alerts = check_stop_file, alerts
        self.ledger = SettlementLedger()
        self.day = self.day_start_equity = self.halted = None
        self.kill_switch_triggered, self.blocked_trades, self._last_block = False, 0, {}

    # ---- kill switches -------------------------------------------------------------
    def check(self, day, equity):
        """Call every bar (backtest) / at least every minute (live). Returns the halt reason while
        trading is halted (caller must flatten everything), else None. The daily kill switch resets
        at the next session; the manual one lasts while the STOP file exists."""
        if day != self.day:
            self.day, self.day_start_equity = day, equity
            self.kill_switch_triggered, self.blocked_trades, self._last_block = False, 0, {}
            if self.halted == "daily_loss":
                self.halted = None
        stop_file = self.check_stop_file and self.stop_path.exists()
        if stop_file and self.halted != "manual":
            self._trigger("manual", f"STOP file found ({self.stop_path.name})", equity)
        elif not stop_file and self.halted == "manual":
            self.halted = None
            log.warning("STOP file removed, trading resumed")
        if not self.halted and equity <= self.day_start_equity * (1 - self.r["daily_kill_switch_pct"]):
            drop = 1 - equity / self.day_start_equity
            self._trigger("daily_loss", f"equity down {drop:.1%} today (limit {self.r['daily_kill_switch_pct']:.0%})", equity)
        return self.halted

    def _trigger(self, kind, reason, equity):
        self.halted, self.kill_switch_triggered = kind, True
        log.error("KILL SWITCH: %s; flattening and halting", reason)
        if self.alerts:
            self.alerts.kill_switch(reason, equity)

    # ---- entries -------------------------------------------------------------------
    def shorts_allowed(self, leg):
        if self.shorts_override is not None:
            return self.shorts_override
        return bool(self.acct["allow_shorts"]) and not self.acct["cash_account_mode"] and self.leg_shorts.get(leg, True)

    def evaluate(self, leg, symbol, side, price, stop, equity, held, closes=None, asof=None, cash=None, crypto=False):
        """May `leg` open `side` in `symbol` at `price` with `stop`? Returns a Decision with the size.
        Call check() first each bar/minute. `cash` is required in cash-account mode."""
        if self.halted:
            return self._block(leg, symbol, side, f"trading halted ({self.halted})")
        if side < 0 and crypto:
            return self._block(leg, symbol, side, "crypto can't be shorted")
        if side < 0 and not self.shorts_allowed(leg):
            return self._block(leg, symbol, side, "shorts disabled")
        if any(h.symbol == symbol for h in held):
            return self._block(leg, symbol, side, "already holding this symbol")
        if len(held) >= self.r["max_open_positions"]:
            return self._block(leg, symbol, side, "max open positions")
        if closes is not None:
            for h in held:
                if h.side == side and symbol in closes and h.symbol in closes:
                    c = correlation(closes, symbol, h.symbol, self.r["correlation_lookback_days"], asof)
                    if c > self.r["correlation_threshold"]:
                        return self._block(leg, symbol, side, f"correlated {c:.2f} with open {h.symbol} same direction")
        gross = sum(abs(h.qty * h.price) for h in held)
        fractional = crypto  # whole shares for stocks/ETFs so their stops can sit server-side as GTC orders
        qty, rule = position_size(equity, price, stop, gross, len(held), self.r, fractional)
        if self.acct["cash_account_mode"]:
            spendable = max(0.0, cash - self.ledger.unsettled(self.day))
            if qty * price > spendable:
                qty, rule = spendable / price, "settled_cash"
                qty = qty if fractional else math.floor(qty)
        if qty <= 0:
            return self._block(leg, symbol, side, f"size is zero ({rule})")
        self._last_block.pop(symbol, None)
        return Decision(qty, rule)

    def record_sale(self, amount, trade_date, crypto=False):
        """Call on every closing sale so cash-account mode knows what hasn't settled yet."""
        if not crypto:
            self.ledger.record_sale(amount, trade_date)

    def _block(self, leg, symbol, side, reason):
        log.info("blocked %s %s %s: %s", leg, "long" if side > 0 else "short", symbol, reason)
        # a signal that stays true re-asks every bar: count/alert once per symbol per reason per day
        if self._last_block.get(symbol) != reason:
            self._last_block[symbol] = reason
            self.blocked_trades += 1
            if self.alerts:
                self.alerts.blocked(leg, symbol, "long" if side > 0 else "short", reason)
        return Decision(0.0, reason)
