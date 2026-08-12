import json

import anthropic

from src.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CONFIDENCE_THRESHOLD, LEVERAGE, STOP_LOSS_PCT

SYSTEM_PROMPT = f"""You are a disciplined leveraged futures trading agent trading BTC/USDT perpetual
futures at {LEVERAGE}x leverage. Leverage amplifies both gains and losses — a small adverse move can
wipe out the position's margin, so caution is more important here than in spot trading.

Rules you must follow:
- Stop-loss distance is fixed by policy at {STOP_LOSS_PCT:.2%} from entry and is NOT yours to set —
  you only decide direction and conviction.
- If there is currently NO open position, valid decisions are LONG, SHORT, or NO_TRADE.
- If there IS an open position, valid decisions are CLOSE (exit now) or HOLD (let the existing
  stop-loss manage the trade) — never LONG/SHORT again until the position is closed.
- When the risk context says trading is blocked (circuit breaker), you must output NO_TRADE (flat)
  or HOLD (in position) regardless of market view.
- Prefer NO_TRADE / HOLD over a low-conviction directional call — leverage punishes being wrong.
- You must call the record_futures_decision tool exactly once.
"""

FUTURES_DECISION_TOOL = {
    "name": "record_futures_decision",
    "description": "Record the futures trading decision for this cycle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["LONG", "SHORT", "CLOSE", "HOLD", "NO_TRADE"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string"},
        },
        "required": ["decision", "confidence", "reasoning"],
    },
}


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def decide(client: anthropic.Anthropic, snapshot: dict, account_ctx: dict, risk_context: dict,
           model: str = CLAUDE_MODEL) -> dict:
    payload = {"market": snapshot, "account": account_ctx, "risk": risk_context}

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[FUTURES_DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_futures_decision"},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return tool_use.input


def validate_decision(decision: dict, has_position: bool) -> dict:
    """포지션 유무에 맞지 않는 결정(예: 이미 포지션이 있는데 LONG)을 안전한 기본값으로 강등한다."""
    action = decision["decision"]
    if has_position and action in ("LONG", "SHORT"):
        return {**decision, "decision": "HOLD",
                "reasoning": f"[invalid: already in position] {decision['reasoning']}"}
    if not has_position and action == "CLOSE":
        return {**decision, "decision": "NO_TRADE",
                "reasoning": f"[invalid: no open position] {decision['reasoning']}"}
    return decision


def apply_confidence_gate(decision: dict, threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    """확신도가 낮으면 방향성 있는 결정(LONG/SHORT/CLOSE)을 더 보수적인 기본값으로 강등한다.
    포지션이 없을 때는 NO_TRADE로, 포지션이 있을 때(CLOSE)는 기존 손절이 관리하도록 HOLD로."""
    action = decision["decision"]
    if decision["confidence"] >= threshold or action in ("HOLD", "NO_TRADE"):
        return decision

    downgraded = "HOLD" if action == "CLOSE" else "NO_TRADE"
    return {**decision, "decision": downgraded,
            "reasoning": f"[confidence gate] {decision['confidence']:.2f} < {threshold:.2f}. " + decision["reasoning"]}
