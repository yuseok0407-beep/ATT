"""배포 게이트 — 실험 결과를 사람 눈이 아니라 **사전에 고정된 기준**이 판정한다.

이 모듈이 있는 이유(로드맵 `docs/BACKTEST_VALIDATION_ROADMAP.md` Phase 0):
`run_portfolio_sweep.py`는 표를 찍어주고 사람이 보고 고른다. 그 방식은 "넓고 평평한 영역을
고르라"는 지침을 지켰는지 사후에 확인할 방법이 없고, **몇 번 시도해서 나온 결론인지도 남지
않는다.** 200번 돌려서 제일 좋은 칸을 고른 것과 처음부터 그 칸을 짚은 것은 전혀 다른 근거인데
결과 표만 보면 구분이 안 된다.

그래서 판정을 코드로 고정한다. 기준값은 `docs/BACKTEST_PROTOCOL.md`에 사람이 먼저 선언한
것과 같은 값이고, 이 파일은 **탐색 중에 고치지 않는다** — 고치면 기준을 결과에 맞추는 것이라
탐색 전체가 무의미해진다. 바꿔야 한다면 `GATE_VERSION`을 올리고 원장(`docs/experiments.tsv`)에
그 경계가 남게 한 뒤, 이전 버전의 결과와 섞어서 비교하지 않는다.

Karpathy의 autoresearch에서 에이전트가 `prepare.py`(평가 유틸)를 못 건드리게 막아둔 것과 같은
역할이다 — 모델을 좋게 만드는 대신 시험을 쉽게 만드는 길을 원천적으로 없앤다.
"""

from __future__ import annotations

import statistics as st

import pandas as pd

from .portfolio import trades_by_symbol
from .report import portfolio_stats

# 기준을 바꾸면 이 번호를 올린다. 원장의 행마다 이 값이 같이 남으므로, 버전이 다른 행끼리
# 총R을 비교하는 실수를 나중에 잡아낼 수 있다.
#   v1 (2026-09-24): 수수료 편도 0.04%, 익절 조건부 시장가(청산 슬리피지 있음).
#   v2 (2026-09-28): 판정 기준값은 그대로, **비용 모델**이 실측으로 바뀜 — 테이커 0.05%(실계좌
#       실측), 익절은 지정가(메이커 0.02%, 청산 슬리피지 없음). 기준값이 아니어도 같은 설정의
#       총R이 달라지므로 버전을 올린다(BACKTEST_PROTOCOL.md "게이트 버전 이력").
GATE_VERSION = 2

N_SPLITS = 4

# 비용 스트레스: 편도 슬리피지 0.05R(손절 1.75%에서 가격 약 9bp). 이 값은 임의가 아니라
# 2026-09-22 측정에서 **전략이 무너지기 시작한 지점**이다(NEXT_STEPS.md). 기대값이 이보다
# 작으면 체결 비용이 엣지를 먹는다.
STRESS_SLIPPAGE_R_PER_SIDE = 0.05

# --- 사전 등록된 통과 기준 -------------------------------------------------
# 전부 "이 값 이상/이하여야 통과". 근거는 BACKTEST_PROTOCOL.md에 적는다.
MIN_TRADES = 200                    # 표본. 건당 표준편차 1.45R에서 이보다 적으면 부호 판정 불가
MIN_TOTAL_R_P5 = 0.0                # 배정 순서 seed 하위 5%에서도 양수 — 운에 의존하지 않을 것
MIN_OOS_PASS_RATE = 0.80            # 시간 4분할이 전부 양수인 seed 비율
MIN_RECOVERY_FACTOR = 1.5           # 총R / |포트폴리오 낙폭|
MAX_TOP2_SHARE = 0.60               # 상위 2종목이 총R에서 차지하는 몫
MIN_POSITIVE_SYMBOL_SHARE = 0.60    # 양수로 끝난 종목 비율
MIN_STRESS_TOTAL_R = 0.0            # 슬리피지 0.05R에서도 총R 양수


def split_bounds(df_by_symbol: dict[str, pd.DataFrame], i: int,
                 n_splits: int = N_SPLITS) -> dict[str, tuple[int, int]]:
    """종목별로 봉 수를 n등분한 i번째 구간의 (시작, 끝) 봉 위치."""
    return {symbol: (round(len(df) * i / n_splits), round(len(df) * (i + 1) / n_splits))
            for symbol, df in df_by_symbol.items()}


def run_metrics(result: dict, df_by_symbol: dict[str, pd.DataFrame],
                n_splits: int = N_SPLITS) -> dict:
    """시뮬레이션 한 번(= seed 하나)의 지표.

    낙폭은 반드시 `report.portfolio_stats`로 낸다 — `aggregate_stats`의 `max_drawdown_r`은
    종목별 최악값이라 상관이 높은 암호화폐에서는 실제 낙폭을 크게 과소보고한다.
    """
    by_symbol = trades_by_symbol(result)
    port = portfolio_stats(by_symbol)

    splits = []
    for i in range(n_splits):
        bounds = split_bounds(df_by_symbol, i, n_splits)
        part = {s: [t for t in trades if bounds[s][0] <= t["exit_index"] < bounds[s][1]]
                for s, trades in by_symbol.items()}
        splits.append(portfolio_stats(part)["total_r"])

    per_symbol_r = {s: sum(t["pnl_r"] for t in trades) for s, trades in by_symbol.items()}
    ranked = sorted(per_symbol_r.values(), reverse=True)
    total_r = port["total_r"]
    mdd = abs(port["max_drawdown_r"])
    return {
        "total_r": total_r,
        "mdd_r": port["max_drawdown_r"],
        "trades": port["num_trades"],
        "splits": splits,
        "all_splits_positive": all(s > 0 for s in splits),
        "positive_symbols": sum(1 for v in per_symbol_r.values() if v > 0),
        "symbols": len(per_symbol_r),
        "top2_r": sum(ranked[:2]),
        # 두 비율 모두 총R이 양수일 때만 정의한다. 음수 총R로 비율을 내면 부호가 섞여
        # 오히려 좋아 보이는 값이 나온다(-10R / 낙폭 10R = -1.0 같은 식).
        "top2_share": (sum(ranked[:2]) / total_r) if total_r > 0 else None,
        "recovery_factor": (total_r / mdd) if (total_r > 0 and mdd > 0) else None,
        "blocked": result.get("blocked", {}),
    }


def summarize(results: list[dict], df_by_symbol: dict[str, pd.DataFrame],
              n_splits: int = N_SPLITS) -> dict:
    """여러 seed의 결과를 하나의 요약으로. 중앙값과 하위 5%를 같이 본다.

    한 번의 숫자를 결론으로 쓰면 같은 봉에 여러 신호가 났을 때의 **배정 순서라는 임의성**을
    성과로 착각한다(portfolio 모듈 설명 참고). 그래서 분포를 요약한다.
    """
    if not results:
        raise ValueError("결과가 비어 있다 — seed를 하나 이상 돌려야 한다")

    metrics = [run_metrics(r, df_by_symbol, n_splits) for r in results]
    totals = sorted(m["total_r"] for m in metrics)
    shares = [m["top2_share"] for m in metrics if m["top2_share"] is not None]
    recoveries = [m["recovery_factor"] for m in metrics if m["recovery_factor"] is not None]
    symbols = metrics[0]["symbols"]

    return {
        "seeds": len(metrics),
        "symbols": symbols,
        "trades_median": st.median(m["trades"] for m in metrics),
        "total_r_median": st.median(totals),
        "total_r_p5": totals[max(0, int(len(totals) * 0.05))],
        "total_r_p95": totals[min(len(totals) - 1, int(len(totals) * 0.95))],
        "mdd_r_median": st.median(m["mdd_r"] for m in metrics),
        "oos_pass_rate": sum(1 for m in metrics if m["all_splits_positive"]) / len(metrics),
        "recovery_factor_median": st.median(recoveries) if recoveries else None,
        "top2_share_median": st.median(shares) if shares else None,
        "positive_symbol_share_median": (
            st.median(m["positive_symbols"] for m in metrics) / symbols if symbols else 0.0),
    }


def _check(name: str, value, threshold, passed: bool, why: str) -> dict:
    return {"name": name, "value": value, "threshold": threshold, "passed": passed, "why": why}


def evaluate(base: dict, stress: dict) -> dict:
    """기본 비용 요약 + 스트레스 비용 요약 -> 통과 여부와 항목별 근거.

    `base`는 실거래 수수료만 반영한 결과, `stress`는 거기에 편도 슬리피지
    `STRESS_SLIPPAGE_R_PER_SIDE`를 더한 결과다. 둘 다 `summarize()`가 낸 dict.
    """
    top2 = base.get("top2_share_median")
    recovery = base.get("recovery_factor_median")

    checks = [
        _check("trades", base["trades_median"], MIN_TRADES,
               base["trades_median"] >= MIN_TRADES,
               "표본이 부족하면 총R의 부호 자체를 판정할 수 없다"),
        _check("total_r_p5", base["total_r_p5"], MIN_TOTAL_R_P5,
               base["total_r_p5"] > MIN_TOTAL_R_P5,
               "배정 순서 운이 나쁜 seed에서도 양수여야 한다"),
        _check("oos_pass_rate", base["oos_pass_rate"], MIN_OOS_PASS_RATE,
               base["oos_pass_rate"] >= MIN_OOS_PASS_RATE,
               "특정 시기 한 구간이 전체 총R을 만들면 안 된다"),
        _check("recovery_factor", recovery, MIN_RECOVERY_FACTOR,
               recovery is not None and recovery >= MIN_RECOVERY_FACTOR,
               "낙폭 대비 수익. 낮으면 실계좌에서 버틸 수 없다"),
        _check("top2_share", top2, MAX_TOP2_SHARE,
               top2 is not None and top2 <= MAX_TOP2_SHARE,
               "상위 1~2종목이 총R의 대부분을 만들면 종목 운이다"),
        _check("positive_symbol_share", base["positive_symbol_share_median"],
               MIN_POSITIVE_SYMBOL_SHARE,
               base["positive_symbol_share_median"] >= MIN_POSITIVE_SYMBOL_SHARE,
               "규칙이 종목 전반에 통해야 한다"),
        _check("stress_total_r", stress["total_r_median"], MIN_STRESS_TOTAL_R,
               stress["total_r_median"] > MIN_STRESS_TOTAL_R,
               f"편도 슬리피지 {STRESS_SLIPPAGE_R_PER_SIDE}R에서도 살아남아야 한다"),
    ]
    return {
        "gate_version": GATE_VERSION,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["passed"]],
    }
