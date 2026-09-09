from typing import Callable

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


def apply_regime_filter(signal: str | None, above_long_sma: bool | None,
                         *, filter_longs: bool = False) -> str | None:
    """장기추세 레짐으로 진입 신호를 걸러낸다 — **이 규칙의 유일한 정의**이고, 백테스트
    (`make_regime_filtered_signal_fn`)와 실거래 봇(`futures_rule_bot._evaluate_symbol`)이 둘 다
    이걸 부른다(두 곳에 같은 조건을 따로 적어놨다가 서로 어긋나는 걸 막기 위함).

    종가가 장기 SMA **위**(above_long_sma=True)면 숏 진입을 막는다. 상승 레짐에서 역행 숏이
    계속 깎이는 걸 차단하는 게 목적이고, 롱은 기본적으로 안 건드린다(filter_longs=True면 대칭으로
    종가가 장기선 아래일 때 롱도 막는다).

    above_long_sma가 None이면(레짐 SMA warm-up 부족 등) 필터를 적용하지 않고 신호를 그대로
    통과시킨다 — 데이터가 없다는 게 "레짐이 나쁘다"는 근거는 아니므로."""
    if signal is None or above_long_sma is None:
        return signal
    if signal == "SHORT" and above_long_sma:
        return None
    if filter_longs and signal == "LONG" and not above_long_sma:
        return None
    return signal


def is_above_long_sma(df: pd.DataFrame, regime_sma_period: int) -> bool | None:
    """df의 마지막 봉 종가가 장기 SMA(regime_sma_period) 위인지. 필터가 꺼져 있거나
    (regime_sma_period<=0) 봉이 모자라 SMA가 아직 NaN이면 None을 돌려준다 — 호출자는 그걸
    `apply_regime_filter`에 그대로 넘기면 "필터 미적용"으로 처리된다.

    실거래 봇은 신호 계산과 마찬가지로 **마감된 봉만 담긴 df**를 넘겨야 한다."""
    if regime_sma_period <= 0 or len(df) < regime_sma_period:
        return None
    long_sma = df["close"].rolling(regime_sma_period).mean().iloc[-1]
    if pd.isna(long_sma):
        return None
    return bool(df["close"].iloc[-1] > long_sma)


def make_regime_filtered_signal_fn(
    df_full: pd.DataFrame,
    regime_sma_period: int,
    *,
    filter_longs: bool = False,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    sma_period: int = RULE_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
    base_signal_fn: Callable[[pd.DataFrame], str | None] | None = None,
) -> Callable[[pd.DataFrame], str | None]:
    """`detect_signal`에 장기추세 레짐 필터를 씌운 `signal_fn`을 만들어 돌려준다 —
    `run_backtest(signal_fn=...)`에 그대로 넘길 수 있다.

    필터 규칙: 종가가 장기 SMA(regime_sma_period) **위**면 숏 진입을 막는다. 상승 레짐에서
    역행 숏이 계속 깎이는 걸 차단하는 것이 목적이고, 롱은 기본적으로 건드리지 않는다
    (filter_longs=True면 대칭으로 종가가 장기선 아래일 때 롱도 막는다).

    df_full을 통째로 받는 이유: 엔진은 signal_fn에 최근 100봉(SIGNAL_LOOKBACK_BARS) 윈도우만
    넘기는데, 장기 SMA는 그보다 긴 기간을 요구해서 윈도우 안에서는 계산 자체가 불가능하다.
    그래서 전체 df로 레짐 시리즈를 미리 계산해두고 윈도우의 마지막 봉 위치로 조회한다 — 각 시점의
    값은 그 시점까지의 종가만으로 계산되므로 미래 정보를 쓰지 않는다. 조회 키로는 df_full의
    인덱스가 아니라 "timestamp 열이 있으면 그 값, 없으면 인덱스 값"을 쓴다 —
    `run_walk_forward`처럼 df를 구간별로 잘라 `reset_index(drop=True)`한 조각을 넘기는 호출자와도
    안전하게 맞물리게 하기 위함이다.

    레짐 SMA가 아직 warm-up 중(NaN)인 구간에서는 필터를 적용하지 않는다(진입 허용) — 데이터가
    없다는 게 "레짐이 나쁘다"는 근거는 아니므로.

    base_signal_fn: 안쪽에서 부를 진입 신호 함수를 갈아끼운다(기본값 None이면 detect_signal을
    위 파라미터로 부른다). 백테스트에서 같은 종목을 여러 필터 길이로 반복해서 돌릴 때, 필터와
    무관하게 매번 똑같이 나오는 detect_signal 결과를 메모이즈해서 넘기기 위한 것 —
    넘기면 adx_threshold/sma_period/rsi_* 파라미터는 무시된다(호출자가 이미 반영했다고 본다).

    주의: 실거래 봇에 이걸 도입하려면 `_evaluate_symbol`이 지금처럼 101봉이 아니라
    최소 regime_sma_period+1 봉을 조회하도록 같이 바꿔야 한다.
    """
    keys = df_full["timestamp"] if "timestamp" in df_full.columns else df_full.index.to_series()
    regime_up = (df_full["close"] > df_full["close"].rolling(regime_sma_period).mean())
    regime_up.index = keys.values
    lookup = regime_up.to_dict()
    valid = (~df_full["close"].rolling(regime_sma_period).mean().isna())
    valid.index = keys.values
    valid_lookup = valid.to_dict()

    def signal_fn(window: pd.DataFrame) -> str | None:
        if base_signal_fn is not None:
            signal = base_signal_fn(window)
        else:
            signal = detect_signal(
                window, adx_threshold=adx_threshold, sma_period=sma_period, rsi_period=rsi_period,
                rsi_threshold=rsi_threshold, require_rsi_confirm=require_rsi_confirm,
            )
        if signal is None:
            return None
        key = window["timestamp"].iloc[-1] if "timestamp" in window.columns else window.index[-1]
        above = lookup.get(key) if valid_lookup.get(key, False) else None
        return apply_regime_filter(signal, above, filter_longs=filter_longs)

    return signal_fn
