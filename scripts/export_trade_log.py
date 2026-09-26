"""저널을 분석용 CSV로 내보내는 얇은 진입점 (로직은 src/execution/export_log.py).

    python scripts/export_trade_log.py                  # exports/ 에 데모+실계좌 전부
    python scripts/export_trade_log.py --env live       # 실계좌만
    python scripts/export_trade_log.py --out-dir exports/2026-09-22
    python scripts/export_trade_log.py --stamped        # exports/YYYYMMDD-HHMM/ 로 (백업 보존용)

읽기만 하므로 돌고 있는 봇에 영향을 주지 않는다 — 진행 중인 사이클이 저널에 한 줄 더 쓰는
중이었다면 그 줄만 다음 내보내기에 포함된다.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution import export_log  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="거래 저널을 분석용 CSV로 내보낸다")
    parser.add_argument("--env", choices=["demo", "live"], action="append",
                        help="내보낼 계좌(반복 지정 가능). 생략하면 데모+실계좌 둘 다")
    parser.add_argument("--out-dir", default=export_log.DEFAULT_OUT_DIR)
    parser.add_argument("--stamped", action="store_true",
                        help="--out-dir 아래에 실행 시각 폴더를 만들어 과거 내보내기를 덮어쓰지 않는다")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if args.stamped:
        out_dir = out_dir / datetime.now().strftime("%Y%m%d-%H%M")

    envs = tuple(dict.fromkeys(args.env)) if args.env else ("demo", "live")
    manifest = export_log.export_all(out_dir, envs=envs)

    for summary in manifest["envs"]:
        def _r(value):
            return f"{value:+.2f}R" if value is not None else "-"

        print(f"[{summary['env']}] 이벤트 {summary['events']}건, 청산거래 {summary['trades']}건 "
              f"(R유효 {summary['trades_with_r']}건)")
        # 순R을 먼저 적는다 — 총R은 수수료 이전 값이라 이 전략에서는 부호가 반대로 나온다.
        print(f"         순R(수수료후) {_r(summary['total_net_r'])} / 수수료전 {_r(summary['total_r'])} "
              f"· 실현손익 {summary['realized_pnl']:+.2f} USDT(수수료 제외)")
        if summary["first_trade"]:
            print(f"         기간 {summary['first_trade'][:16]} ~ {summary['last_trade'][:16]} (UTC)")
    print(f"\n내보냄: {out_dir.resolve()}")
    print(f"  합본 {Path(manifest['combined']).name} / 목록·요약 manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
