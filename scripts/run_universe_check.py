"""종목 확장 점검(2026-10-03) — 성과가 아니라 거래대금으로 정한 후보군(U30)에서 지금 실거래 규칙이 통하는가.

    python scripts/fetch_universe_ohlcv.py      # 후보 80종목 캔들(.ohlcv_cache_universe, 기존 캐시와 같은 기간)
    python scripts/run_universe_check.py [seeds] # A 종목별 / B 포트폴리오 게이트(dry-run, 원장 안 남김)
    python scripts/run_symbol_selection_check.py # C 과거 성과로 고른 종목이 다음 달에도 나은가

결과와 해석은 UPDATE_LOG 2026-10-03. 전부 탐색 구간만 쓴다(홀드아웃은 잘라 버린다)."""
import glob
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8")

import pandas as pd

import run_experiment as rx
from src.backtest import gate
from src.backtest.data import slice_window
from src.backtest.engine import gated_signals, run_backtest
from src.core.config import FUTURES_SYMBOLS

UNI = ROOT / ".ohlcv_cache_universe"
OLD = ROOT / ".ohlcv_cache"
CUTOFF = pd.Timestamp("2026-06-24 03:00")  # 기존 캐시 마지막 봉 - 90일 (run_experiment와 같은 경계)
N_UNIVERSE = 30
SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = ROOT / "exports"

params = rx._defaults()


def sym_from_path(p):
    name = Path(p).stem  # BTC_USDT-USDT
    base, rest = name.split("_", 1)
    return f"{base}/{rest.replace('-', ':')}"


def load(dirpath):
    out = {}
    for p in glob.glob(str(dirpath / "*.json")):
        if Path(p).name.startswith("_"):
            continue
        df = pd.read_json(p)
        if df.empty:
            print("빈 파일:", Path(p).name)
            continue
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = slice_window(df, end=CUTOFF)
        if len(df) >= rx.MIN_BARS:
            out[sym_from_path(p)] = df
    return out


def signals_for(df_by_symbol, cache_dir):
    out = {}
    for symbol, df in df_by_symbol.items():
        lookup = rx._signal_lookup(symbol, df, params, cache_dir)
        at = {ts: i for i, ts in enumerate(df["timestamp"].tolist())}

        def fn(window, _l=lookup, _a=at):
            return _l.get(_a.get(window["timestamp"].iloc[-1]))
        out[symbol] = (fn, gated_signals(
            df, stop_loss_pct=params["stop_loss_pct"], regime_sma_period=params["regime_sma_period"],
            min_atr_to_stop_ratio=params["min_atr_to_stop_ratio"], htf_hours=params["htf_hours"],
            signal_fn=fn))
    return out


def gate_run(label, dfs, sigs):
    signals = {s: sigs[s][1] for s in dfs}
    base, base_results = rx._run_one(dfs, params, seeds=SEEDS, breaker_reset="daily",
                                     slippage=0.0, signals_by_symbol=signals)
    stress, _ = rx._run_one(dfs, params, seeds=SEEDS, breaker_reset="daily",
                            slippage=gate.STRESS_SLIPPAGE_R_PER_SIDE, signals_by_symbol=signals)
    verdict = gate.evaluate(base, stress)
    info = rx._info(base_results, dfs)
    row = {
        "label": label, "symbols": len(dfs), "trades": base["trades_median"],
        "total_r": round(base["total_r_median"], 1), "mdd_r": round(base["mdd_r_median"], 1),
        "recovery": base["recovery_factor_median"], "top2": base["top2_share_median"],
        "pos_share": base["positive_symbol_share_median"], "oos": base["oos_pass_rate"],
        "stress": round(stress["total_r_median"], 1), "win": info["win_rate"],
        "splits": [round(v, 1) for v in info["splits"]],
        "verdict": "PASS" if verdict["passed"] else "FAIL", "failed": verdict["failed"],
    }
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
    return row


def main():
    uni = load(UNI)
    old = {s: df for s, df in load(OLD).items() if s in FUTURES_SYMBOLS}

    # U30: 탐색 구간 일 거래대금 중앙값 상위 30 (성과를 보지 않는다)
    def daily_qv(df):
        qv = (df["close"] * df["volume"]).groupby(df["timestamp"].dt.date).sum()
        return float(qv.median())
    ranked = sorted(uni, key=lambda s: daily_qv(uni[s]), reverse=True)
    u30 = ranked[:N_UNIVERSE]
    print("U30:", ", ".join(s.split("/")[0] for s in u30))
    print("기존12 중 U30에 든 것:", [s.split("/")[0] for s in old if s in u30])

    sig_uni = signals_for({s: uni[s] for s in u30}, UNI)
    sig_old = signals_for(old, OLD)

    # --- A. 종목별 단일 백테스트
    per = {}
    for label, dfs, sigs in (("기존", old, sig_old), ("U30", {s: uni[s] for s in u30}, sig_uni)):
        for s, df in dfs.items():
            trades = run_backtest(df, signal_fn=sigs[s][0])
            ts = df["timestamp"]
            per.setdefault(s, {"group": set(), "trades": [
                {"exit": str(ts.iloc[t["exit_index"]]), "r": t["pnl_r"]} for t in trades]})
            per[s]["group"].add(label)
    print("\n[A] 종목별 (탐색 구간, 단일 백테스트)")
    for s, v in sorted(per.items(), key=lambda kv: -sum(t["r"] for t in kv[1]["trades"])):
        rs = [t["r"] for t in v["trades"]]
        print(f"  {s.split('/')[0]:<10} {'/'.join(sorted(v['group'])):<8} 거래{len(rs):>4} 총R{sum(rs):+7.1f} "
              f"건당{(sum(rs)/len(rs) if rs else 0):+.3f}")
    json.dump({s: {"group": sorted(v["group"]), "trades": v["trades"]} for s, v in per.items()},
              open(OUT / "per_symbol.json", "w", encoding="utf-8"))

    # --- B. 포트폴리오 게이트
    print("\n[B] 포트폴리오 게이트 (seed", SEEDS, ")")
    rows = [gate_run("기존12", old, sig_old),
            gate_run("U30", {s: uni[s] for s in u30}, sig_uni)]
    json.dump(rows, open(OUT / "gate_rows.json", "w", encoding="utf-8"), ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
