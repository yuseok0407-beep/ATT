import pandas as pd

from src.core.indicators import atr, rsi, sma, volatility
from src.core.regime import classify_regime
from src.data.exchange import fetch_ohlcv_df, get_market_data_client, get_ticker


def _safe_round(value, decimals):
    if pd.isna(value):
        return None
    return round(float(value), decimals)


def build_snapshot(symbol: str, timeframe: str = "1h") -> dict:
    """시세/지표는 공개 시장 데이터 클라이언트로 조회한다 (테스트넷 히스토리 부족 문제 회피)."""
    market_client = get_market_data_client()
    df = fetch_ohlcv_df(market_client, symbol, timeframe=timeframe, limit=100)
    close = df["close"]

    ticker = get_ticker(market_client, symbol)
    regime = classify_regime(df)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": ticker["last"],
        "rsi_14": _safe_round(rsi(close, 14).iloc[-1], 2),
        "sma_20": _safe_round(sma(close, 20).iloc[-1], 2),
        "sma_50": _safe_round(sma(close, 50).iloc[-1], 2),
        "atr_14": _safe_round(atr(df, 14).iloc[-1], 2),
        "volatility_20": _safe_round(volatility(close, 20).iloc[-1], 4),
        "regime": regime,
        "last_updated": df["timestamp"].iloc[-1].isoformat(),
    }
