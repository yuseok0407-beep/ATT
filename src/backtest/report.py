def aggregate_stats(stats_list: list[dict]) -> dict:
    """summarize() 결과 여러 개(예: 종목별)를 하나로 합산한다. 거래수 가중평균이 아니라 거래
    자체를 다시 합쳐 계산하는 것과 동치가 되도록 total_r/num_trades로부터 win_rate/avg_r을
    재계산한다.

    **`max_drawdown_r`은 포트폴리오 낙폭이 아니다** — 종목별 낙폭 중 가장 나쁜 하나일 뿐이고,
    그 값은 실제로 겪는 낙폭을 크게 과소평가한다. 암호화폐 종목은 상관이 높아서 손실이 여러
    종목에 **동시에** 오는데, 이 값은 종목 하나의 곡선만 보므로 그 합쳐진 낙폭을 볼 수 없다
    (종목 3개가 같은 주에 각각 -8R씩 빠지면 실제 낙폭은 -24R인데 이 값은 -8R로 보고한다).

    진짜 포트폴리오 낙폭은 거래를 **청산 시각 순으로 하나의 곡선에 합쳐야** 계산되므로 요약
    통계만으로는 불가능하다 — `portfolio_stats(trades_by_symbol)`을 쓸 것. 이 함수는 그 값을
    `max_drawdown_r_worst_symbol`이라는 이름으로도 같이 돌려주므로, 어떤 수를 보고 있는지
    호출자가 헷갈리지 않게 한다(옛 이름은 호환을 위해 남겨둔다)."""
    stats_list = [s for s in stats_list if s["num_trades"] > 0]
    if not stats_list:
        return {
            "num_trades": 0, "win_rate": None, "avg_r": None,
            "total_r": 0.0, "max_drawdown_r": 0.0,
            "max_drawdown_r_worst_symbol": 0.0, "avg_hold_bars": None,
        }

    n = sum(s["num_trades"] for s in stats_list)
    total_r = sum(s["total_r"] for s in stats_list)
    wins = sum(s["win_rate"] * s["num_trades"] for s in stats_list)
    hold_bars = sum(s["avg_hold_bars"] * s["num_trades"] for s in stats_list)

    worst_symbol_dd = min(s["max_drawdown_r"] for s in stats_list)
    return {
        "num_trades": n,
        "win_rate": wins / n,
        "avg_r": total_r / n,
        "total_r": total_r,
        "max_drawdown_r": worst_symbol_dd,
        "max_drawdown_r_worst_symbol": worst_symbol_dd,
        "avg_hold_bars": hold_bars / n,
    }


def portfolio_stats(trades_by_symbol: dict[str, list[dict]]) -> dict:
    """종목별 거래 목록을 **청산 시각 순으로 하나의 자산곡선에 합쳐** 포트폴리오 통계를 낸다.

    `aggregate_stats`가 못 하는 일을 한다: 손실이 여러 종목에 동시에 도착할 때의 합쳐진 낙폭.
    종목별 낙폭의 최솟값(`aggregate_stats`의 max_drawdown_r)은 상관이 높은 유니버스에서 실제
    낙폭을 크게 과소평가하고, 그 수를 리스크 한도 판단에 쓰면 감당 가능한 낙폭을 잘못 본다.

    순서 기준은 청산 시점이다 — R은 그때 확정되므로 자산곡선에 반영되는 시각도 그때다. 진입
    시각으로 세면 아직 확정되지 않은 손익을 곡선에 먼저 얹는 셈이 된다.

    **`exit_step`이 있으면 그것을 쓰고, 없으면 `exit_index`로 떨어진다.** 봉 인덱스는 종목마다
    상장일이 달라 서로 비교할 수 없다(SOXL의 100번째 봉과 BTC의 100번째 봉은 다른 시각이다) —
    `portfolio.simulate_portfolio`는 통합 타임라인 상의 위치를 `exit_step`으로 실어 주므로 그걸
    쓰면 순서가 정확해진다. 한 종목의 `run_backtest` 결과만 넘기는 경우에는 둘이 같다.

    **동시보유 한도와 서킷브레이커는 여기서 적용하지 않는다** — 그건 신호를 버리는 규칙이라
    이미 청산된 거래 목록을 받는 이 함수 밖에서, 진입을 만들 때 걸러야 한다. 이 함수는
    "주어진 거래들을 한 계좌에서 굴렸다면 자산곡선이 어땠나"만 계산한다.

    trades_by_symbol의 각 거래는 run_backtest가 돌려주는 형식(`pnl_r`, `exit_index`)을 따른다."""
    merged = []
    for symbol, trades in trades_by_symbol.items():
        for trade in trades:
            order = trade.get("exit_step")
            if order is None:
                order = trade.get("exit_index", 0)
            merged.append((order, symbol, trade))
    merged.sort(key=lambda row: row[0])

    if not merged:
        return {
            "num_trades": 0, "win_rate": None, "avg_r": None, "total_r": 0.0,
            "max_drawdown_r": 0.0, "recovery_trades": None, "equity_curve": [],
        }

    cum = peak = 0.0
    max_dd = 0.0
    trough_at = peak_at = 0
    worst_recovery = 0
    curve = []
    for i, (_, _, trade) in enumerate(merged):
        cum += trade["pnl_r"]
        # `>=`인 이유: 직전 고점과 **같은** 값으로 돌아온 것도 회복이다. `>`로 두면 손실을
        # 정확히 만회한 곡선(+2 -1 -1 +2)이 영원히 미회복으로 남는다.
        if cum >= peak:
            # 고점을 회복한 순간에만 "이번 낙폭이 몇 거래 만에 회복됐는지"가 확정된다.
            if trough_at > peak_at:
                worst_recovery = max(worst_recovery, i - peak_at)
            peak, peak_at = cum, i
        if cum - peak < max_dd:
            max_dd = cum - peak
            trough_at = i
        curve.append(cum)

    pnls = [trade["pnl_r"] for _, _, trade in merged]
    return {
        "num_trades": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "avg_r": sum(pnls) / len(pnls),
        "total_r": sum(pnls),
        "max_drawdown_r": max_dd,
        # 아직 고점을 회복하지 못했으면 None — "회복까지 N거래"라고 단정할 수 없다.
        "recovery_trades": worst_recovery if peak_at >= trough_at else None,
        "equity_curve": curve,
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
