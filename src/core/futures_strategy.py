import pandas as pd

from src.core.config import RULE_ADX_THRESHOLD, RULE_SMA_PERIOD, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.core.indicators import adx, rsi, sma

VALID_SIDES = ("long", "short")


def detect_signal(
    df: pd.DataFrame,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    sma_period: int = RULE_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
) -> str | None:
    """df의 마지막 두 봉(직전 -> 현재)을 비교해 SMA 돌파 + ADX 추세 확인 + (선택) RSI 방향성으로
    진입 신호를 낸다.

    - ADX >= adx_threshold: 추세가 확인된 구간에서만 진입 (횡보장 회피)
    - 종가가 SMA를 막 상향/하향 돌파한 "순간"만 신호로 잡는다(레벨이 아니라 크로스) — 그래야 SMA 위/아래에
      계속 머무는 동안 매 주기마다 중복 진입 신호가 나는 걸 막을 수 있다.
    - require_rsi_confirm=True(기본, 실거래 봇 동작)면 RSI로 방향성을 한 번 더 확인(rsi_threshold 기준)해
      추세와 모멘텀이 같은 방향일 때만 진입. sma_period/rsi_period/rsi_threshold/require_rsi_confirm은
      백테스트에서 진입 조건 변형을 비교하기 위한 파라미터 — 기본값은 실거래 봇의 현재 규칙과 동일하다.

    df는 아직 마감되지 않은(진행 중인) 마지막 봉을 포함해도 되고 제외해도 된다 — 호출자가 결정한다.
    지표 warm-up을 위해 최소 35개 이상의 봉이 필요하다.
    """
    if len(df) < 35:
        return None

    close = df["close"]
    sma_line = sma(close, sma_period)
    latest_adx = adx(df, 14)["adx"].iloc[-1]

    if pd.isna(latest_adx) or latest_adx < adx_threshold:
        return None

    prev_diff = close.iloc[-2] - sma_line.iloc[-2]
    curr_diff = close.iloc[-1] - sma_line.iloc[-1]
    if pd.isna(prev_diff) or pd.isna(curr_diff):
        return None

    bullish_cross = prev_diff <= 0 and curr_diff > 0
    bearish_cross = prev_diff >= 0 and curr_diff < 0

    if not require_rsi_confirm:
        if bullish_cross:
            return "LONG"
        if bearish_cross:
            return "SHORT"
        return None

    latest_rsi = rsi(close, rsi_period).iloc[-1]
    if pd.isna(latest_rsi):
        return None

    if bullish_cross and latest_rsi >= rsi_threshold:
        return "LONG"
    if bearish_cross and latest_rsi <= (100 - rsi_threshold):
        return "SHORT"
    return None


def compute_bracket_prices(entry_price: float, side: str, stop_loss_pct: float = STOP_LOSS_PCT,
                            take_profit_rr: float = TAKE_PROFIT_RR) -> tuple[float, float]:
    """진입가 기준 손절가/익절가를 손익비(take_profit_rr)로 계산한다. (stop_loss_price, take_profit_price)"""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")

    take_profit_pct = stop_loss_pct * take_profit_rr
    if side == "long":
        return entry_price * (1 - stop_loss_pct), entry_price * (1 + take_profit_pct)
    return entry_price * (1 + stop_loss_pct), entry_price * (1 - take_profit_pct)
