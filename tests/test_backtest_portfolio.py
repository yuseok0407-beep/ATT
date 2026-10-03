import numpy as np
import pandas as pd
import pytest

from src.backtest.portfolio import (
    daily_loss_limit_r,
    simulate_many,
    simulate_portfolio,
    trades_by_symbol,
)

# 진입 게이트/수수료를 끈 기본값 — 이 파일이 검증하는 것은 **포트폴리오 제약**(동시보유 한도,
# 서킷브레이커, 배정 순서)이고 게이트 자체는 tests/test_backtest_engine.py에서 본다.
NO_GATES = dict(regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0,
                adx_threshold=1, sma_period=10, stop_loss_pct=0.01, take_profit_rr=2.0)


def _closed(result):
    """구간 안에서 청산된 거래만. 2026-10-03부터 구간 끝에 열려 있던 포지션도 마지막 종가로 닫혀
    `end_of_data` 거래로 들어간다(외부 검토 3.7) — 청산 규칙을 보는 테스트는 그것을 뺀다."""
    return [t for t in result["trades"] if t["reason"] != "end_of_data"]


def _flat(n, price=100.0):
    return pd.DataFrame({"high": np.full(n, price + 0.1), "low": np.full(n, price - 0.1),
                          "close": np.full(n, price)})


def _with_signal_at(n, *, bars, side="LONG", price=100.0):
    """지정한 봉들에서만 신호가 나는 것으로 취급할 수 있게, 신호를 직접 주입할 준비를 한 df.

    실제 지표를 만족시키는 가격 패턴을 합성하는 대신 `signals_by_symbol`로 신호를 넘긴다 —
    이 파일의 관심사는 "신호가 있을 때 포트폴리오 제약이 어떻게 작동하는가"이므로, 신호 생성은
    고정값으로 두는 편이 테스트가 무엇을 재는지 분명해진다."""
    df = _flat(n, price)
    return df, {bar: side for bar in bars}


def _resolving(df, bar, *, price=100.0, hit="stop", stop_pct=0.01, rr=2.0):
    """지정한 봉에서 손절(또는 익절)에 닿도록 그 봉의 고저를 넓힌다."""
    df = df.copy()
    if hit == "stop":
        df.loc[bar, "low"] = price * (1 - stop_pct) - 0.01
    else:
        df.loc[bar, "high"] = price * (1 + stop_pct * rr) + 0.01
    return df


def test_daily_loss_limit_converts_pct_to_r_using_risk_per_trade():
    """실거래 한도는 자산 대비 %인데 시뮬레이션은 R로 계산하므로 환산이 필요하다.
    1R = 자산의 risk_per_trade%이므로 5% / 0.75% = 6.67R."""
    assert daily_loss_limit_r(0.05, 0.0075) == pytest.approx(6.6667, abs=1e-4)
    assert daily_loss_limit_r(0.05, 0.02) == pytest.approx(2.5)
    assert daily_loss_limit_r(0.0, 0.02) is None  # 한도 없음
    assert daily_loss_limit_r(0.05, 0.0) is None


def test_concurrency_cap_blocks_entries_and_counts_them():
    df, signals = _with_signal_at(80, bars=[40])
    dfs = {"A": df, "B": df, "C": df}
    sig = {s: signals for s in dfs}

    result = simulate_portfolio(dfs, **NO_GATES, signals_by_symbol=sig,
                                max_concurrent_positions=2)

    # 세 종목이 같은 봉에 신호를 냈고 자리는 둘 — 하나는 버려져야 한다.
    assert len(result["still_open"]) == 2
    assert result["blocked"]["max_positions"] == 1


def test_freed_slot_is_reusable_in_the_same_bar():
    """실거래 봇도 한 사이클에서 청산을 반영한 뒤 진입을 판단한다(run_once가 청산 정리 후
    _evaluate_symbol을 부른다) — 청산된 봉에 자리가 바로 나야 한다."""
    a = _resolving(_flat(80), 45)          # A는 45봉에서 손절
    b = _flat(80)
    result = simulate_portfolio(
        {"A": a, "B": b}, **NO_GATES, max_concurrent_positions=1,
        signals_by_symbol={"A": {40: "LONG"}, "B": {45: "LONG"}})

    assert [t["symbol"] for t in _closed(result)] == ["A"]
    # A가 45봉에서 비운 자리를 B가 같은 봉에 받았다.
    assert result["still_open"] == ["B"]
    assert result["blocked"]["max_positions"] == 0


def test_consecutive_loss_breaker_counts_only_closed_trades():
    """**이 프로젝트에서 실제로 났던 오류**(UPDATE_LOG 2026-09-09): 연속손실을 진입 시각
    기준으로 세면 동시보유를 늘릴수록 결과가 좋아지는 것처럼 보인다. 연속손실은 청산이
    기록된 뒤에만 알 수 있으므로, 아직 안 닫힌 포지션은 세어선 안 된다.

    여기서는 손실 2건이 **열려만 있는** 상태에서 세 번째 진입이 막히지 않아야 한다
    (한도를 2로 낮춰서, 잘못 세면 바로 막히게 만든다)."""
    holding = _flat(90)                     # 청산이 안 일어나는 종목
    result = simulate_portfolio(
        {"A": holding, "B": holding, "C": holding}, **NO_GATES,
        max_concurrent_positions=5, max_consecutive_losses=2,
        signals_by_symbol={"A": {40: "LONG"}, "B": {41: "LONG"}, "C": {42: "LONG"}})

    assert len(result["still_open"]) == 3
    assert result["blocked"]["consecutive_losses"] == 0


def test_consecutive_loss_breaker_blocks_after_enough_closed_losses():
    losers = {}
    signals = {}
    for i, symbol in enumerate("ABC"):
        bar = 40 + i * 6
        losers[symbol] = _resolving(_flat(90), bar + 1)
        signals[symbol] = {bar: "LONG"}
    # D는 마지막에 신호를 내므로, A/B의 손실이 이미 닫힌 뒤에 판단된다.
    losers["D"] = _flat(90)
    signals["D"] = {70: "LONG"}

    result = simulate_portfolio(losers, **NO_GATES, max_concurrent_positions=5,
                                max_consecutive_losses=2, signals_by_symbol=signals)

    closed_losses = [t for t in result["trades"] if t["pnl_r"] < 0]
    assert len(closed_losses) >= 2
    assert result["blocked"]["consecutive_losses"] >= 1
    assert "D" not in result["still_open"]


def test_a_win_resets_the_consecutive_loss_counter():
    # 한도를 1로 두었으므로 A의 손실이 닫히는 순간 신규 진입이 막힌다 — 그래서 승리할 B는
    # 그 전에 이미 열려 있어야 한다(그게 실거래에서 카운터가 리셋되는 경로다).
    dfs = {
        "A": _resolving(_flat(90), 50),                        # 40봉 진입 -> 50봉 손절
        "B": _resolving(_flat(90), 55, hit="target"),           # 41봉 진입 -> 55봉 익절
        "C": _flat(90),                                         # 60봉 진입이 허용돼야 한다
    }
    signals = {"A": {40: "LONG"}, "B": {41: "LONG"}, "C": {60: "LONG"}}

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                max_consecutive_losses=1, signals_by_symbol=signals)

    assert sorted(t["reason"] for t in _closed(result)) == ["stop_loss", "take_profit"]
    assert result["still_open"] == ["C"]
    assert result["blocked"]["consecutive_losses"] == 0


def test_daily_loss_limit_stops_new_entries_for_that_day_only():
    """일일 한도는 **로컬 날짜**로 리셋된다 — 한도에 닿은 날의 진입만 막고 다음 날은 다시 받는다."""
    n = 120
    stamps = pd.date_range("2026-09-01", periods=n, freq="h")

    def framed(df):
        df = df.copy()
        df["timestamp"] = stamps
        return df

    # 09-01 00:00부터 1시간봉이므로 봉 24~47이 09-02, 48~71이 09-03이다.
    dfs = {
        "A": framed(_resolving(_flat(n), 41)),   # 40봉 진입 -> 41봉 손절 (09-02)
        "B": framed(_resolving(_flat(n), 46)),   # 45봉 진입 -> 46봉 손절 (09-02)
        "C": framed(_flat(n)),                    # 47봉 신호: 같은 날 -> 막혀야 한다
        "D": framed(_flat(n)),                    # 50봉 신호: 다음 날 -> 받아야 한다
    }
    signals = {"A": {40: "LONG"}, "B": {45: "LONG"}, "C": {47: "LONG"}, "D": {50: "LONG"}}

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                max_consecutive_losses=None, max_daily_loss_r=1.5,
                                signals_by_symbol=signals)

    assert result["blocked"]["daily_loss"] >= 1
    assert "C" not in result["still_open"]
    assert "D" in result["still_open"]      # 다음 날은 한도가 리셋됐다


def test_allocation_order_follows_symbol_order_without_a_seed():
    """실거래 봇은 FUTURES_SYMBOLS 순서로 순회하므로 그것이 기본 배정 순서다."""
    df, signals = _with_signal_at(80, bars=[40])
    sig = {s: signals for s in ("A", "B")}

    result = simulate_portfolio({"A": df, "B": df}, **NO_GATES, signals_by_symbol=sig,
                                max_concurrent_positions=1)

    assert result["still_open"] == ["A"]


def test_seed_changes_which_symbol_gets_the_slot():
    """같은 봉의 다중 신호에서 누가 자리를 받는지는 임의적 선택이고 결과를 바꾼다 —
    한 번의 숫자를 결론으로 쓰면 그 임의성을 성과로 착각한다."""
    df, signals = _with_signal_at(80, bars=[40])
    dfs = {s: df for s in ("A", "B", "C", "D")}
    sig = {s: signals for s in dfs}

    winners = {tuple(simulate_portfolio(dfs, **NO_GATES, signals_by_symbol=sig,
                                         max_concurrent_positions=1, seed=seed)["still_open"])
               for seed in range(30)}

    assert len(winners) > 1


def test_simulate_many_returns_one_result_per_seed():
    df, signals = _with_signal_at(80, bars=[40])
    dfs = {s: df for s in ("A", "B", "C")}

    results = simulate_many(dfs, seeds=range(5), **NO_GATES,
                             signals_by_symbol={s: signals for s in dfs},
                             max_concurrent_positions=1)

    assert len(results) == 5
    assert all(len(r["still_open"]) == 1 for r in results)


def test_timeline_aligns_symbols_by_timestamp_not_bar_index():
    """종목마다 상장일이 달라 길이가 다르다(SOXL 3110봉 vs BTC 8760봉) — 봉 인덱스로 맞추면
    서로 다른 시각의 봉이 같은 시점으로 묶여 동시보유 판정이 엉킨다."""
    stamps = pd.date_range("2026-09-01", periods=140, freq="h")
    early = _resolving(_flat(140), 50)      # A: 40봉 진입 -> 50봉 청산
    early["timestamp"] = stamps
    # B는 뒤늦게 상장 — B의 0봉이 전체 타임라인의 60번째 시각이다.
    late = _flat(80)
    late["timestamp"] = stamps[60:]

    result = simulate_portfolio(
        {"A": early, "B": late}, **NO_GATES, max_concurrent_positions=1,
        # 둘 다 "자기 40봉"에 신호를 낸다 — 시각으로는 A가 40번째, B가 100번째다.
        signals_by_symbol={"A": {40: "LONG"}, "B": {40: "LONG"}})

    # 시각으로 맞추면 A는 50봉에 이미 청산됐으므로 B가 100번째 시각에 자리를 받는다.
    # 봉 인덱스로 맞췄다면 둘의 40봉이 같은 시점이 되어 A가 자리를 먼저 가져가고 B는 막힌다.
    assert [t["symbol"] for t in _closed(result)] == ["A"]
    assert result["still_open"] == ["B"]
    assert result["blocked"]["max_positions"] == 0


def test_blocked_entry_changes_the_symbols_later_trades():
    """동시보유 한도의 효과는 **사후에 거래 목록을 걸러내는 것으로 재현할 수 없다** —
    진입이 막히면 그 종목은 포지션을 안 들고 있으므로 다음 신호를 받을 수 있다.
    이것이 종목별 백테스트 합산으로는 안 되는 핵심 이유다."""
    df = _resolving(_resolving(_flat(90), 45), 75)   # 45봉과 75봉에서 손절에 닿는다
    signals = {40: "LONG", 70: "LONG"}

    solo = simulate_portfolio({"A": df}, **NO_GATES, max_concurrent_positions=5,
                               max_consecutive_losses=None,
                               signals_by_symbol={"A": signals})
    # B가 40봉에 자리를 먼저 채워 A의 진입이 전부 막히는 경우와 비교한다.
    shared = simulate_portfolio({"B": _flat(90), "A": df}, **NO_GATES,
                                 max_concurrent_positions=1, max_consecutive_losses=None,
                                 signals_by_symbol={"B": {40: "LONG"}, "A": signals})

    assert len(solo["trades"]) == 2
    assert _closed(shared) == []            # A의 두 진입이 전부 막혔다
    assert shared["blocked"]["max_positions"] == 2


def test_trades_by_symbol_groups_for_the_report_helpers():
    df = _resolving(_flat(80), 45)
    result = simulate_portfolio({"A": df, "B": df}, **NO_GATES,
                                 max_concurrent_positions=5, max_consecutive_losses=None,
                                 signals_by_symbol={"A": {40: "LONG"}, "B": {40: "LONG"}})

    grouped = trades_by_symbol(result)

    assert set(grouped) == {"A", "B"}
    assert all(len(v) == 1 for v in grouped.values())


def test_records_concurrency_at_entry_on_each_trade():
    """동시보유 상한을 정하는 근거가 정확히 "동시보유가 많을 때의 거래가 더 좋은가"이므로,
    거래마다 진입 순간의 동시보유 수가 남아 있어야 한다."""
    df = _resolving(_flat(80), 45)
    result = simulate_portfolio({"A": df, "B": df}, **NO_GATES,
                                 max_concurrent_positions=5, max_consecutive_losses=None,
                                 signals_by_symbol={"A": {40: "LONG"}, "B": {40: "LONG"}})

    assert sorted(t["concurrent_at_entry"] for t in result["trades"]) == [1, 2]


def test_fee_and_slippage_match_the_single_symbol_engine():
    """포트폴리오 경로와 단일 종목 경로가 같은 비용 계산을 써야 한다 — 어긋나면 두 경로의
    숫자를 비교하는 것 자체가 무의미해진다."""
    from src.backtest.engine import run_backtest

    df = _resolving(_flat(90), 45)
    common = dict(regime_sma_period=0, min_atr_to_stop_ratio=0.0, adx_threshold=1,
                  stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0,
                  fee_pct_per_side=0.0004, slippage_r_per_side=0.1)

    single = run_backtest(df, **common, signal_fn=lambda w: "LONG" if len(w) == 41 else None)
    port = simulate_portfolio(
        {"A": df}, max_concurrent_positions=5, max_consecutive_losses=None,
        signals_by_symbol={"A": {40: "LONG"}},
        regime_sma_period=0, min_atr_to_stop_ratio=0.0, adx_threshold=1,
        stop_loss_pct=0.01, take_profit_rr=2.0,
        fee_pct_per_side=0.0004, slippage_r_per_side=0.1)

    assert single and port["trades"]
    assert port["trades"][0]["pnl_r"] == pytest.approx(single[0]["pnl_r"])


# ------------------------------- 서킷브레이커 리셋 가정 (2026-09-22)
# 실거래에서 연속손실 브레이커는 "걸쇠"다 — 한도에 닿으면 신규 진입이 막히고, 막히면 새 청산이
# 안 생기므로 카운터가 저절로 안 내려간다. 사람이 수동 리셋을 누르거나 이미 열린 포지션이
# 이익으로 닫혀야 풀린다(실제 저널에 수동 리셋이 6주간 6~7회 찍혀 있다). 그래서 365일
# 시뮬레이션은 그 개입 주기를 **명시적인 가정**으로 만들어야 한다.


def _framed(df, stamps):
    df = df.copy()
    df["timestamp"] = stamps
    return df


def _loss_streak_setup(n=200):
    """1일차에 연속손실 2건을 만들고, 1일차와 2일차에 각각 후보 신호를 두는 구성.

    09-01 00:00부터 1시간봉이므로 봉 0~23이 09-01, 24~47이 09-02다."""
    stamps = pd.date_range("2026-09-01", periods=n, freq="h")
    dfs = {
        "A": _framed(_resolving(_flat(n), 41), stamps),   # 40봉 진입 -> 41봉 손절 (09-02)
        "B": _framed(_resolving(_flat(n), 46), stamps),   # 45봉 진입 -> 46봉 손절 (09-02)
        "SAME_DAY": _framed(_flat(n), stamps),            # 47봉 신호: 같은 날
        "NEXT_DAY": _framed(_flat(n), stamps),            # 60봉 신호: 다음 날(09-03)
    }
    signals = {"A": {40: "LONG"}, "B": {45: "LONG"},
               "SAME_DAY": {47: "LONG"}, "NEXT_DAY": {60: "LONG"}}
    return dfs, signals


def test_breaker_never_resets_latches_and_blocks_forever():
    """실거래 코드 그대로의 동작 — 한 번 걸리면 이후 후보가 전부 막힌다. 이 모드로 상한을
    비교하면 안 된다(정지 이후엔 상한이 결과에 영향을 주지 않는다)."""
    dfs, signals = _loss_streak_setup()

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                 max_consecutive_losses=2, breaker_reset="never",
                                 max_daily_loss_r=0, signals_by_symbol=signals)

    assert result["still_open"] == []            # 둘 다 막혔다
    assert result["blocked"]["consecutive_losses"] >= 2
    assert result["breaker_resets_needed"] == 0


def test_breaker_daily_reset_lets_the_next_day_trade_again():
    """기본 가정 — 사람이 하루에 한 번은 확인한다고 보고 날짜 경계에서 카운터를 되돌린다."""
    dfs, signals = _loss_streak_setup()

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                 max_consecutive_losses=2, breaker_reset="daily",
                                 max_daily_loss_r=0, signals_by_symbol=signals)

    # 같은 날 후보는 막히고, 다음 날 후보는 받는다.
    assert result["still_open"] == ["NEXT_DAY"]
    assert result["blocked"]["consecutive_losses"] >= 1
    assert result["breaker_resets_needed"] >= 1


def test_breaker_off_ignores_the_consecutive_loss_rule():
    dfs, signals = _loss_streak_setup()

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                 max_consecutive_losses=2, breaker_reset="off",
                                 max_daily_loss_r=0, signals_by_symbol=signals)

    assert sorted(result["still_open"]) == ["NEXT_DAY", "SAME_DAY"]
    assert result["blocked"]["consecutive_losses"] == 0


def test_breaker_reset_choice_changes_the_result_so_it_must_be_reported():
    """리셋 가정이 결과를 바꾼다는 것 자체를 고정한다 — 이 가정을 안 밝히고 낸 숫자는
    "상한이 8이 좋다" 같은 결론의 근거가 될 수 없다."""
    dfs, signals = _loss_streak_setup()
    common = dict(NO_GATES, max_concurrent_positions=5, max_consecutive_losses=2,
                  max_daily_loss_r=0)

    never = simulate_portfolio(dfs, **common, breaker_reset="never", signals_by_symbol=signals)
    daily = simulate_portfolio(dfs, **common, breaker_reset="daily", signals_by_symbol=signals)

    assert never["still_open"] != daily["still_open"]


def test_breaker_resets_are_not_counted_when_the_counter_was_already_clear():
    """날짜가 바뀌어도 연속손실이 0이었으면 "리셋이 필요했다"고 세지 않는다 — 이 수를 저널의
    실측 리셋 횟수(6주에 6~7회)와 비교하려면 실제로 걸려 있던 경우만 세야 한다."""
    n = 200
    stamps = pd.date_range("2026-09-01", periods=n, freq="h")
    dfs = {"A": _framed(_flat(n), stamps)}      # 손실이 아예 없다

    result = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                 max_consecutive_losses=2, breaker_reset="daily",
                                 signals_by_symbol={"A": {40: "LONG"}})

    assert result["breaker_resets_needed"] == 0


# ---------- 부분 익절 (2026-09-24 포팅) ----------
# `run_backtest`에만 있던 스케일 아웃을 포트폴리오 경로로 옮긴 것이라, 여기서 지키는 것은
# **두 경로가 같은 답을 내는가**와 **비용을 제대로 무는가** 둘이다. 후자가 중요한 이유:
# 포팅 전 이 경로는 슬리피지를 아예 무시했고, 그대로 게이트에 넣으면 부분 익절만 비용을
# 안 내는 셈이라 스트레스 판정이 통과해도 의미가 없다.

_PARTIAL = dict(use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5,
                breakeven_after_partial=True)


def _partial_then_breakeven_df(n=60, entry_bar=40, price=100.0):
    """진입 -> 다음 봉에 +1R 터치(부분 익절) -> 그다음 봉에 진입가로 되돌아옴(손익분기 손절)."""
    df = _flat(n, price)
    # 여유를 넉넉히 둔다 — 슬리피지를 켜면 진입가와 목표가가 조금씩 밀리는데, 봉을 빠듯하게
    # 잡으면 "비용 때문에 R이 줄었다"가 아니라 "이벤트 자체가 안 일어났다"가 되어 버린다.
    df.loc[entry_bar + 1, "high"] = price * 1.015         # 손절폭 1% -> +1R = 101
    df.loc[entry_bar + 2, "low"] = price * 0.995          # 진입가(≈100) 아래로 되돌아옴
    return df


def test_partial_tp_banks_half_then_stops_at_breakeven():
    """절반을 +1R에 확정하고 나머지는 본전에 끊기면 최종 +0.5R이다.

    손익분기 이동(E0013~16)이 전략을 망가뜨린 이유는 **익절을 0으로 바꿔서**였는데, 부분
    익절은 절반을 먼저 확정하므로 그 실패 방식을 피한다 — 그게 이 경로를 여는 이유다."""
    df = _partial_then_breakeven_df()
    result = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                                 **NO_GATES, **_PARTIAL)
    assert [t["reason"] for t in result["trades"]] == ["breakeven_stop"]
    assert result["trades"][0]["pnl_r"] == pytest.approx(0.5)


def test_partial_tp_matches_the_single_symbol_engine():
    """동시보유 제약이 걸리지 않는 한 종목이면 `run_backtest`와 같은 답이어야 한다 —
    두 경로가 갈라지면 원장의 행끼리 비교가 안 된다."""
    from src.backtest.engine import run_backtest

    df = _partial_then_breakeven_df()
    engine_trades = run_backtest(
        df, signal_fn=lambda w: "LONG" if len(w) == 41 else None,
        regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0,
        stop_loss_pct=0.01, take_profit_rr=2.0, **_PARTIAL)
    port = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                               **NO_GATES, **_PARTIAL)

    assert len(engine_trades) == len(port["trades"]) == 1
    assert engine_trades[0]["reason"] == port["trades"][0]["reason"]
    assert engine_trades[0]["pnl_r"] == pytest.approx(port["trades"][0]["pnl_r"])


def test_partial_tp_charges_fees_on_both_legs():
    """수수료는 다리마다 비중만큼 물어서 합계가 왕복 1회분이다."""
    df = _partial_then_breakeven_df()
    gates = {**NO_GATES, "fee_pct_per_side": 0.001}   # 손절폭 1% -> 왕복 0.2R
    result = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                                 **gates, **_PARTIAL)
    # 0.5*(1.0 - 0.2) + 0.5*(0.0 - 0.2) = 0.4 - 0.1 = 0.3
    assert result["trades"][0]["pnl_r"] == pytest.approx(0.3)


def test_partial_tp_is_charged_for_slippage():
    """포팅 전 이 경로는 슬리피지를 아예 무시했다 — 게이트의 스트레스 판정이 무의미해진다."""
    df = _partial_then_breakeven_df()
    clean = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                                **NO_GATES, **_PARTIAL)
    slipped = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                                  **NO_GATES, **_PARTIAL, slippage_r_per_side=0.05)
    assert slipped["trades"][0]["pnl_r"] < clean["trades"][0]["pnl_r"]


def test_partial_tp_full_stop_before_partial_is_a_normal_loss():
    """부분 익절에 닿기 전에 손절되면 평소와 같은 -1R이다(적립된 R이 없으므로)."""
    df = _flat(60)
    df.loc[41, "low"] = 100 * 0.99 - 0.01
    result = simulate_portfolio({"A": df}, signals_by_symbol={"A": {40: "LONG"}},
                                 **NO_GATES, **_PARTIAL)
    assert result["trades"][0]["reason"] == "stop_loss"
    assert result["trades"][0]["pnl_r"] == pytest.approx(-1.0)


def test_positions_open_at_the_end_are_closed_at_the_last_close():
    """외부 검토 3.7(2026-10-03): 구간 끝에 열려 있는 포지션을 빼면 결과가 그 포지션의 손익
    방향으로 치우친다. 마지막 종가로 시장가 청산해 성과에 넣는다."""
    df, signals = _with_signal_at(60, bars=[40])
    df.loc[59, "close"] = 101.0   # 손절·익절에는 안 닿았고 마지막 종가는 진입가보다 높다

    result = simulate_portfolio({"A": df}, **NO_GATES, signals_by_symbol={"A": signals})

    trade, = result["trades"]
    assert trade["reason"] == "end_of_data"
    assert trade["exit_index"] == 59
    assert trade["pnl_r"] > 0
    assert result["still_open"] == ["A"]


def test_cooldown_breaker_follows_the_live_rule_not_midnight():
    """외부 검토 3.2: 기본 브레이커는 실거래와 같은 규칙이다 — 날짜가 바뀌어도 풀리지 않고,
    마지막 손실 뒤 쿨다운 시간이 지나야 풀린다."""
    dfs, signals = _loss_streak_setup()

    long_cooldown = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                       max_consecutive_losses=2, cooldown_hours=1000,
                                       max_daily_loss_r=0, signals_by_symbol=signals)
    daily = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                               max_consecutive_losses=2, breaker_reset="daily",
                               max_daily_loss_r=0, signals_by_symbol=signals)

    # 마지막 손실(46봉) 뒤 14시간에 NEXT_DAY 신호(60봉)가 온다.
    short_cooldown = simulate_portfolio(dfs, **NO_GATES, max_concurrent_positions=5,
                                        max_consecutive_losses=2, cooldown_hours=10,
                                        max_daily_loss_r=0, signals_by_symbol=signals)

    assert long_cooldown["still_open"] == []          # 다음 날 후보도 막혔다
    assert short_cooldown["still_open"] == ["NEXT_DAY"]   # 10시간이 지나 풀렸다
    assert daily["still_open"] == ["NEXT_DAY"]
