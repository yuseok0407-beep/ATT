def summarize_performance(entries: list[dict]) -> dict:
    """저널 항목 목록에서 event=="closed"인 것만 골라 대시보드용 성과 요약을 만든다.

    R배수가 아니라 실현손익($) 기준이다 — R배수는 진입 시점의 손절폭(entry_info)까지 다시
    매칭해야 해서 더 정확하지만, 대시보드는 "실제로 얼마 벌고 잃었는지"를 보여주는 용도라
    달러 기준이 더 직관적이고 매칭 없이 바로 계산 가능하다.

    reason=="manual"인 청산(사용자가 직접 넣고 직접 닫은 거래, 테스트 주문 포함)은 제외한다 —
    이 섹션은 "규칙 기반 전략이 얼마나 잘하고 있는지"를 보여주려는 것이라, 전략이 판단조차
    안 한 수동 거래가 승률/손익에 섞이면 안 된다(2026-08-14, 사용자가 테스트 삼아 넣은
    주문이 성과 지표를 오염시킨 사고로 실제 확인됨)."""
    closed = [e for e in entries if e.get("event") == "closed" and e.get("reason") != "manual"]

    per_symbol: dict[str, dict] = {}
    reason_counts: dict[str, int] = {}
    total_pnl = 0.0
    wins = 0

    for entry in closed:
        pnl = entry.get("realized_pnl") or 0.0
        symbol = entry.get("symbol") or "unknown"
        reason = entry.get("reason") or "unknown"

        total_pnl += pnl
        if pnl > 0:
            wins += 1

        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        bucket = per_symbol.setdefault(symbol, {"trades": 0, "wins": 0, "total_pnl": 0.0})
        bucket["trades"] += 1
        bucket["total_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    num_trades = len(closed)
    return {
        "num_trades": num_trades,
        "win_rate": (wins / num_trades) if num_trades else None,
        "total_realized_pnl": total_pnl,
        "avg_realized_pnl": (total_pnl / num_trades) if num_trades else None,
        "per_symbol": per_symbol,
        "reason_counts": reason_counts,
    }
