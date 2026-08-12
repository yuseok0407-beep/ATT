import pandas as pd

from src.core.indicators import adx, volatility

ADX_TREND_THRESHOLD = 25
HIGH_VOLATILITY_THRESHOLD = 0.05  # 20기간 수익률 표준편차 기준, 심하게 출렁이는 구간


def _safe_round(value, decimals):
    if pd.isna(value):
        return None
    return round(float(value), decimals)


def classify_regime(df: pd.DataFrame, period: int = 14) -> dict:
    """ADX(추세 강도) + DI(방향성) + 변동성으로 시장 레짐을 분류한다.

    우선순위: 변동성이 임계치를 넘으면 HIGH_VOLATILITY(추세 여부와 무관하게 리스크 우선 표시),
    그 다음 ADX >= 25면 추세장(+DI/-DI로 방향 결정), 그 외엔 RANGING(횡보).
    """
    adx_df = adx(df, period)
    latest = adx_df.iloc[-1]
    vol = volatility(df["close"], 20).iloc[-1]

    adx_value, plus_di, minus_di = latest["adx"], latest["plus_di"], latest["minus_di"]

    if pd.notna(vol) and vol > HIGH_VOLATILITY_THRESHOLD:
        label = "HIGH_VOLATILITY"
    elif pd.notna(adx_value) and adx_value >= ADX_TREND_THRESHOLD:
        label = "TRENDING_UP" if plus_di > minus_di else "TRENDING_DOWN"
    else:
        label = "RANGING"

    return {
        "label": label,
        "adx": _safe_round(adx_value, 2),
        "plus_di": _safe_round(plus_di, 2),
        "minus_di": _safe_round(minus_di, 2),
        "volatility_20": _safe_round(vol, 4),
    }
