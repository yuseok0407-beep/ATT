import re
from datetime import datetime, timedelta, timezone

from src.core.config import FEE_PCT_PER_SIDE


def summarize_performance(entries: list[dict]) -> dict:
    """저널 항목 목록에서 event=="closed"인 것만 골라 대시보드용 성과 요약을 만든다.

    R배수가 아니라 실현손익($) 기준이다 — R배수는 진입 시점의 손절폭(entry_info)까지 다시
    매칭해야 해서 더 정확하지만, 대시보드는 "실제로 얼마 벌고 잃었는지"를 보여주는 용도라
    달러 기준이 더 직관적이고 매칭 없이 바로 계산 가능하다.

    reason=="manual"인 청산(사용자가 직접 넣고 직접 닫은 거래, 테스트 주문 포함)은 제외한다 —
    이 섹션은 "규칙 기반 전략이 얼마나 잘하고 있는지"를 보여주려는 것이라, 전략이 판단조차
    안 한 수동 거래가 승률/손익에 섞이면 안 된다(2026-08-14, 사용자가 테스트 삼아 넣은
    주문이 성과 지표를 오염시킨 사고로 실제 확인됨)."""
    closed = [e for e in entries if e.get("event") == "closed" and e.get("reason") != "manual"]

    per_symbol: dict[str, dict] = {}
    reason_counts: dict[str, int] = {}
    total_pnl = 0.0
    wins = 0

    for entry in closed:
        pnl = entry.get("realized_pnl") or 0.0
        symbol = entry.get("symbol") or "unknown"
        reason = entry.get("reason") or "unknown"

        total_pnl += pnl
        if pnl > 0:
            wins += 1

        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        bucket = per_symbol.setdefault(symbol, {"trades": 0, "wins": 0, "total_pnl": 0.0})
        bucket["trades"] += 1
        bucket["total_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    num_trades = len(closed)
    return {
        "num_trades": num_trades,
        "win_rate": (wins / num_trades) if num_trades else None,
        "total_realized_pnl": total_pnl,
        "avg_realized_pnl": (total_pnl / num_trades) if num_trades else None,
        "per_symbol": per_symbol,
        "reason_counts": reason_counts,
    }


_BINANCE_CODE_RE = re.compile(r'"code"\s*:\s*(-?\d+)')

# 자주 나오는 거래소 거부 코드의 한 줄 설명 — 코드만 보여주면 매번 검색해야 한다.
_ERROR_CODE_HINTS = {
    "-2027": "주문 명목가가 이 심볼의 허용 한도를 넘음 (사이징/레버리지 확인)",
    "-2021": "브라켓이 즉시 발동될 가격 — 진입가와 현재가가 너무 벌어짐",
    "-4411": "TradFi-Perps 약관 미동의 (주식형 심볼)",
    "-1008": "거래소 쪽 요청 과부하 — 보통 저절로 풀림",
    "-1021": "로컬 시각이 거래소와 어긋남",
}


def summarize_recent_issues(entries: list[dict], hours: int = 24, now: datetime = None) -> dict:
    """최근 N시간 동안 "사용자가 알아야 할" 저널 기록만 골라 집계한다.

    rejected_exchange_error는 저널엔 남지만 텔레그램 알림에서는 노이즈라 빠져 있고, 대시보드
    "최근 내역"의 고정 30줄 안에서도 금방 밀려난다 — 그래서 실제로는 몇 달째 조용히 쌓이기만
    했다(2026-09-09 확인: TSLA -2027 141건, ZEC -2021 22건. 둘 다 사이징/진입가 문제라
    사용자가 알았어야 할 신호였다). 개별 기록이 아니라 코드별 건수로 묶으면 스팸 없이 드러난다.

    같은 심볼의 같은 오류는 저널 기록 단계에서 이미 중복 제거되므로(futures_rule_bot.run_once)
    여기 건수는 "반복 횟수"가 아니라 "구분되는 발생 횟수"에 가깝다."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    by_code: dict[str, dict] = {}
    circuit_breaker_count = 0
    for entry in entries:
        timestamp = entry.get("timestamp")
        if not timestamp:
            continue
        try:
            when = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if when < cutoff:
            continue

        event = entry.get("event")
        if event == "circuit_breaker_blocked":
            circuit_breaker_count += 1
            continue
        if event != "rejected_exchange_error":
            continue

        reason = str(entry.get("reason") or "")
        match = _BINANCE_CODE_RE.search(reason)
        code = match.group(1) if match else "network"
        bucket = by_code.setdefault(code, {"code": code, "count": 0, "symbols": [],
                                            "hint": _ERROR_CODE_HINTS.get(code, "")})
        bucket["count"] += 1
        symbol = (entry.get("symbol") or "?").split("/")[0]
        if symbol not in bucket["symbols"]:
            bucket["symbols"].append(symbol)

    return {
        "since_hours": hours,
        "rejections": sorted(by_code.values(), key=lambda b: b["count"], reverse=True),
        "circuit_breaker_count": circuit_breaker_count,
    }



def _side_from(entry_info: dict) -> str | None:
    """진입 기록에서 방향을 뽑는다. signal이 있으면 그대로, 없으면(사용자가 직접 넣은 포지션을
    백필한 기록) 손절가가 진입가보다 아래면 롱으로 읽는다."""
    signal = entry_info.get("signal")
    if signal in ("LONG", "SHORT"):
        return "long" if signal == "LONG" else "short"
    entry_price, stop_loss_price = entry_info.get("entry_price"), entry_info.get("stop_loss_price")
    if entry_price is None or stop_loss_price is None:
        return None
    return "long" if stop_loss_price < entry_price else "short"


def resolve_closed_trades(entries: list[dict]) -> list[dict]:
    """청산 기록에 진입 맥락(방향/손절가/R배수)을 채워서 시간순으로 돌려준다.

    2026-09-09부터는 청산 기록이 이 값들을 직접 들고 있지만, 그 이전 기록에는 실현손익($)밖에
    없다 — 그때는 방향도 손절가도 안 남겼기 때문에 R을 사후에 복원하는 것 자체가 불가능했다.
    다만 **진입 기록에는 손절가가 처음부터 있었다.** 저널을 시간순으로 훑으며 심볼별 마지막
    "entered"를 기억해두면 옛 청산도 R을 복원할 수 있고, 그래야 새 지표가 몇 주 동안 빈
    화면으로 있지 않고 지금까지의 이력 전체를 바로 보여준다.

    reason=="manual"인 청산은 제외한다(summarize_performance와 같은 이유 — 전략이 판단조차
    안 한 거래가 성과에 섞이면 안 된다)."""
    last_entry: dict[str, dict] = {}
    resolved = []

    for entry in entries:
        event = entry.get("event")
        symbol = entry.get("symbol")
        if event == "entered" and symbol:
            last_entry[symbol] = entry
            continue
        if event != "closed" or entry.get("reason") == "manual":
            continue

        source = last_entry.get(symbol, {})
        trade = dict(entry)
        # 새 기록은 자기가 들고 있는 값을 그대로 쓰고, 없을 때만 진입 기록에서 메운다.
        if trade.get("side") is None:
            trade["side"] = _side_from(source)
        if trade.get("stop_loss_price") is None:
            trade["stop_loss_price"] = source.get("stop_loss_price")
        if trade.get("entry_price") is None:
            trade["entry_price"] = source.get("entry_price")
        if trade.get("entry_timestamp") is None:
            # 옛 청산 기록엔 진입 시각이 없다 — 전략 버전은 진입 시각으로 배정하므로 채워둔다.
            trade["entry_timestamp"] = source.get("timestamp")
        if trade.get("realized_r") is None:
            trade["realized_r"] = _price_r(trade.get("exit_price"), trade.get("entry_price"),
                                            trade.get("stop_loss_price"), trade.get("side"))
        if _r_contradicts_pnl(trade):
            # R은 저널의 진입가를 기준으로 재는데, 그 진입가가 이 청산의 실제 진입과 다르면
            # 완전히 엉뚱한 값이 나온다. 2026-09-07 ZEC 재진입 루프가 실제 사례다 — 청산 6건이
            # 같은 "entered" 기록에 묶이면서 손실 거래인데 R이 +2.6~+3.1로 계산됐다(실현손익
            # -1.53인데 R +2.63). 부호가 서로 모순되면 그 R은 못 믿는 값이라 버린다 — 그 거래는
            # 달러 통계에는 그대로 남고 R 통계에서만 빠진다. 손절/익절 판정에 이미 쓰고 있는
            # 것과 같은 기준이다(futures_rule_bot.check_and_log_closed_trade).
            trade["realized_r"] = None
        resolved.append(trade)

    return resolved


def _r_contradicts_pnl(trade: dict) -> bool:
    """R배수와 실현손익($)이 서로 반대 방향을 가리키는지 — 둘 중 하나는 틀렸다는 뜻이다."""
    realized_r, realized_pnl = trade.get("realized_r"), trade.get("realized_pnl")
    if realized_r is None or realized_pnl is None:
        return False
    return (realized_r > 0 and realized_pnl < 0) or (realized_r < 0 and realized_pnl > 0)


def _price_r(exit_price, entry_price, stop_loss_price, side) -> float | None:
    """가격만으로 R을 잰다 — 손절가까지가 정확히 -1R. 수량/레버리지와 무관해서 사이징을 바꿔도
    백테스트의 R과 그대로 비교된다(실현손익 $는 그렇지 않다)."""
    if side is None or exit_price is None or entry_price is None or stop_loss_price is None:
        return None
    try:
        risk = abs(float(entry_price) - float(stop_loss_price))
        if risk <= 0:
            return None
        move = float(exit_price) - float(entry_price)
    except (TypeError, ValueError):
        return None
    return move / risk if side == "long" else -move / risk


def _fee_r(trade: dict) -> tuple[float | None, bool]:
    """이 거래의 왕복 수수료를 R로. (값, 추정여부).

    **저널의 realized_r에는 수수료가 안 들어있다** — 가격만으로 재기 때문이고, 거래소가 주는
    realized_pnl도 수수료를 뺀 값이 아니다(commission이 별개 필드, 2026-09-22 실계좌 확인).
    손절폭 1.25%에서 왕복 수수료는 0.04~0.064R이고 이 전략의 건당 기대값이 +0.02~0.06R이라
    **수수료를 빼면 총R의 부호가 바뀐다** — 그래서 화면에 순R을 같이 내야 한다.

    2026-09-22 이후 청산 기록은 실제 수수료(`fee_r`)를 들고 있다. 그 이전 기록에는 없으므로
    설정된 수수료율과 그 거래의 손절폭으로 추정한다(수수료R = 2 x 편도율 / 손절폭%, 사이징과
    무관). 추정치는 그렇다고 표시해서 화면이 실측과 섞어 말하지 않게 한다."""
    actual = trade.get("fee_r")
    if isinstance(actual, (int, float)):
        return float(actual), False

    entry_price, stop_loss_price = trade.get("entry_price"), trade.get("stop_loss_price")
    try:
        stop_pct = abs(float(entry_price) - float(stop_loss_price)) / float(entry_price)
    except (TypeError, ValueError, ZeroDivisionError):
        return None, True
    if stop_pct <= 0:
        return None, True
    return 2 * FEE_PCT_PER_SIDE / stop_pct, True


def _cash_net_r(trade: dict) -> float | None:
    """현금 기준 순R = 수수료 뺀 실현손익 / 계획 위험금액(외부 검토 3.3, 2026-10-03).

    청산 기록이 `cash_net_r`을 들고 있으면 그것. 없으면(10-03 이전) 같은 기록의 `total_fee`와
    `fee_r`로 계획 위험금액을 되살린다 — fee_r = 수수료 / 계획 위험금액이므로 위험금액 =
    수수료 / fee_r. 실측 수수료가 있는 09-22 이후 기록은 전부 이렇게 복원된다."""
    cash = trade.get("cash_net_r")
    if isinstance(cash, (int, float)):
        return float(cash)
    net_pnl, fee, fee_r = trade.get("net_realized_pnl"), trade.get("total_fee"), trade.get("fee_r")
    if all(isinstance(v, (int, float)) for v in (net_pnl, fee, fee_r)) and fee > 0 and fee_r > 0:
        return float(net_pnl) * float(fee_r) / float(fee)
    return None


def _net_r(trade: dict) -> tuple[float | None, bool]:
    """수수료를 뺀 R. realized_r이 없으면(못 믿어서 버린 거래 등) None.

    현금 기준 값(`_cash_net_r`)이 있으면 그것을 쓴다 — 진입 슬리피지까지 들어간 실제 결과다.
    없으면 신호가 기준 R에서 수수료만 뺀 값(실측 수수료 → 추정 수수료 순)."""
    realized_r = trade.get("realized_r")
    if realized_r is None:
        return None, False
    cash = _cash_net_r(trade)
    if cash is not None:
        return cash, False
    net = trade.get("net_realized_r")
    if isinstance(net, (int, float)):
        return float(net), False
    fee_r, estimated = _fee_r(trade)
    if fee_r is None:
        return None, estimated
    return realized_r - fee_r, estimated


def _net_pnl(trade: dict) -> tuple[float | None, bool]:
    """수수료를 뺀 실현손익($). (값, 추정여부).

    거래소의 `realizedPnl`에는 수수료가 안 들어있어서, 그 값만 보고 "오늘 +5 USDT 벌었다"고
    말하면 계좌 총자산과 맞지 않는다(2026-09-24에 실제로 문제가 됐다). 2026-09-22 이후 기록은
    `net_realized_pnl`을 직접 들고 있고, 그 전 기록은 수수료 R과 이 거래의 "R당 달러"로
    환산해 추정한다 — 사이징이 거래마다 달라서 고정 금액으로는 추정할 수 없다.
    """
    net = trade.get("net_realized_pnl")
    if isinstance(net, (int, float)):
        return float(net), False

    pnl = trade.get("realized_pnl")
    if not isinstance(pnl, (int, float)):
        return None, False

    fee_r, _ = _fee_r(trade)
    realized_r = trade.get("realized_r")
    if fee_r is None or not isinstance(realized_r, (int, float)) or realized_r == 0:
        return float(pnl), True          # 수수료를 못 재면 총액 그대로(추정 표시)
    dollars_per_r = abs(float(pnl) / float(realized_r))
    return float(pnl) - fee_r * dollars_per_r, True


def _drawdown(values: list[float]) -> float:
    """누적 곡선의 최대 낙폭(고점 대비 최대 하락폭). 자산 대비 %가 아니라 절대값이다 —
    입출금 이력이 없어서 신뢰할 수 있는 시작 자산을 모르기 때문."""
    peak, worst = 0.0, 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def config_changes(entries: list[dict]) -> list[dict]:
    """저널의 config_changed 기록을 시간순으로 돌려준다(futures_rule_bot.log_config_change가
    봇 시작 시 설정이 바뀌었을 때만 남긴다).

    성과 곡선 위에 "여기서 규칙이 바뀌었다"를 그리기 위한 것 — 레짐 필터나 저변동 필터가
    들어가기 전후의 거래가 한 줄의 총R로 뭉뚱그려지면, 그 숫자로는 지금 규칙이 통하는지를
    판단할 수 없다."""
    return [
        {"timestamp": e.get("timestamp"),
          "changes": e.get("changes") or {},
          "first_record": bool(e.get("first_record"))}
        for e in entries if e.get("event") == "config_changed"
    ]


def _summarize_trades_since(trades: list[dict], since: str | None) -> dict:
    """since(저널 타임스탬프) 이후 청산된 거래만 골라 건수/총R/실현손익을 센다."""
    recent = [t for t in trades if since is None or (t.get("timestamp") or "") > since]
    with_r = [t for t in recent if t.get("realized_r") is not None]
    return {
        "trades": len(recent),
        "trades_with_r": len(with_r),
        "total_r": sum(t["realized_r"] for t in with_r) if with_r else None,
        "realized_pnl": sum(t.get("realized_pnl") or 0.0 for t in recent),
        "win_rate": (sum(1 for t in recent if (t.get("realized_pnl") or 0) > 0) / len(recent)) if recent else None,
    }


def summarize_r_performance(entries: list[dict], max_points: int = 400) -> dict:
    """R배수 기준 성과 + 자산곡선. 달러 기준 요약(summarize_performance)과 별개로 두는 이유는
    묻는 질문이 다르기 때문 — 달러는 "실제로 얼마 벌었나", R은 "계획 대비 잘하고 있나"이고
    백테스트 기대치와 비교할 수 있는 건 후자뿐이다.

    max_points: 자산곡선이 길어지면 응답만 커지고 화면에선 구분이 안 되므로 최근 것만 보낸다."""
    trades = resolve_closed_trades(entries)
    with_r = [t for t in trades if t.get("realized_r") is not None]
    changes = config_changes(entries)

    # 순R(수수료 차감)을 같이 굴린다 — 총R만 보면 이 전략이 흑자로 보이지만 수수료를 넣으면
    # 부호가 바뀐다. 두 곡선을 같이 줘서 화면이 그 차이를 보여줄 수 있게 한다.
    net_values, net_estimated = [], 0
    for trade in trades:
        net, estimated = _net_r(trade)
        net_values.append(net)
        if net is not None and estimated:
            net_estimated += 1

    curve = []
    cumulative_pnl, cumulative_r, cumulative_net_r = 0.0, 0.0, 0.0
    for trade, net in zip(trades, net_values):
        cumulative_pnl += trade.get("realized_pnl") or 0.0
        if trade.get("realized_r") is not None:
            cumulative_r += trade["realized_r"]
        if net is not None:
            cumulative_net_r += net
        curve.append({"timestamp": trade.get("timestamp"), "symbol": trade.get("symbol"),
                       "cumulative_pnl": cumulative_pnl, "cumulative_r": cumulative_r,
                       "cumulative_net_r": cumulative_net_r})

    by_side: dict[str, dict] = {}
    for trade in with_r:
        side = trade.get("side") or "unknown"
        bucket = by_side.setdefault(side, {"trades": 0, "wins": 0, "total_r": 0.0})
        bucket["trades"] += 1
        bucket["total_r"] += trade["realized_r"]
        if trade["realized_r"] > 0:
            bucket["wins"] += 1

    # "익절 코앞까지 갔다가 손절났다"가 실제로 얼마나 되는지 — 이 체감이 전략을 다시 들여다본
    # 출발점이었고, 이제 보유 중 최고점(excursion)이 청산 기록에 남으므로 바로 셀 수 있다.
    losers = [t for t in with_r if t["realized_r"] <= 0 and t.get("max_favorable_r") is not None]
    near_misses = [t for t in losers if t["max_favorable_r"] >= 1.0]

    return {
        "num_trades": len(with_r),
        # 달러 요약과 모수가 다를 수 있다 — 진입가가 이 청산과 안 맞아 R을 못 믿는 거래는
        # 여기서만 빠지기 때문. 화면이 그 차이를 설명할 수 있게 전체 건수도 같이 준다.
        "trades_total": len(trades),
        "total_r": sum(t["realized_r"] for t in with_r),
        "avg_r": (sum(t["realized_r"] for t in with_r) / len(with_r)) if with_r else None,
        "win_rate": (sum(1 for t in with_r if t["realized_r"] > 0) / len(with_r)) if with_r else None,
        "max_drawdown_r": _drawdown([p["cumulative_r"] for p in curve]),
        "max_drawdown_net_r": _drawdown([p["cumulative_net_r"] for p in curve]),
        "max_drawdown_usd": _drawdown([p["cumulative_pnl"] for p in curve]),
        # 수수료 차감 후 — 이쪽이 실제 성과다. total_r은 수수료 이전 값이라 백테스트의
        # 무수수료 결과와만 비교된다.
        "total_net_r": sum(v for v in net_values if v is not None),
        "avg_net_r": (sum(v for v in net_values if v is not None) / sum(1 for v in net_values if v is not None)
                       if any(v is not None for v in net_values) else None),
        # 실측 수수료가 저널에 없어서 설정값으로 추정한 거래 수 — 화면이 실측과 섞어 말하지
        # 않도록 몇 건이 추정인지 밝힌다(2026-09-22 이전 청산은 전부 추정이다).
        "net_r_estimated_trades": net_estimated,
        "fee_pct_per_side": FEE_PCT_PER_SIDE,
        "by_side": by_side,
        "equity_curve": curve[-max_points:],
        # 설정 변경 경계와 "현재 설정으로만" 낸 성과 — 필터를 추가하기 전후가 한 숫자로
        # 뭉뚱그려지면 지금 규칙이 통하는지 알 수 없다(2026-09-12).
        "config_changes": changes,
        "since_config_change": ({**_summarize_trades_since(trades, changes[-1]["timestamp"]),
                                  "timestamp": changes[-1]["timestamp"],
                                  "changes": changes[-1]["changes"]} if changes else None),
        "mfe": {
            "losers_measured": len(losers),
            "losers_reaching_1r": len(near_misses),
            "median_loser_peak_r": (
                sorted(t["max_favorable_r"] for t in losers)[len(losers) // 2] if losers else None),
        },
    }


def _local_day(timestamp: str) -> str | None:
    """저널의 UTC 타임스탬프를 **로컬 날짜**로 바꾼다. 일일 손실 한도가 리셋되는 경계
    (state.get_daily_pnl_pct의 date.today())와 같은 기준이라야 요약의 "하루"가 사용자가
    화면에서 보는 "오늘"과 어긋나지 않는다."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).astimezone().date().isoformat()
    except ValueError:
        return None


def summarize_day(entries: list[dict], day: str) -> dict:
    """하루치 성과 한 줄 요약용 집계 — 청산 결과(건수/승패/총R/실현손익)와 그날 있었던
    거부·서킷브레이커 횟수. 텔레그램 일일 요약 푸시가 쓴다.

    R 복원은 resolve_closed_trades에 맡긴다(옛 기록도 진입 기록에서 손절가를 끌어와 R을
    복원해주므로, 이 기능이 생기기 전 거래도 그대로 집계된다)."""
    trades = [t for t in resolve_closed_trades(entries) if _local_day(t.get("timestamp")) == day]
    with_r = [t for t in trades if t.get("realized_r") is not None]

    rejections = 0
    circuit_breakers = 0
    for entry in entries:
        if _local_day(entry.get("timestamp")) != day:
            continue
        if entry.get("event") == "rejected_exchange_error":
            rejections += 1
        elif entry.get("event") == "circuit_breaker_blocked":
            circuit_breakers += 1

    wins = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)

    # 승패는 수수료 전 부호로 센다(거래소 체결 내역과 같은 기준). 금액은 수수료 후로 말한다 —
    # 화면의 "+N USDT"가 계좌 총자산과 어긋나면 요약 자체가 쓸모없어지기 때문이다.
    nets = [_net_pnl(t) for t in trades]
    net_values = [v for v, _ in nets if v is not None]
    net_r = [_net_r(t) for t in trades]
    net_r_values = [v for v, _ in net_r if v is not None]

    return {
        "day": day,
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "total_r": sum(t["realized_r"] for t in with_r) if with_r else None,
        "net_total_r": sum(net_r_values) if net_r_values else None,
        "realized_pnl": sum(t.get("realized_pnl") or 0.0 for t in trades),
        "net_realized_pnl": sum(net_values) if net_values else None,
        "fees": (sum(t.get("realized_pnl") or 0.0 for t in trades) - sum(net_values)
                 if net_values else None),
        "fees_estimated": any(est for v, est in nets if v is not None),
        "rejections": rejections,
        "circuit_breakers": circuit_breakers,
    }
