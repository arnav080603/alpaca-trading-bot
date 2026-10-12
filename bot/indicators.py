"""Causal indicators: every value at bar t uses only bars <= t."""
import pandas as pd


def sma(s, n):
    return s.rolling(n).mean()


def stdev(s, n):
    return s.rolling(n).std(ddof=0)


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df, n):
    """Wilder's ATR."""
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
