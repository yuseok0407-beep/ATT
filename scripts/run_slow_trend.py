"""대안 전략 #2(일봉 채널 돌파 추세 추종) 사전 등록 판정 — docs/ALT_SLOW_TREND.md의 기준을 그대로 잰다.

    python scripts/fetch_universe_ohlcv.py; python scripts/fetch_funding_history.py
    python scripts/run_slow_trend.py

기준·이웃값·스트레스는 사전 등록 문서와 같고, 여기서 바꾸지 않는다. 거래 목록은 exports/에 남긴다.
"""
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from run_xs_momentum import _load  # noqa: E402  같은 데이터(80종목 캔들 + 펀딩 이력)

from src.backtest import slow_trend as st  # noqa: E402
from src.backtest.xs_momentum import funding_panel  # noqa: E402
from src.core.config import FEE_PCT_PER_SIDE  # noqa: E402


def main() -> int:
    hourly, funding_rows = _load()
    panels = st.daily_ohlc(hourly)
    funding = funding_panel(funding_rows, panels["close"].index)
    print(f"종목 {panels['close'].shape[1]}개 · 일봉 {panels['close'].index[0]:%Y-%m-%d} ~ {panels['close'].index[-1]:%Y-%m-%d}")

    base = st.Params(fee_pct_per_side=FEE_PCT_PER_SIDE)
    runs = {"등록 설정(20/10)": base,
            "이웃 10/5": replace(base, entry_days=10, exit_days=5),
            "이웃 40/20": replace(base, entry_days=40, exit_days=20),
            "수수료 2배": replace(base, fee_pct_per_side=2 * FEE_PCT_PER_SIDE)}
    res = {}
    print(f"\n{'':<16}{'거래':>5}{'총수익':>9}{'전반':>8}{'후반':>8}{'샤프':>7}{'최대낙폭':>9}{'승률':>7}{'건당R':>7}")
    for label, params in runs.items():
        out = st.simulate(panels, funding, params)
        out["trades"].to_csv(PROJECT_ROOT / "exports" / f"slow_trend_{params.entry_days}_{params.fee_pct_per_side}.csv",
                             index=False, encoding="utf-8-sig")
        s = res[label] = st.summary(out)
        print(f"{label:<16}{s['trades']:>5}{s['total_return']:>+9.1%}{s['first_half']:>+8.1%}{s['second_half']:>+8.1%}"
              f"{(s['sharpe'] or 0):>7.2f}{-s['max_drawdown']:>+9.1%}{(s['win_rate'] or 0):>7.0%}{(s['avg_r'] or 0):>+7.2f}")

    m = res["등록 설정(20/10)"]
    print(f"\n월별(등록 설정): " + ", ".join(f"{k} {v:+.1%}" for k, v in m["monthly"].items()))
    checks = [("1 거래 ≥ 100건", m["trades"] >= 100),
              ("2 비용·펀딩 후 총수익 > 0", m["total_return"] > 0),
              ("3 두 반 모두 > 0", m["first_half"] > 0 and m["second_half"] > 0),
              ("4 연환산 샤프 ≥ 0.5", (m["sharpe"] or 0) >= 0.5),
              ("5 최대 낙폭 ≤ 25%", m["max_drawdown"] <= 0.25),
              ("6 이웃 10/5·40/20 총수익 > 0", res["이웃 10/5"]["total_return"] > 0 and res["이웃 40/20"]["total_return"] > 0),
              ("7 수수료 2배 총수익 > 0", res["수수료 2배"]["total_return"] > 0)]
    print(f"\n평가 시작 {m['start']} — 사전 등록 기준(docs/ALT_SLOW_TREND.md)")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n판정: {'PASS' if all(ok for _, ok in checks) else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
