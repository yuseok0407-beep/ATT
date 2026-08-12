from src.monitoring.summary import format_summary


def test_format_summary_shows_circuit_breaker_block():
    cycle = {"results": [{"event": "circuit_breaker_blocked", "reason": "일일 손실 한도 초과"}]}
    text = format_summary(cycle)
    assert "서킷 브레이커" in text
    assert "일일 손실 한도 초과" in text


def test_format_summary_shows_portfolio_and_decisions():
    cycle = {
        "portfolio_value": 76842.15,
        "daily_pnl_pct": -0.012,
        "regime": "TRENDING_UP",
        "results": [
            {
                "symbol": "BTC/USDT",
                "decision": {"decision": "NO_TRADE", "confidence": 0.35, "reasoning": "signal too weak to act on"},
            },
            {
                "symbol": "ETH/USDT",
                "decision": {"decision": "BUY", "confidence": 0.8, "reasoning": "clear uptrend"},
                "execution": {"status": "filled", "side": "buy", "quantity": 0.5},
            },
        ],
    }
    text = format_summary(cycle)
    assert "76,842.15" in text
    assert "-1.20%" in text
    assert "TRENDING_UP" in text
    assert "BTC/USDT" in text
    assert "NO_TRADE" in text
    assert "필터" not in text  # sanity: no stray placeholder text
    assert "filled" in text


def test_format_summary_truncates_long_reasoning():
    cycle = {
        "portfolio_value": 1000,
        "daily_pnl_pct": 0.0,
        "regime": "RANGING",
        "results": [
            {
                "symbol": "BTC/USDT",
                "decision": {"decision": "NO_TRADE", "confidence": 0.5, "reasoning": "x" * 100},
            },
        ],
    }
    text = format_summary(cycle)
    assert "…" in text
    assert "x" * 100 not in text


def test_format_summary_shows_rejected_orders():
    cycle = {
        "portfolio_value": 1000,
        "daily_pnl_pct": 0.0,
        "regime": "RANGING",
        "results": [
            {"symbol": "BTC/USDT", "event": "order_rejected", "reason": "단일 주문 규모 한도 초과"},
        ],
    }
    text = format_summary(cycle)
    assert "REJECTED" in text
    assert "단일 주문 규모 한도 초과" in text
