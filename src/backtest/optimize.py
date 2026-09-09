"""전략 파라미터 그리드서치 + 워크포워드(아웃오브샘플) 검증 하네스.

지금까지 scripts/의 여러 백테스트 스크립트가 각자 VARIANTS 딕셔너리를 손으로 채우고 거의 동일한
"종목별 loop -> 집계 -> 정렬" 코드를 반복해왔다 — 이 반복 자체가 사고를 낸 적이 있다(run_backtest.py가
sma_period를 명시 안 해서 실거래값과 다른 하드코딩 기본값이 조용히 쓰인 사고, UPDATE_LOG.md
2026-08-23 참고). 이 모듈은 그 반복을 하나로 모으고, "현재 실거래 설정"을 config에서 직접 가져와
결과에 항상 강제로 포함시켜서 같은 종류의 실수가 구조적으로 안 나게 한다."""
import itertools
from typing import Callable

import pandas as pd

from src.backtest.engine import run_backtest
from src.backtest.report import aggregate_stats, summarize
from src.core.config import RULE_ADX_THRESHOLD, RULE_SMA_PERIOD, STOP_LOSS_PCT, TAKE_PROFIT_RR

BASELINE_LABEL = "현재 실거래 설정"
# 그리드서치에서 다룰 수 있는 파라미터 중 "지금 실거래 봇이 실제로 쓰는 값"을 여기 등록해둔다 —
# param_grid에 새 파라미터 축을 추가하면 여기에도 반드시 추가해야 하고, 안 하면 _baseline_combo가
# 바로 에러를 낸다(사람이 깜빡하고 베이스라인 없이 그리드만 돌리는 걸 방지).
BASELINE_PARAMS = {
    "adx_threshold": RULE_ADX_THRESHOLD,
    "sma_period": RULE_SMA_PERIOD,
    "stop_loss_pct": STOP_LOSS_PCT,
    "take_profit_rr": TAKE_PROFIT_RR,
}


def default_score(stats: dict, *, min_trades: int = 20, drawdown_penalty: float = 0.0) -> float | None:
    """거래수가 min_trades 미만이면 실격(None) — 표본이 너무 적어 우연일 가능성이 큰 조합을
    상위권에 못 올라오게 한다. 그 외엔 total_r에서 최대낙폭 페널티를 뺀 값. drawdown_penalty
    기본값 0은 기존 스크립트들이 해온 "총R 기준 정렬"과 동일한 동작이고, 필요해지면 올려서
    낙폭이 큰 조합에 불이익을 줄 수 있다."""
    if stats["num_trades"] < min_trades:
        return None
    return stats["total_r"] - drawdown_penalty * abs(stats["max_drawdown_r"])


def _label(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _baseline_combo(param_grid: dict[str, list]) -> dict:
    missing = [k for k in param_grid if k not in BASELINE_PARAMS]
    if missing:
        raise ValueError(
            f"베이스라인(현재 실거래) 값을 모르는 파라미터: {missing} — BASELINE_PARAMS에 추가할 것"
        )
    return {k: BASELINE_PARAMS[k] for k in param_grid}


def _param_combos(param_grid: dict[str, list]) -> list[tuple[str, dict]]:
    """param_grid의 카테시안 곱에 베이스라인 조합을 강제로 추가한다(이미 그리드 안에 있으면
    중복 추가하지 않고 라벨만 베이스라인으로 바꾼다)."""
    keys = list(param_grid.keys())
    combos = [dict(zip(keys, values)) for values in itertools.product(*param_grid.values())]
    baseline = _baseline_combo(param_grid)

    labeled = []
    baseline_included = False
    for combo in combos:
        if combo == baseline:
            labeled.append((f"{BASELINE_LABEL} ({_label(combo)})", combo))
            baseline_included = True
        else:
            labeled.append((_label(combo), combo))
    if not baseline_included:
        labeled.append((f"{BASELINE_LABEL} ({_label(baseline)})", baseline))
    return labeled


def _sort_key(result: dict):
    score = result["score"]
    qualified = score is not None
    return (qualified, score if qualified else float("-inf"))


def run_grid_search(
    df_by_symbol: dict[str, pd.DataFrame],
    param_grid: dict[str, list],
    *,
    fixed_params: dict | None = None,
    score_fn: Callable[[dict], float | None] = default_score,
) -> list[dict]:
    """param_grid의 모든 조합(+ 베이스라인)을 df_by_symbol의 전 종목에 대해 백테스트하고,
    종목별 결과를 aggregate_stats로 합산한 뒤 score_fn으로 점수를 매겨 내림차순 정렬한다.
    실격(score=None) 조합은 맨 뒤로 밀리되 결과 목록에는 그대로 남는다."""
    fixed_params = fixed_params or {}
    results = []

    for label, params in _param_combos(param_grid):
        per_symbol_stats = []
        for df in df_by_symbol.values():
            trades = run_backtest(df, **fixed_params, **params)
            per_symbol_stats.append(summarize(trades))
        stats = aggregate_stats(per_symbol_stats)
        results.append({
            "label": label, "params": params, "stats": stats, "score": score_fn(stats),
        })

    results.sort(key=_sort_key, reverse=True)
    return results


def _split_by_symbol(df_by_symbol: dict[str, pd.DataFrame], n_splits: int) -> list[dict[str, pd.DataFrame]]:
    """각 종목의 df를 시간순으로 n_splits개의 동일 길이 연속 구간으로 나눈다.
    run_symbol_oos_backtest.py의 전반/후반(len(df)//2) 분할을 N등분으로 일반화한 것 — n_splits=2면
    동일한 분할이 나온다."""
    splits: list[dict[str, pd.DataFrame]] = [dict() for _ in range(n_splits)]
    for symbol, df in df_by_symbol.items():
        n = len(df)
        edges = [round(n * i / n_splits) for i in range(n_splits + 1)]
        for i in range(n_splits):
            splits[i][symbol] = df.iloc[edges[i]:edges[i + 1]].reset_index(drop=True)
    return splits


def run_walk_forward(
    df_by_symbol: dict[str, pd.DataFrame],
    param_grid: dict[str, list],
    *,
    n_splits: int = 2,
    fixed_params: dict | None = None,
    score_fn: Callable[[dict], float | None] = default_score,
) -> list[dict]:
    """run_grid_search와 같은 조합 집합을 n_splits개의 시간 구간으로 쪼개서 각각 독립적으로
    백테스트한다. 모든 구간에서 total_r > 0이어야 passed_oos=True — 특정 구간에만 우연히 맞은
    조합을 걸러낸다(run_symbol_oos_backtest.py의 PASS/FAIL 판정 기준과 동일).

    반환 항목의 "stats"는 구간별 결과(aggregate_stats 결과, 그 자체로 num_trades/total_r 등을
    갖는 딕셔너리)를 다시 aggregate_stats로 합산한 전체 합산 통계다."""
    if n_splits < 2:
        raise ValueError("n_splits는 2 이상이어야 한다 (아웃오브샘플 비교를 하려면 최소 2분할 필요)")

    fixed_params = fixed_params or {}
    split_dfs = _split_by_symbol(df_by_symbol, n_splits)
    results = []

    for label, params in _param_combos(param_grid):
        split_stats = []
        for split_df_by_symbol in split_dfs:
            per_symbol_stats = []
            for df in split_df_by_symbol.values():
                trades = run_backtest(df, **fixed_params, **params)
                per_symbol_stats.append(summarize(trades))
            split_stats.append(aggregate_stats(per_symbol_stats))

        overall = aggregate_stats(split_stats)
        passed_oos = all(s["total_r"] > 0 for s in split_stats)
        results.append({
            "label": label, "params": params, "splits": split_stats,
            "stats": overall, "passed_oos": passed_oos, "score": score_fn(overall),
        })

    results.sort(key=_sort_key, reverse=True)
    return results
