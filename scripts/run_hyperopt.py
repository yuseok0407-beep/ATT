"""ADX 임계값 x SMA 기간 그리드를 전수 탐색해 전반/후반 아웃오브샘플 양쪽에서 이기는 조합을 찾는다.
기존 run_combo_backtest.py/run_oos_backtest.py처럼 손으로 VARIANTS를 채우는 대신
src/backtest/optimize.py의 공용 하네스를 쓴다 — "현재 실거래 설정"이 config에서 직접 채워져 항상
결과에 포함되므로, 손으로 채운 VARIANTS에서 실거래값을 깜빡 빼먹는 실수(UPDATE_LOG.md 2026-08-23)가
구조적으로 안 난다. 실주문 없이 공개 시세만 조회한다."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from src.backtest.data import fetch_historical_ohlcv
from src.backtest.optimize import BASELINE_LABEL, run_walk_forward
from src.core.config import FUTURES_SYMBOLS, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

TIMEFRAME = "1h"
BACKTEST_DAYS = 365
FEE_PCT_PER_SIDE = 0.0004
N_SPLITS = 2
TOP_N = 10
MIN_BARS = 400  # 반으로 쪼갰을 때 한쪽이 최소 200봉은 되도록 (run_symbol_oos_backtest.py와 동일 기준)

PARAM_GRID = {
    "adx_threshold": [20, 25, 30, 35],
    "sma_period": [5, 10, 15, 20],
}
# 손절폭/손익비는 이번 탐색 축이 아니라 지금 실거래값으로 고정 — run_backtest()의 sma_period
# 하드코딩 기본값(20) 함정과 같은 종류의 실수를 피하려고 항상 명시한다.
FIXED_PARAMS = dict(
    stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
    fee_pct_per_side=FEE_PCT_PER_SIDE, require_rsi_confirm=True,
)


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def _print_row(result):
    stats = result["stats"]
    verdict = "PASS" if result["passed_oos"] else "FAIL"
    splits_str = "  |  ".join(
        f"구간{i + 1} 거래{s['num_trades']:>4} 총R{_fmt(s['total_r']):>8}"
        for i, s in enumerate(result["splits"])
    )
    print(f"  [{verdict}] {result['label']:<45} "
          f"합산 거래{stats['num_trades']:>4} 승률{_fmt(stats['win_rate'], pct=True):>7} "
          f"총R{_fmt(stats['total_r']):>9}  ({splits_str})")


def main():
    client = get_futures_market_data_client()
    df_by_symbol = {}
    for symbol in FUTURES_SYMBOLS:
        df = fetch_historical_ohlcv(client, symbol, timeframe=TIMEFRAME, days=BACKTEST_DAYS)
        if len(df) < MIN_BARS:
            print(f"{symbol:<16} 데이터 부족(봉 {len(df)}개, 최소 {MIN_BARS} 필요) — 건너뜀")
            continue
        df_by_symbol[symbol] = df
    print(f"{len(df_by_symbol)}종목, {TIMEFRAME}, {BACKTEST_DAYS}일, {N_SPLITS}분할 아웃오브샘플\n")

    results = run_walk_forward(
        df_by_symbol, PARAM_GRID, n_splits=N_SPLITS, fixed_params=FIXED_PARAMS,
    )

    baseline = next(r for r in results if r["label"].startswith(BASELINE_LABEL))
    print(f"=== 베이스라인({BASELINE_LABEL}) ===")
    _print_row(baseline)

    passed = [r for r in results if r["passed_oos"]]
    print(f"\n=== OOS 통과({N_SPLITS}개 구간 전부 총R 양수) 상위 {TOP_N} — 점수(총R) 순 ===")
    if not passed:
        print("  통과한 조합 없음")
    for result in passed[:TOP_N]:
        _print_row(result)

    print(f"\n총 {len(results)}개 조합 탐색, {len(passed)}개 OOS 통과")


if __name__ == "__main__":
    main()
