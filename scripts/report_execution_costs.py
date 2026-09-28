"""실거래 체결 비용 리포트 (로직은 src/execution/cost_report.py).

    python scripts/report_execution_costs.py              # 데모+실계좌
    python scripts/report_execution_costs.py --env live   # 실계좌만
    python scripts/report_execution_costs.py --by-symbol  # 종목별 표까지
    python scripts/report_execution_costs.py --since 2026-09-22

기본으로 **2026-09-09 이후 청산만** 본다. 그 전 청산 기록에는 방향·손절가가 없어서 진입 기록에서
끌어와 복원하는데, 2026-09-07 재진입 루프처럼 청산이 엉뚱한 진입과 짝지어진 기록이 섞여 있어
슬리피지가 -0.9R~+0.8R로 튄다(손절 청산 평균이 0.005R → 0.08R로 부풀려진다).

"백테스트가 공짜로 가정한 것을 실제로는 얼마나 내고 있나"를 답한다. 저널만 읽으므로 돌고 있는
봇에 영향을 주지 않는다.

숫자 읽는 법(모든 값은 R 단위, 양수 = 손해):
- 진입 슬리피지: 신호 봉 종가보다 얼마나 불리하게 샀나/팔았나
- 청산 슬리피지: 손절/익절 주문이 발동한 가격보다 얼마나 불리하게 체결됐나
- 수수료: 왕복 수수료
- 총비용: 위 셋의 합 — 백테스트 기본 시나리오에 없는 비용(수수료 제외분은 스트레스에만 있다)
"""

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.gate import STRESS_SLIPPAGE_R_PER_SIDE  # noqa: E402
from src.execution import cost_report, export_log, journal  # noqa: E402

# 청산 기록이 자기 방향·손절가·익절가를 직접 들고 다니기 시작한 날(CLAUDE.md "청산 기록" 항목).
DEFAULT_SINCE = "2026-09-09"

LABELS = {
    "entry_slippage_r": "진입 슬리피지",
    "exit_slippage_r": "청산 슬리피지",
    "fee_r": "수수료",
    "execution_cost_r": "총비용",
}


def _line(label: str, dist: dict | None) -> str:
    if not dist:
        return f"  {label:<10} 표본 없음"
    return (f"  {label:<10} {dist['n']:>4}건  평균 {dist['mean']:+.3f}  보통(중앙) {dist['median']:+.3f}"
            f"  나쁜10%(p90) {dist['p90']:+.3f}  최악 {dist['worst']:+.3f}")


def _print_block(title: str, block: dict) -> None:
    print(title)
    for metric, label in LABELS.items():
        print(_line(label, block.get(metric)))


def main() -> int:
    parser = argparse.ArgumentParser(description="실거래 체결 비용 분포")
    parser.add_argument("--env", choices=["demo", "live"], action="append")
    parser.add_argument("--by-symbol", action="store_true")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"이 날짜(UTC) 이후 청산만 (기본 {DEFAULT_SINCE}, 전체는 --since 0)")
    args = parser.parse_args()
    envs = tuple(dict.fromkeys(args.env)) if args.env else ("demo", "live")

    rows = []
    for env in envs:
        rows += export_log.build_trade_rows(journal.read_entries(export_log.JOURNALS[env]), env)
    rows = [r for r in rows if (r.get("exit_timestamp") or "") >= args.since]
    summary = cost_report.summarize_costs(rows, STRESS_SLIPPAGE_R_PER_SIDE)

    print(f"{args.since} 이후 청산 거래 {summary['trades']}건 중 비용을 전부 잰 거래 {summary['complete_samples']}건 "
          f"(판단에 쓰려면 {summary['min_samples_for_decision']}건 필요)\n")
    _print_block("[전체]", summary["overall"])
    for env, block in summary["by_env"].items():
        print()
        _print_block(f"[{env}]", block)
    print("\n[청산 사유별 — 청산 슬리피지]")
    for reason, block in summary["by_reason"].items():
        print(_line(reason, block.get("exit_slippage_r")))
    if args.by_symbol:
        print("\n[종목별 — 총비용]")
        for symbol, block in summary["by_symbol"].items():
            print(_line(symbol.split("/")[0], block.get("execution_cost_r")))

    stress = summary["stress_assumption"]
    measured = stress["measured_per_trade"]
    print(f"\n[백테스트 스트레스 가정과 비교] 가정: 거래당 슬리피지 {stress['per_trade_r']:.2f}R "
          f"(편도 {stress['per_side_r']:.2f}R × 2)")
    if measured:
        print(f"  실측(진입+청산) 평균 {measured['mean']:+.3f}R, 나쁜10% {measured['p90']:+.3f}R "
              f"— 평균은 가정 {'안쪽' if stress['mean_within_assumption'] else '바깥(가정이 너무 낙관적)'}, "
              f"나쁜10%는 가정 {'안쪽' if stress['p90_within_assumption'] else '바깥'}")
    if not summary["enough_samples"]:
        print("  ※ 표본이 아직 적다 — 이 숫자로 게이트 기준을 바꾸지 말 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
