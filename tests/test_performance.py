import pytest

from datetime import datetime, timedelta, timezone

from src.core.config import FEE_PCT_PER_SIDE
from src.execution import performance
from src.execution.performance import (
    _local_day,
    resolve_closed_trades,
    summarize_day,
    summarize_performance,
    summarize_r_performance,
    summarize_recent_issues,
)


def test_summarize_performance_empty_entries():
    result = summarize_performance([])
    assert result == {
        "num_trades": 0, "win_rate": None, "total_realized_pnl": 0.0, "avg_realized_pnl": None,
        "per_symbol": {}, "reason_counts": {},
    }


def test_summarize_performance_ignores_non_closed_events():
    entries = [
        {"event": "entered", "symbol": "BTC/USDT:USDT"},
        {"event": "no_signal", "symbol": "ETH/USDT:USDT"},
    ]
    result = summarize_performance(entries)
    assert result["num_trades"] == 0


def test_summarize_performance_win_rate_and_total_pnl():
    entries = [
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "take_profit", "realized_pnl": 100.0},
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "stop_loss", "realized_pnl": -40.0},
        {"event": "closed", "symbol": "ETH/USDT:USDT", "reason": "take_profit", "realized_pnl": 20.0},
    ]
    result = summarize_performance(entries)

    assert result["num_trades"] == 3
    assert result["win_rate"] == pytest.approx(2 / 3)
    assert result["total_realized_pnl"] == pytest.approx(80.0)
    assert result["avg_realized_pnl"] == pytest.approx(80.0 / 3)


def test_summarize_performance_per_symbol_breakdown():
    entries = [
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "take_profit", "realized_pnl": 100.0},
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "stop_loss", "realized_pnl": -40.0},
        {"event": "closed", "symbol": "ETH/USDT:USDT", "reason": "take_profit", "realized_pnl": 20.0},
    ]
    result = summarize_performance(entries)

    assert result["per_symbol"]["BTC/USDT:USDT"] == {"trades": 2, "wins": 1, "total_pnl": pytest.approx(60.0)}
    assert result["per_symbol"]["ETH/USDT:USDT"] == {"trades": 1, "wins": 1, "total_pnl": pytest.approx(20.0)}


def test_summarize_performance_reason_counts():
    entries = [
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "take_profit", "realized_pnl": 100.0},
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "stop_loss", "realized_pnl": -40.0},
    ]
    result = summarize_performance(entries)
    assert result["reason_counts"] == {"take_profit": 1, "stop_loss": 1}


def test_summarize_performance_treats_missing_realized_pnl_as_zero():
    entries = [{"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "unknown"}]
    result = summarize_performance(entries)
    assert result["total_realized_pnl"] == 0.0
    assert result["win_rate"] == pytest.approx(0.0)  # 0원은 "이김"이 아님


def test_summarize_performance_excludes_manual_trades():
    """실전 버그 재현: 사용자가 테스트로 넣은 수동 거래가 승률/손익/종목별 집계를 오염시켰다 —
    reason=manual인 청산은 이 요약(규칙 기반 전략 성과)에서 완전히 제외되어야 한다."""
    entries = [
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "take_profit", "realized_pnl": 100.0},
        {"event": "closed", "symbol": "TSLA/USDT:USDT", "reason": "manual", "realized_pnl": -1.0087},
        {"event": "closed", "symbol": "BTC/USDT:USDT", "reason": "manual", "realized_pnl": -8.83406},
    ]
    result = summarize_performance(entries)
    assert result["num_trades"] == 1
    assert result["total_realized_pnl"] == pytest.approx(100.0)
    assert "TSLA/USDT:USDT" not in result["per_symbol"]
    assert result["per_symbol"]["BTC/USDT:USDT"]["trades"] == 1



# ---------- summarize_recent_issues (2026-09-09) ----------

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _at(hours_ago, **fields):
    return {"timestamp": (NOW - timedelta(hours=hours_ago)).isoformat(), **fields}


def test_summarize_recent_issues_groups_rejections_by_exchange_code():
    entries = [
        _at(1, event="rejected_exchange_error", symbol="TSLA/USDT:USDT",
            reason='binance {"code":-2027,"msg":"Exceeded the maximum allowable position"}'),
        _at(2, event="rejected_exchange_error", symbol="CRCL/USDT:USDT",
            reason='binance {"code":-2027,"msg":"Exceeded the maximum allowable position"}'),
        _at(3, event="rejected_exchange_error", symbol="ZEC/USDT:USDT",
            reason='binance {"code":-2021,"msg":"Order would immediately trigger."}'),
    ]

    summary = summarize_recent_issues(entries, now=NOW)

    assert summary["rejections"][0]["code"] == "-2027"
    assert summary["rejections"][0]["count"] == 2
    assert summary["rejections"][0]["symbols"] == ["TSLA", "CRCL"]
    assert summary["rejections"][0]["hint"]  # 코드만 보여주면 매번 검색해야 한다
    assert summary["rejections"][1]["code"] == "-2021"


def test_summarize_recent_issues_ignores_anything_older_than_the_window():
    entries = [
        _at(30, event="rejected_exchange_error", symbol="TSLA/USDT:USDT",
            reason='binance {"code":-2027,"msg":"x"}'),
        _at(2, event="rejected_exchange_error", symbol="ZEC/USDT:USDT",
            reason='binance {"code":-2021,"msg":"y"}'),
    ]

    summary = summarize_recent_issues(entries, hours=24, now=NOW)

    assert [r["code"] for r in summary["rejections"]] == ["-2021"]


def test_summarize_recent_issues_labels_non_api_failures_as_network():
    entries = [_at(1, event="rejected_exchange_error", symbol="SOXL/USDT:USDT",
                    reason="binance GET https://fapi.binance.com/fapi/v1/exchangeInfo")]

    assert summarize_recent_issues(entries, now=NOW)["rejections"][0]["code"] == "network"


def test_summarize_recent_issues_counts_circuit_breaker_events_separately():
    entries = [
        _at(1, event="circuit_breaker_blocked", reason="연속 손실 5회"),
        _at(2, event="circuit_breaker_blocked", reason="일일 손실 한도"),
        _at(3, event="entered", symbol="BTC/USDT:USDT"),
    ]

    summary = summarize_recent_issues(entries, now=NOW)

    assert summary["circuit_breaker_count"] == 2
    assert summary["rejections"] == []


def test_summarize_recent_issues_survives_entries_without_a_usable_timestamp():
    entries = [
        {"event": "rejected_exchange_error", "symbol": "BTC/USDT:USDT", "reason": "x"},
        {"timestamp": "not-a-date", "event": "rejected_exchange_error", "symbol": "BTC/USDT:USDT"},
    ]

    assert summarize_recent_issues(entries, now=NOW)["rejections"] == []



# ---------- R배수 성과 / 자산곡선 (2026-09-09) ----------

def _entered(symbol, entry, stop, signal="LONG", ts="2026-09-08T01:00:00+00:00"):
    return {"timestamp": ts, "event": "entered", "symbol": symbol, "signal": signal,
            "entry_price": entry, "stop_loss_price": stop}


def _closed(symbol, entry, exit_price, pnl, ts="2026-09-08T02:00:00+00:00", **extra):
    return {"timestamp": ts, "event": "closed", "symbol": symbol, "reason": "stop_loss",
            "entry_price": entry, "exit_price": exit_price, "realized_pnl": pnl, **extra}


def test_resolve_closed_trades_backfills_r_from_the_matching_entry():
    """2026-09-09 이전 청산 기록엔 방향도 손절가도 없다 — 진입 기록에서 끌어와 R을 복원해야
    새 지표가 몇 주 동안 빈 화면으로 있지 않는다."""
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0),
        _closed("BTC/USDT:USDT", 100.0, 102.0, 20.0),
    ]

    trades = resolve_closed_trades(entries)

    assert len(trades) == 1
    assert trades[0]["side"] == "long"
    assert trades[0]["stop_loss_price"] == 99.0
    assert trades[0]["realized_r"] == pytest.approx(2.0)


def test_resolve_closed_trades_prefers_values_already_on_the_record():
    """새 기록은 청산 시점에 계산된 값을 들고 있다 — 그걸 덮어쓰면 안 된다."""
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0),
        _closed("BTC/USDT:USDT", 100.0, 102.0, 20.0, side="long", stop_loss_price=99.5,
                realized_r=4.0),
    ]

    trade = resolve_closed_trades(entries)[0]

    assert trade["stop_loss_price"] == 99.5
    assert trade["realized_r"] == 4.0


def test_resolve_closed_trades_drops_r_that_contradicts_realized_pnl():
    """실제 사고 재현(2026-09-07 ZEC 재진입 루프): 청산 6건이 같은 "entered" 기록에 묶이면서
    손실 거래인데 R이 +2.6~+3.1로 계산됐다. 부호가 모순되면 그 R은 못 믿는 값이다."""
    entries = [
        _entered("ZEC/USDT:USDT", 1181.86, 1167.08),
        _closed("ZEC/USDT:USDT", 1181.86, 1220.66, -1.53),  # 가격상 +2.6R인데 실현손익은 손실
    ]

    trade = resolve_closed_trades(entries)[0]

    assert trade["realized_r"] is None
    assert trade["realized_pnl"] == -1.53  # 달러 통계에서는 빠지지 않는다


def test_resolve_closed_trades_skips_manual_closes():
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0),
        {"timestamp": "2026-09-08T02:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "manual", "entry_price": 100.0, "exit_price": 101.0, "realized_pnl": 10.0},
    ]

    assert resolve_closed_trades(entries) == []


def test_summarize_r_performance_splits_by_side():
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0, signal="LONG"),
        _closed("BTC/USDT:USDT", 100.0, 102.0, 20.0),
        _entered("ETH/USDT:USDT", 50.0, 50.5, signal="SHORT"),
        _closed("ETH/USDT:USDT", 50.0, 50.5, -5.0),
    ]

    summary = summarize_r_performance(entries)

    assert summary["num_trades"] == 2
    assert summary["total_r"] == pytest.approx(1.0)
    assert summary["by_side"]["long"] == {"trades": 1, "wins": 1, "total_r": pytest.approx(2.0)}
    assert summary["by_side"]["short"]["total_r"] == pytest.approx(-1.0)


def test_summarize_r_performance_builds_a_cumulative_curve():
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0),
        _closed("BTC/USDT:USDT", 100.0, 102.0, 20.0, ts="2026-09-08T02:00:00+00:00"),
        _entered("BTC/USDT:USDT", 100.0, 99.0, ts="2026-09-08T03:00:00+00:00"),
        _closed("BTC/USDT:USDT", 100.0, 99.0, -10.0, ts="2026-09-08T04:00:00+00:00"),
    ]

    curve = summarize_r_performance(entries)["equity_curve"]

    assert [round(p["cumulative_r"], 2) for p in curve] == [2.0, 1.0]
    assert [p["cumulative_pnl"] for p in curve] == [20.0, 10.0]


def test_summarize_r_performance_measures_drawdown_from_the_peak():
    entries = []
    for i, (exit_price, pnl) in enumerate([(102.0, 20.0), (99.0, -10.0), (99.0, -10.0)]):
        ts = f"2026-09-08T0{i}:00:00+00:00"
        entries.append(_entered("BTC/USDT:USDT", 100.0, 99.0, ts=ts))
        entries.append(_closed("BTC/USDT:USDT", 100.0, exit_price, pnl, ts=ts))

    summary = summarize_r_performance(entries)

    assert summary["max_drawdown_r"] == pytest.approx(-2.0)  # +2R 고점에서 0R까지
    assert summary["max_drawdown_usd"] == pytest.approx(-20.0)


def test_summarize_r_performance_counts_losers_that_nearly_won():
    """"익절 코앞까지 갔다가 손절났다"가 실제로 얼마나 되는지 — 원래 질문에 대한 상시 답."""
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0),
        _closed("BTC/USDT:USDT", 100.0, 99.0, -10.0, max_favorable_r=1.6),
        _entered("ETH/USDT:USDT", 50.0, 49.5, ts="2026-09-08T03:00:00+00:00"),
        _closed("ETH/USDT:USDT", 50.0, 49.5, -5.0, ts="2026-09-08T04:00:00+00:00",
                max_favorable_r=0.2),
    ]

    mfe = summarize_r_performance(entries)["mfe"]

    assert mfe["losers_measured"] == 2
    assert mfe["losers_reaching_1r"] == 1


def test_summarize_r_performance_is_empty_without_trades():
    summary = summarize_r_performance([])
    assert summary["num_trades"] == 0
    assert summary["avg_r"] is None
    assert summary["equity_curve"] == []


def test_summarize_r_performance_caps_the_curve_length():
    entries = []
    for i in range(20):
        ts = f"2026-09-08T{i:02d}:00:00+00:00"
        entries.append(_entered("BTC/USDT:USDT", 100.0, 99.0, ts=ts))
        entries.append(_closed("BTC/USDT:USDT", 100.0, 102.0, 20.0, ts=ts))

    assert len(summarize_r_performance(entries, max_points=5)["equity_curve"]) == 5


# ---------- summarize_day (일일 요약용) ----------

def test_summarize_day_counts_only_that_local_day():
    entries = [
        _entered("BTC/USDT:USDT", 100.0, 99.0, ts="2026-09-08T02:00:00+00:00"),
        _closed("BTC/USDT:USDT", 100.0, 102.0, 20.0, ts="2026-09-08T03:00:00+00:00"),
        _entered("ETH/USDT:USDT", 50.0, 49.5, ts="2026-09-20T02:00:00+00:00"),
        _closed("ETH/USDT:USDT", 50.0, 49.5, -5.0, ts="2026-09-20T03:00:00+00:00"),
    ]
    day = _local_day("2026-09-08T03:00:00+00:00")

    stats = summarize_day(entries, day)

    assert stats["trades"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 0
    assert stats["realized_pnl"] == pytest.approx(20.0)
    assert stats["total_r"] == pytest.approx(2.0)


def test_summarize_day_counts_rejections_and_circuit_breakers():
    ts = "2026-09-08T03:00:00+00:00"
    entries = [
        {"timestamp": ts, "event": "rejected_exchange_error", "symbol": "TSLA/USDT:USDT",
         "reason": 'binance {"code":-2027}'},
        {"timestamp": ts, "event": "circuit_breaker_blocked", "reason": "연속 손실"},
    ]

    stats = summarize_day(entries, _local_day(ts))

    assert stats["trades"] == 0
    assert stats["rejections"] == 1
    assert stats["circuit_breakers"] == 1


# --- 설정 변경 경계 --------------------------------------------------------------

def test_config_changes_lists_snapshots_in_order():
    entries = [
        {"timestamp": "2026-09-01T00:00:00+00:00", "event": "config_changed",
         "config": {"regime_sma_period": 0}, "changes": {}, "first_record": True},
        {"timestamp": "2026-09-05T00:00:00+00:00", "event": "closed", "realized_pnl": 1.0},
        {"timestamp": "2026-09-07T00:00:00+00:00", "event": "config_changed",
         "config": {"regime_sma_period": 400},
         "changes": {"regime_sma_period": {"from": 0, "to": 400}}, "first_record": False},
    ]

    changes = performance.config_changes(entries)

    assert [c["timestamp"] for c in changes] == ["2026-09-01T00:00:00+00:00", "2026-09-07T00:00:00+00:00"]
    assert changes[0]["first_record"] is True
    assert changes[1]["changes"]["regime_sma_period"]["to"] == 400


def test_r_performance_reports_only_the_trades_since_the_last_config_change():
    """필터를 넣기 전후가 한 숫자로 뭉뚱그려지면 지금 규칙이 통하는지 알 수 없다 — 마지막
    설정 변경 이후 구간을 따로 센다."""
    entries = [
        {"timestamp": "2026-09-01T00:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-02T00:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "stop_loss", "exit_price": 99.0, "realized_pnl": -10.0},
        {"timestamp": "2026-09-03T00:00:00+00:00", "event": "config_changed",
         "config": {"regime_sma_period": 400},
         "changes": {"regime_sma_period": {"from": 0, "to": 400}}, "first_record": False},
        {"timestamp": "2026-09-04T00:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-05T00:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "take_profit", "exit_price": 102.0, "realized_pnl": 20.0},
    ]

    result = performance.summarize_r_performance(entries)

    assert result["num_trades"] == 2  # 전체는 그대로 둘 다 센다
    assert result["since_config_change"]["trades"] == 1
    assert result["since_config_change"]["total_r"] == pytest.approx(2.0)
    assert result["since_config_change"]["realized_pnl"] == pytest.approx(20.0)
    assert result["since_config_change"]["changes"]["regime_sma_period"]["to"] == 400


def test_r_performance_has_no_config_span_before_the_first_snapshot():
    entries = [
        {"timestamp": "2026-09-01T00:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "stop_loss", "realized_pnl": -10.0},
    ]

    result = performance.summarize_r_performance(entries)

    assert result["config_changes"] == []
    assert result["since_config_change"] is None


# ---------------------------------------------- 수수료 차감 순R (2026-09-22)
# 저널의 realized_r은 가격만으로 재므로 수수료가 안 들어있고, 거래소의 realized_pnl도 수수료를
# 뺀 값이 아니다(commission이 별개 필드). 손절폭 1.25%에서 왕복 수수료는 0.064R인데 이 전략의
# 건당 기대값이 +0.02~0.06R이라, 수수료를 넣으면 총R의 부호가 바뀐다.


def _closed_with_r(realized_r, *, entry=100.0, stop=98.75, pnl=None, **extra):
    """손절폭 1.25%(= entry 100, stop 98.75)인 청산 기록."""
    return {"timestamp": "2026-09-20T01:00:00+00:00", "symbol": "BTC/USDT:USDT", "event": "closed",
            "reason": "take_profit" if realized_r > 0 else "stop_loss", "side": "long",
            "entry_price": entry, "stop_loss_price": stop, "exit_price": entry * 1.02,
            "realized_r": realized_r, "realized_pnl": pnl if pnl is not None else realized_r * 10,
            **extra}


# 옛 기록의 수수료 추정치 — 설정값에서 계산한다(숫자로 박아 두면 수수료율을 실측으로 고칠
# 때마다 테스트가 깨진다, 2026-09-28).
_FEE_R_AT_1_25PCT_STOP = 2 * FEE_PCT_PER_SIDE / 0.0125


def test_net_r_subtracts_the_configured_round_trip_fee_for_old_records():
    """2026-09-22 이전 기록에는 실측 수수료가 없다 — 설정 수수료율과 손절폭으로 추정한다.
    수수료R = 2 x 편도율 / 0.0125 (편도 0.05%면 0.08R)."""
    result = summarize_r_performance([_closed_with_r(2.0)])

    assert result["total_r"] == pytest.approx(2.0)
    assert result["total_net_r"] == pytest.approx(2.0 - _FEE_R_AT_1_25PCT_STOP)
    assert result["net_r_estimated_trades"] == 1
    assert result["fee_pct_per_side"] == pytest.approx(performance.FEE_PCT_PER_SIDE)


def test_net_r_uses_the_actual_recorded_fee_when_the_journal_has_it():
    """실측이 있으면 추정하지 않고 그 값을 쓰고, 추정 건수에도 세지 않는다."""
    result = summarize_r_performance([_closed_with_r(2.0, fee_r=0.03)])

    assert result["total_net_r"] == pytest.approx(2.0 - 0.03)
    assert result["net_r_estimated_trades"] == 0


def test_net_r_prefers_net_realized_r_recorded_by_the_bot():
    result = summarize_r_performance([_closed_with_r(2.0, fee_r=0.03, net_realized_r=1.95)])

    assert result["total_net_r"] == pytest.approx(1.95)


def test_fee_can_flip_a_positive_total_r_negative():
    """이 프로젝트에서 실제로 벌어진 일 — 데모 저널의 총R +2.50이 수수료를 넣으면 음수가 된다.
    수수료가 오차항이 아니라 기대값과 같은 크기라는 것을 고정한다."""
    # 건당 +0.02R짜리 거래 40건: 총 +0.8R, 수수료는 40 x 0.064 = 2.56R.
    entries = [_closed_with_r(0.02) for _ in range(40)]

    result = summarize_r_performance(entries)

    assert result["total_r"] > 0
    assert result["total_net_r"] < 0


def test_equity_curve_carries_both_gross_and_net_r():
    result = summarize_r_performance([_closed_with_r(2.0), _closed_with_r(-1.0)])

    last = result["equity_curve"][-1]
    assert last["cumulative_r"] == pytest.approx(1.0)
    assert last["cumulative_net_r"] == pytest.approx(1.0 - 2 * _FEE_R_AT_1_25PCT_STOP)


def test_net_r_skips_trades_whose_r_was_discarded():
    """R을 못 믿어서 버린 거래는 순R에도 안 들어간다(달러 통계에는 그대로 남는다)."""
    # 가격으로는 +2R인데 실현손익이 음수 -> resolve_closed_trades가 R을 버린다.
    contradictory = _closed_with_r(2.0, pnl=-1.53)
    result = summarize_r_performance([contradictory])

    assert result["num_trades"] == 0
    assert result["total_net_r"] == pytest.approx(0.0)


def test_net_drawdown_is_at_least_as_bad_as_gross():
    entries = [_closed_with_r(1.0), _closed_with_r(-1.0), _closed_with_r(-1.0)]

    result = summarize_r_performance(entries)

    assert result["max_drawdown_net_r"] <= result["max_drawdown_r"]
