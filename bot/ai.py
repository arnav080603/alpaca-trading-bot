"""AI layer (added 2026-10-10 at Arnav's request; replaces the spec's "no LLM in trade decisions" rule).

A Claude scheduled task runs every hour on weekdays. It reads ai/context.json (written here by the bot),
looks at market data (Co-Invest market analysis, Coinversa positioning, Congress and insider trades),
and writes ai/decisions.json:

  {"generated_at": "2026-10-12T09:05:00-04:00",
   "vetoes": {"MSFT": "why the rule trade should be skipped"},
   "picks":  [{"symbol": "NVDA", "reason": "why to buy"}],
   "exits":  {"NVDA": "why to sell an AI pick early"}}

The AI can only (1) veto entries the rules want to make, (2) pick stocks from the bot's universe to buy
(long only, at most ai.max_picks held) and (3) close its own picks. The risk engine still sizes every
trade and every position gets a server-side stop. A missing, stale or broken file means no AI: the
bot trades on rules only, exactly as before.
"""
import json
import logging

import pandas as pd

log = logging.getLogger(__name__)
VETO_COLS = ["timestamp", "leg", "symbol", "price", "stop", "reason"]


def load(path, now, max_age_minutes):
    """The decisions file as {generated_at, vetoes, picks, exits}, or None if missing, stale or broken."""
    try:
        d = json.loads(path.read_text())
        at = pd.Timestamp(d["generated_at"])
        if at.tzinfo is None:
            raise ValueError("generated_at has no timezone")
        if not (pd.Timedelta(0) <= now - at <= pd.Timedelta(minutes=max_age_minutes)):
            return None
        sym = lambda s: str(s).strip().upper()
        return dict(generated_at=at,
                    vetoes={sym(k): str(v) for k, v in (d.get("vetoes") or {}).items()},
                    picks=[dict(symbol=sym(p["symbol"]), reason=str(p.get("reason", ""))) for p in d.get("picks") or []],
                    exits={sym(k): str(v) for k, v in (d.get("exits") or {}).items()})
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("ignoring broken AI decisions file %s: %s", path, e)
        return None


def write_context(path, now, equity, positions, setups, decisions):
    """What the AI task needs to decide: account, open positions and the rule setups near triggering."""
    path.parent.mkdir(exist_ok=True)
    ctx = dict(
        written_at=str(now), equity=round(equity, 2),
        positions=[dict(symbol=s, leg=p["leg"], qty=p["qty"], entry=round(p["entry"], 2), stop=round(p["stop"], 2),
                        entry_time=p["entry_time"]) for s, p in positions.items()],
        rule_setups=[dict(symbol=s.symbol, leg=s.leg, action=s.action, price=round(s.price, 2),
                          trigger=round(s.trigger, 2), move_needed=f"{s.dist:+.1%}") for s in setups],
        ai_last_decisions_at=str(decisions["generated_at"]) if decisions else None)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ctx, indent=1))
    tmp.replace(path)
