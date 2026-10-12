"""Discord alerts via webhook: one message per trade entry and exit, plus kill-switch triggers,
blocked trades and errors. A failed alert is logged and never stops the bot."""
import json
import logging
import time

import requests

log = logging.getLogger(__name__)


class Discord:
    def __init__(self, webhook_url, retries=3, backoff=2.0, post=requests.post, mention=None):
        """`mention` ("@everyone" or "<@user id>") is put in front of every message so Discord
        notifies the phone even with default notification settings."""
        self.url, self.retries, self.backoff, self._post, self.mention = webhook_url, retries, backoff, post, mention

    def send(self, text, file=None):
        """`file` = (filename, bytes) attaches an image (e.g. an animated GIF) to the message."""
        if not self.url:
            log.info("discord (no webhook set): %s", text)
            return False
        body = {"content": (f"{self.mention} {text}" if self.mention else text)[:2000],
                "allowed_mentions": {"parse": ["everyone", "users"]}}
        for attempt in range(self.retries):
            try:
                if file:
                    r = self._post(self.url, data={"payload_json": json.dumps(body)},
                                   files={"files[0]": (file[0], file[1], "image/gif")}, timeout=30)
                else:
                    r = self._post(self.url, json=body, timeout=10)
                if r.status_code == 429:  # rate limited: Discord says how long to wait
                    time.sleep(float(r.json().get("retry_after", self.backoff)))
                    continue
                if r.status_code < 300:
                    return True
                log.warning("discord HTTP %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.warning("discord send failed: %s", e)
            time.sleep(self.backoff * 2 ** attempt)
        log.error("discord alert dropped after %d tries: %s", self.retries, text)
        return False

    def entry(self, leg, symbol, side, qty, price, stop):
        arrow = "🟢 BUY" if side == "long" else "🔴 SHORT"
        return self.send(f"{arrow} **{symbol}** ({leg})\nqty {qty:g} @ ${price:,.2f} · stop ${stop:,.2f}")

    def exit(self, leg, symbol, side, qty, price, stop, pnl, reason):
        mark = "✅" if pnl > 0 else "❌"
        return self.send(f"{mark} CLOSED {side} **{symbol}** ({leg}) — {reason}\n"
                         f"qty {qty:g} @ ${price:,.2f} · stop was ${stop:,.2f} · P&L **{'-' if pnl < 0 else '+'}${abs(pnl):,.2f}**")

    def kill_switch(self, reason, equity):
        return self.send(f"🛑 **KILL SWITCH** — {reason}\nAll positions flattened, trading halted. Equity ${equity:,.2f}")

    def blocked(self, leg, symbol, side, reason):
        return self.send(f"⛔ Blocked {side} **{symbol}** ({leg}): {reason}")

    def error(self, text):
        return self.send(f"⚠️ **Bot error:** {text}")
