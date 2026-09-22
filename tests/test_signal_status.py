import json

import numpy as np
import pandas as pd
import pytest

from src.core.config import STOP_LOSS_PCT
from src.core.signal_status import CROSS_NEAR_PCT, MIN_BARS, evaluate_conditions

SMA_PERIOD = 10


# 봉 폭을 손절폭에 비례시킨다 — 저변동 필터는 ATR/손절폭 비율이라, 고정 폭이면 .env의
# STOP_LOSS_PCT를 바꾸는 것만으로(2026-09-22, 1.25% -> 1.75%) 이 파일의 시계열이 필터에 걸린다.
_HL_PCT = 0.005 * STOP_LOSS_PCT / 0.0125


def _df(closes, hl_pct=_HL_PCT):
    """고가/저가 폭은 가격 대비 비율로 잡는다 — 고정폭으로 두면 가격이 오를수록 ATR%가 작아져
    저변동 필터(MIN_ATR_TO_STOP_RATIO)에 걸려버린다."""
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=len(closes), freq="h"),
        "high": closes * (1 + hl_pct), "low": closes * (1 - hl_pct), "close": closes,
    })


def _trend(n=500, start=100.0, step=0.5, noise=0.15, seed=0):
    """추세 + 약간의 노이즈. 노이즈가 있어야 ADX/RSI가 NaN이 아닌 실제 값으로 나온다
    (완전히 평탄한 시계열은 방향성 이동이 0이라 ADX가 0/0 = NaN)."""
    rng = np.random.default_rng(seed)
    return start + np.arange(n) * step + rng.normal(0, noise, n)


def _with_last_close_at(closes, distance_pct):
    """마지막 봉의 종가를 'SMA10 대비 정확히 distance_pct%' 위치로 맞춘 시계열을 돌려준다.

    마지막 종가 x 자신이 SMA에도 들어가므로 그냥 sma*(1+p)로 두면 안 맞는다 —
    sma = (S9 + x)/period, x = sma*(1+p) 를 x에 대해 풀면 x = S9*(1+p)/(period-1-p)."""
    closes = list(np.asarray(closes, dtype=float))
    p = distance_pct / 100.0
    s9 = sum(closes[-SMA_PERIOD:-1])
    closes[-1] = s9 * (1 + p) / (SMA_PERIOD - 1 - p)
    return closes


def test_helper_places_the_last_close_where_we_asked():
    """이 파일의 다른 테스트가 전부 이 헬퍼의 정확도에 기대므로 헬퍼 자체를 먼저 검증한다."""
    for target in (-2.0, -0.3, 0.4, 1.5):
        result = evaluate_conditions(_df(_with_last_close_at(_trend(), target)), regime_sma_period=0)
        assert result["distance_pct"] == pytest.approx(target, abs=1e-6)


def test_returns_blocker_when_not_enough_candles():
    result = evaluate_conditions(_df(_trend(n=10)), "BTC/USDT:USDT")
    assert result["proximity"] == 0.0
    assert result["ready"] is False
    assert any("캔들 부족" in b for b in result["blockers"])


def test_candidate_side_is_long_when_price_sits_below_the_sma():
    """상향 돌파는 '직전엔 SMA 아래 -> 지금은 위'라서, 지금 아래에 있어야 다음 봉에 롱이 뜬다."""
    result = evaluate_conditions(_df(_with_last_close_at(_trend(), -0.4)), regime_sma_period=0)
    assert result["candidate_side"] == "LONG"
    assert result["distance_pct"] < 0


def test_candidate_side_is_short_when_price_sits_above_the_sma():
    result = evaluate_conditions(_df(_with_last_close_at(_trend(), 0.4)), regime_sma_period=0)
    assert result["candidate_side"] == "SHORT"
    assert result["distance_pct"] > 0


def test_proximity_falls_off_as_price_moves_away_from_the_sma():
    def prox(distance_pct):
        return evaluate_conditions(
            _df(_with_last_close_at(_trend(), distance_pct)), regime_sma_period=0)["proximity"]

    near, far = prox(-0.1), prox(-0.9)
    beyond = prox(-(CROSS_NEAR_PCT + 2.0))
    assert near > far > 0
    assert beyond == 0.0


def test_regime_blocks_short_and_zeroes_proximity():
    """상승 레짐에서 숏 후보면 proximity를 0으로 둔다 — 가격이 조금 움직인다고 풀리는 조건이
    아니라서 '가깝다'고 표시하면 오해를 준다."""
    result = evaluate_conditions(
        _df(_with_last_close_at(_trend(step=0.5), 0.2)), regime_sma_period=200)
    assert result["candidate_side"] == "SHORT"
    assert result["above_regime"] is True
    assert result["regime_blocks_short"] is True
    assert result["proximity"] == 0.0
    assert any("숏 차단" in b for b in result["blockers"])


def test_regime_does_not_block_longs():
    result = evaluate_conditions(
        _df(_with_last_close_at(_trend(step=0.5), -0.2)), regime_sma_period=200)
    assert result["candidate_side"] == "LONG"
    assert result["above_regime"] is True  # 상승 레짐이지만 롱은 막지 않는다
    assert result["regime_blocks_short"] is False
    assert result["proximity"] > 0


def test_shorts_are_allowed_in_a_downtrend_regime():
    result = evaluate_conditions(
        _df(_with_last_close_at(_trend(start=400.0, step=-0.5), 0.2)), regime_sma_period=200)
    assert result["candidate_side"] == "SHORT"
    assert result["above_regime"] is False
    assert result["regime_blocks_short"] is False
    assert result["proximity"] > 0


def test_regime_filter_off_when_period_is_zero():
    result = evaluate_conditions(_df(_trend()), regime_sma_period=0)
    assert result["above_regime"] is None
    assert result["regime_blocks_short"] is False


def test_blockers_name_the_failing_adx():
    result = evaluate_conditions(_df(_with_last_close_at(_trend(), -0.2)),
                                 adx_threshold=99.0, regime_sma_period=0)
    assert result["adx_ok"] is False
    assert any(b.startswith("ADX") for b in result["blockers"])
    assert result["proximity"] < 1.0


def test_blockers_report_the_pending_sma_cross():
    result = evaluate_conditions(_df(_with_last_close_at(_trend(), -0.4)), regime_sma_period=0)
    assert result["ready"] is False
    assert any("돌파 대기" in b for b in result["blockers"])


def test_ready_matches_detect_signal_with_regime_filter(monkeypatch):
    """ready/signal은 이 모듈이 조건을 다시 구현한 결과가 아니라 실거래와 같은 함수의 결과여야
    한다 — detect_signal을 갈아끼우면 그대로 따라와야 한다."""
    from src.core import signal_status

    monkeypatch.setattr(signal_status, "detect_signal", lambda df, **kw: "LONG")
    result = evaluate_conditions(_df(_trend()), regime_sma_period=0)
    assert result["signal"] == "LONG" and result["ready"] is True

    monkeypatch.setattr(signal_status, "detect_signal", lambda df, **kw: "SHORT")
    blocked = evaluate_conditions(_df(_trend(step=0.5)), regime_sma_period=200)  # 상승 레짐
    assert blocked["signal"] is None and blocked["ready"] is False


def test_output_is_json_safe():
    """NaN이 그대로 실리면 프론트에서 다루기 번거로워 None으로 통일한다."""
    for df in (_df(_trend(n=MIN_BARS + 2)), _df(_trend()), _df(_trend(n=10))):
        result = evaluate_conditions(df, "BTC/USDT:USDT")
        json.dumps(result)  # 예외 없이 직렬화돼야 한다
        for key in ("close", "sma", "adx", "rsi", "regime_sma", "distance_pct"):
            assert result[key] is None or isinstance(result[key], float)


@pytest.mark.parametrize("period", [0, 200])
@pytest.mark.parametrize("step", [0.5, -0.5])
def test_proximity_is_bounded(period, step):
    result = evaluate_conditions(_df(_trend(start=300.0, step=step)), regime_sma_period=period)
    assert 0.0 <= result["proximity"] <= 1.0


# --- collect_conditions (여러 종목 조회 계층) -----------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """TTL 캐시가 모듈 전역이라 테스트끼리 결과가 새면 안 된다."""
    from src.core import signal_status

    signal_status._cache.update({"key": None, "at": 0.0, "payload": None})
    yield
    signal_status._cache.update({"key": None, "at": 0.0, "payload": None})


def test_collect_sorts_ready_first_then_by_proximity():
    from src.core.signal_status import collect_conditions

    near = _df(_with_last_close_at(_trend(), -0.1))
    far = _df(_with_last_close_at(_trend(), -0.9))
    frames = {"NEAR/USDT:USDT": near, "FAR/USDT:USDT": far}
    payload = collect_conditions(list(frames), fetch=lambda sym, limit: frames[sym])

    assert [r["symbol"] for r in payload["symbols"]] == ["NEAR/USDT:USDT", "FAR/USDT:USDT"]
    assert payload["errors"] == []


def test_collect_isolates_a_failing_symbol():
    """한 종목 조회가 실패해도 나머지는 그대로 나와야 한다 — run_once의 심볼별 예외 격리와 같은 방침."""
    from src.core.signal_status import collect_conditions

    def fetch(symbol, limit):
        if symbol == "BAD/USDT:USDT":
            raise TimeoutError("backend timeout")
        return _df(_trend())

    payload = collect_conditions(["BAD/USDT:USDT", "OK/USDT:USDT"], fetch=fetch)
    assert [r["symbol"] for r in payload["symbols"]] == ["OK/USDT:USDT"]
    assert payload["errors"][0]["symbol"] == "BAD/USDT:USDT"
    assert "timeout" in payload["errors"][0]["message"]


def test_collect_reuses_the_cache_within_ttl_and_refetches_after():
    from src.core.signal_status import collect_conditions

    calls = []

    def fetch(symbol, limit):
        calls.append(symbol)
        return _df(_trend())

    collect_conditions(["A/USDT:USDT"], fetch=fetch)
    collect_conditions(["A/USDT:USDT"], fetch=fetch)
    assert len(calls) == 1, "TTL 안에서는 다시 조회하면 안 된다"

    collect_conditions(["A/USDT:USDT"], ttl=0, fetch=fetch)
    assert len(calls) == 2, "TTL이 지나면 다시 조회해야 한다"


def test_collect_refetches_when_the_symbol_list_changes():
    """env를 바꾸면 심볼 목록이 달라진다(SOXL은 실계좌에만 있음) — 캐시가 그걸 무시하면
    다른 계좌의 결과가 그대로 보인다."""
    from src.core.signal_status import collect_conditions

    calls = []

    def fetch(symbol, limit):
        calls.append(symbol)
        return _df(_trend())

    collect_conditions(["A/USDT:USDT"], fetch=fetch)
    collect_conditions(["A/USDT:USDT", "B/USDT:USDT"], fetch=fetch)
    assert calls == ["A/USDT:USDT", "A/USDT:USDT", "B/USDT:USDT"]


def test_collect_requests_enough_candles_for_the_regime_sma():
    """레짐 SMA가 켜져 있으면 100봉으로는 계산이 안 된다 — 봇과 같은 개수를 요청해야 한다."""
    from src.core import signal_status
    from src.core.signal_status import collect_conditions

    seen = {}

    def fetch(symbol, limit):
        seen["limit"] = limit
        return _df(_trend())

    collect_conditions(["A/USDT:USDT"], fetch=fetch)
    expected = (max(101, signal_status.RULE_REGIME_SMA_PERIOD + 2)
                if signal_status.RULE_REGIME_SMA_PERIOD > 0 else 101)
    assert seen["limit"] == expected


def test_low_volatility_blocks_the_signal_like_the_live_bot_does():
    """봇은 저변동이면 진입을 건너뛴다 — 화면이 '진입 가능'으로 보이면 안 된다."""
    df = _df(_trend(), hl_pct=0.0005)   # ATR이 손절폭 대비 한참 아래
    result = evaluate_conditions(df, regime_sma_period=0)
    assert result["atr_ok"] is False
    assert result["atr_ratio"] < result["min_atr_ratio"]
    assert result["ready"] is False
    assert result["proximity"] == 0.0
    assert any("저변동" in b for b in result["blockers"])


def test_volatility_gate_passes_when_atr_clears_the_floor():
    result = evaluate_conditions(_df(_trend()), regime_sma_period=0)
    assert result["atr_ok"] is True
    assert result["atr_ratio"] >= result["min_atr_ratio"]
    assert not any("저변동" in b for b in result["blockers"])


def test_volatility_gate_off_when_ratio_is_zero():
    result = evaluate_conditions(_df(_trend(), hl_pct=0.0005), regime_sma_period=0, min_atr_ratio=0)
    assert result["atr_ok"] is True
