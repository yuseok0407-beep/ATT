"""R로 잰 거래 목록을 "거래당 리스크 x%로 굴렸다면 계좌가 어떻게 됐나"로 바꾼다.

백테스트와 게이트는 전부 R(손절폭 = 1)로 말한다. 사람이 결정하는 것은 **거래당 계좌의 몇 %를
걸 것인가**이고, 그 결과는 자산(%)으로 나온다. 둘 사이의 환산이 이 모듈이다.

실거래와 같은 **복리**로 계산한다 — 봇은 매 진입마다 그때의 자산에 리스크 비율을 곱해 수량을
정하므로(`futures_risk.leveraged_position_size`), 거래 한 건의 자산 변화는
`자산 x (1 + 리스크 x R)`이다. 거래 순서는 청산 시각이다(자산이 실제로 바뀌는 순간).

**리스크 비율은 결과를 거의 비례로 키우거나 줄일 뿐, 전략이 이기는지 지는지는 못 바꾼다.**
기대값이 0인 전략은 어느 비율에서도 0 근처이고, 비율을 키우면 낙폭만 커진다. 이 모듈이 답하는
질문은 "엣지가 백테스트만큼 있다면, 목표(월 +1%)에 닿는 가장 작은 비율은 얼마이고 그때 얼마나
빠질 수 있나"다.
"""

import math


def equity_path(trades: list[dict], risk_per_trade: float) -> list[float]:
    """자산 배수의 경로(시작 1.0, 청산마다 한 점). trades는 `pnl_r`, `exit_step`, `entry_step`을 가진 dict.

    **걸린 금액은 진입 시점의 자산으로 정한다**(2026-10-03, 외부 검토 3.7). 봇은 진입할 때 그때의
    자산 x 리스크 비율로 수량을 정한다 — 예전 계산(`자산 x (1 + 리스크 x R)`을 청산 순서로 곱하기)은
    동시에 열린 포지션의 크기를 **먼저 닫힌 포지션의 결과가 반영된 자산**으로 다시 정한 셈이었다
    (위험 10%로 동시에 연 두 포지션이 둘 다 -1R이면 0.80이어야 하는데 0.81이 나왔다).
    같은 시점에는 청산을 먼저, 진입을 나중에 처리한다(포트폴리오 시뮬레이션의 순서와 같다).
    `entry_step`이 없는 거래 목록(옛 결과)은 옛 방식으로 계산한다.

    한계: 실현 손익만 따라간다 — 보유 중 평가손익(시가평가)은 이 경로에 없다."""
    if any("entry_step" not in t for t in trades):
        ordered = sorted(trades, key=lambda t: t.get("exit_step", 0))
        equity = 1.0
        path = [equity]
        for trade in ordered:
            equity *= 1.0 + risk_per_trade * trade["pnl_r"]
            equity = max(equity, 0.0)
            path.append(equity)
        return path

    events = ([(t["exit_step"], 0, i) for i, t in enumerate(trades)]
              + [(t["entry_step"], 1, i) for i, t in enumerate(trades)])
    equity = 1.0
    path = [equity]
    stake: dict[int, float] = {}
    for _, kind, i in sorted(events):
        if kind == 1:
            stake.setdefault(i, risk_per_trade * equity)
            continue
        # 진입한 시점과 같은 시점에 청산되는 거래(구간 끝 정리)는 지금 자산으로 건다.
        amount = stake.pop(i, risk_per_trade * equity)
        equity = max(equity + amount * trades[i]["pnl_r"], 0.0)
        path.append(equity)
    return path


def max_drawdown_pct(path: list[float]) -> float:
    """고점 대비 최대 하락률(0.10 = -10%). 양수로 돌려준다."""
    peak, worst = path[0], 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1.0 - value / peak)
    return worst


def months_to_recover(drawdown: float, monthly_return: float = 0.01) -> float | None:
    """drawdown만큼 빠진 뒤 월 monthly_return 복리로 원금을 되찾는 데 걸리는 달 수."""
    if drawdown <= 0:
        return 0.0
    if drawdown >= 1 or monthly_return <= 0:
        return None
    return math.log(1.0 / (1.0 - drawdown)) / math.log(1.0 + monthly_return)


def sizing_stats(trades: list[dict], risk_per_trade: float, months: float) -> dict:
    """한 시뮬레이션의 거래를 한 리스크 비율로 굴린 결과."""
    path = equity_path(trades, risk_per_trade)
    final = path[-1]
    monthly = (final ** (1.0 / months) - 1.0) if (months > 0 and final > 0) else None
    drawdown = max_drawdown_pct(path)
    return {
        "risk_per_trade": risk_per_trade,
        "total_return": final - 1.0,
        "monthly_return": monthly,
        "max_drawdown": drawdown,
        "months_to_recover_at_1pct": months_to_recover(drawdown),
        "trades": len(trades),
    }
