"""Watch alerts: a few times a day, which setups are closest to triggering, the exact closing price
that would trigger each one, why that matters, and an animated chart of the top setup.

Deterministic: trigger prices are solved from the same indicators the strategies trade on, and the
text comes from fixed templates (no AI). Watch alerts never place orders.
"""
import io
from dataclasses import dataclass

import pandas as pd
from PIL import Image, ImageDraw, ImageFont


@dataclass
class Setup:
    symbol: str
    leg: str
    action: str     # "buy" or "sell"
    price: float    # current price
    trigger: float  # the close that would fire the signal
    dist: float     # move needed to reach the trigger (0.012 = 1.2%; <= 0 means already past it)
    why: str
    frame: object   # prepared bars, for the chart
    labels: tuple = ("fast avg", "slow avg")  # chart legend for the two indicator lines


def ema_cross_price(fast, slow, n_fast, n_slow):
    """The close at which tomorrow's fast EMA equals tomorrow's slow EMA:
    a1*p + (1-a1)*fast = a2*p + (1-a2)*slow  ->  p = ((1-a2)*slow - (1-a1)*fast) / (a1-a2)."""
    a1, a2 = 2 / (n_fast + 1), 2 / (n_slow + 1)
    return ((1 - a2) * slow - (1 - a1) * fast) / (a1 - a2)


def _money(x):
    return f"${x:,.2f}"


def scan(frames, prices, positions, legs, max_dist=0.05):
    """All setups within `max_dist` of triggering. frames/prices: symbol -> prepared bars / live price;
    positions: the bot's open positions; legs: symbol -> Leg."""
    out = []
    for sym, df in frames.items():
        leg = legs.get(sym)
        if leg is None or df is None or df.empty:
            continue
        price, row, pos = prices.get(sym, float(df["close"].iloc[-1])), df.iloc[-1], positions.get(sym)
        p = leg.p
        if "ema_f" in df:  # EMA-cross trend legs (stocks)
            f, s = row["ema_f"], row["ema_s"]
            lab = (f"{p['ema_fast']}-day avg", f"{p['ema_slow']}-day avg")
            trig = ema_cross_price(f, s, p["ema_fast"], p["ema_slow"])
            if pos is None and f < s and trig > 0:
                out.append(Setup(sym, leg.name, "buy", price, trig, trig / price - 1,
                                 f"Its {p['ema_fast']}-day average is {1 - f / s:.2%} under its {p['ema_slow']}-day average and closing in. "
                                 f"A close above **{_money(trig)}** makes the short-term trend cross above the long-term one "
                                 f"(a *golden cross*): the bot buys at the next open.", df, lab))
            elif pos is not None and pos["side"] > 0 and f > s:
                stop = pos["stop"]
                if stop >= trig:
                    out.append(Setup(sym, leg.name, "sell", price, stop, 1 - stop / price,
                                     f"Held since {_money(pos['entry'])}. Its stop is at **{_money(stop)}**: a drop there sells it "
                                     f"to cap the loss.", df, lab))
                else:
                    out.append(Setup(sym, leg.name, "sell", price, trig, 1 - trig / price,
                                     f"Held since {_money(pos['entry'])}. A close below **{_money(trig)}** makes the {p['ema_fast']}-day average "
                                     f"dip under the {p['ema_slow']}-day (a *death cross*): the uptrend is over, the bot sells at the next open.", df, lab))
        elif "hh" in df:  # BTC breakout
            lab = (f"{p['ema_period']}-day avg", f"{p['breakout_lookback']}-day high")
            if pos is None:
                hh_next = df["high"].tail(p["breakout_lookback"]).max()
                trig = max(hh_next, row["ema"])
                out.append(Setup(sym, leg.name, "buy", price, trig, trig / price - 1,
                                 f"A daily close above **{_money(trig)}** is a new {p['breakout_lookback']}-day high while above the "
                                 f"{p['ema_period']}-day average: a *breakout*, the bot's buy signal.", df, lab))
            else:
                ll_next = df["low"].tail(p["exit_low_lookback"]).min()
                trig = max(ll_next, pos["stop"])
                out.append(Setup(sym, leg.name, "sell", price, trig, 1 - trig / price,
                                 f"Held since {_money(pos['entry'])}. Under **{_money(trig)}** it sells (trailing stop "
                                 f"{_money(pos['stop'])} / {p['exit_low_lookback']}-day low {_money(ll_next)}).", df, lab))
    return sorted([s for s in out if s.dist <= max_dist], key=lambda s: s.dist)


def message(setups, title, top=5):
    lines = [f"👀 **{title}**: closest to triggering"]
    for i, s in enumerate(setups[:top], 1):
        need = "already past it: fires at the next close if it holds" if s.dist <= 0 else \
            f"needs {'+' if s.action == 'buy' else '-'}{s.dist:.1%}"
        lines.append(f"**{i}. {s.symbol}** {'🟢 could BUY' if s.action == 'buy' else '🔴 could SELL'} · now {_money(s.price)} · {need}\n   {s.why}")
    lines.append("_Why watch: trends tend to keep going (momentum). The bot only acts on the **close**, so a move "
                 "during the day can still reverse. Chart: the top setup._")
    return "\n".join(lines)


# ---- animated chart ---------------------------------------------------------------------
BG, GRID, TEXT, MUTED = (30, 31, 34), (55, 57, 62), (235, 236, 238), (150, 152, 158)
PRICE, FAST, SLOW, BAND = (235, 236, 238), (250, 166, 26), (88, 101, 242), (120, 124, 132)
GREEN, RED = (87, 242, 135), (237, 66, 69)
W, H, L, R, T, B = 720, 405, 70, 20, 56, 64


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def gif(setup, bars=120, reveal_frames=24):
    """Animated GIF: the price and its averages draw in, then the trigger line and what it means."""
    df = setup.frame.tail(bars)
    if "ema_f" in df:
        series = [("price", df["close"], PRICE), (setup.labels[0], df["ema_f"], FAST), (setup.labels[1], df["ema_s"], SLOW)]
    else:
        series = [("price", df["close"], PRICE), (setup.labels[0], df["ema"], SLOW), (setup.labels[1], df["hh"], BAND)]
    lo = min(min(s.min() for _, s, _ in series), setup.trigger, setup.price)
    hi = max(max(s.max() for _, s, _ in series), setup.trigger, setup.price)
    pad = (hi - lo) * 0.08 or 1
    lo, hi, n = lo - pad, hi + pad, len(df)

    def xy(i, v):
        return L + (W - L - R) * i / max(1, n - 1), T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    f_title, f_txt, f_small = _font(20), _font(15), _font(13)
    color = GREEN if setup.action == "buy" else RED

    def base():
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            y = xy(0, v)[1]
            d.line([(L, y), (W - R, y)], fill=GRID)
            d.text((6, y - 8), f"{v:,.0f}" if v >= 100 else f"{v:,.2f}", fill=MUTED, font=f_small)
        d.text((L, 10), f"{setup.symbol} · {'BUY' if setup.action == 'buy' else 'SELL'} watch", fill=TEXT, font=f_title)
        x = W - R
        for name, _, c in reversed(series):
            w = d.textlength(name, font=f_small) + 26
            x -= w
            d.rectangle([x, 18, x + 12, 28], fill=c)
            d.text((x + 16, 15), name, fill=MUTED, font=f_small)
        return im, d

    frames, durations = [], []
    for k in range(1, reveal_frames + 1):
        im, d = base()
        upto = max(2, int(n * k / reveal_frames))
        for _, s, c in series:
            pts = [xy(i, v) for i, v in enumerate(s.iloc[:upto]) if pd.notna(v)]
            if len(pts) > 1:
                d.line(pts, fill=c, width=3 if c == PRICE else 2)
        frames.append(im)
        durations.append(70)
    # final frame: trigger line + caption
    im, d = base()
    for _, s, c in series:
        pts = [xy(i, v) for i, v in enumerate(s) if pd.notna(v)]
        d.line(pts, fill=c, width=3 if c == PRICE else 2)
    y = xy(0, setup.trigger)[1]
    for x0 in range(L, W - R, 14):
        d.line([(x0, y), (min(x0 + 8, W - R), y)], fill=color, width=2)
    d.text((L + 6, y - 20 if y > T + 24 else y + 6), f"trigger {setup.trigger:,.2f}", fill=color, font=f_txt)
    px, py = xy(n - 1, setup.price)
    d.ellipse([px - 5, py - 5, px + 5, py + 5], fill=color)
    verb = "closes above" if setup.action == "buy" else "closes below"
    gap = "already past it now" if setup.dist <= 0 else f"{'+' if setup.action == 'buy' else '-'}{setup.dist:.1%} from now"
    when = "right after the daily close" if setup.leg == "btc_breakout" else "at the next open"
    d.text((L, H - B + 14), f"If it {verb} {setup.trigger:,.2f} ({gap}) -> bot {'BUYS' if setup.action == 'buy' else 'SELLS'} {when}",
           fill=TEXT, font=f_txt)
    d.text((L, H - B + 36), "Lines: price and its averages over the last ~6 months. Dashed: the trigger price.", fill=MUTED, font=f_small)
    frames.append(im)
    durations.append(4000)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return buf.getvalue()
