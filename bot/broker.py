"""Thin Alpaca wrapper: the broker is the source of truth for cash, positions and orders.
Transient API errors (network, 429, 5xx) are retried with exponential backoff."""
import logging
import time

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLimitOrderRequest, StopOrderRequest

from .data import is_crypto, with_timeout

log = logging.getLogger(__name__)


def norm(symbol):
    """Alpaca reports crypto positions as BTCUSD but trades them as BTC/USD."""
    return symbol.replace("/", "")


def _transient(e):
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    return isinstance(e, APIError) and (e.status_code == 429 or (e.status_code or 0) >= 500)


def retry(fn, tries=5, backoff=1.0):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if not _transient(e) or i == tries - 1:
                raise
            wait = backoff * 2 ** i
            log.warning("transient API error (%s), retry %d in %.0fs", e, i + 1, wait)
            time.sleep(wait)


class Broker:
    CRYPTO_STOP_LIMIT = 0.01  # crypto stops are stop-limit: sell no lower than 1% under the stop

    def __init__(self, env, dry_run=False):
        key, secret = env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]
        paper = (env.get("ALPACA_PAPER") or "true").lower() != "false"
        self.tc = with_timeout(TradingClient(key, secret, paper=paper))
        self.sd = with_timeout(StockHistoricalDataClient(key, secret))
        self.cd = with_timeout(CryptoHistoricalDataClient(key, secret))
        self.dry = dry_run

    def account(self):
        a = retry(self.tc.get_account)
        return float(a.equity), float(a.cash)

    def positions(self):
        """{normalized symbol: dict(qty=signed qty, entry=avg entry, price=current)}"""
        return {p.symbol: dict(qty=float(p.qty) * (1 if p.side.value == "long" else -1),
                               entry=float(p.avg_entry_price), price=float(p.current_price))
                for p in retry(self.tc.get_all_positions)}

    def open_orders(self):
        return retry(lambda: self.tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)))

    def market(self, symbol, qty, side):
        """side +1 buy / -1 sell. Returns order id. After hours, equity orders queue for the next open."""
        tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY if side > 0 else OrderSide.SELL, time_in_force=tif)
        return self._submit(req, f"market {'buy' if side > 0 else 'sell'} {qty:g} {symbol}")

    def stop(self, symbol, qty, side, stop_price):
        """Server-side GTC stop. side is the order side: -1 sells a long. Stocks: stop-market (whole
        shares). Crypto (Alpaca has no crypto stop-market): stop-limit with the limit 1% past the stop."""
        s = OrderSide.BUY if side > 0 else OrderSide.SELL
        if is_crypto(symbol):
            limit = stop_price * (1 + side * self.CRYPTO_STOP_LIMIT)
            req = StopLimitOrderRequest(symbol=symbol, qty=qty, side=s, time_in_force=TimeInForce.GTC,
                                        stop_price=round(stop_price, 2), limit_price=round(limit, 2))
        else:
            req = StopOrderRequest(symbol=symbol, qty=qty, side=s, time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 2))
        return self._submit(req, f"stop {'buy' if side > 0 else 'sell'} {qty:g} {symbol} @ {stop_price:.2f}")

    def _submit(self, req, what):
        if self.dry:
            log.info("DRY RUN, would place: %s", what)
            return None
        o = retry(lambda: self.tc.submit_order(req))
        log.info("placed %s (order %s)", what, o.id)
        return str(o.id)

    def cancel(self, order_id):
        if self.dry or not order_id:
            return
        try:
            retry(lambda: self.tc.cancel_order_by_id(order_id))
        except APIError as e:
            log.warning("cancel %s failed (probably already filled/canceled): %s", order_id, e)

    def order(self, order_id):
        o = retry(lambda: self.tc.get_order_by_id(order_id))
        return dict(status=o.status.value, filled_qty=float(o.filled_qty or 0),
                    price=float(o.filled_avg_price) if o.filled_avg_price else None)

    def last_fill(self, symbol, side):
        """Average price of the most recent filled order on `symbol` with order side `side`."""
        orders = retry(lambda: self.tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=20)))
        want = "buy" if side > 0 else "sell"
        for o in orders:  # newest first
            if o.side.value == want and o.filled_avg_price:
                return float(o.filled_avg_price)
        return None

    def latest_price(self, symbol):
        if is_crypto(symbol):
            r = retry(lambda: self.cd.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol)))
        else:
            r = retry(lambda: self.sd.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)))
        return float(r[symbol].price)

    def latest_prices(self, symbols):
        """Latest trade price for many symbols in one request per asset class."""
        out, stocks, crypto = {}, [s for s in symbols if not is_crypto(s)], [s for s in symbols if is_crypto(s)]
        if stocks:
            r = retry(lambda: self.sd.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=stocks, feed=DataFeed.IEX)))
            out.update({s: float(t.price) for s, t in r.items()})
        if crypto:
            r = retry(lambda: self.cd.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=crypto)))
            out.update({s: float(t.price) for s, t in r.items()})
        return out

    def market_open(self):
        return retry(self.tc.get_clock).is_open
