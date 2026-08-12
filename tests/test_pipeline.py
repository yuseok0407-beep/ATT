from unittest.mock import MagicMock, patch

from src import pipeline


def test_run_cycle_blocked_by_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr("src.execution.journal.DEFAULT_PATH", str(tmp_path / "trades.jsonl"))

    with patch("src.pipeline.get_client"), patch("src.pipeline.decision.get_client"), \
         patch("src.pipeline.get_asset_values", return_value={"BTC/USDT": 0, "ETH/USDT": 0, "USDT": 1000}), \
         patch("src.pipeline.build_snapshot") as mock_snapshot:
        mock_snapshot.return_value = {
            "symbol": "BTC/USDT", "price": 65000, "regime": {"label": "RANGING"},
        }
        cycle = pipeline.run_cycle(consecutive_losses=0, daily_pnl_pct=-0.10)

    assert len(cycle["results"]) == 1
    assert cycle["results"][0]["event"] == "circuit_breaker_blocked"
    assert cycle["regime"] == "RANGING"


def test_run_cycle_executes_approved_buy(tmp_path, monkeypatch):
    monkeypatch.setattr("src.execution.journal.DEFAULT_PATH", str(tmp_path / "trades.jsonl"))

    mock_trading_client = MagicMock()
    mock_trading_client.create_order.return_value = {"id": "abc"}
    mock_claude_client = MagicMock()

    fake_decision = {
        "decision": "BUY", "symbol": "BTC/USDT", "quantity": 0.01,
        "confidence": 0.9, "reasoning": "strong uptrend",
    }

    with patch("src.pipeline.get_client", return_value=mock_trading_client), \
         patch("src.pipeline.decision.get_client", return_value=mock_claude_client), \
         patch("src.pipeline.get_asset_values", return_value={"BTC/USDT": 0, "ETH/USDT": 0, "USDT": 1000}), \
         patch("src.pipeline.build_snapshot") as mock_snapshot, \
         patch("src.pipeline.decision.decide", return_value=fake_decision):
        mock_snapshot.return_value = {
            "symbol": "BTC/USDT", "price": 65000, "regime": {"label": "TRENDING_UP"},
        }
        cycle = pipeline.run_cycle(consecutive_losses=0, daily_pnl_pct=0.0)

    # BTC/USDT and ETH/USDT are both underweight vs. the TRENDING_UP target, so both
    # get rebalanced; the mocked decision approves a BUY for whichever symbol is asked.
    buy_results = [r for r in cycle["results"] if r.get("symbol") == "BTC/USDT"]
    assert len(buy_results) == 1
    assert buy_results[0]["decision"]["decision"] == "BUY"
    assert buy_results[0]["execution"]["status"] == "filled"
    mock_trading_client.create_order.assert_any_call("BTC/USDT", type="market", side="buy", amount=0.01)
