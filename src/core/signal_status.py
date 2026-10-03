"""진입 조건에 지금 얼마나 가까운지를 계산한다 — 대시보드와 텔레그램이 공유하는 단일 정의.

`detect_signal`은 "지금 진입인가 아닌가"만 알려주기 때문에, 신호가 안 뜨는 대부분의 시간 동안
사용자는 "왜 안 들어가는지 / 얼마나 가까운지"를 알 수 없다. 이 모듈은 같은 조건들을 통과/미통과
여부와 함께 **거리**로도 환산해서 돌려준다.

조건을 두 군데(대시보드, 텔레그램)에 각각 적으면 실거래 로직과 어긋나기 쉬워서, 실제 판정은
전부 `futures_strategy`의 함수(`detect_signal`/`apply_regime_filter`/`is_above_long_sma`)를 그대로
호출해서 얻고, 이 모듈은 거기에 "거리/점수"만 덧붙인다.

입력 df는 실거래 봇과 마찬가지로 **마감된 봉만** 담겨 있어야 한다(호출자가 마지막 진행중 봉을
버리고 넘긴다) — 안 그러면 화면에 보이는 값과 봇이 실제로 판단하는 값이 달라진다.
"""
import pandas as pd

from src.core.config import (
    MIN_ATR_TO_STOP_RATIO,
    RULE_DIRECTION_FILTER,
    RULE_ADX_THRESHOLD,
    RULE_HTF_HOURS,
    RULE_REGIME_SMA_PERIOD,
    RULE_SMA_PERIOD,
    RULE_TIMEFRAME,
    STOP_LOSS_PCT,
)
from src.core.futures_strategy import (
    SIGNAL_LOOKBACK_BARS,
    apply_htf_filter,
    apply_regime_filter,
    atr_to_stop_ratio,
    closed_bars_needed,
    detect_signal,
    is_above_long_sma,
    latest_htf_direction,
    passes_volatility_floor,
)
from src.core.indicators import adx, rsi, sma

# SMA까지의 거리가 이 % 이내면 "한 봉 안에 닿을 수 있는 거리"로 보고 근접도를 1에 가깝게 준다.
# 1시간봉 기준 1%는 이 전략의 손절폭(1.25%)보다 약간 좁은 수준 — 한 봉에 충분히 움직이는 거리다.
CROSS_NEAR_PCT = 1.0

MIN_BARS = 35  # detect_signal이 요구하는 최소 워밍업과 동일


def _safe(value):
    """NaN/None을 그대로 JSON에 실으면 프론트에서 다루기 번거로워서 None으로 통일한다."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _direction_check(candidate_side, direction_filter, rsi_value, rsi_threshold,
                      rsi_ok, plus_di, minus_di):
    """방향 확인의 **유일한 표시 정의** — (통과여부, 근접도 점수, 화면 문구).

    실제 진입 판정은 `detect_signal`이 하고 여기서는 그 판정을 화면용으로 재현한다. 두 곳이
    갈라지지 않도록 분기 조건은 `futures_strategy.detect_signal`과 같은 모양으로 적는다
    (2026-09-24: RSI에서 +DI/-DI로 전환하면서 대시보드가 옛 지표를 계속 보여주던 문제).
    """
    if direction_filter == "none":
        return True, 1.0, "방향 확인 없음"

    if direction_filter == "di":
        if plus_di is None or minus_di is None:
            return False, 0.0, "DI 워밍업 중"
        if candidate_side == "LONG":
            ok = plus_di > minus_di
            score = min(1.0, plus_di / minus_di) if minus_di else 1.0
        else:
            ok = minus_di > plus_di
            score = min(1.0, minus_di / plus_di) if plus_di else 1.0
        return ok, score, f"+DI {plus_di:.1f} / -DI {minus_di:.1f}"

    if candidate_side == "LONG":
        score = min(1.0, rsi_value / rsi_threshold) if rsi_threshold > 0 else 1.0
    else:
        score = min(1.0, (100 - rsi_value) / rsi_threshold) if rsi_threshold > 0 else 1.0
    return rsi_ok, score, f"RSI {rsi_value:.1f}"


def evaluate_conditions(
    df: pd.DataFrame,
    symbol: str = "",
    *,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    sma_period: int = RULE_SMA_PERIOD,
    regime_sma_period: int = RULE_REGIME_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    direction_filter: str = RULE_DIRECTION_FILTER,
    min_atr_ratio: float = MIN_ATR_TO_STOP_RATIO,
    stop_loss_pct: float = STOP_LOSS_PCT,
    htf_hours: int = RULE_HTF_HOURS,
) -> dict:
    """마감된 봉만 담긴 df로 "지금 진입 조건에 얼마나 가까운지"를 계산한다.

    후보 방향(candidate_side)은 **지금 종가가 SMA의 어느 쪽에 있는지**로 정한다 — 상향 돌파
    신호는 "직전엔 SMA 아래, 지금은 위"라서 지금 아래에 있어야 다음 봉에 롱이 뜰 수 있고,
    그 반대면 숏이다. 그래서 SMA 아래에 있으면 후보는 롱, 위에 있으면 숏이 된다.

    proximity(0~1)는 세 요소의 곱이다:
      - adx_score: ADX가 임계값에 얼마나 다다랐는지(넘으면 1)
      - cross_score: 종가가 SMA에서 얼마나 가까운지(CROSS_NEAR_PCT 이내면 선형으로 1에 접근)
      - rsi_score: RSI가 후보 방향 기준선에 얼마나 다다랐는지(넘으면 1)
    상승 레짐이라 숏이 아예 막히는 경우엔 0으로 둔다 — 가격이 조금 움직인다고 풀리는 조건이
    아니라서 "가깝다"고 표시하면 오해를 준다. 상위봉 방향이 반대인 경우도 같다(상위봉은 몇
    시간에 한 번 바뀐다).
    """
    result = {
        "symbol": symbol, "bars": len(df), "close": None, "sma": None, "sma_period": sma_period,
        "distance_pct": None, "candidate_side": None,
        "adx": None, "adx_threshold": adx_threshold, "adx_ok": False,
        "rsi": None, "rsi_threshold": rsi_threshold, "rsi_ok": False,
        # 방향 확인은 설정(direction_filter)에 따라 RSI일 수도 +DI/-DI일 수도 있다.
        # 화면은 이 세 값만 보면 되도록 **여기서** 사람이 읽을 문구까지 만든다 — 대시보드와
        # 텔레그램이 각자 지표 이름을 적으면 설정을 바꿀 때마다 두 곳이 어긋난다.
        "direction_filter": direction_filter, "direction_ok": False, "direction_label": None,
        "direction_score": 0.0,
        "plus_di": None, "minus_di": None,
        "regime_sma": None, "regime_sma_period": regime_sma_period,
        "above_regime": None, "regime_blocks_short": False,
        # 상위봉 방향: +1(+DI>-DI) / -1(-DI>+DI) / None(꺼짐·워밍업)
        "htf_hours": htf_hours, "htf_direction": None, "htf_blocks": False,
        "atr_ratio": None, "min_atr_ratio": min_atr_ratio, "atr_ok": False,
        "signal": None, "ready": False, "proximity": 0.0, "blockers": [],
    }
    if len(df) < MIN_BARS:
        result["blockers"].append(f"캔들 부족 ({len(df)}/{MIN_BARS})")
        return result

    close = df["close"]
    latest_close = float(close.iloc[-1])
    sma_value = _safe(sma(close, sma_period).iloc[-1])
    # ADX/DI와 신호 판정은 실거래·백테스트와 같은 최근 SIGNAL_LOOKBACK_BARS봉으로(외부 검토 3.8)
    signal_df = df.iloc[-SIGNAL_LOOKBACK_BARS:]
    adx_frame = adx(signal_df, 14)
    adx_value = _safe(adx_frame["adx"].iloc[-1])
    plus_di = _safe(adx_frame["plus_di"].iloc[-1])
    minus_di = _safe(adx_frame["minus_di"].iloc[-1])
    rsi_value = _safe(rsi(signal_df["close"], rsi_period).iloc[-1])
    above_regime = is_above_long_sma(df, regime_sma_period)
    regime_sma_value = _safe(close.rolling(regime_sma_period).mean().iloc[-1]) if regime_sma_period > 0 else None

    # 실제 진입 판정은 실거래와 완전히 같은 함수로 얻는다(여기서 조건을 다시 구현하지 않는다).
    raw_signal = detect_signal(signal_df, adx_threshold=adx_threshold, sma_period=sma_period,
                               rsi_period=rsi_period, rsi_threshold=rsi_threshold,
                               direction_filter=direction_filter)
    signal = apply_regime_filter(raw_signal, above_regime)
    htf_direction = latest_htf_direction(df, htf_hours)
    signal = apply_htf_filter(signal, htf_direction)
    atr_ratio = atr_to_stop_ratio(df, stop_loss_pct)
    atr_ok = passes_volatility_floor(atr_ratio, min_atr_ratio)
    if not atr_ok:
        signal = None  # 실거래 봇도 여기서 건너뛴다 — 화면이 "진입 가능"으로 보이면 안 된다

    result.update({
        "close": latest_close, "sma": sma_value, "adx": adx_value, "rsi": rsi_value,
        "regime_sma": regime_sma_value, "above_regime": above_regime,
        "plus_di": plus_di, "minus_di": minus_di, "htf_direction": htf_direction,
        "atr_ratio": atr_ratio, "atr_ok": atr_ok,
        "signal": signal, "ready": signal is not None,
    })

    if sma_value is None or adx_value is None or rsi_value is None:
        result["blockers"].append("지표 워밍업 중")
        return result

    distance_pct = (latest_close - sma_value) / sma_value * 100
    candidate_side = "SHORT" if distance_pct > 0 else "LONG"
    result["distance_pct"] = distance_pct
    result["candidate_side"] = candidate_side

    adx_ok = adx_value >= adx_threshold
    rsi_ok = (rsi_value >= rsi_threshold) if candidate_side == "LONG" else (rsi_value <= (100 - rsi_threshold))
    direction_ok, direction_score, direction_label = _direction_check(
        candidate_side, direction_filter, rsi_value, rsi_threshold, rsi_ok, plus_di, minus_di)
    regime_blocks_short = bool(candidate_side == "SHORT" and above_regime)
    htf_blocks = apply_htf_filter(candidate_side, htf_direction) is None
    result.update({"adx_ok": adx_ok, "rsi_ok": rsi_ok, "regime_blocks_short": regime_blocks_short,
                    "htf_blocks": htf_blocks,
                    "direction_ok": direction_ok, "direction_label": direction_label,
                    "direction_score": round(direction_score, 4)})

    blockers = []
    if not adx_ok:
        blockers.append(f"ADX {adx_value:.1f} < {adx_threshold:.0f}")
    if not direction_ok:
        side_label = "롱" if candidate_side == "LONG" else "숏"
        blockers.append(f"{direction_label} ({side_label} 방향 아님)")
    if regime_blocks_short:
        blockers.append(f"상승 레짐 (SMA{regime_sma_period} 위) — 숏 차단")
    if htf_blocks:
        trend = "상승" if htf_direction > 0 else "하락"
        side_label = "롱" if candidate_side == "LONG" else "숏"
        blockers.append(f"{htf_hours}시간봉 {trend} 흐름 — {side_label} 차단")
    if not atr_ok:
        blockers.append(f"저변동 (ATR이 손절폭의 {atr_ratio:.2f}배 < {min_atr_ratio})")
    if not signal:
        blockers.append(f"SMA{sma_period} 돌파 대기 ({distance_pct:+.2f}%)")
    result["blockers"] = blockers

    adx_score = min(1.0, adx_value / adx_threshold) if adx_threshold > 0 else 1.0
    cross_score = max(0.0, 1.0 - abs(distance_pct) / CROSS_NEAR_PCT)
    # 저변동도 레짐 차단과 같이 0으로 둔다 — 가격이 조금 움직인다고 풀리는 조건이 아니라서
    # "가깝다"고 표시하면 오해를 준다(ATR은 봉이 여러 개 쌓여야 바뀐다).
    atr_score = min(1.0, atr_ratio / min_atr_ratio) if (min_atr_ratio > 0 and atr_ratio) else 1.0
    blocked = regime_blocks_short or htf_blocks or not atr_ok
    result["proximity"] = (0.0 if blocked
                          else round(adx_score * cross_score * direction_score * atr_score, 4))
    return result


# --- 여러 종목을 한 번에 조회하는 계층 (대시보드 /api/conditions 와 텔레그램 /conditions 가 공유) ---

CACHE_TTL_SECONDS = 20
_cache: dict = {"key": None, "at": 0.0, "payload": None}


def _fetch_closed_candles(symbol: str, limit: int):
    """실거래 봇과 동일하게 캔들을 받아 마지막(아직 마감 안 된) 봉을 버린다.

    화면/알림에 보이는 값이 봇이 실제로 판단하는 값과 달라지면 안 되므로, 트리밍 규칙을
    `futures_rule_bot._evaluate_symbol`과 똑같이 맞춘다."""
    from src.data.futures_exchange import fetch_ohlcv_df
    from src.data.futures_exchange import get_futures_market_data_client

    df = fetch_ohlcv_df(get_futures_market_data_client(), symbol, timeframe=RULE_TIMEFRAME, limit=limit)
    return df.iloc[:-1]


def collect_conditions(symbols, *, ttl: float = CACHE_TTL_SECONDS, fetch=None) -> dict:
    """여러 종목의 조건 근접도를 모아 proximity 내림차순으로 돌려준다.

    종목마다 캔들을 새로 받아야 해서(레짐 SMA 때문에 402봉) 대시보드가 몇 초마다 부르면 낭비다 —
    ttl초 동안은 같은 결과를 재사용한다. 대시보드와 텔레그램이 같은 캐시를 공유하지는 않는다
    (별개 프로세스) — 각자의 프로세스 안에서만 유효하다.

    한 종목 조회가 실패해도 나머지는 그대로 돌려준다(심볼별 예외 격리) — 실거래 봇의 run_once가
    심볼 하나의 실패로 사이클 전체를 죽이지 않는 것과 같은 방침.
    """
    import time

    symbols = list(symbols)
    key = tuple(symbols)
    now = time.time()
    if _cache["payload"] is not None and _cache["key"] == key and (now - _cache["at"]) < ttl:
        return _cache["payload"]

    fetch = fetch or _fetch_closed_candles
    limit = closed_bars_needed(RULE_REGIME_SMA_PERIOD, RULE_HTF_HOURS) + 1  # +1 = 버릴 진행 중 봉

    rows, errors = [], []
    for symbol in symbols:
        try:
            rows.append(evaluate_conditions(fetch(symbol, limit), symbol))
        except Exception as exc:  # noqa: BLE001 - 심볼 하나의 실패가 전체를 막지 않게
            errors.append({"symbol": symbol, "message": str(exc)})

    rows.sort(key=lambda r: (r["ready"], r["proximity"]), reverse=True)
    payload = {
        "timeframe": RULE_TIMEFRAME, "adx_threshold": RULE_ADX_THRESHOLD,
        "sma_period": RULE_SMA_PERIOD, "regime_sma_period": RULE_REGIME_SMA_PERIOD,
        "htf_hours": RULE_HTF_HOURS,
        "cross_near_pct": CROSS_NEAR_PCT, "symbols": rows, "errors": errors,
        "generated_at": now,
    }
    _cache.update({"key": key, "at": now, "payload": payload})
    return payload
