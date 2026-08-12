import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.where(avg_loss != 0, 100)
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return result


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    middle = sma(close, period)
    std = close.rolling(window=period).std()
    return pd.DataFrame({
        "upper": middle + num_std * std,
        "middle": middle,
        "lower": middle - num_std * std,
    })


def volatility(close: pd.Series, period: int = 20) -> pd.Series:
    returns = close.pct_change()
    return returns.rolling(window=period).std() * np.sqrt(period)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index + directional indicators (+DI/-DI).
    ADX > 25 conventionally signals a trending market; direction comes from +DI vs -DI."""
    high, low = df["high"], df["low"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr_smooth = atr(df, period) * period  # Wilder's smoothed TR (undo the ewm mean back to a sum-like scale)
    plus_dm_smooth = plus_dm.ewm(alpha=1 / period, min_periods=period).mean() * period
    minus_dm_smooth = minus_dm.ewm(alpha=1 / period, min_periods=period).mean() * period

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_line = dx.ewm(alpha=1 / period, min_periods=period).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line})
