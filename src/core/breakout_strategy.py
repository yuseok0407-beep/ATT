import pandas as pd

from src.core.indicators import adx

VALID_SIDES = ("long", "short")


def detect_signal(
    df: pd.DataFrame,
    channel_period: int = 20,
    adx_threshold: float | None = None,
) -> str | None:
    """Donchian 채널 브레이크아웃(터틀 트레이딩 스타일) 신호. 직전 channel_period개 봉의
    최고가/최저가를 "채널"로 보고, 종가가 그 채널을 막 벗어나는 "순간"만 신호로 잡는다(추세추종의
    SMA 크로스와 동일한 이유로 크로스만 — 채널 밖에 계속 머무는 동안 매 주기 중복 신호가 나는 걸
    막기 위함).

    - 종가가 직전 channel_period봉 최고가를 상향 돌파 -> LONG
    - 종가가 직전 channel_period봉 최저가를 하향 돌파 -> SHORT
    - adx_threshold가 주어지면 ADX가 그 이상일 때만 신호를 낸다(상위 추세 확인). None(기본값)이면
      필터 없이 브레이크아웃 자체만으로 판단 — 브레이크아웃 초입엔 ADX가 아직 안 따라온 경우가
      많아, 필터 유무 효과를 백테스트로 비교하기 위한 옵션이다.

    df는 아직 마감되지 않은 마지막 봉을 포함해도 되고 제외해도 된다. 지표 warm-up을 위해
    최소 channel_period+5개 이상의 봉이 필요하다.
    """
    if len(df) < channel_period + 5:
        return None

    close = df["close"]
    highest = df["high"].rolling(window=channel_period).max().shift(1)
    lowest = df["low"].rolling(window=channel_period).min().shift(1)

    if adx_threshold is not None:
        latest_adx = adx(df, 14)["adx"].iloc[-1]
        if pd.isna(latest_adx) or latest_adx < adx_threshold:
            return None

    prev_high_diff = close.iloc[-2] - highest.iloc[-2]
    curr_high_diff = close.iloc[-1] - highest.iloc[-1]
    prev_low_diff = close.iloc[-2] - lowest.iloc[-2]
    curr_low_diff = close.iloc[-1] - lowest.iloc[-1]
    if pd.isna(curr_high_diff) or pd.isna(curr_low_diff):
        return None

    bullish_break = prev_high_diff <= 0 and curr_high_diff > 0
    bearish_break = prev_low_diff >= 0 and curr_low_diff < 0

    if bullish_break:
        return "LONG"
    if bearish_break:
        return "SHORT"
    return None
