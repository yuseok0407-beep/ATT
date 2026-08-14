import pytest

from src.execution.performance import summarize_performance


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
