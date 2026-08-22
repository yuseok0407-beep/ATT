import pandas as pd

from src.core.indicators import adx, rsi

VALID_SIDES = ("long", "short")


def detect_signal(
    df: pd.DataFrame,
    lookback: int = 14,
    rsi_period: int = 14,
    adx_threshold: float | None = 25.0,
) -> str | None:
    """RSI 다이버전스(가격과 모멘텀이 반대로 움직이는 것) 신호. 지금 봉이 최근 lookback봉 안에서
    신저가/신고가를 갱신했는데, 그 구간의 직전 신저가/신고가 시점보다 RSI가 오히려 개선돼 있으면
    (가격은 더 밀렸는데 모멘텀은 덜 밀렸으면/그 반대면) 추세 소진으로 보고 반전 신호를 낸다.

    지금 실거래 봇의 RSI 사용법(SMA 크로스 방향과 RSI 임계값이 같은 방향인지 "재확인"만 함)과는
    다른 종류의 신호다 — 여기서는 가격의 극값과 RSI의 극값을 직접 비교한다(2026-08-22, 문헌조사로
    새로 추가한 후보).

    엄밀한 프랙탈/피벗 확인(향후 몇 봉이 더 지나야 그 지점이 진짜 극값이었는지 확정됨) 대신, "지금
    봉이 창 안에서 극값을 찍었는지"와 "그 직전 극값 시점의 RSI와 비교"만 보는 단순화된 방식이라 —
    향후 데이터를 전혀 안 봐도(lookahead 없이) 매 봉 계산 가능하다는 게 장점이다.

    - 강세 다이버전스: 종가가 lookback봉 신저가 -> LONG (직전 신저가 시점보다 RSI가 더 높을 때만)
    - 약세 다이버전스: 종가가 lookback봉 신고가 -> SHORT (직전 신고가 시점보다 RSI가 더 낮을 때만)
    - adx_threshold가 주어지면(기본 25) ADX가 그 아래일 때만 신호를 낸다 — 다이버전스는 추세
      소진/반전 신호라 강한 추세장에서는 오탐이 많다는 게 평균회귀 계열과 같은 이유(
      reversion_strategy.py 참고). None이면 필터 없이 모든 구간에서 신호를 낸다.

    df는 아직 마감되지 않은 마지막 봉을 포함해도 되고 제외해도 된다. 지표 warm-up을 위해
    최소 lookback + rsi_period + 5개 이상의 봉이 필요하다.
    """
    if len(df) < lookback + rsi_period + 5:
        return None

    close = df["close"]
    rsi_series = rsi(close, rsi_period)

    window_close = close.iloc[-lookback:]
    window_rsi = rsi_series.iloc[-lookback:]
    if window_rsi.isna().any():
        return None

    if adx_threshold is not None:
        latest_adx = adx(df, 14)["adx"].iloc[-1]
        if pd.isna(latest_adx) or latest_adx >= adx_threshold:
            return None

    current_close = window_close.iloc[-1]
    current_rsi = window_rsi.iloc[-1]
    prior_close = window_close.iloc[:-1]
    prior_rsi = window_rsi.iloc[:-1]

    is_new_low = current_close <= window_close.min()
    is_new_high = current_close >= window_close.max()

    if is_new_low:
        prior_low_idx = prior_close.idxmin()
        if current_rsi > prior_rsi.loc[prior_low_idx]:
            return "LONG"
    if is_new_high:
        prior_high_idx = prior_close.idxmax()
        if current_rsi < prior_rsi.loc[prior_high_idx]:
            return "SHORT"
    return None
