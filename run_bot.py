"""Start the trading bot.

  .venv/bin/python run_bot.py            # paper trading, runs until stopped
  .venv/bin/python run_bot.py --dry-run  # decides and logs, places no orders, sends no Discord pings
  .venv/bin/python run_bot.py --once     # one check, then exit (handy for testing)

Stop it safely by creating a file named STOP in this folder (it flattens everything and halts);
delete STOP to let it trade again.
"""
import argparse
import logging
import signal
import sys

from bot.alerts import Discord
from bot.config import load_config, load_env
from bot.live import Bot, setup_logging


def _stop(*_):
    raise KeyboardInterrupt  # shutdown/auto-start stop -> same clean exit (and Discord ping) as Ctrl+C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    setup_logging()
    signal.signal(signal.SIGTERM, _stop)
    cfg, env = load_config(), load_env()
    alerts = Discord(None if args.dry_run else env.get("DISCORD_WEBHOOK_URL"), mention=cfg.get("alerts", {}).get("mention"))
    try:
        bot = Bot(cfg, env, alerts=alerts, dry_run=args.dry_run)
        if args.once:
            bot.reconcile()
            bot.tick()
            return
        bot.run()
    except KeyboardInterrupt:
        logging.getLogger("bot").info("stopped by user")
        alerts.send("⏹️ Bot stopped by user")
    except Exception as e:
        logging.getLogger("bot").exception("bot crashed")
        alerts.error(f"**Bot crashed:** {type(e).__name__}: {e}. Auto-start will restart it.")
        sys.exit(1)


if __name__ == "__main__":
    main()
