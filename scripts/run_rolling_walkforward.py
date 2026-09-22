"""손절폭·손익비(·손익분기 이동)를 **롤링 워크포워드**로 고른다 — 로드맵 Phase 3의 첫 구현.

지금까지의 4분할 검증은 같은 데이터로 필터를 고른 뒤 그 데이터를 평가한 인샘플이었다. 여기서는
각 테스트 구간(30일)마다 **그 이전 구간만 보고** 설정을 고른 뒤 바로 다음 30일에만 적용하고,
그 테스트 구간 결과만 이어 붙여 OOS 자산곡선을 만든다.

구현 메모:
- 설정마다 전 기간을 `portfolio.simulate_portfolio`로 한 번 돌리고 거래를 진입 시각으로 창에
  배정한다. 창마다 다시 돌리면 레짐 SMA400의 워밍업(400봉)이 창마다 날아가서 신호가 달라진다.
  대가는 창 경계에서 동시보유/브레이커 상태가 설정 간에 이어지지 않는다는 것뿐이다.
- 슬리피지는 **가격 기준**(bp)으로 받아 설정별 손절폭으로 R 환산한다. R 기준으로 고정하면
  손절폭이 넓은 설정이 비용을 부당하게 더 많이(가격으로) 내는 셈이 된다.
- 배정 순서는 seed 여러 개의 평균을 쓴다.

    python scripts/run_rolling_walkforward.py --cache-dir .ohlcv_cache_3y
"""

import argparse
import itertools
import json
import statistics as st
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from concurrent.futures import ProcessPoolExecutor  # noqa: E402

import pandas as pd  # noqa: E402

from src.backtest.engine import SIGNAL_LOOKBACK_BARS, entry_start_bar, gated_signals  # noqa: E402
from src.backtest.portfolio import simulate_portfolio  # noqa: E402
from src.core.config import (  # noqa: E402
    FEE_PCT_PER_SIDE,
    FUTURES_SYMBOLS,
    RULE_ADX_THRESHOLD,
    RULE_REGIME_SMA_PERIOD,
    RULE_SMA_PERIOD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_RR,
)
from src.core.futures_strategy import detect_signal  # noqa: E402

STOPS = [0.0125, 0.0175, 0.025]
RRS = [1.0, 1.25, 1.5, 2.0, 2.5]
BREAKEVEN = [False, True]          # True면 +1R 도달 시 손절을 진입가로(익절이 1R 초과일 때만 의미)
TRAIN_MIN_DAYS = 180
TEST_DAYS = 30
MIN_TRAIN_TRADES = 30


def _load(cache_dir: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol in FUTURES_SYMBOLS:
        path = cache_dir / f"{symbol.replace('/', '_').replace(':', '-')}.json"
        if path.exists():
            df = pd.read_json(path)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            out[symbol] = df.reset_index(drop=True)
    return out


def _raw_signals(df: pd.DataFrame) -> dict[int, str]:
    """detect_signal만(게이트 전) 봉마다 한 번. 손절폭과 무관하므로 설정 간에 재사용한다 —
    3년치 12종목이면 이게 계산의 대부분이다(run_portfolio_sweep.py와 같은 memoize)."""
    out = {}
    for i in range(entry_start_bar(len(df), RULE_REGIME_SMA_PERIOD), len(df)):
        window = df.iloc[max(0, i - SIGNAL_LOOKBACK_BARS + 1):i + 1]
        signal = detect_signal(window, adx_threshold=RULE_ADX_THRESHOLD,
                                sma_period=RULE_SMA_PERIOD, require_rsi_confirm=True)
        if signal:
            out[i] = signal
    return out


def _load_raw_signals(dfs, cache_dir: Path) -> dict[str, dict[int, str]]:
    path = cache_dir / "_raw_signals.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = {s: {int(k): v for k, v in cached[s].items()} for s in dfs if s in cached}
    todo = [s for s in dfs if s not in raw]
    if todo:
        with ProcessPoolExecutor() as pool:
            for s, sig in zip(todo, pool.map(_raw_signals, [dfs[s] for s in todo])):
                raw[s] = sig
        path.write_text(json.dumps({s: {str(k): v for k, v in sig.items()} for s, sig in raw.items()}),
                        encoding="utf-8")
    return raw


def _gated(dfs, raw, stop: float) -> dict[str, dict[int, str]]:
    out = {}
    for s, df in dfs.items():
        stamps = {t: i for i, t in enumerate(df["timestamp"])}
        lookup = raw[s]
        out[s] = gated_signals(df, stop_loss_pct=stop,
                                signal_fn=lambda w, lookup=lookup, stamps=stamps:
                                lookup.get(stamps.get(w["timestamp"].iloc[-1])))
    return out


_W = {}


def _init_worker(cache_dir: str):
    dfs = _load(Path(cache_dir))
    raw = _load_raw_signals(dfs, Path(cache_dir))
    _W["dfs"] = dfs
    _W["signals"] = {stop: _gated(dfs, raw, stop) for stop in STOPS}


def _job(args):
    cfg, slip, seeds = args
    return _label(cfg), slip, _trades(_W["dfs"], _W["signals"][cfg["stop"]], cfg, slip, seeds)


def _configs():
    for stop, rr, be in itertools.product(STOPS, RRS, BREAKEVEN):
        if be and rr <= 1.0:
            continue
        yield {"stop": stop, "rr": rr, "breakeven": be}


def _label(cfg: dict) -> str:
    return f"SL{cfg['stop'] * 100:.2f}%·RR{cfg['rr']:.2f}" + ("·BE" if cfg["breakeven"] else "")


def _trades(dfs, signals, cfg, slip_bp: float, seeds) -> list[dict]:
    """seed별 거래를 전부 모아서 돌려준다(각 거래에 weight=1/len(seeds)) — 합계가 seed 평균이 된다."""
    out = []
    for seed in seeds:
        result = simulate_portfolio(
            dfs, stop_loss_pct=cfg["stop"], take_profit_rr=cfg["rr"],
            use_breakeven=cfg["breakeven"], breakeven_at_r=1.0,
            fee_pct_per_side=FEE_PCT_PER_SIDE, slippage_r_per_side=slip_bp / 10000 / cfg["stop"],
            signals_by_symbol=signals, seed=seed)
        for t in result["trades"]:
            df = dfs[t["symbol"]]
            out.append({"entry_time": df["timestamp"].iloc[t["entry_index"]],
                        "exit_step": t["exit_step"], "symbol": t["symbol"], "side": t["side"],
                        "pnl_r": t["pnl_r"], "w": 1 / len(seeds)})
    return out


def _stats(trades: list[dict]) -> dict:
    n = sum(t["w"] for t in trades)
    if not n:
        return {"n": 0, "win": None, "total_r": 0.0, "avg_r": None, "mdd": 0.0}
    total = sum(t["pnl_r"] * t["w"] for t in trades)
    wins = sum(t["w"] for t in trades if t["pnl_r"] > 0)
    cum = peak = mdd = 0.0
    for t in sorted(trades, key=lambda t: t["exit_step"]):
        cum += t["pnl_r"] * t["w"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"n": n, "win": wins / n, "total_r": total, "avg_r": total / n, "mdd": mdd}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=".ohlcv_cache_3y")
    ap.add_argument("--slip-bp", type=float, nargs="+", default=[0.0, 3.0, 6.0],
                    help="편도 슬리피지(가격 bp). 첫 값이 아니라 --select-slip으로 고른 값에서 선택한다")
    ap.add_argument("--select-slip", type=float, default=3.0)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default="exports/rolling_walkforward.json")
    args = ap.parse_args()

    dfs = _load(PROJECT_ROOT / args.cache_dir)
    seeds = list(range(args.seeds))
    start = min(df["timestamp"].iloc[0] for df in dfs.values())
    end = max(df["timestamp"].iloc[-1] for df in dfs.values())
    first_test = start + pd.Timedelta(days=TRAIN_MIN_DAYS)
    windows = []
    t0 = first_test
    while t0 + pd.Timedelta(days=TEST_DAYS) <= end + pd.Timedelta(hours=1):
        windows.append((t0, t0 + pd.Timedelta(days=TEST_DAYS)))
        t0 += pd.Timedelta(days=TEST_DAYS)
    print(f"데이터 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}, 종목 {len(dfs)}, 테스트 창 {len(windows)}개")

    _load_raw_signals(dfs, PROJECT_ROOT / args.cache_dir)  # 워커들이 파일에서 읽도록 먼저 채운다
    configs = list(_configs())
    jobs = [(cfg, slip, seeds) for cfg in configs for slip in args.slip_bp]
    trades = {}  # (label, slip) -> trades
    with ProcessPoolExecutor(initializer=_init_worker,
                             initargs=(str(PROJECT_ROOT / args.cache_dir),)) as pool:
        for label, slip, ts in pool.map(_job, jobs):
            trades[(label, slip)] = ts
            print(".", end="", flush=True)
    print()

    def within(ts, a, b):
        return [t for t in ts if a <= t["entry_time"] < b]

    labels = [_label(c) for c in configs]
    current = _label({"stop": STOP_LOSS_PCT, "rr": TAKE_PROFIT_RR, "breakeven": False})
    report = {"windows": [], "configs": {}, "current": current,
              "slip_bp": args.slip_bp, "select_slip": args.select_slip}

    oos = {slip: [] for slip in args.slip_bp}
    for a, b in windows:
        # 선택: 이 창 이전(학습 구간, 확장형)만 본다 — select_slip 비용에서의 총R 최대.
        scored = []
        for label in labels:
            s = _stats(within(trades[(label, args.select_slip)], start, a))
            if s["n"] >= MIN_TRAIN_TRADES:
                scored.append((s["total_r"], label))
        chosen = max(scored)[1] if scored else current
        row = {"test_start": str(a.date()), "chosen": chosen}
        for slip in args.slip_bp:
            test = within(trades[(chosen, slip)], a, b)
            oos[slip].extend(test)
            row[f"oos_r@{slip}"] = round(_stats(test)["total_r"], 2)
            row[f"current_r@{slip}"] = round(_stats(within(trades[(current, slip)], a, b))["total_r"], 2)
        report["windows"].append(row)

    oos_span = (windows[0][0], windows[-1][1])
    for label in labels:
        report["configs"][label] = {
            str(slip): _stats(within(trades[(label, slip)], *oos_span)) for slip in args.slip_bp}

    report["oos_selected"] = {str(s): _stats(oos[s]) for s in args.slip_bp}
    report["oos_current"] = {str(s): _stats(within(trades[(current, s)], *oos_span)) for s in args.slip_bp}

    # 출력
    print("\n=== 창별 선택 (선택 기준: 편도 %.0fbp 비용에서 학습구간 총R 최대) ===" % args.select_slip)
    for row in report["windows"]:
        print(f"{row['test_start']}  선택 {row['chosen']:<24} "
              + "  ".join(f"OOS@{s:g}bp {row[f'oos_r@{s}']:+6.1f} / 현재 {row[f'current_r@{s}']:+6.1f}"
                          for s in args.slip_bp))
    print("\n=== OOS 합계 (워크포워드 선택 vs 현재 설정 고정) ===")
    for s in args.slip_bp:
        a, c = report["oos_selected"][str(s)], report["oos_current"][str(s)]
        print(f"편도 {s:g}bp  선택: n={a['n']:.0f} 승률 {a['win']:.1%} 총R {a['total_r']:+.1f} "
              f"건당 {a['avg_r']:+.3f} MDD {a['mdd']:.1f}   |   현재: n={c['n']:.0f} 승률 {c['win']:.1%} "
              f"총R {c['total_r']:+.1f} 건당 {c['avg_r']:+.3f} MDD {c['mdd']:.1f}")
    print("\n=== 설정별 전체 OOS 구간 성과 (주의: 여기서 최고를 고르면 그게 인샘플이다) ===")
    for label in labels:
        cells = report["configs"][label]
        print(f"{label:<24} " + "  ".join(
            f"@{s:g}bp 승률 {cells[str(s)]['win']:.0%} 총R {cells[str(s)]['total_r']:+7.1f} "
            f"MDD {cells[str(s)]['mdd']:6.1f}" for s in args.slip_bp))

    out = PROJECT_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
