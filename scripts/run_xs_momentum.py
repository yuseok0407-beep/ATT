"""대안 전략(저회전 횡단면 모멘텀) 사전 등록 판정 — docs/ALT_XS_MOMENTUM.md의 기준을 그대로 잰다.

    python scripts/fetch_universe_ohlcv.py     # 후보 캔들(이미 있으면 건너뜀)
    python scripts/fetch_funding_history.py    # 펀딩 이력
    python scripts/run_xs_momentum.py

기준·이웃값·스트레스는 사전 등록 문서와 같고, 여기서 바꾸지 않는다. 주간 결과는 exports/에 남긴다.
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from src.backtest import xs_momentum as xm  # noqa: E402
from src.core.config import FEE_PCT_PER_SIDE  # noqa: E402

CACHE = PROJECT_ROOT / ".ohlcv_cache_universe"


def _load():
    hourly, funding = {}, {}
    for path in sorted(CACHE.glob("*.json")):
        if path.name.startswith("_"):
            continue
        df = pd.read_json(path)
        if df.empty:
            continue
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        hourly[path.stem] = df
        fpath = CACHE / "funding" / path.name
        funding[path.stem] = json.loads(fpath.read_text(encoding="utf-8")) if fpath.exists() else []
    return hourly, funding


def main() -> int:
    hourly, funding_rows = _load()
    missing = [s for s, rows in funding_rows.items() if not rows]
    closes, volumes = xm.daily_panels(hourly)
    funding = xm.funding_panel(funding_rows, closes.index)
    print(f"종목 {closes.shape[1]}개 · 일봉 {closes.index[0]:%Y-%m-%d} ~ {closes.index[-1]:%Y-%m-%d}"
          + (f" · 펀딩 이력 없음 {len(missing)}개" if missing else ""))

    base = xm.Params(fee_pct_per_side=FEE_PCT_PER_SIDE)
    runs = {
        "등록 설정(28일)": base,
        "이웃 14일": replace(base, lookback_days=14),
        "이웃 56일": replace(base, lookback_days=56),
        "수수료 2배": replace(base, fee_pct_per_side=2 * FEE_PCT_PER_SIDE),
    }
    results = {}
    print(f"\n{'':<14}{'주':>4}{'총수익':>9}{'전반':>8}{'후반':>8}{'샤프':>7}{'최대낙폭':>9}{'수수료':>8}{'펀딩':>8}{'최악주':>8}")
    for label, params in runs.items():
        weekly = xm.backtest(closes, volumes, funding, params)
        weekly.to_csv(PROJECT_ROOT / "exports" / f"xs_momentum_{params.lookback_days}d_fee{params.fee_pct_per_side}.csv",
                      encoding="utf-8-sig")
        s = results[label] = xm.summary(weekly)
        print(f"{label:<14}{s['weeks']:>4}{s['total_return']:>+9.1%}{s['first_half']:>+8.1%}{s['second_half']:>+8.1%}"
              f"{(s['sharpe'] or 0):>7.2f}{-s['max_drawdown']:>+9.1%}{-s['fees']:>+8.1%}{s['funding']:>+8.1%}"
              f"{s['worst_week']:>+8.1%}")

    main_run = results["등록 설정(28일)"]
    checks = [
        ("1 비용·펀딩 후 총수익 > 0", main_run["total_return"] > 0),
        ("2 두 반 모두 > 0", main_run["first_half"] > 0 and main_run["second_half"] > 0),
        ("3 연환산 샤프 ≥ 0.5", (main_run["sharpe"] or 0) >= 0.5),
        ("4 최대 낙폭 ≤ 25%", main_run["max_drawdown"] <= 0.25),
        ("5 이웃 14·56일 총수익 > 0", results["이웃 14일"]["total_return"] > 0 and results["이웃 56일"]["total_return"] > 0),
        ("6 수수료 2배 총수익 > 0", results["수수료 2배"]["total_return"] > 0),
    ]
    print(f"\n평가 시작 {main_run['start']} — 사전 등록 기준(docs/ALT_XS_MOMENTUM.md)")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n판정: {'PASS' if all(ok for _, ok in checks) else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
