"""실험 한 건 = 원장 한 줄. 사전 등록된 게이트가 PASS/FAIL을 판정한다.

이 스크립트는 `docs/BACKTEST_PROTOCOL.md`의 실행부다. 기존 스윕(`run_portfolio_sweep.py`)이
표를 찍고 사람이 고르는 도구라면, 이쪽은 **한 설정을 판정하고 그 시도를 지워지지 않게 남기는**
도구다. 차이가 중요한 이유: 스윕 표에는 "이 결론이 몇 번째 시도인지"가 안 남는다. 30칸짜리
표에서 제일 좋은 칸을 고르는 것은 30번 시도한 것이고, 순수 잡음에서도 그럴듯한 칸이 나온다.

    # 지금 실거래 설정을 그대로 판정 (탐색 구간)
    python scripts/run_experiment.py --note "현재 .env 설정 기준선"

    # 한 파라미터만 바꿔서 판정
    python scripts/run_experiment.py --set stop_loss_pct=0.025 --note "손절 2.5% 시도"

    # 원장 보기
    python scripts/run_experiment.py --ledger

홀드아웃(최근 90일)은 `--window holdout`으로만 열리고, 탐색 구간에서 PASS한 설정이 원장에
있어야 하며, `--confirm-holdout`을 같이 줘야 한다. 이유는 프로토콜 문서에 적혀 있다.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src.backtest import gate  # noqa: E402
from src.backtest.data import load_cached_ohlcv, slice_window  # noqa: E402
from src.backtest.engine import (  # noqa: E402
    SIGNAL_LOOKBACK_BARS,
    entry_start_bar,
    gated_signals,
)
from src.backtest.portfolio import simulate_many  # noqa: E402
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
from src.core.futures_strategy import (  # noqa: E402
    DEFAULT_DIRECTION_FILTER,
    VALID_DIRECTION_FILTERS,
    detect_signal,
)
from src.data.futures_exchange import get_futures_market_data_client  # noqa: E402

TIMEFRAME = "1h"
MIN_BARS = 900
LEDGER_PATH = PROJECT_ROOT / "docs" / "experiments.tsv"

# 홀드아웃: 최근 90일은 탐색에 쓰지 않는다. 한 번 보면 되돌릴 수 없으므로 기간을 코드에 박아
# 두고, 여는 조건도 코드가 강제한다(사람 기억에 맡기면 반드시 새어 나간다).
HOLDOUT_DAYS = 90

# 사전 등록된 탐색 공간. **여기 없는 값은 바꿀 수 없다.**
# Karpathy의 autoresearch에서 에이전트가 train.py만 건드릴 수 있는 것과 같은 제약이다 —
# 탐색 중에 조용히 다른 손잡이를 돌리기 시작하면 원장의 행끼리 비교가 안 된다.
SEARCHABLE = {
    "stop_loss_pct": float,
    "take_profit_rr": float,
    "adx_threshold": float,
    "sma_period": int,
    "regime_sma_period": int,
    "min_atr_to_stop_ratio": float,
    "max_concurrent_positions": int,
    # 청산 관리(2026-09-24 추가). 저장소 관례대로 **0이면 끈다** — 켜고 끄는 불리언을 따로 두면
    # "off인데 값은 1.0"처럼 원장에서 읽을 수 없는 조합이 생긴다.
    # 주의: 이 둘은 **실거래 봇에 아직 구현이 없다.** 게이트를 통과해도 채택하려면 브라켓 주문
    # 수정 경로를 먼저 만들어야 한다(백테스트와 실거래가 같은 규칙이어야 한다는 원칙).
    "breakeven_at_r": float,    # 이만큼 유리하게 가면 손절을 진입가로 옮긴다. 0=끔
    "max_hold_bars": int,       # 이 봉 수를 넘기면 시장가 청산. 0=끔
    "partial_at_r": float,      # 이 지점에서 일부 익절(스케일 아웃). 0=끔
    "partial_fraction": float,  # 부분 익절로 닫는 비중
    "breakeven_after_partial": int,  # 부분 익절 뒤 남은 물량의 손절을 진입가로. 1/0
    # SMA 돌파의 방향을 무엇으로 확인하는가: "rsi"(실거래 현재) / "di"(+DI>-DI) / "none"
    "direction_filter": str,
}

LEDGER_COLUMNS = [
    "run_id", "utc", "commit", "gate_version", "window", "days", "seeds", "breaker_reset",
    "params", "trades", "total_r_median", "total_r_p5", "mdd_r_median", "oos_pass_rate",
    "recovery_factor", "top2_share", "positive_symbol_share", "stress_total_r",
    "verdict", "failed", "note",
]


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _defaults() -> dict:
    return {
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_rr": TAKE_PROFIT_RR,
        "adx_threshold": RULE_ADX_THRESHOLD,
        "sma_period": RULE_SMA_PERIOD,
        "regime_sma_period": RULE_REGIME_SMA_PERIOD,
        "min_atr_to_stop_ratio": MIN_ATR_TO_STOP_RATIO,
        "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        # 실거래 봇에 이 둘이 없으므로 기본값은 "끔"이다 — 그래야 기준선이 실거래와 같다.
        "breakeven_at_r": 0.0,
        "max_hold_bars": 0,
        "partial_at_r": 0.0,
        "partial_fraction": 0.5,
        "breakeven_after_partial": 1,
        "direction_filter": DEFAULT_DIRECTION_FILTER,
    }


def _parse_overrides(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set 형식은 key=value여야 한다: {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in SEARCHABLE:
            raise SystemExit(
                f"탐색 공간에 없는 파라미터: {key!r}\n"
                f"허용: {', '.join(sorted(SEARCHABLE))}\n"
                "새 손잡이를 열려면 docs/BACKTEST_PROTOCOL.md를 먼저 고치고 그 커밋을 남길 것.")
        out[key] = SEARCHABLE[key](raw)
    return out


def rejected_ids(rows: list[dict]) -> set[str]:
    """기각된 실험 ID. `verdict="REJECTED"` 행이 `failed` 칸에 대상 ID를 들고 있다.

    왜 필요한가(2026-09-24, E0007에서 실제로 걸렸다): PASS가 나와도 이웃값이 하나도 못 따라오면
    그건 잡음에서 튄 한 칸이라 채택할 수 없다. 그런데 **원장에 PASS 행이 남아 있는 한 홀드아웃
    잠금이 풀린다** — 기각한 설정 때문에 홀드아웃이 열리면 봉인의 의미가 없어진다.

    기각도 **지우지 않고 한 줄 더 쌓는다**(원장은 append-only다). `failed` 칸을 재사용하는 이유는
    칼럼을 추가하면 이미 쓰인 헤더와 어긋나 옛 행이 밀리기 때문이다.
    """
    return {row.get("failed", "").strip() for row in rows
            if row.get("verdict") == "REJECTED" and row.get("failed", "").strip()}


def adoptable_passes(rows: list[dict]) -> list[dict]:
    """탐색 구간에서 **지금 게이트 버전으로** PASS했고 아직 기각되지 않은 행.

    버전 조건이 필요한 이유(2026-09-28): 게이트 v1의 PASS(E0027·E0031)가 v2 비용 모델로 다시
    재니 둘 다 FAIL했다(E0036·E0037). 옛 기준으로 통과한 행이 새 기준에서 홀드아웃을 열 수
    있으면, 기준을 고친 의미가 없어진다 — 프로토콜의 "버전이 다른 행끼리 비교하지 않는다"를
    잠금에도 적용한 것."""
    dropped = rejected_ids(rows)
    return [row for row in rows
            if row.get("verdict") == "PASS" and row.get("window") == "search"
            and str(row.get("gate_version", "")).strip() == str(gate.GATE_VERSION)
            and row.get("run_id") not in dropped]


def read_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line.strip()]


def append_ledger(row: dict) -> None:
    """원장은 append-only다. 기존 행을 고치거나 지우지 않는다 — 실패한 시도의 개수가
    남아야 '몇 번 만에 나온 결론인지'를 나중에 알 수 있다."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LEDGER_PATH.exists()
    with LEDGER_PATH.open("a", encoding="utf-8", newline="") as fh:
        if new_file:
            fh.write("\t".join(LEDGER_COLUMNS) + "\n")
        fh.write("\t".join(str(row.get(c, "")) for c in LEDGER_COLUMNS) + "\n")


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _signal_lookup(symbol: str, df: pd.DataFrame, params: dict, cache_dir: Path | None) -> dict:
    """봉 위치 -> 원신호. 저변동/레짐 게이트 **전**의 detect_signal 결과만 캐시한다.

    게이트 판정 자체는 항상 `gated_signals`(= 실거래와 같은 함수)가 하므로 조건이 두 군데로
    갈라지지 않는다. 캐시하는 건 무거운 지표 계산뿐이고, 그래서 손절폭이나 저변동 하한을
    바꿔가며 돌려도 재계산이 필요 없다(그 둘은 detect_signal에 안 들어간다).
    """
    key = (f"{symbol.replace('/', '_').replace(':', '-')}"
           f"__adx{params['adx_threshold']}__sma{params['sma_period']}"
           f"__dir{params['direction_filter']}"
           f"__n{len(df)}__{df['timestamp'].iloc[0]:%Y%m%d%H}-{df['timestamp'].iloc[-1]:%Y%m%d%H}")
    path = (cache_dir / "signals" / f"{key}.json") if cache_dir else None
    if path is not None and path.exists():
        return {int(k): v for k, v in json.loads(path.read_text()).items()}

    lookup = {}
    for i in range(entry_start_bar(len(df), params["regime_sma_period"]), len(df)):
        window = df.iloc[max(0, i - SIGNAL_LOOKBACK_BARS + 1):i + 1]
        signal = detect_signal(window, adx_threshold=params["adx_threshold"],
                               sma_period=params["sma_period"], require_rsi_confirm=True,
                               direction_filter=params["direction_filter"])
        if signal:
            lookup[i] = signal
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lookup), encoding="utf-8")
    return lookup


def _run_one(df_by_symbol: dict[str, pd.DataFrame], params: dict, *, seeds: int,
             breaker_reset: str, slippage: float, signals_by_symbol: dict) -> dict:
    results = simulate_many(
        df_by_symbol, seeds=range(seeds),
        signals_by_symbol=signals_by_symbol,
        max_concurrent_positions=params["max_concurrent_positions"],
        breaker_reset=breaker_reset,
        stop_loss_pct=params["stop_loss_pct"], take_profit_rr=params["take_profit_rr"],
        fee_pct_per_side=FEE_PCT_PER_SIDE, slippage_r_per_side=slippage,
        regime_sma_period=params["regime_sma_period"],
        min_atr_to_stop_ratio=params["min_atr_to_stop_ratio"],
        # 0을 "끔"으로 쓰는 관례를 엔진의 불리언 인자로 옮긴다.
        use_breakeven=params["breakeven_at_r"] > 0,
        breakeven_at_r=params["breakeven_at_r"] or 1.0,
        use_max_hold=params["max_hold_bars"] > 0,
        max_hold_bars=params["max_hold_bars"] or 72,
        use_partial_tp=params["partial_at_r"] > 0,
        partial_at_r=params["partial_at_r"] or 1.0,
        partial_fraction=params["partial_fraction"],
        breakeven_after_partial=bool(params["breakeven_after_partial"]),
    )
    return gate.summarize(results, df_by_symbol)


def _reject(run_id: str, reason: str) -> int:
    """PASS 행을 기각한다. 원장의 기존 줄은 손대지 않고 REJECTED 줄을 하나 더 쌓는다."""
    rows = read_ledger()
    target = next((r for r in rows if r.get("run_id") == run_id), None)
    if target is None:
        print(f"거부: 원장에 {run_id}가 없다.")
        return 2
    if target.get("verdict") != "PASS":
        print(f"거부: {run_id}는 PASS가 아니다(판정 {target.get('verdict')}) — 기각할 것이 없다.")
        return 2
    if run_id in rejected_ids(rows):
        print(f"{run_id}는 이미 기각돼 있다.")
        return 0
    if not reason:
        print("거부: --note로 기각 이유를 남겨야 한다. 이유 없는 기각은 나중에 재현할 수 없다.")
        return 2

    append_ledger({
        "run_id": f"E{len(rows) + 1:04d}",
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git_commit(),
        "gate_version": gate.GATE_VERSION,
        "window": target.get("window", ""),
        "params": target.get("params", ""),
        "verdict": "REJECTED",
        "failed": run_id,
        "note": reason.replace("\t", " "),
    })
    print(f"{run_id} 기각 기록. 이제 이 PASS로는 홀드아웃이 열리지 않는다.")
    return 0


def _print_ledger() -> int:
    rows = read_ledger()
    if not rows:
        print("원장이 비어 있다. 아직 기록된 실험이 없다.")
        return 0
    print(f"{'ID':>6s} {'창':>8s} {'총R(중앙)':>10s} {'하위5%':>8s} {'OOS':>5s} "
          f"{'스트레스':>8s} {'판정':>6s}  설정 / 메모")
    for row in rows:
        print(f"{row['run_id']:>6s} {row['window']:>8s} {row['total_r_median']:>10s} "
              f"{row['total_r_p5']:>8s} {row['oos_pass_rate']:>5s} "
              f"{row['stress_total_r']:>8s} {row['verdict']:>6s}  "
              f"{row['params']} {row['note']}")
    adoptable = adoptable_passes(rows)
    dropped = rejected_ids(rows)
    print(f"\n총 {len(rows)}회 기록 · 탐색 구간 PASS "
          f"{sum(1 for r in rows if r['verdict'] == 'PASS')}회 "
          f"(기각 {len(dropped)}회 → 채택 가능 {len(adoptable)}개)")
    print("시도 횟수가 많을수록 한 번의 PASS가 의미하는 바는 작아진다 — 프로토콜의 다중비교 항목 참고.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="게이트 판정 실험 한 건")
    parser.add_argument("--set", dest="overrides", nargs="*", default=[],
                        help="파라미터 덮어쓰기 (예: stop_loss_pct=0.025)")
    parser.add_argument("--window", choices=["search", "holdout", "full"], default="search",
                        help="search=홀드아웃 제외 구간(기본), holdout=봉인된 최근 구간")
    parser.add_argument("--confirm-holdout", action="store_true",
                        help="홀드아웃을 여는 명시적 확인. 되돌릴 수 없다")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--breaker-reset", choices=["daily", "never", "off"], default="daily")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / ".ohlcv_cache")
    parser.add_argument("--note", default="")
    parser.add_argument("--ledger", action="store_true", help="원장만 출력하고 끝낸다")
    parser.add_argument("--reject", metavar="RUN_ID",
                        help="PASS한 실험을 기각한다(이웃값이 안 따라오는 고립된 칸 등). "
                             "원장에 한 줄을 더 쌓으며 기존 행은 건드리지 않고, 그 뒤로는 "
                             "그 PASS로 홀드아웃이 열리지 않는다. --note로 이유를 남길 것")
    parser.add_argument("--dry-run", action="store_true", help="판정만 하고 원장에 안 남긴다")
    args = parser.parse_args()

    if args.ledger:
        return _print_ledger()
    if args.reject:
        return _reject(args.reject, args.note)

    params = _defaults() | _parse_overrides(args.overrides)
    if params["direction_filter"] not in VALID_DIRECTION_FILTERS:
        print(f"거부: direction_filter는 {VALID_DIRECTION_FILTERS} 중 하나여야 한다.")
        return 2
    if params["partial_at_r"] > 0 and params["breakeven_at_r"] > 0:
        print("거부: 부분 익절과 손익분기 이동은 엔진이 동시에 지원하지 않는다. 하나만 켤 것.")
        return 2

    if args.window == "holdout":
        if not args.confirm_holdout:
            print("거부: 홀드아웃은 --confirm-holdout 없이 열 수 없다.")
            print("한 번 보면 그 구간은 더 이상 '안 본 데이터'가 아니다 — 프로토콜 참고.")
            return 2
        rows = read_ledger()
        if not adoptable_passes(rows):
            passed = [r["run_id"] for r in rows if r.get("verdict") == "PASS"]
            print("거부: 탐색 구간에서 PASS했고 기각되지 않은 설정이 원장에 없다.")
            if passed:
                print(f"       PASS는 있으나 전부 기각됐다: {', '.join(passed)}")
            print("홀드아웃은 후보를 고르는 곳이 아니라 이미 고른 후보를 **확인**하는 곳이다.")
            return 2

    client = get_futures_market_data_client()
    client.load_markets()
    symbols = [s for s in FUTURES_SYMBOLS if s in client.markets]

    raw = {}
    for symbol in symbols:
        df = load_cached_ohlcv(client, symbol, timeframe=TIMEFRAME, days=args.days,
                               cache_dir=args.cache_dir)
        if len(df) >= MIN_BARS:
            raw[symbol] = df
        else:
            print(f"제외 {symbol}: {len(df)}봉 < {MIN_BARS}")

    last = max(df["timestamp"].iloc[-1] for df in raw.values())
    cutoff = last - timedelta(days=HOLDOUT_DAYS)
    if args.window == "search":
        df_by_symbol = {s: slice_window(df, end=cutoff) for s, df in raw.items()}
    elif args.window == "holdout":
        df_by_symbol = {s: slice_window(df, start=cutoff) for s, df in raw.items()}
    else:
        df_by_symbol = raw
    df_by_symbol = {s: df for s, df in df_by_symbol.items() if len(df) >= MIN_BARS}

    print(f"\n창: {args.window} (홀드아웃 경계 {cutoff:%Y-%m-%d})")
    print(f"종목 {len(df_by_symbol)}개 · {TIMEFRAME} · seed {args.seeds}개 · "
          f"브레이커 리셋 '{args.breaker_reset}'")
    print(f"설정: {json.dumps(params, ensure_ascii=False)}")
    print(f"게이트 v{gate.GATE_VERSION} · 수수료 편도 {FEE_PCT_PER_SIDE:.3%} · "
          f"스트레스 슬리피지 편도 {gate.STRESS_SLIPPAGE_R_PER_SIDE}R\n")

    print("진입 신호 계산(캐시 사용)...", flush=True)
    signals = {}
    for symbol, df in df_by_symbol.items():
        lookup = _signal_lookup(symbol, df, params, args.cache_dir)
        # 타임스탬프 -> 봉 위치. 매 봉 df를 훑으면 O(n^2)이라 한 번만 만들어 둔다.
        at = {ts: i for i, ts in enumerate(df["timestamp"].tolist())}

        def signal_fn(window, _lookup=lookup, _at=at):
            return _lookup.get(_at.get(window["timestamp"].iloc[-1]))

        signals[symbol] = gated_signals(
            df, stop_loss_pct=params["stop_loss_pct"],
            regime_sma_period=params["regime_sma_period"],
            min_atr_to_stop_ratio=params["min_atr_to_stop_ratio"],
            signal_fn=signal_fn)
    print(f"  진입 후보 {sum(len(v) for v in signals.values())}건", flush=True)

    print("기본 비용 시뮬레이션...", flush=True)
    base = _run_one(df_by_symbol, params, seeds=args.seeds, breaker_reset=args.breaker_reset,
                    slippage=0.0, signals_by_symbol=signals)
    print("스트레스 비용 시뮬레이션...", flush=True)
    stress = _run_one(df_by_symbol, params, seeds=args.seeds, breaker_reset=args.breaker_reset,
                      slippage=gate.STRESS_SLIPPAGE_R_PER_SIDE, signals_by_symbol=signals)

    verdict = gate.evaluate(base, stress)

    print(f"\n{'항목':<24s} {'값':>10s} {'기준':>10s}  판정")
    for check in verdict["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"{check['name']:<24s} {_fmt(check['value']):>10s} "
              f"{_fmt(check['threshold']):>10s}  {mark}   {check['why']}")

    rows = read_ledger()
    run_id = f"E{len(rows) + 1:04d}"
    row = {
        "run_id": run_id,
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git_commit(),
        "gate_version": gate.GATE_VERSION,
        "window": args.window,
        "days": args.days,
        "seeds": args.seeds,
        "breaker_reset": args.breaker_reset,
        "params": json.dumps(params, sort_keys=True, separators=(",", ":")),
        "trades": base["trades_median"],
        "total_r_median": round(base["total_r_median"], 1),
        "total_r_p5": round(base["total_r_p5"], 1),
        "mdd_r_median": round(base["mdd_r_median"], 1),
        "oos_pass_rate": round(base["oos_pass_rate"], 2),
        "recovery_factor": _fmt(base["recovery_factor_median"], 2),
        "top2_share": _fmt(base["top2_share_median"], 2),
        "positive_symbol_share": round(base["positive_symbol_share_median"], 2),
        "stress_total_r": round(stress["total_r_median"], 1),
        "verdict": "PASS" if verdict["passed"] else "FAIL",
        "failed": ",".join(verdict["failed"]) or "-",
        "note": (args.note or "-").replace("\t", " "),
    }

    print(f"\n판정: {row['verdict']}"
          + (f"  (실패 항목: {row['failed']})" if not verdict["passed"] else ""))
    print(f"총R 중앙 {row['total_r_median']:+} (하위5% {row['total_r_p5']:+}) · "
          f"낙폭 {row['mdd_r_median']:+} · 거래 {row['trades']:.0f}건 · "
          f"스트레스 총R {row['stress_total_r']:+}")

    if args.dry_run:
        print("\n--dry-run: 원장에 남기지 않았다.")
        return 0

    append_ledger(row)
    print(f"\n원장 기록: {LEDGER_PATH.relative_to(PROJECT_ROOT)} ({run_id}, 누적 {len(rows) + 1}회)")
    if len(rows) + 1 >= 20:
        print("경고: 누적 시도가 20회를 넘었다. 이 지점부터는 잡음에서도 PASS가 나올 수 있다 — "
              "프로토콜의 다중비교 항목을 읽을 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
