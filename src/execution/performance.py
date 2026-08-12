def summarize_performance(entries: list[dict]) -> dict:
    """저널 항목 목록에서 event=="closed"인 것만 골라 대시보드용 성과 요약을 만든다.

    R배수가 아니라 실현손익($) 기준이다 — R배수는 진입 시점의 손절폭(entry_info)까지 다시
    매칭해야 해서 더 정확하지만, 대시보드는 "실제로 얼마 벌고 잃었는지"를 보여주는 용도라
    달러 기준이 더 직관적이고 매칭 없이 바로 계산 가능하다."""
    closed = [e for e in entries if e.get("event") == "closed"]

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
