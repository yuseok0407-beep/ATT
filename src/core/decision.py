import json

import anthropic

from src.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CONFIDENCE_THRESHOLD

SYSTEM_PROMPT = """You are a disciplined algorithmic trading agent managing a crypto portfolio.

Rules you must follow:
- Never recommend risking more than 2% of portfolio equity on a single trade.
- Avoid new entries when RSI is at an extreme (>80 or <20) in the direction of the trade.
- Respect the provided market regime and risk context; when the risk context says trading is
  blocked, you must output NO_TRADE regardless of market view.
- The input includes a rule-based suggested_quantity (base asset units, from the portfolio
  rebalance logic) and suggested_order_value_usdt. Treat these as a starting point, not a
  mandate: reduce, increase, or override them based on your own read of the market snapshot.
- When signals are mixed or unclear, prefer NO_TRADE over a low-conviction trade.
- You must call the record_trade_decision tool exactly once with your decision.
"""

DECISION_TOOL = {
    "name": "record_trade_decision",
    "description": "Record the trading decision for this cycle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["BUY", "SELL", "NO_TRADE"]},
            "symbol": {"type": "string"},
            "quantity": {"type": "number", "minimum": 0, "description": "Order quantity in base asset units (e.g. BTC amount for BTC/USDT), not USD."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string"},
        },
        "required": ["decision", "symbol", "quantity", "confidence", "reasoning"],
    },
}


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def decide(client: anthropic.Anthropic, snapshot: dict, portfolio: dict, risk_context: dict,
           model: str = CLAUDE_MODEL) -> dict:
    """시장 스냅샷 + 포트폴리오 상태 + 리스크 컨텍스트를 Claude에 전달해 매매 결정을 받는다.
    tool_choice로 record_trade_decision 호출을 강제해 항상 스키마에 맞는 JSON을 받는다."""
    payload = {"market": snapshot, "portfolio": portfolio, "risk": risk_context}

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_trade_decision"},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return tool_use.input


def apply_confidence_gate(decision: dict, threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    """confidence가 임계값 미만이면 원래 결정과 무관하게 NO_TRADE로 강등한다."""
    if decision["decision"] != "NO_TRADE" and decision["confidence"] < threshold:
        return {**decision, "decision": "NO_TRADE",
                "reasoning": f"[confidence gate] {decision['confidence']:.2f} < {threshold:.2f}. " + decision["reasoning"]}
    return decision
