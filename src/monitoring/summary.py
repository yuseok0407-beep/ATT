import unicodedata
from datetime import datetime

WIDTH = 64


def _display_width(text: str) -> int:
    """한글/전각 문자는 터미널에서 2칸을 차지하므로 len()만으로는 표가 어긋난다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_summary(cycle: dict) -> str:
    """run_cycle()의 반환값을 사람이 읽기 좋은 터미널 표로 정리한다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["=" * WIDTH, f" auto2 트레이딩 봇 — {now}", "=" * WIDTH]

    results = cycle.get("results", [])

    if results and results[0].get("event") == "circuit_breaker_blocked":
        lines.append("⚠ 서킷 브레이커 작동 — 이번 사이클은 신규 거래 없음")
        lines.append(f"  사유: {results[0]['reason']}")
        lines.append("=" * WIDTH)
        return "\n".join(lines)

    lines.append(f"포트폴리오 총액   : ${cycle['portfolio_value']:,.2f}")
    lines.append(f"오늘 손익률       : {cycle['daily_pnl_pct'] * 100:+.2f}%")
    lines.append(f"시장 레짐(BTC)    : {cycle['regime']}")
    lines.append("-" * WIDTH)
    lines.append(_pad("종목", 10) + _pad("결정", 10) + _pad("신뢰도", 8) + "근거")
    lines.append("-" * WIDTH)

    for r in results:
        if "event" in r:
            lines.append(_pad(r.get("symbol", "-"), 10) + _pad("REJECTED", 10) + _pad("", 8) + r.get("reason", ""))
            continue

        d = r["decision"]
        row = _pad(r["symbol"], 10) + _pad(d["decision"], 10) + _pad(f"{d['confidence']:.2f}", 8) + _truncate(d["reasoning"])
        lines.append(row)

        if "execution" in r:
            ex = r["execution"]
            lines.append(_pad("", 10) + f"└─ 주문 실행: {ex['status']} ({ex.get('side', '')} {ex.get('quantity', '')})")

    lines.append("=" * WIDTH)
    return "\n".join(lines)
