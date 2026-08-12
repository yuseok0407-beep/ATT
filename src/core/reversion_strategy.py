import pandas as pd

from src.core.indicators import adx, bollinger_bands, rsi

VALID_SIDES = ("long", "short")


def detect_signal(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    adx_threshold: float | None = 25.0,
) -> str | None:
    """볼린저밴드 평균회귀 신호. df의 마지막 두 봉을 비교해 종가가 밴드를 막 뚫고 나가는 "순간"만
    신호로 잡는다(추세추종의 SMA 크로스 로직과 동일한 이유 — 밴드 밖에 계속 머무는 동안 매 주기
    중복 신호가 나는 걸 막기 위함).

    - 종가가 하단밴드를 하향 돌파 + RSI가 과매도(<=oversold) -> LONG (반등 기대)
    - 종가가 상단밴드를 상향 돌파 + RSI가 과매수(>=overbought) -> SHORT (되돌림 기대)
    - adx_threshold가 주어지면(기본 25) ADX가 그 아래일 때만 신호를 낸다 — 평균회귀는 추세장에서
      "밴드를 따라 걷는(band walk)" 현상으로 손절만 반복해서 맞기 쉬우므로, 횡보 레짐으로 한정하는
      게 기본 동작이다. None으로 주면 레짐 필터 없이(모든 구간에서) 신호를 낸다 — 필터 효과를
      백테스트로 비교하기 위한 옵션.

    df는 아직 마감되지 않은 마지막 봉을 포함해도 되고 제외해도 된다. 지표 warm-up을 위해
    최소 35개 이상의 봉이 필요하다.
    """
    if len(df) < 35:
        return None

    close = df["close"]
    bands = bollinger_bands(close, bb_period, bb_std)

    if adx_threshold is not None:
        latest_adx = adx(df, 14)["adx"].iloc[-1]
        if pd.isna(latest_adx) or latest_adx >= adx_threshold:
            return None

    prev_lower_diff = close.iloc[-2] - bands["lower"].iloc[-2]
    curr_lower_diff = close.iloc[-1] - bands["lower"].iloc[-1]
    prev_upper_diff = close.iloc[-2] - bands["upper"].iloc[-2]
    curr_upper_diff = close.iloc[-1] - bands["upper"].iloc[-1]
    if pd.isna(curr_lower_diff) or pd.isna(curr_upper_diff):
        return None

    bearish_breach = prev_lower_diff >= 0 and curr_lower_diff < 0  # 하단밴드 하향 돌파
    bullish_breach = prev_upper_diff <= 0 and curr_upper_diff > 0  # 상단밴드 상향 돌파

    latest_rsi = rsi(close, rsi_period).iloc[-1]
    if pd.isna(latest_rsi):
        return None

    if bearish_breach and latest_rsi <= oversold:
        return "LONG"
    if bullish_breach and latest_rsi >= overbought:
        return "SHORT"
    return None
