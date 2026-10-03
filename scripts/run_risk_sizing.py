"""거래당 리스크 비율별로 "월 수익률 / 최악 낙폭"을 백테스트한다 (로직은 src/backtest/sizing.py).

    python scripts/run_risk_sizing.py                        # 지금 실거래 설정
    python scripts/run_risk_sizing.py --risks 0.0025 0.005 0.0075
    python scripts/run_risk_sizing.py --set min_atr_to_stop_ratio=0.4

실험 원장(`run_experiment.py`)과 같은 데이터·같은 구간(**탐색 구간 — 봉인된 최근 90일은 안 쓴다**)
·같은 비용 모델을 쓴다. 원장에는 남기지 않는다 — 전략을 고르는 실험이 아니라, 이미 있는 거래
결과를 계좌 크기로 환산하는 계산이기 때문이다(비율을 바꿔도 진입·청산은 하나도 안 바뀐다).

비율마다 달라지는 것은 **일일 손실 한도의 R 환산**뿐이다 — 한도는 자산의 5%라서, 거래당 0.75%면
6.67R, 0.25%면 20R에서 걸린다. 그래서 비율마다 시뮬레이션을 다시 돌린다.

주의: 이 백테스트 전략은 아직 게이트를 통과하지 못했다. 여기 나오는 월 수익률은 "엣지가
백테스트만큼 있다면"이라는 가정 위의 숫자다. 실거래 순R은 지금 0 근처다(docs/OBJECTIVE.md).
"""

import argparse
import statistics as st
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_experiment as rx  # noqa: E402

from src.backtest import gate  # noqa: E402
from src.backtest.data import load_cached_ohlcv, slice_window  # noqa: E402
from src.backtest.engine import entry_start_bar, gated_signals  # noqa: E402
from src.backtest.portfolio import daily_loss_limit_r, simulate_many  # noqa: E402
from src.backtest.sizing import sizing_stats  # noqa: E402
from src.core.config import FEE_PCT_PER_SIDE, FUTURES_RISK_PER_TRADE, FUTURES_SYMBOLS  # noqa: E402
from src.core.risk import MAX_DAILY_LOSS_PCT  # noqa: E402
from src.data.futures_exchange import get_futures_market_data_client  # noqa: E402

DAYS_PER_MONTH = 30.44


def _load_search_window(cache_dir: Path, days: int) -> dict:
    client = get_futures_market_data_client()
    client.load_markets()
    raw = {}
    for symbol in [s for s in FUTURES_SYMBOLS if s in client.markets]:
        df = load_cached_ohlcv(client, symbol, timeframe=rx.TIMEFRAME, days=days, cache_dir=cache_dir)
        if len(df) >= rx.MIN_BARS:
            raw[symbol] = df
    cutoff = rx.HOLDOUT_START   # 탐색 구간 = 홀드아웃 시작 전 전부(게이트 v3)
    window = {s: slice_window(df, end=cutoff) for s, df in raw.items()}
    return {s: df for s, df in window.items() if len(df) >= rx.MIN_BARS}


def _signals(df_by_symbol: dict, params: dict, cache_dir: Path) -> dict:
    out = {}
    for symbol, df in df_by_symbol.items():
        lookup = rx._signal_lookup(symbol, df, params, cache_dir)
        at = {ts: i for i, ts in enumerate(df["timestamp"].tolist())}

        def signal_fn(window, _lookup=lookup, _at=at):
            return _lookup.get(_at.get(window["timestamp"].iloc[-1]))

        out[symbol] = gated_signals(
            df, stop_loss_pct=params["stop_loss_pct"], regime_sma_period=params["regime_sma_period"],
            min_atr_to_stop_ratio=params["min_atr_to_stop_ratio"], htf_hours=params["htf_hours"],
            signal_fn=signal_fn)
    return out


def _months(df_by_symbol: dict, regime_sma_period: int) -> float:
    """진입이 가능했던 기간(레짐 SMA 워밍업 이후 ~ 구간 끝)의 달 수."""
    start = min(df["timestamp"].iloc[entry_start_bar(len(df), regime_sma_period)]
                for df in df_by_symbol.values())
    end = max(df["timestamp"].iloc[-1] for df in df_by_symbol.values())
    return (end - start).total_seconds() / 86400 / DAYS_PER_MONTH


def _simulate(df_by_symbol, params, signals, *, risk, slippage, seeds, breaker_reset):
    return simulate_many(
        df_by_symbol, seeds=range(seeds), signals_by_symbol=signals,
        max_concurrent_positions=params["max_concurrent_positions"], breaker_reset=breaker_reset,
        max_daily_loss_r=daily_loss_limit_r(MAX_DAILY_LOSS_PCT, risk),
        stop_loss_pct=params["stop_loss_pct"], take_profit_rr=params["take_profit_rr"],
        fee_pct_per_side=FEE_PCT_PER_SIDE, slippage_r_per_side=slippage,
        regime_sma_period=params["regime_sma_period"],
        min_atr_to_stop_ratio=params["min_atr_to_stop_ratio"],
    )


def _pct(value, digits=2):
    return "-" if value is None else f"{value * 100:+.{digits}f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="거래당 리스크 비율별 월 수익률·낙폭")
    parser.add_argument("--risks", type=float, nargs="*",
                        default=[0.001, 0.0025, 0.004, 0.005, 0.0075, 0.01])
    parser.add_argument("--set", dest="overrides", nargs="*", default=[])
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--breaker-reset", choices=["cooldown", "daily", "never", "off"], default="cooldown")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / ".ohlcv_cache")
    args = parser.parse_args()

    params = rx._defaults() | rx._parse_overrides(args.overrides)
    df_by_symbol = _load_search_window(args.cache_dir, args.days)
    signals = _signals(df_by_symbol, params, args.cache_dir)
    months = _months(df_by_symbol, params["regime_sma_period"])
    print(f"탐색 구간 {months:.1f}개월 · 종목 {len(df_by_symbol)}개 · seed {args.seeds}개 · "
          f"게이트 v{gate.GATE_VERSION} 비용 모델 · 지금 실거래 리스크 {FUTURES_RISK_PER_TRADE:.2%}")
    print(f"방향 {params['direction_filter']} · 저변동 {params['min_atr_to_stop_ratio']}\n")

    scenarios = (("기본 비용", 0.0), ("나쁜 비용(스트레스)", gate.STRESS_SLIPPAGE_R_PER_SIDE))
    for label, slippage in scenarios:
        print(f"[{label}]  값은 seed {args.seeds}개의 중앙값, 괄호는 운 나쁜 seed(하위 10%)")
        print(f"{'거래당 리스크':>10} {'월 수익률':>16} {'최악 낙폭':>16} {'낙폭 회복(월1%)':>14} {'누적':>9}")
        for risk in args.risks:
            results = _simulate(df_by_symbol, params, signals, risk=risk, slippage=slippage,
                                seeds=args.seeds, breaker_reset=args.breaker_reset)
            stats = [sizing_stats(r["trades"], risk, months) for r in results]
            monthly = sorted(s["monthly_return"] for s in stats if s["monthly_return"] is not None)
            dd = sorted(s["max_drawdown"] for s in stats)
            total = st.median(s["total_return"] for s in stats)
            worst_monthly = monthly[int(len(monthly) * 0.1)] if monthly else None
            worst_dd = dd[min(len(dd) - 1, int(len(dd) * 0.9))]
            median_dd = st.median(dd)
            recover = st.median(s["months_to_recover_at_1pct"] for s in stats)
            print(f"{risk:>10.2%} {_pct(st.median(monthly)):>8} ({_pct(worst_monthly)}) "
                  f"{-median_dd * 100:>7.1f}% ({-worst_dd * 100:.1f}%) "
                  f"{recover:>11.1f}개월 {_pct(total, 1):>9}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
