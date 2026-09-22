"""동시보유 상한과 저변동 필터 하한을 **포트폴리오 시뮬레이션으로** 훑는다.

이 스크립트가 존재하는 이유: `config.py`의 `MIN_ATR_TO_STOP_RATIO=0.64`와
`MAX_CONCURRENT_POSITIONS=8`은 "12종목·1h·365일, 동시보유/서킷브레이커 반영, 배정순서 50회"
시뮬레이션 결과를 근거로 적혀 있는데, **그 코드가 git 전체 이력에 없다**(2026-09-22 확인 —
`git log --all -S"배정순서"`가 config.py 주석만 찾는다). 즉 지금 실계좌가 쓰는 두 리스크
파라미터의 근거를 아무도 재현할 수 없었다. 이 스크립트가 그 구멍을 메운다.

옛 시뮬레이션과 달라진 것(전부 결과를 보수적인 쪽으로 옮긴다):
- 수수료가 기본으로 들어간다(`config.FEE_PCT_PER_SIDE`). 옛 엔진 기본값은 0이었다.
- 저변동/레짐 게이트가 엔진 기본 경로에 있다. 저변동 필터는 예전엔 어떤 백테스트에도 없었다.
- 연속손실/일일손실을 **청산 시각 기준으로만** 센다(`portfolio` 모듈 참고) — 진입 시각 기준으로
  세면 동시보유를 늘릴수록 좋아지는 것처럼 보인다(그 오류로 상한 12를 잘못 권고한 적이 있다).
- 배정 순서를 여러 seed로 돌려 **분포**를 보고한다. 한 번의 숫자는 그 임의성을 성과로 착각한다.

    python scripts/run_portfolio_sweep.py
    python scripts/run_portfolio_sweep.py --slippage 0.05        # 비용 스트레스
    python scripts/run_portfolio_sweep.py --cache-dir .ohlcv     # 받아둔 캔들 재사용
"""

import argparse
import json
import statistics as st
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src.backtest.data import fetch_historical_ohlcv  # noqa: E402
from src.backtest.engine import SIGNAL_LOOKBACK_BARS, entry_start_bar, gated_signals  # noqa: E402
from src.backtest.portfolio import simulate_many, trades_by_symbol  # noqa: E402
from src.backtest.report import portfolio_stats  # noqa: E402
from src.core.config import (  # noqa: E402
    FEE_PCT_PER_SIDE,
    FUTURES_SYMBOLS,
    MAX_CONCURRENT_POSITIONS,
    MIN_ATR_TO_STOP_RATIO,
    RULE_ADX_THRESHOLD,
    RULE_REGIME_SMA_PERIOD,
    RULE_SMA_PERIOD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_RR,
)
from src.core.futures_strategy import detect_signal  # noqa: E402
from src.data.futures_exchange import get_futures_market_data_client  # noqa: E402

TIMEFRAME = "1h"
MIN_BARS = 900          # 4분할 시 한 구간이 최소 200봉은 되도록
N_SPLITS = 4
CONCURRENCY = [2, 4, 6, 8, 10, 12]
MIN_ATR = [0.0, 0.4, 0.64, 0.8, 1.2]


def _load(client, symbol: str, days: int, cache_dir: Path | None) -> pd.DataFrame:
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{symbol.replace('/', '_').replace(':', '-')}.json"
        if path.exists():
            return pd.read_json(path)
    df = fetch_historical_ohlcv(client, symbol, timeframe=TIMEFRAME, days=days)
    if cache_dir is not None:
        df.to_json(path)
    return df


def _split_bounds(df_by_symbol: dict[str, pd.DataFrame], i: int) -> dict[str, tuple[int, int]]:
    """종목별로 봉 수를 N등분한 i번째 구간의 (시작, 끝) 인덱스."""
    out = {}
    for symbol, df in df_by_symbol.items():
        n = len(df)
        out[symbol] = (round(n * i / N_SPLITS), round(n * (i + 1) / N_SPLITS))
    return out


def _stats_for(result: dict, df_by_symbol: dict[str, pd.DataFrame]) -> dict:
    by_symbol = trades_by_symbol(result)
    port = portfolio_stats(by_symbol)
    splits = []
    for i in range(N_SPLITS):
        bounds = _split_bounds(df_by_symbol, i)
        part = {s: [t for t in trades if bounds[s][0] <= t["exit_index"] < bounds[s][1]]
                 for s, trades in by_symbol.items()}
        splits.append(portfolio_stats(part)["total_r"])
    per_symbol_r = {s: sum(t["pnl_r"] for t in trades) for s, trades in by_symbol.items()}
    ranked = sorted(per_symbol_r.values(), reverse=True)
    return {
        "total_r": port["total_r"],
        "mdd_r": port["max_drawdown_r"],
        "trades": port["num_trades"],
        "splits": splits,
        "all_splits_positive": all(s > 0 for s in splits),
        "positive_symbols": sum(1 for v in per_symbol_r.values() if v > 0),
        "symbols": len(per_symbol_r),
        "top2_r": sum(ranked[:2]),
        "blocked": result["blocked"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="포트폴리오 제약을 반영한 파라미터 스윕")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--seeds", type=int, default=20,
                        help="배정 순서 seed 개수 — 결과 분포를 보기 위한 반복 횟수")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="편도 슬리피지(R). 비용 스트레스 테스트용")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="캔들을 저장/재사용할 디렉터리(스윕을 여러 번 돌릴 때)")
    parser.add_argument("--breaker-reset", choices=["daily", "never", "off"], default="daily",
                        help="연속손실 브레이커가 언제 풀리는지에 대한 가정. 실거래에서는 사람이 "
                              "수동 리셋해야 풀리므로(걸쇠) 이 가정 없이는 365일 시뮬레이션이 "
                              "첫 5연패에서 영구 정지되어 상한 비교가 불가능하다")
    args = parser.parse_args()

    client = get_futures_market_data_client()
    client.load_markets()
    symbols = [s for s in FUTURES_SYMBOLS if s in client.markets]

    df_by_symbol = {}
    for symbol in symbols:
        df = _load(client, symbol, args.days, args.cache_dir)
        if len(df) >= MIN_BARS:
            df_by_symbol[symbol] = df
        else:
            print(f"제외 {symbol}: {len(df)}봉 < {MIN_BARS}", flush=True)

    print(f"\n종목 {len(df_by_symbol)}개 · {TIMEFRAME} · 최근 {args.days}일 · seed {args.seeds}개")
    print(f"고정: 손절 {STOP_LOSS_PCT:.2%} · 손익비 {TAKE_PROFIT_RR} · ADX {RULE_ADX_THRESHOLD} · "
          f"SMA {RULE_SMA_PERIOD} · 레짐SMA {RULE_REGIME_SMA_PERIOD} · "
          f"수수료 편도 {FEE_PCT_PER_SIDE:.3%} · 슬리피지 편도 {args.slippage}R\n")

    # detect_signal은 저변동 하한과 무관하게 항상 같은 값을 낸다. 하한별로 gated_signals를
    # 그냥 다시 부르면 그 무거운 계산을 5번 반복하므로(종목당 8760봉 x 5), 종목마다 한 번만
    # 계산해서 memoize하고 gated_signals에는 조회 함수를 넘긴다 — 게이트 판정 자체는 여전히
    # gated_signals(= 실거래와 같은 함수)가 하므로 조건이 두 군데로 갈라지지 않는다.
    print("진입 신호 계산 중(종목당 한 번)...", flush=True)
    base = {}
    for symbol, df in df_by_symbol.items():
        lookup = {}
        for i in range(entry_start_bar(len(df), RULE_REGIME_SMA_PERIOD), len(df)):
            window = df.iloc[max(0, i - SIGNAL_LOOKBACK_BARS + 1):i + 1]
            lookup[i] = detect_signal(window, adx_threshold=RULE_ADX_THRESHOLD,
                                       sma_period=RULE_SMA_PERIOD, require_rsi_confirm=True)
        base[symbol] = lookup
        print(f"  {symbol.split('/')[0]}: 원신호 {sum(1 for v in lookup.values() if v)}건",
              flush=True)

    positions = {s: {key: i for i, key in enumerate(
        (df["timestamp"] if "timestamp" in df.columns else df.index.to_series()).tolist())}
        for s, df in df_by_symbol.items()}

    def _memoized(symbol):
        lookup, pos = base[symbol], positions[symbol]

        def signal_fn(window):
            key = window["timestamp"].iloc[-1] if "timestamp" in window.columns else window.index[-1]
            return lookup.get(pos.get(key))
        return signal_fn

    # 진입 후보는 저변동 하한에만 의존하고 동시보유 상한과는 무관하다 — 하한별로 한 번만 계산한다.
    signals_cache = {}
    for min_atr in MIN_ATR:
        signals_cache[min_atr] = {
            s: gated_signals(df, stop_loss_pct=STOP_LOSS_PCT,
                              regime_sma_period=RULE_REGIME_SMA_PERIOD,
                              min_atr_to_stop_ratio=min_atr, signal_fn=_memoized(s))
            for s, df in df_by_symbol.items()
        }
        total = sum(len(v) for v in signals_cache[min_atr].values())
        print(f"  진입 후보 (저변동 하한 {min_atr}): {total}건", flush=True)

    print(f"\n{'저변동':>6s} {'동시':>4s} {'거래':>6s} {'총R 중앙(5~95%)':>26s} {'MDD 중앙':>9s} "
          f"{'전구간+':>7s} {'양수종목':>8s} {'상위2몫':>7s} {'자리막힘':>8s} {'CB막힘':>7s} {'리셋필요':>8s}")
    rows = []
    for min_atr in MIN_ATR:
        for cap in CONCURRENCY:
            results = simulate_many(
                df_by_symbol, seeds=range(args.seeds),
                signals_by_symbol=signals_cache[min_atr],
                max_concurrent_positions=cap, breaker_reset=args.breaker_reset,
                stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                fee_pct_per_side=FEE_PCT_PER_SIDE, slippage_r_per_side=args.slippage,
                regime_sma_period=RULE_REGIME_SMA_PERIOD, min_atr_to_stop_ratio=min_atr,
            )
            stats = [_stats_for(r, df_by_symbol) for r in results]
            totals = sorted(s["total_r"] for s in stats)
            pass_rate = sum(1 for s in stats if s["all_splits_positive"]) / len(stats)
            median = st.median(totals)
            lo, hi = totals[max(0, int(len(totals) * 0.05))], totals[min(len(totals) - 1, int(len(totals) * 0.95))]
            row = {
                "min_atr_to_stop_ratio": min_atr, "max_concurrent_positions": cap,
                "trades_median": st.median(s["trades"] for s in stats),
                "total_r_median": round(median, 1),
                "total_r_p5": round(lo, 1), "total_r_p95": round(hi, 1),
                "mdd_r_median": round(st.median(s["mdd_r"] for s in stats), 1),
                "oos_pass_rate": round(pass_rate, 2),
                "positive_symbols_median": st.median(s["positive_symbols"] for s in stats),
                "symbols": stats[0]["symbols"],
                "top2_share_median": (round(st.median(
                    s["top2_r"] / s["total_r"] for s in stats if s["total_r"] > 0), 2)
                    if any(s["total_r"] > 0 for s in stats) else None),
                "blocked_max_positions_median": st.median(
                    s["blocked"]["max_positions"] for s in stats),
                "blocked_circuit_breaker_median": st.median(
                    s["blocked"]["consecutive_losses"] + s["blocked"]["daily_loss"] for s in stats),
                "breaker_resets_needed_median": st.median(
                    r["breaker_resets_needed"] for r in results),
            }
            rows.append(row)
            mark = " <- 현재" if (min_atr == MIN_ATR_TO_STOP_RATIO
                                   and cap == MAX_CONCURRENT_POSITIONS) else ""
            print(f"{min_atr:>6.2f} {cap:>4d} {row['trades_median']:>6.0f} "
                  f"{median:>+9.1f} ({lo:>+7.1f}~{hi:>+7.1f}) {row['mdd_r_median']:>+9.1f} "
                  f"{pass_rate:>6.0%} {row['positive_symbols_median']:>4.0f}/{row['symbols']:<3d} "
                  f"{str(row['top2_share_median']):>7s} {row['blocked_max_positions_median']:>8.0f} "
                  f"{row['blocked_circuit_breaker_median']:>7.0f} "
                  f"{row['breaker_resets_needed_median']:>8.0f}{mark}", flush=True)

    out = PROJECT_ROOT / "exports" / "portfolio_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "days": args.days, "seeds": args.seeds, "slippage_r_per_side": args.slippage,
        "breaker_reset": args.breaker_reset,
        "fee_pct_per_side": FEE_PCT_PER_SIDE, "symbols": sorted(df_by_symbol),
        "rows": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n결과 저장: {out}")
    print("해석: 특정 한 칸의 최고값을 고르지 말 것 — 넓고 평평한 영역을 볼 것. "
          "seed 분포(5~95%)가 넓으면 그 설정은 배정 운에 크게 좌우된다는 뜻이다.")
    print(f"      리셋 가정('{args.breaker_reset}')이 결과를 바꾸므로, 결론을 내기 전에 "
          "--breaker-reset never/off로도 돌려 민감도를 볼 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
