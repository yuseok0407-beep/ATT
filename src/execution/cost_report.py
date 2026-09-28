"""실거래 체결 비용을 "평소엔 얼마, 나쁠 땐 얼마"로 요약한다.

백테스트는 세 가지를 공짜로 가정한다: 신호 봉 종가에 정확히 진입, 손절/익절 가격에 정확히 청산,
(스트레스 시나리오에서) 편도 0.05R의 균일한 슬리피지. 실제로는 그 셋이 전부 다르고, 이
전략은 건당 기대값이 비용과 같은 크기라서 그 차이가 수익의 부호를 바꾼다. 이 모듈은
`export_log.build_trade_rows`가 거래마다 잰 값(`entry_slippage_r`/`exit_slippage_r`/
`fee_r`/`execution_cost_r`)을 모아 분포로 요약한다 — 평균 하나만 보면 가끔 크게 터지는 꼬리가
가려지므로 중앙값·75/90/95퍼센타일·최악값을 같이 낸다.

거래 목록은 반드시 `build_trade_rows`에서 받는다 — R 복원과 슬리피지 정의를 여기 따로 적으면
CSV와 이 요약이 서로 다른 말을 하게 된다.
"""

import statistics as st
from collections import defaultdict

# 이 표본 수 전에는 비용 측정값으로 백테스트 게이트의 스트레스 기준을 바꾸지 않는다
# (PROFITABILITY_GROWTH_PLAN.md 3.4 — 30건 남짓이면 드문 큰 슬리피지 한 건이 평균을 좌우한다).
MIN_SAMPLES_FOR_DECISION = 100

METRICS = ("entry_slippage_r", "exit_slippage_r", "fee_r", "execution_cost_r")


def _percentile(sorted_values: list[float], q: float) -> float:
    """최근접 순위 방식. 표본이 작으므로 보간으로 없는 값을 만들어내지 않는다."""
    index = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[index]


def distribution(values) -> dict | None:
    clean = sorted(float(v) for v in values
                   if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not clean:
        return None
    return {
        "n": len(clean),
        "mean": st.mean(clean),
        "median": st.median(clean),
        "p75": _percentile(clean, 0.75),
        "p90": _percentile(clean, 0.90),
        "p95": _percentile(clean, 0.95),
        "worst": clean[-1],
        "best": clean[0],
    }


def _group(rows: list[dict], key) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(key(row))].append(row)
    return {name: {m: distribution(r.get(m) for r in bucket) for m in METRICS}
            for name, bucket in sorted(buckets.items())}


def summarize_costs(rows: list[dict], stress_slippage_r_per_side: float) -> dict:
    """거래 행 목록 → 비용 분포 요약.

    `stress_slippage_r_per_side`는 백테스트 게이트가 가정하는 편도 슬리피지다. 실측 슬리피지
    (진입+청산)와 나란히 놓아서 "그 가정이 현실보다 후한가 박한가"를 한 줄로 답한다.
    """
    overall = {m: distribution(r.get(m) for r in rows) for m in METRICS}
    complete = sum(1 for r in rows if r.get("execution_cost_r") is not None)

    slippage_per_trade = [
        r["entry_slippage_r"] + r["exit_slippage_r"] for r in rows
        if isinstance(r.get("entry_slippage_r"), (int, float))
        and isinstance(r.get("exit_slippage_r"), (int, float))
    ]
    measured = distribution(slippage_per_trade)
    assumed = 2 * stress_slippage_r_per_side
    return {
        "trades": len(rows),
        "complete_samples": complete,
        "min_samples_for_decision": MIN_SAMPLES_FOR_DECISION,
        "enough_samples": complete >= MIN_SAMPLES_FOR_DECISION,
        "overall": overall,
        "by_env": _group(rows, lambda r: r.get("env")),
        "by_reason": _group(rows, lambda r: r.get("reason")),
        "by_side": _group(rows, lambda r: r.get("side")),
        "by_symbol": _group(rows, lambda r: r.get("symbol")),
        "stress_assumption": {
            "per_side_r": stress_slippage_r_per_side,
            "per_trade_r": assumed,
            "measured_per_trade": measured,
            # 평균이 가정보다 작으면 "스트레스 시나리오가 현실보다 나쁘다(보수적)"는 뜻이다.
            "mean_within_assumption": (measured["mean"] <= assumed) if measured else None,
            "p90_within_assumption": (measured["p90"] <= assumed) if measured else None,
        },
    }
