"""장기추세 레짐 필터(상승 레짐에서 숏 진입 차단)를 워크포워드로 정식 검증한다.

배경: 2026-09-07 분석에서 실거래 부진의 지배적 원인이 "익절선이 멀어서"가 아니라 "상승 레짐에서
숏이 계속 깎여서"로 나왔고, 임시 스크립트로 돌린 스윕에서 이 필터가 유망해 보였다. 다만 그건
여러 변형을 훑어보고 좋은 걸 고른 것이라 과최적화 가능성이 남아 있어, `src/backtest/optimize.py`의
공용 하네스(`run_walk_forward`)로 다시 검증한다 — 모든 시간 구간에서 총R이 양수여야 PASS.

필터 길이(regime_sma_period)는 run_backtest의 파라미터가 아니라 signal_fn으로 주입되므로 그리드
축이 아니라 바깥 루프로 돌린다. 각 조합마다 `optimize._param_combos`가 베이스라인(현재 실거래
설정)을 강제로 포함시키므로 비교 대상을 깜빡 빼먹는 실수가 구조적으로 안 난다.

성능: `detect_signal`은 매 봉마다 100봉 윈도우로 ADX를 새로 계산해서 종목 하나에 수 분이 걸린다.
필터 길이를 바꿔도 detect_signal 결과 자체는 같으므로(필터는 그 뒤에 적용됨) 종목별 워커 안에서
(마지막 봉 시각, 윈도우 길이)로 메모이즈해 5개 설정이 계산을 공유하게 하고, 종목 단위로 병렬화한다.

실주문 없이 공개 시세만 조회한다."""
import sys
from multiprocessing import Pool
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from src.backtest.data import fetch_historical_ohlcv
from src.backtest.optimize import BASELINE_LABEL, aggregate_stats, run_walk_forward
from src.core.config import (FUTURES_SYMBOLS, RULE_ADX_THRESHOLD, RULE_SMA_PERIOD,
                             STOP_LOSS_PCT, TAKE_PROFIT_RR)
from src.core.futures_strategy import detect_signal, make_regime_filtered_signal_fn
from src.data.futures_exchange import get_futures_market_data_client

TIMEFRAME = "1h"
BACKTEST_DAYS = 365
FEE_PCT_PER_SIDE = 0.0004
N_SPLITS = 4
MIN_BARS = 800  # 4분할 시 한 구간이 최소 200봉은 되도록
WORKERS = 6

REGIME_SMA_PERIODS = [100, 200, 400, 700]
NO_FILTER = "필터 없음 (현재 실거래 설정)"

# 그리드 축은 안 쓰고(필터 길이는 signal_fn으로 주입) 베이스라인 조합만 만들기 위한 최소 그리드.
PARAM_GRID = {"take_profit_rr": [TAKE_PROFIT_RR]}
# run_backtest의 sma_period 하드코딩 기본값(20) 함정을 피하려고 항상 명시한다(UPDATE_LOG 2026-08-11).
FIXED_PARAMS = dict(
    stop_loss_pct=STOP_LOSS_PCT, adx_threshold=RULE_ADX_THRESHOLD, sma_period=RULE_SMA_PERIOD,
    require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE,
)


def _memoized_detect_signal():
    """(마지막 봉 시각, 윈도우 길이)로 detect_signal 결과를 캐시한다. 같은 종목·같은 df에서
    윈도우가 그 둘로 유일하게 정해지기 때문에 안전하다(길이를 키에 넣는 이유는 구간 분할 직후
    윈도우가 100봉보다 짧아지는 경계 구간을 구분하기 위함)."""
    cache = {}

    def wrapped(window):
        key = (window["timestamp"].iloc[-1], len(window))
        if key not in cache:
            cache[key] = detect_signal(
                window, adx_threshold=RULE_ADX_THRESHOLD, sma_period=RULE_SMA_PERIOD,
            )
        return cache[key]

    return wrapped


def _splits_for(df_by_symbol, signal_fn):
    res = run_walk_forward(df_by_symbol, PARAM_GRID, n_splits=N_SPLITS,
                           fixed_params={**FIXED_PARAMS, "signal_fn": signal_fn})
    return next(r for r in res if r["label"].startswith(BASELINE_LABEL))["splits"]


def _work(item):
    """종목 하나에 대해 필터 없음 + 각 필터 길이의 구간별 통계를 계산한다."""
    symbol, df = item
    base_signal = _memoized_detect_signal()
    out = {NO_FILTER: _splits_for({symbol: df}, base_signal)}
    for period in REGIME_SMA_PERIODS:
        # base_signal_fn으로 메모이즈된 detect_signal을 주입해, 필터 길이가 달라도 종목당
        # ADX 계산이 한 번만 일어나게 한다.
        filtered = make_regime_filtered_signal_fn(df, period, base_signal_fn=base_signal)
        out[f"SMA{period} 상승레짐 숏차단"] = _splits_for({symbol: df}, filtered)
    print(f"  ...{symbol} 완료", file=sys.stderr, flush=True)
    return out


def _print_row(label, splits):
    stats = aggregate_stats(splits)
    verdict = "PASS" if all(s["total_r"] > 0 for s in splits) else "FAIL"
    per = "  ".join(f"{s['total_r']:>+7.1f}({s['num_trades']:>4})" for s in splits)
    print(f"  [{verdict}] {label:<30} 거래{stats['num_trades']:>5} "
          f"승률{stats['win_rate']:>6.1%} 총R{stats['total_r']:>+9.2f} "
          f"최대DD{stats['max_drawdown_r']:>8.2f}   {per}")


def main():
    client = get_futures_market_data_client()
    data = {}
    for symbol in FUTURES_SYMBOLS:
        try:
            df = fetch_historical_ohlcv(client, symbol, TIMEFRAME, BACKTEST_DAYS)
        except Exception as exc:  # 상장 안 됨/일시 오류 — 한 종목 때문에 전체를 멈추지 않는다
            print(f"  건너뜀 {symbol}: {exc}", file=sys.stderr)
            continue
        if len(df) < MIN_BARS:
            print(f"  건너뜀 {symbol}: 봉 {len(df)}개로 {N_SPLITS}분할하기엔 부족", file=sys.stderr)
            continue
        data[symbol] = df

    print(f"종목 {len(data)}개 / {TIMEFRAME} {BACKTEST_DAYS}일 / {N_SPLITS}분할 워크포워드")
    print(f"진입 ADX{RULE_ADX_THRESHOLD:.0f}+SMA{RULE_SMA_PERIOD}+RSI, 손절 {STOP_LOSS_PCT:.2%}, "
          f"손익비 {TAKE_PROFIT_RR}, 수수료 편도 {FEE_PCT_PER_SIDE:.2%}")
    print("모든 구간에서 총R 양수여야 PASS. 구간별 표기: 총R(거래수)\n", flush=True)

    with Pool(WORKERS) as pool:
        per_symbol = pool.map(_work, list(data.items()))

    for label in [NO_FILTER] + [f"SMA{p} 상승레짐 숏차단" for p in REGIME_SMA_PERIODS]:
        merged = [aggregate_stats([sym_out[label][i] for sym_out in per_symbol])
                  for i in range(N_SPLITS)]
        _print_row(label, merged)


if __name__ == "__main__":
    main()
