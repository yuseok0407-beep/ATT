from typing import Callable

import pandas as pd

from src.core.config import (
    MIN_ATR_TO_STOP_RATIO,
    RULE_ADX_THRESHOLD,
    RULE_SMA_PERIOD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_RR,
)
from src.core.indicators import adx, atr, rsi, sma

VALID_SIDES = ("long", "short")

# 설정값이 아니라 **코드로 적힌 진입/청산 규칙**이 바뀌면 이 숫자를 1 올린다(2026-09-22).
# 저널의 config_changed는 설정값만 비교하므로, 이게 없으면 "마감봉으로만 신호 계산" 같은 코드
# 변경은 전략 버전(`execution.strategy_versions`)에 안 잡힌다. 올리고 봇을 재시작하면
# config_changed에 logic_revision N→N+1이 남고 그게 새 버전이 된다.
# 1: 규칙 봇 개시 / 2: 마감봉 신호(08-25) / 3: 같은 신호봉 재진입 잠금 + 진입가 괴리 검사(09-08)
STRATEGY_LOGIC_REVISION = 3

# SMA 돌파의 **방향**을 무엇으로 확인하는가. 실거래 현재 동작은 "rsi"이고, 바꾸려면 게이트를
# 통과시킨 뒤 이 기본값을 옮긴다(그때 STRATEGY_LOGIC_REVISION도 같이 올릴 것).
#   "rsi"  — RSI가 임계값 위/아래 (현재 실거래)
#   "di"   — +DI > -DI (ADX와 같은 계산에서 나오는 방향 지표. 지금까지 버려지고 있었다)
#   "none" — 방향 확인 없이 돌파만으로 진입
VALID_DIRECTION_FILTERS = ("rsi", "di", "none")
DEFAULT_DIRECTION_FILTER = "rsi"


def detect_signal(
    df: pd.DataFrame,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    sma_period: int = RULE_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
    direction_filter: str = DEFAULT_DIRECTION_FILTER,
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
    # ADX 한 번으로 추세 강도(adx)와 방향(+DI/-DI)을 **둘 다** 얻는다 — 방향 필터에서 다시
    # 부르면 백테스트에서 가장 무거운 계산이 봉마다 두 번 돈다.
    adx_frame = adx(df, 14)
    latest_adx = adx_frame["adx"].iloc[-1]

    if pd.isna(latest_adx) or latest_adx < adx_threshold:
        return None

    prev_diff = close.iloc[-2] - sma_line.iloc[-2]
    curr_diff = close.iloc[-1] - sma_line.iloc[-1]
    if pd.isna(prev_diff) or pd.isna(curr_diff):
        return None

    bullish_cross = prev_diff <= 0 and curr_diff > 0
    bearish_cross = prev_diff >= 0 and curr_diff < 0

    if not require_rsi_confirm or direction_filter == "none":
        if bullish_cross:
            return "LONG"
        if bearish_cross:
            return "SHORT"
        return None

    if direction_filter == "di":
        # +DI/-DI는 ADX와 **같은 계산**에서 이미 나오는데 지금까지 버려지고 있었다 — ADX로
        # "추세가 있는가"를 묻고 방향은 별개 지표(RSI)로 물으면 두 판단의 근거가 갈라진다.
        plus_di = adx_frame["plus_di"].iloc[-1]
        minus_di = adx_frame["minus_di"].iloc[-1]
        if pd.isna(plus_di) or pd.isna(minus_di):
            return None
        if bullish_cross and plus_di > minus_di:
            return "LONG"
        if bearish_cross and minus_di > plus_di:
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


def atr_to_stop_ratio(df: pd.DataFrame, stop_loss_pct: float = STOP_LOSS_PCT,
                       atr_period: int = 14) -> float | None:
    """마지막 봉의 ATR이 손절폭의 몇 배인지. 봉이 모자라 ATR이 NaN이면 None.

    실거래 봇은 신호 계산과 마찬가지로 **마감된 봉만** 담긴 df를 넘겨야 한다."""
    if stop_loss_pct <= 0 or len(df) < atr_period + 1:
        return None
    latest_atr = atr(df, atr_period).iloc[-1]
    close = df["close"].iloc[-1]
    if pd.isna(latest_atr) or close <= 0:
        return None
    return float(latest_atr / close / stop_loss_pct)


def passes_volatility_floor(ratio: float | None,
                             min_ratio: float = MIN_ATR_TO_STOP_RATIO) -> bool:
    """ATR/손절폭 비율이 하한을 넘는지 — **이 규칙의 유일한 정의**이고, 실거래 봇과 조건 근접도
    화면이 둘 다 이걸 부른다(같은 조건을 두 군데 적었다가 어긋나는 걸 막기 위함).

    왜 필요한가: 손절 1.25%/익절 2.5%인데 ATR이 0.5%면 익절까지 5 ATR을 가야 한다 — 1시간봉
    스케일에서 거의 안 일어나고 대신 시간이 흐르며 손절로 흘러간다. 후보 신호를 특성별로 쪼개보면
    이 저변동 구간이 일관된 손실 구간이었다(UPDATE_LOG.md 2026-09-09).

    min_ratio<=0(필터 off)이거나 ratio가 None(ATR warm-up 부족)이면 통과시킨다 — 데이터가
    없다는 게 "변동성이 부족하다"는 근거는 아니므로(레짐 필터와 같은 방침)."""
    if min_ratio <= 0 or ratio is None:
        return True
    return ratio >= min_ratio
