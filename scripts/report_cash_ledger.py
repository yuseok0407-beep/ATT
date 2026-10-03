"""계좌 **현금 원장** — 거래소 수입 내역으로 "자산이 왜 이만큼 움직였나"와 월별 수익률(입출금 제외).

    python scripts/report_cash_ledger.py              # 실계좌
    python scripts/report_cash_ledger.py --env demo
    python scripts/report_cash_ledger.py --since 2026-09-01

저널은 거래만 보고, 펀딩비·수수료 리베이트·입출금을 모른다(외부 검토 3.3). 이 보고서는 거래소
원장 그대로라 계좌 화면과 맞는다. 월초 자산은 "지금 지갑 잔고 - 그 뒤의 수입 내역 합계"로 되돌려
구한다(지갑 잔고 = 실현된 것만, 보유 중 평가손익 제외). 조회 전용 — 주문하지 않는다.
요청 가중치가 커서(수입 내역 30/페이지) 봇이 도는 중에 반복 실행하지 말 것.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from src.data.futures_exchange import get_futures_client  # noqa: E402
from src.execution import income_ledger  # noqa: E402

LABELS = {"REALIZED_PNL": "실현손익", "COMMISSION": "수수료", "COMMISSION_REBATE": "수수료 리베이트",
          "FUNDING_FEE": "펀딩비", "TRANSFER": "입출금"}


def main() -> int:
    parser = argparse.ArgumentParser(description="거래소 수입 내역 기준 현금 원장")
    parser.add_argument("--env", choices=["demo", "live"], default="live")
    parser.add_argument("--since", default="2026-08-01")
    args = parser.parse_args()

    client = get_futures_client(args.env)
    since = pd.Timestamp(args.since, tz="UTC")
    rows = income_ledger.fetch_income(client, int(since.timestamp() * 1000))
    wallet = float(client.fetch_balance()["info"]["totalWalletBalance"])
    if not rows:
        print("수입 내역이 없다.")
        return 0

    t = income_ledger.totals(rows)
    print(f"[{args.env}] {since:%Y-%m-%d} 이후 수입 내역 {len(rows)}건 · 지금 지갑 잔고 {wallet:.2f} USDT")
    for kind, value in sorted(t["by_type"].items(), key=lambda kv: kv[1]):
        print(f"  {LABELS.get(kind, kind):<12} {value:+10.2f}")
    print(f"  {'거래로 인한 변화':<12} {t['trading']:+10.2f}   (입출금 제외)")
    if t["rebate_ratio"] is not None:
        print(f"  리베이트 = 수수료의 {t['rebate_ratio']:.1%} — 저널의 수수료(체결 commission)는 이만큼 과대")
    if t["non_usdt_rows"]:
        print(f"  주의: USDT가 아닌 행 {t['non_usdt_rows']}건은 합계에서 뺐다")

    df = pd.DataFrame({"at": pd.to_datetime([int(r["time"]) for r in rows], unit="ms", utc=True),
                       "kind": [r["incomeType"] for r in rows],
                       "amount": [income_ledger.amount(r) for r in rows]})
    df["month"] = df["at"].dt.strftime("%Y-%m")
    print("\n월별 (수익률 = 거래로 인한 변화 / (월초 자산 + 그달 입금))")
    print(f"  {'월':<8}{'월초 자산':>10}{'입출금':>10}{'거래 손익':>10}{'수익률':>9}   (그중 수수료+리베이트 / 펀딩)")
    for month, part in df.groupby("month"):
        later = df.loc[df["at"] >= part["at"].min(), "amount"].sum()
        start_equity = wallet - later
        transfers = part.loc[part["kind"] == "TRANSFER", "amount"].sum()
        trading = part.loc[part["kind"] != "TRANSFER", "amount"].sum()
        fees = part.loc[part["kind"].isin(["COMMISSION", "COMMISSION_REBATE"]), "amount"].sum()
        funding = part.loc[part["kind"] == "FUNDING_FEE", "amount"].sum()
        base = start_equity + max(transfers, 0.0)
        ret = f"{trading / base:+.2%}" if base > 0 else "-"
        print(f"  {month:<8}{start_equity:>10.2f}{transfers:>+10.2f}{trading:>+10.2f}{ret:>9}   ({fees:+.2f} / {funding:+.2f})")
    print(f"\n조회 시각 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
