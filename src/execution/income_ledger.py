"""거래소 **수입 내역**(income history) — 계좌 자산이 왜 움직였는지의 원장(2026-10-03).

저널은 거래만 기록한다. 그런데 자산은 거래 말고도 움직인다 — 외부 검토(3.3)가 지적한 빈칸:

- **펀딩비**(`FUNDING_FEE`): 무기한 선물 보유자가 8시간마다 주고받는다. 저널·백테스트 어디에도
  없었다. 실측(2026-10-03): 데모 최근 30일 -56 USDT(수수료의 14%), 실계좌 08-21~ +1.76.
- **수수료 리베이트**(`COMMISSION_REBATE`): 실계좌는 수수료의 약 20%를 돌려받는다(08-21~ 수수료
  -95.91, 리베이트 +18.89). 체결 내역의 `commission`만 보는 저널은 실계좌 수수료를 그만큼 크게
  잡고 있었다. 리베이트는 체결보다 수십 분 늦게 들어와 청산 순간에는 거래에 붙일 수 없다.
- **입출금**(`TRANSFER`): 자산 수익률에서 빼야 한다 — 안 빼면 입금이 수익이 된다.

실계좌 08-21 입금 675.31 + 수입 내역 합계 -117.41 = 557.90, 그날 계좌 자산 558.06과 맞았다 —
**이 원장만이 계좌 화면과 1원 단위로 맞는다.**

요청 가중치는 30이다(IP 한도를 봇·대시보드와 나눠 쓴다) — 사이클마다 부르지 말 것.
"""

import time

TRANSFER_TYPES = ("TRANSFER",)
TRADING_TYPES = ("REALIZED_PNL", "COMMISSION", "COMMISSION_REBATE", "FUNDING_FEE")


def fetch_income(client, since_ms: int, income_type: str | None = None,
                 until_ms: int | None = None, pause: float = 0.5) -> list[dict]:
    """since_ms 이후의 수입 내역 전부(1000건씩 페이지). 시간순."""
    rows: list[dict] = []
    since = int(since_ms)
    while True:
        params = {"startTime": since, "limit": 1000}
        if income_type:
            params["incomeType"] = income_type
        if until_ms:
            params["endTime"] = int(until_ms)
        batch = client.fapiPrivateGetIncome(params)
        rows.extend(batch)
        if len(batch) < 1000:
            break
        since = int(batch[-1]["time"]) + 1
        time.sleep(pause)
    return rows


def amount(row: dict) -> float:
    """USDT 금액. USDT가 아닌 자산(BNB 수수료 등)은 0으로 — 단위가 다른 숫자를 더하지 않는다
    (외부 검토 3.3). 그런 행이 있으면 `non_usdt_rows`로 따로 센다."""
    return float(row.get("income") or 0.0) if row.get("asset") == "USDT" else 0.0


def totals(rows: list[dict]) -> dict:
    """유형별 합계와 파생값. trading = 입출금을 뺀 자산 변화(실현손익+수수료+리베이트+펀딩)."""
    by_type: dict[str, float] = {}
    for row in rows:
        by_type[row["incomeType"]] = by_type.get(row["incomeType"], 0.0) + amount(row)
    commission = by_type.get("COMMISSION", 0.0)
    rebate = by_type.get("COMMISSION_REBATE", 0.0)
    return {
        "by_type": by_type,
        "trading": sum(v for k, v in by_type.items() if k not in TRANSFER_TYPES),
        "transfers": sum(by_type.get(k, 0.0) for k in TRANSFER_TYPES),
        # 리베이트가 수수료의 몇 %인가(수수료는 음수, 리베이트는 양수)
        "rebate_ratio": (rebate / -commission) if commission < 0 else None,
        "non_usdt_rows": sum(1 for r in rows if r.get("asset") != "USDT"),
    }


def funding_between(rows: list[dict], symbol_id: str, start_ms: int, end_ms: int) -> float:
    """한 종목(거래소 표기, 예: "BTCUSDT")이 [start, end] 동안 주고받은 펀딩비 합계."""
    return sum(amount(r) for r in rows
               if r.get("incomeType") == "FUNDING_FEE" and r.get("symbol") == symbol_id
               and start_ms <= int(r["time"]) <= end_ms)
