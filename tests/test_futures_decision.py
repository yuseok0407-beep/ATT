from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.futures_decision import (
    FUTURES_DECISION_TOOL,
    apply_confidence_gate,
    decide,
    validate_decision,
)


def test_validate_decision_downgrades_long_when_already_in_position():
    decision = {"decision": "LONG", "confidence": 0.9, "reasoning": "strong breakout"}
    result = validate_decision(decision, has_position=True)
    assert result["decision"] == "HOLD"


def test_validate_decision_downgrades_close_when_no_position():
    decision = {"decision": "CLOSE", "confidence": 0.9, "reasoning": "take profit"}
    result = validate_decision(decision, has_position=False)
    assert result["decision"] == "NO_TRADE"


def test_validate_decision_leaves_valid_combos_untouched():
    long_decision = {"decision": "LONG", "confidence": 0.9, "reasoning": "..."}
    assert validate_decision(long_decision, has_position=False)["decision"] == "LONG"

    hold_decision = {"decision": "HOLD", "confidence": 0.9, "reasoning": "..."}
    assert validate_decision(hold_decision, has_position=True)["decision"] == "HOLD"


def test_confidence_gate_downgrades_low_confidence_long_to_no_trade():
    decision = {"decision": "LONG", "confidence": 0.3, "reasoning": "..."}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "NO_TRADE"


def test_confidence_gate_downgrades_low_confidence_close_to_hold():
    decision = {"decision": "CLOSE", "confidence": 0.3, "reasoning": "..."}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "HOLD"


def test_confidence_gate_passes_high_confidence_short():
    decision = {"decision": "SHORT", "confidence": 0.8, "reasoning": "..."}
    result = apply_confidence_gate(decision, threshold=0.6)
    assert result["decision"] == "SHORT"


def test_confidence_gate_leaves_hold_and_no_trade_untouched_regardless_of_confidence():
    for action in ("HOLD", "NO_TRADE"):
        decision = {"decision": action, "confidence": 0.01, "reasoning": "..."}
        assert apply_confidence_gate(decision, threshold=0.6)["decision"] == action


def test_decide_forces_tool_choice_and_parses_result():
    fake_block = SimpleNamespace(
        type="tool_use",
        input={"decision": "LONG", "confidence": 0.75, "reasoning": "uptrend confirmed"},
    )
    fake_response = SimpleNamespace(content=[fake_block])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = fake_response

    result = decide(mock_client, snapshot={"price": 65000}, account_ctx={"margin": 1000}, risk_context={})

    assert result["decision"] == "LONG"
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "record_futures_decision"}
    assert call_kwargs["tools"] == [FUTURES_DECISION_TOOL]
