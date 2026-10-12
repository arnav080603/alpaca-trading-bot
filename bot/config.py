import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=ROOT / "config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for leg in cfg["legs"].values():  # long symbol lists can live in a file (one per line, # comments)
        if leg.get("symbols_file"):
            lines = (ROOT / leg["symbols_file"]).read_text().splitlines()
            leg["symbols"] = [w for line in lines if not line.startswith("#") for w in line.split()]
    return cfg


def load_env():
    """Secrets come only from .env (never from code or config.yaml)."""
    load_dotenv(ROOT / ".env")
    return {k: os.getenv(k) or None for k in
            ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER", "DISCORD_WEBHOOK_URL")}


def shorts_allowed(cfg):
    a = cfg["account"]
    return bool(a["allow_shorts"]) and not a["cash_account_mode"]
