from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.decision import DECISION_TOOL, apply_confidence_gate, decide


def test_confidence_gate_passes_high_confidence():
    decision = {"decision": "BUY", "symbol": "BTC/USDT", "quantity": 1, "confidence": 0.9, "reasoning": "strong trend"}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "BUY"


def test_confidence_gate_downgrades_low_confidence():
    decision = {"decision": "BUY", "symbol": "BTC/USDT", "quantity": 1, "confidence": 0.4, "reasoning": "unclear"}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "NO_TRADE"


def test_confidence_gate_leaves_no_trade_untouched():
    decision = {"decision": "NO_TRADE", "symbol": "BTC/USDT", "quantity": 0, "confidence": 0.1, "reasoning": "n/a"}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "NO_TRADE"


def test_decide_calls_api_with_forced_tool_choice_and_parses_result():
    fake_tool_use_block = SimpleNamespace(
        type="tool_use",
        input={"decision": "BUY", "symbol": "BTC/USDT", "quantity": 0.01, "confidence": 0.8, "reasoning": "uptrend"},
    )
    fake_response = SimpleNamespace(content=[fake_tool_use_block])

    mock_client = MagicMock()
    mock_client.messages.create.return_value = fake_response

    result = decide(mock_client, snapshot={"price": 65000}, portfolio={"USDT": 1000}, risk_context={"allowed": True})

    assert result["decision"] == "BUY"
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "record_trade_decision"}
    assert call_kwargs["tools"] == [DECISION_TOOL]
