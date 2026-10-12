"""Live paper-trading loop. Same strategy and risk code as backtest.py; Alpaca is the source of truth.

Every minute: read the account, run the kill switches, sync fills (entries, exits, server-side stops),
check bot-managed stops (crypto), and when a leg's bar has closed, evaluate it exactly like the
backtester does: update stop -> exit signal -> entry signal -> risk engine -> order.
"""
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

from .alerts import Discord
from .broker import Broker, norm
from .config import ROOT
from .data import ET, bucket_end, fetch_recent_many, is_crypto, leg_bars
from .risk import Held, RiskEngine
from .strategies import LEGS
from . import ai, journal, watch

log = logging.getLogger("bot")
TRADE_COLS = ["timestamp", "leg", "symbol", "side", "qty", "entry", "stop", "exit", "pnl", "exit_reason", "slippage"]
SUMMARY_COLS = ["date", "start_equity", "end_equity", "pnl", "trades", "wins", "losses", "blocked_trades", "kill_switch_triggered"]
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
RETRY = pd.Timedelta(minutes=15)  # bar expected but not there yet (holiday, data lag): look again


def append_csv(path, cols, row):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({k: row[k] for k in cols})


class Bot:
    def __init__(self, cfg, env, root=ROOT, broker=None, alerts=None, dry_run=False):
        self.cfg, self.env, self.root, self.dry = cfg, env, root, dry_run
        self.logs = root / "logs"
        self.logs.mkdir(exist_ok=True)
        self.state_path = root / "state.json"
        self.alerts = alerts or Discord(None if dry_run else env.get("DISCORD_WEBHOOK_URL"), mention=cfg.get("alerts", {}).get("mention"))
        self.broker = broker or Broker(env, dry_run)
        self.risk = RiskEngine(cfg, root=root, alerts=self.alerts)
        self.legs = {n: LEGS[n](p) for n, p in cfg["legs"].items() if p["enabled"]}
        self.sym_leg = {s: n for n, p in cfg["legs"].items() if p["enabled"] for s in p["symbols"]}
        self.daily = {}   # symbol -> daily closes, for the correlation filter
        self.frames = {}  # symbol -> latest prepared bars (indicators), for watch alerts
        self._pref = {}   # symbol -> bars prefetched in one batch this tick
        self.ai = cfg.get("ai") or {}
        self.dec = None   # the AI's latest decisions (None = no fresh file: rules only)
        self.st = self._load()
        d = self.st.get("day")
        if d:  # restart mid-day: keep the day's baseline and kill-switch state
            self.risk.day, self.risk.day_start_equity = pd.Timestamp(d["date"]).date(), d["start_equity"]
            if d.get("halted") == "daily_loss":
                self.risk.halted, self.risk.kill_switch_triggered = "daily_loss", True

    # ---- state ---------------------------------------------------------------------
    def _load(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return dict(positions={}, pending={}, last_bar={}, next_check={}, day=None)

    def save(self):
        if self.dry:
            return
        if self.st.get("day"):
            self.st["day"]["halted"] = self.risk.halted
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1, default=str))
        tmp.replace(self.state_path)

    def money(self):
        """Equity and cash as the bot's own account. With account.set_aside_extra on, anything the
        paper account holds above starting_equity is set aside on first run; off = use it all."""
        eq, cash = self.broker.account()
        if not self.cfg["account"].get("set_aside_extra", True):
            return eq, cash
        if "equity_offset" not in self.st:
            self.st["equity_offset"] = max(0.0, eq - self.cfg["account"]["starting_equity"])
            if self.st["equity_offset"]:
                log.warning("paper account has $%.0f; bot trades as a $%.0f account (offset $%.0f)",
                            eq, eq - self.st["equity_offset"], self.st["equity_offset"])
        off = self.st["equity_offset"]
        return eq - off, cash - off

    # ---- startup -------------------------------------------------------------------
    def reconcile(self):
        """Broker over local state: adopt positions we don't know about, make sure every stock/ETF
        position has a server-side stop, and flag stray orders. Positions we know about that the
        broker no longer has are settled by _sync on the next tick."""
        bpos, orders = self.broker.positions(), self.broker.open_orders()
        for sym, legname in self.sym_leg.items():
            b = bpos.get(norm(sym))
            if b and sym not in self.st["positions"] and sym not in self.st["pending"]:
                leg = self.legs[legname]
                side = 1 if b["qty"] > 0 else -1
                prepared = leg.prepare(leg_bars(leg, sym, self.cfg, self.env, live=True))
                row = next(prepared.tail(1).itertuples())
                stop = leg.initial_stop(side, b["entry"], row)
                self.st["positions"][sym] = dict(leg=legname, side=side, qty=abs(b["qty"]), entry=b["entry"],
                                                 entry_time=str(pd.Timestamp.now(tz=ET)), stop=stop, initial_stop=stop,
                                                 high_water=b["price"], low_water=b["price"], stop_order=None, exit_order=None)
                log.warning("adopted %s position found at broker: %g @ %.2f, stop %.2f", sym, b["qty"], b["entry"], stop)
                self.alerts.error(f"Adopted an existing {sym} position from Alpaca ({b['qty']:g} @ ${b['entry']:,.2f}); stop set at ${stop:,.2f}")
        mine = {norm(s) for s in self.sym_leg}
        known = {p.get("stop_order") for p in self.st["positions"].values()} | {p.get("order_id") for p in self.st["pending"].values()} \
            | {(p.get("exit_order") or {}).get("id") for p in self.st["positions"].values()}
        for o in orders:
            if norm(o.symbol) in mine and str(o.id) not in known:
                held = next((s for s in self.st["positions"] if norm(s) == norm(o.symbol)), None)
                if o.order_type.value in ("stop", "stop_limit") and held:
                    pos = self.st["positions"][held]
                    if pos["stop_order"] is None:
                        pos["stop_order"] = str(o.id)  # our stop from before a restart
                        continue
                log.warning("open order not placed by this bot run: %s %s %s", o.symbol, o.side.value, o.order_type.value)
        for sym, pos in self.st["positions"].items():
            if not pos.get("stop_order") and not pos.get("exit_order") and norm(sym) in bpos:
                pos["stop_order"] = self.broker.stop(sym, pos["qty"], -pos["side"], pos["stop"])
        self.save()

    # ---- the minute loop -----------------------------------------------------------
    def tick(self, now=None):
        now = now or pd.Timestamp.now(tz=ET)
        equity, cash = self.money()
        self._roll_day(now.date(), equity)
        halted = self.risk.check(now.date(), equity)
        bpos = self.broker.positions()
        self._sync(bpos, now)
        if self.ai.get("enabled"):
            self.dec = ai.load(self.root / self.ai["decisions_file"], now, self.ai["max_age_minutes"])
        if halted:
            self._flatten_all(now)
        else:
            mkt = None
            self._prefetch(now)
            for sym, legname in self.sym_leg.items():
                pos = self.st["positions"].get(sym)
                if pos and not pos.get("exit_order"):
                    if is_crypto(sym):  # backup for the crypto stop-limit, which can't fill below its limit
                        self._check_stop(sym, pos, now, backup=bool(pos.get("stop_order")))
                    elif not pos.get("stop_order") and (mkt := mkt if mkt is not None else self.broker.market_open()):
                        self._check_stop(sym, pos, now)
                if self._due(sym, now):
                    self._on_bar(sym, self.legs[legname], now, equity, cash, bpos)
            if self.ai.get("enabled"):
                self._ai_trade(now, equity, cash, bpos)
        self.st["day"]["end_equity"] = equity
        self.st.setdefault("first_equity", equity)
        # live snapshot for the orb dashboard (bot-orb.html reads state.json; it never writes)
        self.st["live"] = dict(at=str(now), equity=round(equity, 2),
                               prices={s: bpos[norm(s)]["price"] for s in self.st["positions"] if norm(s) in bpos})
        self._daily_update(now, equity)
        self._watch(now)
        self._ai_context(now, equity)
        self._journal(now, equity)
        self.save()

    def _daily_update(self, now, equity):
        """One Discord summary per day at alerts.daily_update_time (ET): today's P&L, trades,
        blocks, kill switch, and every open position with its unrealized P&L and stop."""
        at = self.cfg.get("alerts", {}).get("daily_update_time")
        if not at or self.dry or self.st.get("last_update") == str(now.date()) or \
                now.hour * 60 + now.minute < int(at[:2]) * 60 + int(at[3:]):
            return
        d, first = self.st["day"], self.st["first_equity"]
        day_pnl, all_pnl = equity - d["start_equity"], equity - first
        lines = [f"📊 **Daily update, {now:%a %b %d}**",
                 f"Equity **${equity:,.2f}** · today {'+' if day_pnl >= 0 else '-'}${abs(day_pnl):,.2f} "
                 f"({day_pnl / d['start_equity']:+.2%}) · since start {'+' if all_pnl >= 0 else '-'}${abs(all_pnl):,.2f} ({all_pnl / first:+.2%})",
                 f"Trades closed today: {d['trades']} ({d['wins']}W / {d['losses']}L) · blocked: {self.risk.blocked_trades}"
                 f" · kill switch: {'🛑 ' + self.risk.halted if self.risk.halted else 'off'}"]
        if self.ai.get("enabled"):
            n_ai = sum(p["leg"] == "ai_pick" for p in self.st["positions"].values())
            lines.append(f"🤖 AI: {d.get('ai_vetoes', 0)} vetoes today · {n_ai} AI picks held · "
                         + (f"last decisions {self.dec['generated_at']:%H:%M}" if self.dec else "offline (rules only)"))
        bpos = self.broker.positions()
        if self.st["positions"]:
            rows = []
            for sym, p in self.st["positions"].items():
                b = bpos.get(norm(sym))
                price = b["price"] if b else p["entry"]
                rows.append((p["side"] * p["qty"] * (price - p["entry"]), sym, p, price))
            total = sum(r[0] for r in rows)
            lines.append(f"Open positions: {len(rows)} · unrealized {'+' if total >= 0 else '-'}${abs(total):,.2f}"
                         + (" · biggest movers:" if len(rows) > 10 else ":"))
            for upnl, sym, p, price in sorted(rows, key=lambda r: -abs(r[0]))[:10]:
                lines.append(f"• {sym} {'long' if p['side'] > 0 else 'short'} {p['qty']:g} @ ${p['entry']:,.2f} → ${price:,.2f} "
                             f"({'+' if upnl >= 0 else '-'}${abs(upnl):,.2f}), stop ${p['stop']:,.2f}")
        else:
            lines.append("Open positions: none (waiting for a signal)")
        self.alerts.send("\n".join(lines))
        self.st["last_update"] = str(now.date())

    def _prefetch(self, now):
        """Hundreds of daily stock bars come due at the same moment: get them in a few batch requests."""
        groups = {}
        for sym, legname in self.sym_leg.items():
            leg = self.legs[legname]
            if not is_crypto(sym) and leg.timeframe_minutes >= 1440 and self._due(sym, now):
                groups.setdefault(legname, []).append(sym)
        for legname, syms in groups.items():
            if len(syms) > 20:
                p, d = self.legs[legname].p, self.cfg["data"]
                self._pref.update(fetch_recent_many(syms, self.legs[legname].timeframe_minutes,
                                                    p.get("live_lookback_days", d["live_lookback_days"]), self.env,
                                                    p.get("live_feed", d["live_feed"]), now.tz_convert("UTC").to_pydatetime()))

    def _watch(self, now):
        """Watch alerts at alerts.watch_times (ET): the setups closest to triggering, with an animated
        chart of the top one. Weekends: crypto only (stocks are closed). Nothing close -> no message."""
        times = self.cfg.get("alerts", {}).get("watch_times") or []
        sent = self.st.setdefault("watch_sent", {})
        if self.dry or not times or self.risk.halted:
            return
        today, m = str(now.date()), now.hour * 60 + now.minute
        slots = [t for t in times if int(t[:2]) * 60 + int(t[3:]) <= m and t not in sent.get(today, [])]
        if not slots:
            return
        self.st["watch_sent"] = {today: sent.get(today, []) + slots}  # mark first: never double-send
        weekend = now.weekday() >= 5
        if len(self.frames) < len(self.sym_leg) // 2:
            self._load_frames(now)
        legs = {s: self.legs[l] for s, l in self.sym_leg.items() if not (weekend and not is_crypto(s))}
        frames = {s: f for s, f in self.frames.items() if s in legs}
        first = watch.scan(frames, {}, self.st["positions"], legs, max_dist=0.08)[:25]
        prices = self.broker.latest_prices([s.symbol for s in first]) if first else {}
        setups = watch.scan({s.symbol: s.frame for s in first}, prices, self.st["positions"], legs)
        if not setups:
            return
        title = ("Morning watchlist" if m < 12 * 60 else "Afternoon watchlist") if not weekend else "Weekend crypto watch"
        self.alerts.send(watch.message(setups, title), file=(f"{setups[0].symbol.replace('/', '')}_watch.gif", watch.gif(setups[0])))
        log.info("watch alert sent: %s", ", ".join(s.symbol for s in setups[:5]))

    def _load_frames(self, now):
        """After a restart the indicator cache is empty: rebuild it (batch for stocks)."""
        utc = now.tz_convert("UTC").to_pydatetime()
        by_leg = {}
        for s, l in self.sym_leg.items():
            by_leg.setdefault(l, []).append(s)
        for l, syms in by_leg.items():
            leg, d = self.legs[l], self.cfg["data"]
            if leg.timeframe_minutes >= 1440 and not is_crypto(syms[0]) and len(syms) > 20:
                bars = fetch_recent_many(syms, leg.timeframe_minutes, leg.p.get("live_lookback_days", d["live_lookback_days"]),
                                         self.env, leg.p.get("live_feed", d["live_feed"]), utc)
            else:
                bars = {s: leg_bars(leg, s, self.cfg, self.env, live=True, now=utc) for s in syms}
            for s, b in bars.items():
                self.frames[s] = leg.prepare(b)

    def _due(self, sym, now):
        nc = self.st["next_check"].get(sym)
        if nc and now < pd.Timestamp(nc):
            return False
        if is_crypto(sym):
            return True
        m = now.hour * 60 + now.minute
        return now.weekday() < 5 and 9 * 60 + 30 <= m <= 16 * 60 + 30

    def _next_check(self, leg, ts):
        """When the bar after `ts` should be complete (plus a little slack for data to arrive)."""
        slack = pd.Timedelta(minutes=2)
        if leg.name == "commodity_trend":
            if ts.hour < 13:
                return ts.normalize() + pd.Timedelta(hours=16) + slack
            nxt = ts.normalize() + pd.Timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += pd.Timedelta(days=1)
            return nxt + pd.Timedelta(hours=13, minutes=30) + slack
        return ts + 2 * pd.Timedelta(minutes=leg.timeframe_minutes) + slack

    def _on_bar(self, sym, leg, now, equity, cash, bpos):
        bars = self._pref.pop(sym, None)
        if bars is None:
            bars = leg_bars(leg, sym, self.cfg, self.env, live=True, now=now.tz_convert("UTC").to_pydatetime())
        prepared = leg.prepare(bars)
        self.frames[sym] = prepared
        if prepared.empty:
            self.st["next_check"][sym] = str(now + RETRY)
            return
        self.daily[sym] = bars["close"].resample("1D").last()
        row = next(prepared.tail(1).itertuples())
        last = self.st["last_bar"].get(sym)
        if last and row.Index <= pd.Timestamp(last):
            self.st["next_check"][sym] = str(now + RETRY)
            return
        self.st["last_bar"][sym] = str(row.Index)
        self.st["next_check"][sym] = str(self._next_check(leg, row.Index))
        log.info("%s %s bar %s closed at %.2f", leg.name, sym, row.Index, row.close)

        pos = self.st["positions"].get(sym)
        if pos and not pos.get("exit_order") and pos["leg"] == leg.name:  # AI picks are managed in _ai_trade
            ns = SimpleNamespace(**pos)
            leg.update_stop(ns, row)
            if ns.stop != pos["stop"]:
                log.info("%s stop moved %.2f -> %.2f", sym, pos["stop"], ns.stop)
                pos.update(stop=ns.stop, high_water=ns.high_water)
                if pos.get("stop_order"):
                    self.broker.cancel(pos["stop_order"])
                    pos["stop_order"] = self.broker.stop(sym, pos["qty"], -pos["side"], pos["stop"])
            if leg.flatten_at_close(row):
                self._exit(sym, pos, "eod_flatten", row.close)
            elif why := leg.exit_signal(row, ns):
                self._exit(sym, pos, why, row.close)
        if pos or sym in self.st["pending"]:
            return  # entries only from flat (no same-bar reversals live)

        side = leg.entry_signal(row.Index, row, self.risk.shorts_allowed(leg.name))
        if not side:
            return
        crypto = is_crypto(sym)
        stop = leg.initial_stop(side, row.close, row)
        if self.dec and sym in self.dec["vetoes"]:
            self._veto(sym, leg.name, row.close, stop, self.dec["vetoes"][sym], now)
            return
        known = {norm(s): s for s in self.sym_leg}
        held = [Held(known.get(n, n), 1 if b["qty"] > 0 else -1, abs(b["qty"]), b["price"]) for n, b in bpos.items()]
        closes = pd.DataFrame(self.daily) if self.daily else None
        d = self.risk.evaluate(leg.name, sym, side, row.close, stop, equity, held, closes,
                               now.normalize(), cash, crypto)
        if not d.ok:
            return
        qty = round(d.qty, 6) if crypto else int(d.qty)
        oid = self.broker.market(sym, qty, side)
        log.info("ENTRY %s %s %s qty %g (sized by %s), ref price %.2f, stop distance %.2f",
                 leg.name, "long" if side > 0 else "short", sym, qty, d.reason, row.close, abs(row.close - stop))
        if oid:
            self.st["pending"][sym] = dict(leg=leg.name, side=side, qty=qty, order_id=oid,
                                           stop_dist=abs(row.close - stop), submitted=str(now))

    # ---- AI layer (see bot/ai.py) ---------------------------------------------------
    def _veto(self, sym, legname, price, stop, reason, now):
        """The AI blocked a rule entry. Logged with price and stop so vetoes can be scored later."""
        append_csv(self.logs / "ai_vetoes.csv", ai.VETO_COLS, dict(timestamp=str(now), leg=legname, symbol=sym,
                                                                    price=round(price, 4), stop=round(stop, 4), reason=reason))
        self.st["day"]["ai_vetoes"] = self.st["day"].get("ai_vetoes", 0) + 1
        self.alerts.send(f"🤖⛔ AI vetoed **{sym}** ({legname}) at ${price:,.2f}: {reason}")
        log.info("AI VETO %s %s at %.2f: %s", legname, sym, price, reason)

    def _ai_trade(self, now, equity, cash, bpos):
        """Close AI picks the AI wants out of (or held too long), then open new picks. Stocks only, in
        market hours, long only, sized and stopped by the risk engine like every other trade."""
        m = now.hour * 60 + now.minute
        mine = {s: p for s, p in self.st["positions"].items() if p["leg"] == "ai_pick"}
        if (not self.dec and not mine) or now.weekday() >= 5 or not 9 * 60 + 35 <= m <= 15 * 60 + 50 \
                or not self.broker.market_open():
            return
        for sym, pos in mine.items():
            if pos.get("exit_order"):
                continue
            held_days = (now - pd.Timestamp(pos["entry_time"])).days
            if self.dec and sym in self.dec["exits"]:
                log.info("AI EXIT %s: %s", sym, self.dec["exits"][sym])
                self.alerts.send(f"🤖 AI is selling **{sym}**: {self.dec['exits'][sym]}")
                self._exit(sym, pos, "ai_exit", self.broker.latest_price(sym))
            elif held_days >= self.ai["max_hold_days"]:
                self._exit(sym, pos, "ai_max_hold", self.broker.latest_price(sym))
        if not self.dec:
            return
        tried = self.st.setdefault("ai_tried", {})
        today = tried.setdefault(str(now.date()), [])
        for k in [k for k in tried if k != str(now.date())]:
            del tried[k]
        slots = self.ai["max_picks"] - len(mine) - sum(p["leg"] == "ai_pick" for p in self.st["pending"].values())
        stocks = self.legs.get("stock_trend")
        for pick in self.dec["picks"]:
            sym = pick["symbol"]
            if slots <= 0:
                break
            if sym in today or sym in self.st["positions"] or sym in self.st["pending"]:
                continue
            today.append(sym)  # one attempt per symbol per day, so a blocked pick doesn't retry every minute
            if self.sym_leg.get(sym) != "stock_trend" or stocks is None:
                log.warning("AI pick %s ignored: not in the stock universe", sym)
                continue
            frame = self.frames.get(sym)
            if frame is None or frame.empty:
                frame = stocks.prepare(leg_bars(stocks, sym, self.cfg, self.env, live=True, now=now.tz_convert("UTC").to_pydatetime()))
            if frame.empty:
                continue
            price = self.broker.latest_price(sym)
            stop = price - self.ai["stop_atr"] * float(frame["atr"].iloc[-1])
            known = {norm(s): s for s in self.sym_leg}
            held = [Held(known.get(n, n), 1 if b["qty"] > 0 else -1, abs(b["qty"]), b["price"]) for n, b in bpos.items()]
            held += [Held(s, p["side"], p["qty"], float(self.frames[s]["close"].iloc[-1]) if s in self.frames else price)
                     for s, p in self.st["pending"].items() if norm(s) not in bpos]  # ordered this tick, not filled yet
            closes = pd.DataFrame(self.daily) if self.daily else None
            d = self.risk.evaluate("ai_pick", sym, 1, price, stop, equity, held, closes, now.normalize(), cash, False)
            if not d.ok:
                continue
            qty = int(d.qty)
            oid = self.broker.market(sym, qty, 1)
            log.info("AI ENTRY long %s qty %g (sized by %s), ref price %.2f, stop %.2f: %s", sym, qty, d.reason, price, stop, pick["reason"])
            self.alerts.send(f"🤖 AI is buying **{sym}**: {pick['reason']}")
            if oid:
                self.st["pending"][sym] = dict(leg="ai_pick", side=1, qty=qty, order_id=oid, stop_dist=price - stop, submitted=str(now))
                slots -= 1

    def _ai_context(self, now, equity):
        """Hourly snapshot for the AI task (ai/context.json). Also one Discord note a day if the AI
        has gone quiet during market hours, since the bot is then trading on rules only."""
        if not self.ai.get("enabled") or self.st.get("ai_ctx_hour") == now.strftime("%Y-%m-%d %H"):
            return
        self.st["ai_ctx_hour"] = now.strftime("%Y-%m-%d %H")
        if len(self.frames) < len(self.sym_leg) // 2:
            self._load_frames(now)
        legs = {s: self.legs[l] for s, l in self.sym_leg.items()}
        setups = watch.scan(self.frames, {}, self.st["positions"], legs, max_dist=0.03)[:60]
        ai.write_context(self.root / self.ai["context_file"], now, equity, self.st["positions"], setups, self.dec)
        m = now.hour * 60 + now.minute
        if not self.dec and now.weekday() < 5 and 10 * 60 <= m <= 15 * 60 and self.st.get("ai_quiet_warned") != str(now.date()):
            self.st["ai_quiet_warned"] = str(now.date())
            self.alerts.send("🤖💤 No fresh AI decisions (is the Claude app open?). Trading on rules only until it's back.")

    # ---- Obsidian dashboard (see bot/journal.py) -------------------------------------
    _jkey, _jat = None, None

    def _journal(self, now, equity):
        """Rewrite the vault's Bot Dashboard note at once when positions, trades, vetoes or AI decisions
        change, and every 15 minutes otherwise (for equity and prices)."""
        rel = (self.cfg.get("journal") or {}).get("dashboard")
        if not rel or self.dry:
            return
        key = (tuple(sorted(self.st["positions"])), self.st["day"]["trades"], self.st["day"].get("ai_vetoes", 0),
               str(self.dec["generated_at"]) if self.dec else None)
        if key == self._jkey and self._jat is not None and now - self._jat < pd.Timedelta(minutes=15):
            return
        self._jkey, self._jat = key, now
        text = journal.render(now, self.st, equity, self.logs, self.root / (self.ai.get("decisions_file") or "ai/decisions.json"))
        journal.write((self.root / rel).resolve(), text)

    # ---- fills ---------------------------------------------------------------------
    def _sync(self, bpos, now):
        for sym, pos in list(self.st["positions"].items()):
            ex, b = pos.get("exit_order"), bpos.get(norm(sym))
            if ex:
                o = self.broker.order(ex["id"])
                if o["status"] == "filled" or (o["status"] in TERMINAL and o["filled_qty"] >= pos["qty"]):
                    self._record(sym, pos, o["price"], ex["intended"], ex["reason"], now)
                elif o["status"] in TERMINAL:
                    log.error("exit order for %s ended %s; will retry", sym, o["status"])
                    self.alerts.error(f"Exit order for {sym} was {o['status']}; retrying")
                    pos["exit_order"] = None
                    self._exit(sym, pos, ex["reason"], ex["intended"])
            elif b is None:  # gone at the broker: the server-side stop filled (or a manual close)
                fill = self.broker.last_fill(sym, -pos["side"]) or pos["stop"]
                self._record(sym, pos, fill, pos["stop"], "stop" if pos.get("stop_order") else "closed_outside_bot", now)
            elif abs(b["qty"]) != pos["qty"]:
                log.warning("%s qty differs: broker %g vs bot %g; trusting broker", sym, b["qty"], pos["qty"])
                pos["qty"] = abs(b["qty"])

        # entries filled this tick are added after the check above, so they are never mistaken for stop-outs
        for sym, p in list(self.st["pending"].items()):
            o = self.broker.order(p["order_id"])
            if o["status"] not in TERMINAL:
                continue
            del self.st["pending"][sym]
            if o["filled_qty"] <= 0:
                log.warning("entry order for %s ended %s without a fill", sym, o["status"])
                self.alerts.error(f"Entry order for {sym} was {o['status']} with no fill")
                continue
            side, fill = p["side"], o["price"]
            stop = fill - side * p["stop_dist"]
            pos = dict(leg=p["leg"], side=side, qty=o["filled_qty"], entry=fill, entry_time=str(now), stop=stop,
                       initial_stop=stop, high_water=fill, low_water=fill, stop_order=None, exit_order=None)
            pos["stop_order"] = self.broker.stop(sym, pos["qty"], -side, stop)
            self.st["positions"][sym] = pos
            self.alerts.entry(p["leg"], sym, "long" if side > 0 else "short", pos["qty"], fill, stop)
            log.info("FILLED entry %s %g @ %.2f, stop %.2f", sym, pos["qty"], fill, stop)

    def _check_stop(self, sym, pos, now, backup=False):
        """Bot-checked stop, every minute: for positions without a server-side stop, and as a backup
        for crypto stop-limits (only once the price is past the limit, where they can't fill)."""
        price = self.broker.latest_price(sym)
        level = pos["stop"] * (1 - pos["side"] * self.broker.CRYPTO_STOP_LIMIT) if backup else pos["stop"]
        if (pos["side"] > 0 and price <= level) or (pos["side"] < 0 and price >= level):
            log.info("%s hit bot-managed stop %.2f (last %.2f)", sym, pos["stop"], price)
            self._exit(sym, pos, "stop", pos["stop"])

    def _exit(self, sym, pos, reason, intended):
        if pos.get("stop_order"):
            self.broker.cancel(pos["stop_order"])  # or the stop could fire after our exit and open a short
            pos["stop_order"] = None
        oid = self.broker.market(sym, pos["qty"], -pos["side"])
        if oid:
            pos["exit_order"] = dict(id=oid, reason=reason, intended=intended)

    def _flatten_all(self, now):
        for sym, p in list(self.st["pending"].items()):
            self.broker.cancel(p["order_id"])
            del self.st["pending"][sym]
        for sym, pos in self.st["positions"].items():
            if not pos.get("exit_order"):
                self._exit(sym, pos, "kill_switch", self.broker.latest_price(sym))

    def _record(self, sym, pos, fill, intended, reason, now):
        side, qty = pos["side"], pos["qty"]
        pnl = side * qty * (fill - pos["entry"])
        row = dict(timestamp=str(now), leg=pos["leg"], symbol=sym, side="long" if side > 0 else "short", qty=qty,
                   entry=round(pos["entry"], 4), stop=round(pos["stop"], 4), exit=round(fill, 4), pnl=round(pnl, 2),
                   exit_reason=reason, slippage=round(side * (intended - fill) * qty, 2))
        append_csv(self.logs / "trades.csv", TRADE_COLS, row)
        d = self.st["day"]
        d["trades"] += 1
        d["wins" if pnl > 0 else "losses"] += 1
        if side > 0:
            self.risk.record_sale(qty * fill, now.date(), is_crypto(sym))
        del self.st["positions"][sym]
        self.alerts.exit(pos["leg"], sym, row["side"], qty, fill, pos["stop"], pnl, reason)
        log.info("CLOSED %s %s %g @ %.2f (%s) P&L %.2f, slippage %.2f", row["side"], sym, qty, fill, reason, pnl, row["slippage"])

    # ---- daily summary -------------------------------------------------------------
    def _roll_day(self, day, equity):
        d = self.st.get("day")
        if d and d["date"] != str(day) and not self.dry:  # dry runs never write the logs
            end = d.get("end_equity", equity)
            append_csv(self.logs / "daily_summary.csv", SUMMARY_COLS, dict(
                date=d["date"], start_equity=round(d["start_equity"], 2), end_equity=round(end, 2),
                pnl=round(end - d["start_equity"], 2), trades=d["trades"], wins=d["wins"], losses=d["losses"],
                blocked_trades=self.risk.blocked_trades, kill_switch_triggered=self.risk.kill_switch_triggered))
        if not d or d["date"] != str(day):
            self.st["day"] = dict(date=str(day), start_equity=equity, end_equity=equity, trades=0, wins=0, losses=0)

    # ---- run forever ---------------------------------------------------------------
    def run(self):
        legs = ", ".join(self.legs)
        log.info("bot starting (%s), legs: %s", "DRY RUN" if self.dry else "paper", legs)
        self.alerts.send(f"🟢 **Bot started** ({'DRY RUN' if self.dry else 'paper'}), legs: {legs}")
        self._tick_started = None
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.reconcile()
        fails = 0
        while True:
            try:
                self._tick_started = time.monotonic()
                self.tick()
                self._tick_started = None
                if fails:
                    self.alerts.send(f"✅ Bot recovered after {fails} failed checks")
                fails = 0
            except Exception as e:
                fails += 1
                log.exception("tick failed (%d in a row)", fails)
                if fails == 1 or fails % 30 == 0:  # don't spam: first failure, then every ~30 min
                    self.alerts.error(f"{type(e).__name__}: {e} ({fails} failed checks in a row, still retrying)")
            time.sleep(60 - time.time() % 60 + 2)


    WATCHDOG_SECS = 600

    def _watchdog(self):
        """If one check runs > 10 minutes of awake time (monotonic time pauses while the Mac
        sleeps), something is hung: alert and exit non-zero so auto-start restarts the bot."""
        while True:
            time.sleep(30)
            started = self._tick_started
            if started is not None and time.monotonic() - started > self.WATCHDOG_SECS:
                log.error("check stuck for over %d s; restarting", self.WATCHDOG_SECS)
                self.alerts.error(f"A check was stuck for over {self.WATCHDOG_SECS // 60} minutes; restarting the bot")
                os._exit(3)


def setup_logging(root=ROOT):
    from logging.handlers import RotatingFileHandler
    (root / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [RotatingFileHandler(root / "logs" / "bot.log", maxBytes=5_000_000, backupCount=5)]
    if sys.stderr.isatty():  # echo to the terminal when run by hand, not under auto-start
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for noisy in ("urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
