from typing import Callable

import numpy as np
import pandas as pd

from src.core.config import (
    MIN_ATR_TO_STOP_RATIO,
    RULE_ADX_THRESHOLD,
    RULE_DIRECTION_FILTER,
    RULE_HTF_HOURS,
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
# 4: 봉 마감 직후(+3초)에 맞춰 판정(09-28) — 규칙은 같지만 진입 지연이 중간값 39초 → 수 초로
#    줄어 진입 슬리피지·가격이탈 차단 빈도가 달라진다. 전후 비교를 하려고 버전 경계를 만든다.
STRATEGY_LOGIC_REVISION = 4

# SMA 돌파의 **방향**을 무엇으로 확인하는가. 값의 정의와 기본값은 `config.RULE_DIRECTION_FILTER`
# 한 곳에 있다 — 설정이므로 바꿔도 STRATEGY_LOGIC_REVISION은 안 올린다(저널의 config_changed가
# 경계를 남기고 전략 버전이 자동으로 생긴다).
VALID_DIRECTION_FILTERS = ("rsi", "di", "none")
DEFAULT_DIRECTION_FILTER = RULE_DIRECTION_FILTER


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


# 상위봉 DI를 계산할 때 실거래가 확보하는 상위봉 개수. 백테스트는 전체 이력으로 계산하는데,
# DI의 지수평활은 이만큼 지나면 시작점의 영향이 (13/14)^100 ≈ 0.06%로 사라지므로 둘이 같아진다.
HTF_LOOKBACK_BARS = 100


def htf_required_bars(htf_hours: int, bar_hours: float = 1.0) -> int:
    """상위봉 필터에 필요한 **마감된 신호봉** 개수. 실거래 봇과 조건 화면이 캔들 조회 개수를
    정할 때 쓴다(백테스트는 전체 이력을 쓰므로 필요 없다). 0이면 필터가 꺼져 있다는 뜻.

    +1 상위봉은 가장 오래된 묶음이 신호봉 몇 개가 빠진 채 시작될 수 있어서다 — 그 묶음은
    `htf_direction_series`가 버린다."""
    if htf_hours <= 0:
        return 0
    return int(round((HTF_LOOKBACK_BARS + 1) * htf_hours / bar_hours))


def htf_direction_series(df: pd.DataFrame, htf_hours: int, di_period: int = 14) -> pd.Series:
    """각 행(신호봉)마다, **그 봉이 마감된 시각에 이미 마감돼 있던** 상위봉(htf_hours시간)의
    +DI/-DI 방향 — +1.0(+DI > -DI), -1.0(-DI > +DI), NaN(필터 꺼짐·warm-up·동률).
    이 규칙의 **유일한 정의**이고 실거래 봇·백테스트(`engine.gated_signals`)·조건 화면이 전부
    이걸 부른다.

    왜 상위봉인가(2026-09-29): 지금 방향 확인(`direction_filter="di"`)은 1시간봉 DI(14) — 대략
    최근 14시간의 방향이고, 레짐 필터(SMA400)는 약 17일의 방향이다. 그 사이(2~10일)의 흐름은
    아무도 안 본다. 1시간봉 돌파가 4시간봉 흐름을 거스르는 자리라면 되돌림일 가능성이 크다는
    가설이다(Elder의 "삼중 스크린": 큰 흐름 → 중간 흐름 → 진입 타이밍).

    **미래 정보를 쓰지 않는 것이 이 함수의 핵심이다.** 1시간봉 01:00(마감 02:00) 시점에 00:00
    4시간봉은 아직 진행 중이다 — 그 봉의 고가/저가/종가를 쓰면 백테스트가 앞으로 두 시간의
    가격을 미리 본 셈이 된다. 그래서 상위봉은 `시작 + htf_hours <= 신호봉 마감 시각`인 것만 쓴다.
    03:00 봉(마감 04:00)에서 비로소 00:00 4시간봉이 쓰인다 — 바이낸스도 그 순간 그 봉을 마감한다.

    상위봉은 신호봉을 UTC 기준으로 묶어 만든다(거래소 조회를 늘리지 않기 위함 — 요청 한도는 IP
    단위로 봇·대시보드가 나눠 쓴다). 묶음 경계는 epoch 기준이라 2/4/6/8/12시간이 바이낸스 캔들과
    같은 시각(00/04/08… UTC)에 맞는다. 신호봉이 다 안 찬 묶음(조회 시작점이 묶음 중간이거나
    데이터 결측)은 버린다 — 반쪽 봉의 고가/저가는 거래소의 상위봉과 다르다.

    df에는 `timestamp`(봉 시작 시각, tz 없는 UTC) 열이 있어야 하고, 실거래는 **마감된 봉만**
    넘겨야 한다(다른 필터와 같다)."""
    nan = pd.Series(np.nan, index=df.index, dtype=float)
    if htf_hours <= 0 or len(df) < 2 or "timestamp" not in df.columns:
        return nan

    ts = pd.to_datetime(df["timestamp"])
    bar = ts.diff().median()
    if pd.isna(bar) or bar <= pd.Timedelta(0):
        return nan
    span = pd.Timedelta(hours=htf_hours)
    per_bucket = span / bar
    if per_bucket < 1 or per_bucket != int(per_bucket):
        raise ValueError(f"htf_hours={htf_hours}는 신호봉({bar})의 정수배여야 한다")

    bucket = ts.dt.floor(span)
    grouped = df.assign(_bucket=bucket.values).groupby("_bucket", sort=True)
    htf = pd.DataFrame({
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "count": grouped.size(),
    })
    htf = htf[htf["count"] == int(per_bucket)]
    # warm-up 판단은 DI 자체의 NaN에 맡긴다 — 여기서 따로 "상위봉 N개 미만이면 NaN"을 걸면
    # 전체 이력으로 계산한 값(백테스트)과 접두사로 계산한 값(실거래)이 warm-up 경계에서 갈라진다.
    if htf.empty:
        return nan

    frame = adx(htf, di_period)
    diff = frame["plus_di"] - frame["minus_di"]
    direction = np.sign(diff).where(diff != 0)  # 동률과 NaN은 판단하지 않는다

    available_at = (htf.index + span).values           # 이 시각부터 그 상위봉을 쓸 수 있다
    closes_at = (ts + bar).values                      # 각 신호봉이 마감되는 시각
    pos = np.searchsorted(available_at, closes_at, side="right") - 1
    values = np.where(pos >= 0, direction.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.Series(values, index=df.index, dtype=float)


def apply_htf_filter(signal: str | None, htf_direction: float | None) -> str | None:
    """상위봉 방향이 신호와 **반대**면 신호를 버린다 — 롱은 상위봉 +DI > -DI, 숏은 -DI > +DI일
    때만 남는다. 이 규칙의 유일한 정의다(`apply_regime_filter`와 같은 방침).

    htf_direction이 None/NaN(필터 꺼짐·warm-up·동률)이면 그대로 통과시킨다 — 데이터가 없다는 게
    "방향이 반대"라는 근거는 아니므로."""
    if signal is None or htf_direction is None or pd.isna(htf_direction):
        return signal
    if signal == "LONG" and htf_direction < 0:
        return None
    if signal == "SHORT" and htf_direction > 0:
        return None
    return signal


def latest_htf_direction(df: pd.DataFrame, htf_hours: int = RULE_HTF_HOURS) -> float | None:
    """마지막 (마감된) 봉 시점의 상위봉 방향. 실거래 봇·조건 화면용 — None이면 판단 없음."""
    if htf_hours <= 0 or df.empty:
        return None
    value = htf_direction_series(df, htf_hours).iloc[-1]
    return None if pd.isna(value) else float(value)


def closed_bars_needed(regime_sma_period: int, htf_hours: int = RULE_HTF_HOURS) -> int:
    """실거래 판단에 필요한 **마감된** 신호봉 개수 — 신호 계산 100봉, 레짐 SMA, 상위봉 warm-up 중
    가장 긴 것. 캔들을 조회하는 쪽은 여기에 1(진행 중인 봉 몫)을 더해 요청한다. 봇과 조건 화면이
    같은 값을 쓰도록 한 곳에 둔다."""
    need = 100
    if regime_sma_period > 0:
        need = max(need, regime_sma_period + 1)
    return max(need, htf_required_bars(htf_hours))
