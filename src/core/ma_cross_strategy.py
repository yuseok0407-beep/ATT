import pandas as pd

from src.core.indicators import adx, ema, sma

VALID_SIDES = ("long", "short")


def detect_signal(
    df: pd.DataFrame,
    fast_period: int = 20,
    slow_period: int = 50,
    use_ema: bool = False,
    adx_threshold: float | None = None,
) -> str | None:
    """골든크로스/데드크로스(이동평균선 두 개의 교차) 신호. 지금 실거래 봇의 진입 로직(가격이
    이동평균선 하나를 돌파)과 달리, 여기서는 빠른 이동평균선과 느린 이동평균선 "둘"의 관계를 본다.

    - 빠른선이 느린선을 상향 돌파(골든크로스) -> LONG
    - 빠른선이 느린선을 하향 돌파(데드크로스) -> SHORT
    - use_ema=True면 지수이동평균(EMA), False(기본)면 단순이동평균(SMA)을 쓴다. EMA는 최근 가격에
      가중치를 더 둬서 SMA보다 교차가 더 빨리(하지만 더 노이즈에 민감하게) 발생하는 경향이 있다.
    - adx_threshold가 주어지면 ADX가 그 이상일 때만 신호를 낸다(상위 추세 확인). None(기본값)이면
      필터 없음.

    주의: slow_period는 백테스트/실거래 봇이 신호 계산에 쓰는 캔들 개수(100개)보다 충분히 작아야
    한다 — 그래야 워밍업 구간을 제외하고도 크로스 판정에 쓸 유효한 구간이 남는다. df는 아직
    마감되지 않은 마지막 봉을 포함해도 되고 제외해도 된다.
    """
    if len(df) < slow_period + 5:
        return None

    close = df["close"]
    ma_fn = ema if use_ema else sma
    fast_line = ma_fn(close, fast_period)
    slow_line = ma_fn(close, slow_period)

    if adx_threshold is not None:
        latest_adx = adx(df, 14)["adx"].iloc[-1]
        if pd.isna(latest_adx) or latest_adx < adx_threshold:
            return None

    prev_diff = fast_line.iloc[-2] - slow_line.iloc[-2]
    curr_diff = fast_line.iloc[-1] - slow_line.iloc[-1]
    if pd.isna(prev_diff) or pd.isna(curr_diff):
        return None

    golden_cross = prev_diff <= 0 and curr_diff > 0
    dead_cross = prev_diff >= 0 and curr_diff < 0

    if golden_cross:
        return "LONG"
    if dead_cross:
        return "SHORT"
    return None
