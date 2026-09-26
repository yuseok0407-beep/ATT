"""aoa 전략(demo2)을 비용 포함으로 **한 번** 검증한다 — 파라미터 탐색용이 아니다.

docs/DEMO2_PLAN.md의 C6. aoa 규칙의 값은 원본 매매 분석으로 고정했고(config.AOA_*), 이
스크립트는 "그 고정값이 비용을 넣고도 음수가 아닌가"만 본다. 결과를 보고 값을 바꾸기 시작하면
BACKTEST_PROTOCOL.md의 다중비교 문제로 그대로 들어간다 — 그래서 --set 같은 손잡이를 두지 않았다.

규칙은 실거래와 같은 함수다: 진입 후보는 `engine.gated_signals`(signal_fn=`detect_aoa_signal`,
trend 전용 게이트인 레짐/저변동은 끔), 청산은 `portfolio.simulate_portfolio`의 브라켓 판정, 손절
3%·익절 1%는 `futures_strategy.strategy_*`에서 온다. 동시보유 상한·서킷브레이커·수수료는
실거래 설정 그대로다.

데이터는 run_experiment.py와 같은 캐시·같은 탐색 구간(최근 90일 홀드아웃 제외)을 쓴다 —
홀드아웃은 열지 않는다.

    python scripts/run_aoa_backtest.py --cache-dir .ohlcv_cache
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest import gate  # noqa: E402
from src.backtest.data import load_cached_ohlcv, slice_window  # noqa: E402
from src.backtest.engine import gated_signals  # noqa: E402
from src.backtest.portfolio import simulate_many, trades_by_symbol  # noqa: E402
from src.core.config import FEE_PCT_PER_SIDE, FUTURES_SYMBOLS, MAX_CONCURRENT_POSITIONS, RULE_TIMEFRAME  # noqa: E402
from src.core.futures_strategy import (  # noqa: E402
    aoa_min_bars,
    detect_aoa_signal,
    strategy_stop_loss_pct,
    strategy_take_profit_rr,
)
from src.data.futures_exchange import get_futures_market_data_client  # noqa: E402

HOLDOUT_DAYS = 90  # run_experiment.py와 같은 경계


def main() -> int:
    parser = argparse.ArgumentParser(description="aoa 전략 비용 포함 1회 검증")
    parser.add_argument("--cache-dir", type=Path, default=Path(".ohlcv_cache"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--seeds", type=int, default=20)
    args = parser.parse_args()

    stop_loss_pct = strategy_stop_loss_pct("aoa")
    take_profit_rr = strategy_take_profit_rr("aoa")
    # 게이트의 스트레스(편도 0.05R)는 trend의 손절폭 기준으로 정한 값이다. aoa는 손절이 3%라
    # 같은 0.05R이 가격으로는 15bp — 훨씬 가혹하다. 가격 기준으로 같은 스트레스(약 6bp)도 같이 본다.
    price_equiv_slippage_r = 0.0006 / stop_loss_pct

    client = get_futures_market_data_client()
    client.load_markets()
    symbols = [s for s in FUTURES_SYMBOLS if s in client.markets]
    raw = {s: load_cached_ohlcv(client, s, timeframe=RULE_TIMEFRAME, days=args.days, cache_dir=args.cache_dir)
           for s in symbols}
    last = max(df["timestamp"].iloc[-1] for df in raw.values())
    cutoff = last - timedelta(days=HOLDOUT_DAYS)
    df_by_symbol = {s: slice_window(df, end=cutoff) for s, df in raw.items()}
    df_by_symbol = {s: df.reset_index(drop=True) for s, df in df_by_symbol.items() if len(df) >= aoa_min_bars() + 50}

    print(f"탐색 구간 ~{cutoff:%Y-%m-%d} (홀드아웃 {HOLDOUT_DAYS}일 봉인) · 종목 {len(df_by_symbol)}개 · "
          f"seed {args.seeds}개 · 동시보유 {MAX_CONCURRENT_POSITIONS}")
    print(f"손절 {stop_loss_pct:.2%} · 익절 {stop_loss_pct * take_profit_rr:.2%} · 수수료 편도 {FEE_PCT_PER_SIDE:.3%}"
          f" (왕복 {2 * FEE_PCT_PER_SIDE / stop_loss_pct:.3f}R)", flush=True)

    signals = {s: gated_signals(df, stop_loss_pct=stop_loss_pct, regime_sma_period=0,
                                min_atr_to_stop_ratio=0, signal_fn=detect_aoa_signal)
               for s, df in df_by_symbol.items()}
    print(f"진입 후보 {sum(len(v) for v in signals.values())}건", flush=True)

    def run(slippage: float):
        results = simulate_many(
            df_by_symbol, seeds=range(args.seeds), signals_by_symbol=signals,
            stop_loss_pct=stop_loss_pct, take_profit_rr=take_profit_rr,
            fee_pct_per_side=FEE_PCT_PER_SIDE, slippage_r_per_side=slippage, regime_sma_period=0,
        )
        return results, gate.summarize(results, df_by_symbol)

    base_results, base = run(0.0)
    _, stress_gate = run(gate.STRESS_SLIPPAGE_R_PER_SIDE)
    _, stress_price = run(price_equiv_slippage_r)
    verdict = gate.evaluate(base, stress_gate)

    first = gate.run_metrics(base_results[0], df_by_symbol)
    trades = [t for ts in trades_by_symbol(base_results[0]).values() for t in ts]
    wins = sum(1 for t in trades if t["pnl_r"] > 0)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    months = len(max(df_by_symbol.values(), key=len)) / 24 / 30.44

    print("\n=== 결과 (seed 중앙값) ===")
    print(f"거래 {base['trades_median']:.0f}건 · 총R {base['total_r_median']:+.1f} "
          f"(하위5% {base['total_r_p5']:+.1f}) · 낙폭 {base['mdd_r_median']:+.1f}R")
    print(f"seed0: 승률 {wins / len(trades):.1%} (본전 승률 "
          f"{(1 + 2 * FEE_PCT_PER_SIDE / stop_loss_pct) / (1 + take_profit_rr):.1%}) · 청산 {reasons}")
    print(f"seed0 4분할 총R: {[round(x, 1) for x in first['splits']]}")
    print(f"스트레스(게이트 0.05R = 가격 {0.05 * stop_loss_pct:.2%}): 총R {stress_gate['total_r_median']:+.1f}")
    print(f"스트레스(가격 6bp = {price_equiv_slippage_r:.3f}R): 총R {stress_price['total_r_median']:+.1f}")
    print(f"월 환산: {base['total_r_median'] / months:+.2f}R/월 ({months:.1f}개월)")
    print("\n게이트(참고용 — 이 전략은 원장에 올리지 않는다):")
    for check in verdict["checks"]:
        value = check["value"]
        shown = f"{value:.3f}" if isinstance(value, float) else str(value)
        print(f"  {check['name']:<24s} {shown:>10s}  {'PASS' if check['passed'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
