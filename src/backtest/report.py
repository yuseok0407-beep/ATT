def summarize(trades: list[dict]) -> dict:
    """거래 목록을 요약 통계로 변환한다. 전부 R배수(위험 대비 손익) 기준 — 포지션 사이징/레버리지는
    실거래 봇의 리스크 로직(FUTURES_RISK_PER_TRADE)이 이미 담당하므로 백테스트 범위 밖."""
    if not trades:
        return {
            "num_trades": 0, "win_rate": None, "avg_r": None,
            "total_r": 0.0, "max_drawdown_r": 0.0, "avg_hold_bars": None,
        }

    pnls = [t["pnl_r"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    return {
        "num_trades": len(trades),
        "win_rate": wins / len(trades),
        "avg_r": sum(pnls) / len(pnls),
        "total_r": sum(pnls),
        "max_drawdown_r": max_dd,
        "avg_hold_bars": sum(t["hold_bars"] for t in trades) / len(trades),
    }
