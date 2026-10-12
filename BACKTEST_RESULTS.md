# Backtest results — 2026-10-09 23:42

## stock_trend
Data 2021-01-04 → 2026-10-09, in-sample/out-of-sample split at 2025-01-15.
Parameters chosen in-sample: {'ema_fast': 10, 'ema_slow': 100, 'stop_atr': 2.0}
Changed from config defaults: none
Graduation gate 1 (OOS profit factor > 1.2): PASS · gate 2 (long-only profitable OOS): PASS

| leg | sample | mode | trades | win_rate | profit_factor | max_drawdown | sharpe | avg_trade | total_return | buy_hold | blocked |
|---|---|---|---|---|---|---|---|---|---|---|---|
| stock_trend | in_sample | long_only | 689 | 28.9% | 2.08 | -20.7% | 0.90 | $7.40 | 51.0% | 71.3% | 3751 |
| stock_trend | in_sample | both_sides | 900 | 24.1% | 1.09 | -14.2% | 0.22 | $0.67 | 6.1% | 71.3% | 8190 |
| stock_trend | out_of_sample | long_only | 417 | 27.3% | 2.14 | -11.2% | 1.09 | $8.83 | 36.8% | 39.5% | 1741 |
| stock_trend | out_of_sample | both_sides | 475 | 25.3% | 1.23 | -11.4% | 0.49 | $1.89 | 9.0% | 39.5% | 3864 |

## btc_breakout
Data 2020-12-31 → 2026-10-09, in-sample/out-of-sample split at 2025-01-15.
Parameters chosen in-sample: {'breakout_lookback': 20, 'trail_atr': 1.5}
Changed from config defaults: trail_atr 2.0 → 1.5
Graduation gate 1 (OOS profit factor > 1.2): FAIL · gate 2 (long-only profitable OOS): FAIL

| leg | sample | mode | trades | win_rate | profit_factor | max_drawdown | sharpe | avg_trade | total_return | buy_hold | blocked |
|---|---|---|---|---|---|---|---|---|---|---|---|
| btc_breakout | in_sample | long_only | 37 | 37.8% | 1.20 | -0.8% | 0.21 | $1.27 | 0.5% | 241.3% | 0 |
| btc_breakout | out_of_sample | long_only | 16 | 18.8% | 0.45 | -0.7% | -0.95 | $-3.39 | -0.5% | -17.5% | 0 |

## All legs
| leg | sample | mode | trades | win_rate | profit_factor | max_drawdown | sharpe | avg_trade | total_return | buy_hold | blocked |
|---|---|---|---|---|---|---|---|---|---|---|---|
| stock_trend | in_sample | long_only | 689 | 28.9% | 2.08 | -20.7% | 0.90 | $7.40 | 51.0% | 71.3% | 3751 |
| stock_trend | in_sample | both_sides | 900 | 24.1% | 1.09 | -14.2% | 0.22 | $0.67 | 6.1% | 71.3% | 8190 |
| stock_trend | out_of_sample | long_only | 417 | 27.3% | 2.14 | -11.2% | 1.09 | $8.83 | 36.8% | 39.5% | 1741 |
| stock_trend | out_of_sample | both_sides | 475 | 25.3% | 1.23 | -11.4% | 0.49 | $1.89 | 9.0% | 39.5% | 3864 |
| btc_breakout | in_sample | long_only | 37 | 37.8% | 1.20 | -0.8% | 0.21 | $1.27 | 0.5% | 241.3% | 0 |
| btc_breakout | out_of_sample | long_only | 16 | 18.8% | 0.45 | -0.7% | -0.95 | $-3.39 | -0.5% | -17.5% | 0 |
