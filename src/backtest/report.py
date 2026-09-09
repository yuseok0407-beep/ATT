def aggregate_stats(stats_list: list[dict]) -> dict:
    """summarize() 결과 여러 개(예: 종목별)를 하나로 합산한다. 거래수 가중평균이 아니라 거래
    자체를 다시 합쳐 계산하는 것과 동치가 되도록 total_r/num_trades로부터 win_rate/avg_r을
    재계산한다. max_drawdown_r은 종목마다 독립적으로 발생하므로 합산이 아니라 가장 나쁜 값(최솟값)을
    쓴다 — 여러 종목을 동시에 운용할 때 실제로 겪을 수 있는 최악의 단일 구간 낙폭에 더 가깝다."""
    stats_list = [s for s in stats_list if s["num_trades"] > 0]
    if not stats_list:
        return {
            "num_trades": 0, "win_rate": None, "avg_r": None,
            "total_r": 0.0, "max_drawdown_r": 0.0, "avg_hold_bars": None,
        }

    n = sum(s["num_trades"] for s in stats_list)
    total_r = sum(s["total_r"] for s in stats_list)
    wins = sum(s["win_rate"] * s["num_trades"] for s in stats_list)
    hold_bars = sum(s["avg_hold_bars"] * s["num_trades"] for s in stats_list)

    return {
        "num_trades": n,
        "win_rate": wins / n,
        "avg_r": total_r / n,
        "total_r": total_r,
        "max_drawdown_r": min(s["max_drawdown_r"] for s in stats_list),
        "avg_hold_bars": hold_bars / n,
    }


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
