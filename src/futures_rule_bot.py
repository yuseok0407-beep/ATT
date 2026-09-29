import json
import logging
from datetime import datetime
from pathlib import Path

import ccxt

from src.core.config import (
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    FUTURES_RISK_PER_TRADE,
    FUTURES_SYMBOLS,
    LEVERAGE,
    MARGIN_MODE,
    MAX_CONCURRENT_POSITIONS,
    MAX_ENTRY_PRICE_DRIFT_R,
    MIN_ATR_TO_STOP_RATIO,
    RULE_ADX_THRESHOLD,
    RULE_DIRECTION_FILTER,
    RULE_HTF_HOURS,
    RULE_REGIME_SMA_PERIOD,
    RULE_SMA_PERIOD,
    RULE_TIMEFRAME,
    STOP_LOSS_PCT,
    TAKE_PROFIT_ORDER_TYPE,
    TAKE_PROFIT_RR,
)
from src.core.futures_risk import check_stop_before_liquidation, estimate_liquidation_price, leveraged_position_size
from src.core.futures_strategy import (
    STRATEGY_LOGIC_REVISION,
    apply_htf_filter,
    apply_regime_filter,
    atr_to_stop_ratio,
    closed_bars_needed,
    compute_bracket_prices,
    detect_signal,
    is_above_long_sma,
    latest_htf_direction,
    passes_volatility_floor,
)
from src.core.risk import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS_PCT, check_circuit_breaker
from src.core.state import compute_consecutive_losses, get_daily_pnl_pct
from src.data.futures_exchange import (
    fetch_ohlcv_df,
    get_futures_balance,
    get_futures_client,
    get_futures_market_data_client,
    get_max_leverage,
    get_notional_cap,
    get_position,
    get_positions,
    set_leverage,
    set_margin_mode,
)
from src.execution import excursion, filter_stats
from src.execution.futures_orders import cleanup_stale_orders, get_bracket_prices, open_position_with_bracket
from src.execution.journal import append_entry, read_entries

JOURNAL_PATH = "journal/futures_rule_trades.jsonl"
STATE_PATH = "state/futures_rule_daily_equity.json"
LAST_TRADE_STATE_PATH = "state/futures_rule_last_trade.json"
FILTER_STATS_PATH = filter_stats.DEFAULT_PATH
EXCURSION_PATH = excursion.DEFAULT_PATH

# 실계좌(진짜 자금) 전용 — 데모 파일은 절대 안 건드리고 완전히 분리된 저널/상태로 기록한다
# (2026-08-22, 데모/실계좌 동시 운영). run_once(env="live", ...)가 명시적으로 안 넘기면
# 이 상수들이 기본값으로 쓰인다.
LIVE_JOURNAL_PATH = "journal/futures_rule_trades.live.jsonl"
LIVE_STATE_PATH = "state/futures_rule_daily_equity.live.json"
LIVE_LAST_TRADE_STATE_PATH = "state/futures_rule_last_trade.live.json"
LIVE_FILTER_STATS_PATH = filter_stats.LIVE_DEFAULT_PATH
LIVE_EXCURSION_PATH = excursion.LIVE_DEFAULT_PATH

logger = logging.getLogger(__name__)

# 저널에 남기지 않는(스팸 방지) 이벤트 — 매 사이클 반복돼도 상태 변화가 없는 것들.
# skipped_same_signal_bar / skipped_price_drift는 신호가 살아있는 동안 POLL_INTERVAL_SECONDS마다
# 계속 재발생해서(1시간봉이면 한 봉당 최대 120회) 남기면 저널이 터진다 — 게다가 둘 다 "정상적으로
# 막고 있다"는 뜻이라 사용자 조치가 필요한 rejected_exchange_error와 성격이 다르다. 현재 사이클
# 결과(run_once의 반환값)에는 그대로 실려서 대시보드에서는 볼 수 있다.
# 저널엔 안 남지만 "몇 번 걸렀는지"는 필요하므로 run_once가 filter_stats로 봉 단위 집계는 남긴다
# (2026-09-09) — 필터가 백테스트대로 도는지 확인할 유일한 수단이라.
_SILENT_EVENTS = ("no_signal", "holding_position", "skipped_max_positions", "skipped_regime",
                  "skipped_same_signal_bar", "skipped_price_drift", "skipped_low_volatility",
                  "skipped_htf")


# 저널에 기록해두는 "이 거래들이 어떤 규칙으로 나왔는지" — 진입 판단에 실제로 영향을 주는
# 설정만 넣는다(알림 주기 같은 운영 설정은 뺀다). 값이 바뀌면 log_config_change가 봇 시작 시
# 저널에 한 줄 남기므로, 나중에 성과를 볼 때 "이 구간은 어떤 규칙이었나"를 저널만으로 알 수 있다.
# 봉 마감 후 몇 초 뒤에 깰지. 거래소의 새 봉은 정각에 시작하지만 이 PC 시계가 거래소보다 조금
# 느릴 수 있어서 여유를 둔다. 너무 일찍 깨도 안전하다 — 아직 안 끝난 봉은 트리밍으로 버리므로
# 직전(이미 판정한) 봉을 다시 보고, 같은 신호봉 잠금 때문에 재진입하지 않는다. 다음 사이클이 잡는다.
BAR_CLOSE_OFFSET_SECONDS = 3


def seconds_until_next_cycle(now: float, poll_seconds: float, timeframe: str = RULE_TIMEFRAME,
                             offset_seconds: float = BAR_CLOSE_OFFSET_SECONDS) -> float:
    """다음 사이클까지 기다릴 초. 평소엔 poll_seconds, 단 **다음 봉 마감(+offset)이 그보다 먼저면
    그때 깬다**(2026-09-28).

    신호는 마감된 봉으로만 판정하므로 새 신호는 봉이 끝나는 순간에만 생긴다. 30초 주기로만 돌면
    신호 발생 후 진입까지 평균 15초(예전엔 사이클 자체가 느려 중간값 39초)를 기다리고, 그동안 가격이
    신호 봉 종가에서 멀어진다 — 그 거리가 곧 진입 슬리피지이고, 0.1R을 넘으면 가격이탈로 진입을
    포기한다. 주기 전체를 줄이면 거래소 요청이 그만큼 늘어나(IP 한도는 대시보드와 공유) 대신 마감
    시각에만 맞춰 한 번 더 깬다 — 요청 수는 그대로다.
    """
    bar = ccxt.Exchange.parse_timeframe(timeframe)
    this_close = (now // bar) * bar + offset_seconds
    target = this_close if now < this_close else this_close + bar
    return max(0.5, min(poll_seconds, target - now))


def current_strategy_config() -> dict:
    """지금 이 프로세스가 들고 있는 전략/리스크 설정. 저널에 남길 형태 그대로."""
    return {
        "timeframe": RULE_TIMEFRAME,
        "adx_threshold": RULE_ADX_THRESHOLD,
        "sma_period": RULE_SMA_PERIOD,
        "regime_sma_period": RULE_REGIME_SMA_PERIOD,
        "direction_filter": RULE_DIRECTION_FILTER,
        "htf_hours": RULE_HTF_HOURS,
        "min_atr_to_stop_ratio": MIN_ATR_TO_STOP_RATIO,
        "max_entry_price_drift_r": MAX_ENTRY_PRICE_DRIFT_R,
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_rr": TAKE_PROFIT_RR,
        # 체결 방식도 성과를 바꾼다(지정가 익절은 조건부 시장가보다 거래당 약 0.03R 싸다) —
        # 바뀐 시점에 전략 버전 경계가 생겨야 전후 비교가 된다.
        "take_profit_order_type": TAKE_PROFIT_ORDER_TYPE,
        "leverage": LEVERAGE,
        "risk_per_trade": FUTURES_RISK_PER_TRADE,
        "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
        "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        "consecutive_loss_cooldown_hours": CONSECUTIVE_LOSS_COOLDOWN_HOURS,
        "symbols": sorted(FUTURES_SYMBOLS),
        # 코드 규칙 개정 번호 — 설정이 그대로여도 규칙 코드가 바뀌면 새 전략 버전이 되게 한다.
        "logic_revision": STRATEGY_LOGIC_REVISION,
    }


def _last_logged_config(journal_path: str) -> dict | None:
    for entry in reversed(read_entries(path=journal_path)):
        if entry.get("event") == "config_changed":
            return entry.get("config")
    return None


def log_config_change(journal_path: str = None, config: dict = None) -> dict | None:
    """지금 설정이 저널에 마지막으로 기록된 설정과 다르면 config_changed를 한 줄 남긴다.
    같으면 아무것도 안 한다(봇을 하루에 몇 번씩 재시작해도 저널이 안 더러워진다).

    있으면 좋은 기록이 아니라 **성과 해석에 필요한 기록**이다. 지금 저널에는 거래만 있고 그
    거래가 어떤 규칙으로 나왔는지가 없어서, 화면의 "총 -3.18R"이 레짐 필터/저변동 필터가 있던
    구간인지 없던 구간인지 섞여 있는지조차 알 수 없다 — 실거래와 백테스트를 비교하려면
    "같은 규칙으로 낸 거래"만 봐야 하는데 그 경계가 사람 기억(UPDATE_LOG.md)에만 있었다.
    설정 변경일을 저널에 박아두면 대시보드가 자산곡선 위에 그 경계를 그릴 수 있다(2026-09-12).

    심볼 목록도 같이 남긴다 — 종목 추가는 파라미터 변경만큼이나 성과 구간을 갈라놓는다."""
    journal_path = journal_path or JOURNAL_PATH
    config = current_strategy_config() if config is None else config
    previous = _last_logged_config(journal_path)
    if previous == config:
        return None

    # 첫 기록은 "무엇이 바뀌었나"가 없다(비교 대상이 없으므로) — 전 항목을 from=None으로
    # 채우면 알림도 화면도 14줄짜리 잡음이 된다. 값 전체는 config에 그대로 들어있다.
    changes = {}
    if previous is not None:
        for key, value in config.items():
            if previous.get(key) != value:
                changes[key] = {"from": previous.get(key), "to": value}

    entry = {"event": "config_changed", "config": config, "changes": changes,
              "first_record": previous is None}
    append_entry(entry, path=journal_path)
    return entry


def _load_last_trade_ids(path: str = LAST_TRADE_STATE_PATH) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def _save_last_trade_id(symbol: str, trade_id: str, path: str = LAST_TRADE_STATE_PATH) -> None:
    file_path = Path(path)
    data = _load_last_trade_ids(path)
    data[symbol] = trade_id
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data), encoding="utf-8")


def _find_last_entry_journal(symbol: str, journal_path: str = JOURNAL_PATH) -> dict | None:
    for entry in reversed(read_entries(path=journal_path)):
        if entry.get("symbol") == symbol and entry.get("event") == "entered":
            return entry
    return None


# 한 심볼의 "포지션 생애주기" 이벤트 — 지금 이 심볼의 포지션을 봇이 알고 있는지 판정할 때는
# 이것만 본다. 무보호 경보/거래소 오류 같은 다른 기록이 그 사이에 끼어들어도 판정이 흔들리지
# 않게 하려는 것(2026-09-09, unprotected_position 추가하면서 필요해짐 — 안 그러면 무보호 기록이
# 마지막 기록이 되는 순간 check_and_log_untracked_position이 "모르는 포지션"으로 오인해서 매
# 사이클 진입 기록을 중복 백필한다).
_LIFECYCLE_EVENTS = ("entered", "closed")
_PROTECTION_EVENTS = ("entered", "closed", "unprotected_position", "position_protected")


def _find_last_symbol_event(symbol: str, journal_path: str = JOURNAL_PATH,
                             among: tuple = None) -> str | None:
    """이 심볼의 마지막 이벤트 이름. among을 주면 그 목록에 있는 이벤트만 훑는다."""
    for entry in reversed(read_entries(path=journal_path)):
        if entry.get("symbol") != symbol:
            continue
        event = entry.get("event")
        if among is not None and event not in among:
            continue
        return event
    return None


def _find_last_symbol_entry(symbol: str, journal_path: str = JOURNAL_PATH) -> dict | None:
    for entry in reversed(read_entries(path=journal_path)):
        if entry.get("symbol") == symbol:
            return entry
    return None


def check_and_log_untracked_position(client, symbol: str, position: dict,
                                      journal_path: str = None) -> dict | None:
    """이 심볼에 지금 포지션이 있는데 저널의 마지막 기록이 "entered"가 아니면(즉 이 봇이 이
    포지션의 존재를 전혀 모르고 있으면) 대시보드 거래 내역에도 보이도록 진입 기록을 남긴다.
    대시보드에서 종목별 상태(거래소 직접 조회)엔 바로 보이는데 거래 내역(저널 기반)엔 안
    보이는 걸 사용자가 실제로 겪었음(2026-08-14) — 대시보드/봇을 거치지 않고 사용자가 직접
    주문을 넣은 경우가 대표적. 매 사이클 호출돼도, 이미 알고 있는 포지션이면(마지막 기록이
    "entered") 중복 기록하지 않는다."""
    journal_path = journal_path or JOURNAL_PATH
    if _find_last_symbol_event(symbol, journal_path=journal_path, among=_LIFECYCLE_EVENTS) == "entered":
        return None

    stop_loss_price, take_profit_price = get_bracket_prices(client, symbol)
    entry = {
        "symbol": symbol, "event": "entered", "entered": True, "signal": None,
        "entry_price": position.get("entryPrice"),
        "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
        "reason": "manual_position_detected",
    }
    append_entry(entry, path=journal_path)
    return entry


def _entry_side(entry_info: dict) -> str | None:
    """진입 기록에서 방향("long"/"short")을 뽑는다. signal이 있으면 그대로 쓰고, 없으면(사용자가
    직접 넣어서 check_and_log_untracked_position이 백필한 포지션은 signal=None) 손절가 위치로
    추론한다 — 손절가가 진입가보다 아래면 롱."""
    signal = entry_info.get("signal")
    if signal in ("LONG", "SHORT"):
        return "long" if signal == "LONG" else "short"
    entry_price, stop_loss_price = entry_info.get("entry_price"), entry_info.get("stop_loss_price")
    if entry_price is None or stop_loss_price is None:
        return None
    return "long" if stop_loss_price < entry_price else "short"


def check_position_protection(client, symbol: str, journal_path: str = None) -> dict | None:
    """포지션이 있는데 손절(STOP_MARKET) 주문이 안 걸려 있으면 저널에 남긴다(2026-09-09 추가).

    10배 레버리지에서 손절 없는 포지션은 일어날 수 있는 가장 비싼 실패인데, 지금까지 대시보드
    카드에 "-" 한 글자로만 표시돼서 정상 상태와 육안으로 구분이 안 됐다. 저널에 남기면 텔레그램
    알림 경로(_NOTIFY_EVENTS)를 그대로 타므로 폰으로도 즉시 알 수 있다.

    조회 자체가 실패하면 아무것도 안 한다 — get_bracket_prices의 기본 동작은 실패를 (None, None)
    으로 뭉뚱그려서 "일시적 API 오류"와 "정말로 무보호"가 구별되지 않으므로, 여기서는 strict=True로
    불러 오류를 예외로 받아 넘긴다. 잘못된 경보는 진짜 경보를 무시하게 만들어서 없느니만 못하다.

    보호가 복구되면 position_protected를 한 번 남긴다 — 경보만 있고 해제 알림이 없으면 사용자가
    아직 위험한 상태인지 계속 확인해야 한다."""
    journal_path = journal_path or JOURNAL_PATH
    try:
        stop_loss_price, take_profit_price = get_bracket_prices(client, symbol, strict=True)
    except Exception:
        logger.warning("symbol %s failed to query bracket orders — skipping protection check", symbol)
        return None

    last_event = _find_last_symbol_event(symbol, journal_path=journal_path, among=_PROTECTION_EVENTS)

    if stop_loss_price is None:
        if last_event == "unprotected_position":
            return None  # 이미 알렸다 — 매 사이클 반복해서 쌓지 않는다
        entry = {"symbol": symbol, "event": "unprotected_position",
                  "take_profit_price": take_profit_price,
                  "reason": "포지션이 있는데 손절 주문이 걸려있지 않습니다"}
        append_entry(entry, path=journal_path)
        return entry

    if last_event == "unprotected_position":
        entry = {"symbol": symbol, "event": "position_protected",
                  "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price}
        append_entry(entry, path=journal_path)
        return entry
    return None


def _aggregate_closing_trades(client, symbol: str, entry_info: dict,
                               last_trade_path: str = None) -> tuple[list, float | None, float | None, dict]:
    """entry_info(마지막 "entered" 저널 기록) 이후에 일어난 체결들 중 진입 체결 자체를 제외한
    나머지(=청산 체결들)를 모아 수량가중평균 체결가와 합산 실현손익을 계산한다. 청산 체결을
    못 찾으면 ([], None, None, {}).

    네 번째 반환값은 이 왕복 거래의 실제 체결 사실(`fees`) — 진입/청산 수수료와 실제 진입
    체결가다. 같은 `fetch_my_trades` 응답 안에 이미 다 들어있어서 추가 조회 없이 뽑을 수 있고,
    이게 없으면 저널의 손익·R이 영구적으로 수수료 이전 값으로만 남는다(아래 _sum_commission 참고).

    fetch_my_trades를 since 없이 limit만 주면 "최신 N개"가 아니라 "가장 오래된 N개"가 돌아오는
    거래소 동작 때문에(실전 확인, UPDATE_LOG.md 2026-08-12) 진입 시각을 since로 앵커링해야
    한다. STOP_MARKET/TAKE_PROFIT_MARKET/수동청산 체결이 여러 번의 부분 체결로 쪼개질 수 있어
    단일 체결이 아니라 전부 합산한다.

    since에는 60초 버퍼를 두는데(주문 생성/체결 타임스탬프 오차 대비), 직전 포지션이 청산된 지
    60초 안에 새 포지션이 재진입되면 이 버퍼가 "직전 포지션의 이미 처리된 청산 체결"까지 다시
    끌어와 현재 포지션의 실현손익에 합산해버리는 사고가 실제로 있었다(2026-08-20, SOL 실전
    확인 — 직전 포지션의 +63.6659 이익이 현재 포지션의 -16.401 손실에 섞여 최종 pnl이
    +47.2649로, 손절인데 수익이 난 것처럼 저널에 기록됨). 이미 처리되어 LAST_TRADE_STATE_PATH에
    저장된 last_trade_id 이하의 체결은 항상 "이전 포지션 몫"이므로 제외해야 한다."""
    last_trade_path = last_trade_path or LAST_TRADE_STATE_PATH
    since_ms = int(datetime.fromisoformat(entry_info["timestamp"]).timestamp() * 1000) - 60_000
    trades = client.fetch_my_trades(symbol, since=since_ms, limit=50)
    if not trades:
        return [], None, None

    entry_order_id = ((entry_info.get("execution") or {}).get("entry_order") or {}).get("id")
    if entry_order_id is not None:
        closing_trades = [t for t in trades if str((t.get("info") or {}).get("orderId")) != str(entry_order_id)]
    else:
        # 옛 저널 기록 등 진입 주문ID가 없는 경우의 대비책 — 체결가가 진입가와 정확히 같은 것만
        # 진입 체결로 간주한다(슬리피지가 있으면 완벽하지 않지만 없는 것보다 낫다).
        entry_price = entry_info.get("entry_price")
        closing_trades = [t for t in trades if t.get("price") != entry_price]

    last_processed_id = _load_last_trade_ids(path=last_trade_path).get(symbol)
    if last_processed_id is not None:
        try:
            last_processed_id = int(last_processed_id)
            closing_trades = [t for t in closing_trades
                               if t.get("id") is not None and int(t["id"]) > last_processed_id]
        except (TypeError, ValueError):
            pass  # 거래ID가 숫자가 아닌 형식이면(구버전 등) 이 필터는 건너뛴다 — 없는 것보다는 필터 없이 진행이 낫다.

    if not closing_trades:
        return [], None, None, {}

    total_qty = sum(float(t.get("amount") or 0) for t in closing_trades)
    if total_qty > 0:
        exit_price = sum(float(t.get("price") or 0) * float(t.get("amount") or 0) for t in closing_trades) / total_qty
    else:
        exit_price = closing_trades[-1].get("price")
    realized_pnl = sum(float((t.get("info") or {}).get("realizedPnl", 0) or 0) for t in closing_trades)

    # 진입 체결 = 이번 왕복에 속한 체결 중 청산 체결이 아닌 것. 진입 쪽 수수료와 **실제 진입
    # 체결가**가 여기서만 나온다 — 주문 생성 응답에는 둘 다 안 담겨 온다(avgPrice가 "0.00"으로
    # 온다, 2026-09-22 확인).
    closing_ids = {t.get("id") for t in closing_trades}
    entry_trades = [t for t in trades if t.get("id") not in closing_ids]
    if last_processed_id is not None:
        try:
            entry_trades = [t for t in entry_trades
                             if t.get("id") is not None and int(t["id"]) > int(last_processed_id)]
        except (TypeError, ValueError):
            pass

    entry_fee = _sum_commission(entry_trades)
    exit_fee = _sum_commission(closing_trades)
    fees = {}
    if entry_fee is not None or exit_fee is not None:
        fees["entry_fee"] = entry_fee
        fees["exit_fee"] = exit_fee
        fees["total_fee"] = (entry_fee or 0.0) + (exit_fee or 0.0)
        assets = _commission_assets(entry_trades) + [
            a for a in _commission_assets(closing_trades) if a not in _commission_assets(entry_trades)]
        if assets:
            fees["fee_assets"] = assets
    entry_qty = sum(float(t.get("amount") or 0) for t in entry_trades)
    if entry_qty > 0:
        fees["actual_entry_price"] = sum(
            float(t.get("price") or 0) * float(t.get("amount") or 0) for t in entry_trades) / entry_qty
    return closing_trades, exit_price, realized_pnl, fees


def _sum_commission(trades: list) -> float | None:
    """체결 목록의 수수료 합계(USDT). 수수료 정보가 아예 없으면 None.

    **바이낸스의 `realizedPnl`에는 수수료가 안 들어있다** — `commission`이 별개 필드이고,
    지금까지 저널의 `realized_pnl`은 앞쪽만 담아왔다(2026-09-22에 실제 계좌 체결 내역으로 확인:
    CRCL 청산 한 건이 realizedPnl -48.23에 commission 0.80, 진입 쪽 0.81이 따로 있었다).
    그래서 저널의 손익과 R은 둘 다 수수료 이전의 값이고, 손절폭 1.25%에서 왕복 수수료는
    약 0.04~0.064R — 이 전략의 건당 기대값과 같은 크기다. 추정하지 않고 실제 값을 남긴다.

    USDT 외의 자산으로 낸 수수료(BNB 할인 등)는 환산 없이 그대로 더한다 — 금액이 작고, 환율을
    끌어오면 기록 시점에 따라 값이 달라져서 재현이 안 된다. commissionAsset은 같이 남긴다."""
    if not trades:
        return None
    total = 0.0
    found = False
    for trade in trades:
        info = trade.get("info") or {}
        raw = info.get("commission")
        if raw is None:
            continue
        try:
            total += float(raw)
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else None


def _commission_assets(trades: list) -> list[str]:
    assets = []
    for trade in trades or []:
        asset = (trade.get("info") or {}).get("commissionAsset")
        if asset and asset not in assets:
            assets.append(asset)
    return assets


def _build_closed_entry(symbol: str, reason: str, entry_info: dict, exit_price, realized_pnl,
                         excursion_path: str = None, fees: dict = None) -> dict:
    """청산 저널 기록 하나를 만든다. 실현손익($)만 남기던 걸 진입 맥락(방향/손절가/익절가)과
    R배수까지 같이 남기도록 확장했다(2026-09-09).

    이유: $ 금액만으로는 "이 거래가 계획 대비 어땠는지"를 알 수 없다 — 사이징이 달라지면 같은
    -20달러가 -0.3R일 수도 -1R일 수도 있어서 백테스트 기대치와 비교가 안 된다. R은 수량/레버리지와
    무관하게 가격만으로 계산되므로(손절가까지가 정확히 -1R) 백테스트의 R과 곧바로 비교된다.
    지금까지 청산 기록엔 방향도 손절가도 없어서 사후에 R을 복원하는 것 자체가 불가능했다.

    max_favorable_r/max_adverse_r는 보유 중 매 사이클 굴려둔 최고/최저 지점(excursion 모듈) —
    "익절 코앞까지 갔다가 손절났다"를 5분봉으로 경로를 재구성하지 않고 바로 볼 수 있게 한다."""
    side = _entry_side(entry_info)
    entry_price = entry_info.get("entry_price")
    stop_loss_price = entry_info.get("stop_loss_price")

    closed_entry = {
        "symbol": symbol, "event": "closed", "reason": reason,
        "entry_price": entry_price, "exit_price": exit_price, "realized_pnl": realized_pnl,
        "side": side, "stop_loss_price": stop_loss_price,
        "take_profit_price": entry_info.get("take_profit_price"),
        "entry_timestamp": entry_info.get("timestamp"),
    }
    if side is not None and exit_price is not None:
        closed_entry["realized_r"] = excursion.to_r(exit_price, entry_price, stop_loss_price, side)

    # 실제 수수료와 실제 진입 체결가(2026-09-22). realized_pnl과 realized_r은 둘 다 수수료
    # **이전** 값이다 — 거래소의 realizedPnl에 수수료가 안 들어있고, R은 신호 봉 종가를
    # 진입가로 써서 슬리피지도 안 들어있다. 두 값을 같이 남겨야 사후에 순성과를 낼 수 있다.
    for key in ("entry_fee", "exit_fee", "total_fee", "fee_assets", "actual_entry_price"):
        if fees and fees.get(key) is not None:
            closed_entry[key] = fees[key]
    if fees and fees.get("total_fee") is not None and realized_pnl is not None:
        closed_entry["net_realized_pnl"] = realized_pnl - fees["total_fee"]
    # 수수료의 R 환산 — 사이징과 무관하게 R로 비교하려면 이 값이 필요하다(가격만으로 잰 R에서
    # 이걸 빼면 백테스트의 fee_pct_per_side 차감과 같은 의미가 된다).
    risk_per_unit = None
    if entry_price is not None and stop_loss_price is not None:
        risk_per_unit = abs(float(entry_price) - float(stop_loss_price))
    quantity = (entry_info.get("execution") or {}).get("quantity")
    if (fees and fees.get("total_fee") is not None and risk_per_unit
            and isinstance(quantity, (int, float)) and quantity > 0):
        closed_entry["fee_r"] = fees["total_fee"] / (risk_per_unit * quantity)
        if closed_entry.get("realized_r") is not None:
            closed_entry["net_realized_r"] = closed_entry["realized_r"] - closed_entry["fee_r"]

    record = excursion.pop(symbol, path=excursion_path or EXCURSION_PATH)
    if record is not None:
        closed_entry["max_favorable_r"] = record.get("max_favorable_r")
        closed_entry["max_adverse_r"] = record.get("max_adverse_r")
    return closed_entry


def check_and_log_closed_trade(client, symbol: str, journal_path: str = None,
                                last_trade_path: str = None, excursion_path: str = None) -> dict | None:
    """지금 포지션이 없는(flat) 종목에 대해, 직전 체결이 아직 저널에 안 남은 청산인지 확인하고
    있으면 실현손익과 함께 기록한다. 손절/익절 중 어느 쪽이었는지는 진입 시 기록해둔 손절가/익절가와
    체결가를 비교해 추정한다(정확한 체결가는 트리거 가격과 보통 일치하거나 매우 가깝다)."""
    journal_path = journal_path or JOURNAL_PATH
    last_trade_path = last_trade_path or LAST_TRADE_STATE_PATH
    entry_info = _find_last_entry_journal(symbol, journal_path=journal_path)
    if entry_info is None:
        # 이 기능이 생기기 전에 체결된 거래 등 대조할 진입 기록이 없으면, 잘못된 추정을 기록하지
        # 않도록 그냥 마지막 거래ID만 기억해두고 넘어간다.
        try:
            trades = client.fetch_my_trades(symbol, limit=5)
        except Exception:
            return None
        if trades:
            _save_last_trade_id(symbol, trades[-1].get("id"), path=last_trade_path)
        return None

    try:
        closing_trades, exit_price, realized_pnl, fees = _aggregate_closing_trades(
            client, symbol, entry_info, last_trade_path=last_trade_path)
    except Exception:
        return None
    if not closing_trades:
        return None

    last_trade_id = closing_trades[-1].get("id")
    if last_trade_id is None or _load_last_trade_ids(path=last_trade_path).get(symbol) == last_trade_id:
        return None

    stop_loss_price = entry_info.get("stop_loss_price")
    take_profit_price = entry_info.get("take_profit_price")
    reason = "unknown"
    if stop_loss_price is not None and take_profit_price is not None:
        guessed_reason = ("stop_loss" if abs(exit_price - stop_loss_price) < abs(exit_price - take_profit_price)
                           else "take_profit")
        # 손절은 항상 손실(이거나 최악이라도 0에 가까움)이고 익절은 항상 이익이어야 한다 — 체결가
        # 거리로 추정한 reason이 실제 손익 부호와 모순되면(예: "stop_loss"인데 realized_pnl이 +),
        # 추정 자체가 틀렸다는 뜻이니 unknown으로 남긴다. 위 since-앵커링 버그로 오염된 pnl이
        # "손절인데 흑자"처럼 겉보기에도 말이 안 되는 기록을 실제로 남긴 적이 있다(2026-08-20).
        if realized_pnl is not None and (
            (guessed_reason == "stop_loss" and realized_pnl > 0)
            or (guessed_reason == "take_profit" and realized_pnl < 0)
        ):
            reason = "unknown"
        else:
            reason = guessed_reason

    closed_entry = _build_closed_entry(symbol, reason, entry_info, exit_price, realized_pnl,
                                        excursion_path=excursion_path, fees=fees)
    append_entry(closed_entry, path=journal_path)
    _save_last_trade_id(symbol, last_trade_id, path=last_trade_path)
    return closed_entry


def record_manual_close(client, symbol: str, journal_path: str = None,
                         last_trade_path: str = None, excursion_path: str = None) -> dict | None:
    """대시보드의 긴급 청산 버튼 등으로 수동 청산한 직후 호출한다. 방금 체결을 조회해서
    reason="manual"로 저널에 남기고, 감시 루프가 같은 체결을 또 손절/익절로 오인해 중복
    기록하지 않도록 마지막 거래ID 상태도 함께 갱신한다.

    check_and_log_closed_trade와 똑같은 since-앵커링/부분체결 합산 버그가 있었다(2026-08-14
    실전 확인 — 진입 체결 자체를 청산으로 오인해 손익 0으로 기록, 그 직후 다음 사이클의
    check_and_log_closed_trade가 진짜 청산을 또 감지해서 같은 포지션이 두 번 기록되는 사고로
    이어짐). 진입 기록이 있으면 _aggregate_closing_trades로 정확히 집계하고, 그게 없거나
    실패할 때만(드문 경우) 마지막 체결 하나를 그대로 쓰는 예전 방식으로 대체한다."""
    journal_path = journal_path or JOURNAL_PATH
    last_trade_path = last_trade_path or LAST_TRADE_STATE_PATH
    entry_info = _find_last_entry_journal(symbol, journal_path=journal_path)

    if entry_info is not None:
        try:
            closing_trades, exit_price, realized_pnl, fees = _aggregate_closing_trades(
                client, symbol, entry_info, last_trade_path=last_trade_path)
        except Exception:
            closing_trades = []
        if closing_trades:
            closed_entry = _build_closed_entry(symbol, "manual", entry_info, exit_price, realized_pnl,
                                                excursion_path=excursion_path, fees=fees)
            append_entry(closed_entry, path=journal_path)
            last_trade_id = closing_trades[-1].get("id")
            if last_trade_id is not None:
                _save_last_trade_id(symbol, last_trade_id, path=last_trade_path)
            return closed_entry

    try:
        trades = client.fetch_my_trades(symbol, limit=5)
    except Exception:
        trades = []
    if not trades:
        return None

    last_trade = trades[-1]
    trade_id = last_trade.get("id")
    exit_price = last_trade.get("price")
    realized_pnl = float((last_trade.get("info") or {}).get("realizedPnl", 0) or 0)

    closed_entry = _build_closed_entry(symbol, "manual", entry_info or {}, exit_price, realized_pnl,
                                        excursion_path=excursion_path)
    append_entry(closed_entry, path=journal_path)
    if trade_id is not None:
        _save_last_trade_id(symbol, trade_id, path=last_trade_path)
    return closed_entry


def reset_consecutive_losses(journal_path: str = None) -> dict:
    """연속손실 카운트를 0으로 되돌린다. 별도 상태 파일을 새로 안 만들고, 저널에
    event="consecutive_loss_reset" 기록 하나만 남긴다 — compute_consecutive_losses가 이
    이벤트를 만나면 그 이전 손실은 더 이상 세지 않는다(src/core/state.py 참고). 실현손익/승률
    같은 다른 통계는 이 저널 기록을 걸러내지 않으므로 영향 없다.

    일일 손실 한도(`state/futures_rule_daily_equity*.json`의 시작 자산)와 달리 연속손실은 애초에
    상태 파일이 없고 저널만으로 매번 다시 계산되는 구조라, "리셋"도 같은 방식(저널에 경계선
    이벤트 기록)으로 자연스럽게 구현된다 — 대시보드/텔레그램의 "연속손실 리셋" 버튼이 이 함수를
    호출한다(2026-08-23, 사용자가 3연패 상태에서 임계치까지 여유가 얼마 안 남은 걸 보고 요청)."""
    journal_path = journal_path or JOURNAL_PATH
    entry = {"event": "consecutive_loss_reset"}
    append_entry(entry, path=journal_path)
    return entry


def initialize(client) -> dict[str, int]:
    """루프 시작 시 한 번만 호출 — 감시하는 모든 종목의 레버리지/마진모드를 설정한다.

    FUTURES_SYMBOLS 중 지금 연결된 거래소 환경(예: 데모 트레이딩)에 아예 없는 심볼은 건너뛴다 —
    SOXL처럼 실거래 선물에는 있지만 데모 트레이딩에는 없는 경우가 실제로 있었음(BadSymbol로
    initialize() 자체가 죽어서 봇이 아예 못 뜨는 문제였음).

    심볼별로 거래소가 허용하는 최대 레버리지가 다르다(예: TSLA는 5배가 한도라 LEVERAGE=10
    설정으로 set_leverage를 호출하면 거래소가 거부한다 — 2026-08-12 실전에서 initialize()
    전체가 죽어 봇이 아예 안 뜨는 사고로 확인됨). `leveraged_position_size`의 리스크 기반
    수량 계산은 레버리지와 무관하고(포지션 상한으로만 쓰임) 이 전략의 손절폭(1.25%)에서는
    5배만 돼도 그 상한에 안 걸리므로, 거부당한 심볼은 건너뛰는 대신 거래소가 허용하는 한도까지
    자동으로 낮춰서 재시도한다 — 대신 그 낮아진 레버리지는 청산가 추정에 정확히 반영해야
    하므로(레버리지가 낮을수록 청산가가 진입가에서 더 멀어짐) 심볼별 실제 적용 레버리지를
    맵으로 돌려준다.

    이번 세션에서 실제로 감시 가능한 {심볼: 적용된 레버리지} 맵을 반환한다."""
    client.load_markets()
    leverage_by_symbol: dict[str, int] = {}
    for symbol in FUTURES_SYMBOLS:
        if symbol not in client.markets:
            logger.warning("symbol %s is not available on this exchange environment — skipping", symbol)
            continue
        effective_leverage = LEVERAGE
        try:
            set_leverage(client, symbol, LEVERAGE)
        except ccxt.BadRequest:
            max_leverage = get_max_leverage(client, symbol)
            if max_leverage <= 0:
                logger.warning("symbol %s rejected %dx leverage and no usable max-leverage tier was found — skipping",
                                symbol, LEVERAGE)
                continue
            logger.warning("symbol %s does not accept %dx leverage on this exchange — using its max (%dx) instead",
                            symbol, LEVERAGE, max_leverage)
            try:
                set_leverage(client, symbol, max_leverage)
            except ccxt.BadRequest as exc:
                logger.warning("symbol %s rejected even its own reported max leverage (%dx) — skipping (%s)",
                                symbol, max_leverage, exc)
                continue
            effective_leverage = max_leverage
        set_margin_mode(client, symbol, MARGIN_MODE)
        leverage_by_symbol[symbol] = effective_leverage
    return leverage_by_symbol


def _track_excursion(symbol: str, position: dict, journal_path: str, excursion_path: str) -> None:
    """보유 중인 포지션의 최고/최저 지점을 R배수로 갱신한다. R의 기준(진입가·손절가)은 거래소가
    아니라 저널의 진입 기록에서 가져온다 — 브라켓 주문이 걸린 가격이 그 기준이고, 백테스트의 R과
    같은 정의라야 나중에 비교가 된다."""
    entry_info = _find_last_entry_journal(symbol, journal_path=journal_path)
    if entry_info is None:
        return
    side = _entry_side(entry_info) or position.get("side")
    mark_price = position.get("markPrice") or position.get("entryPrice")
    if side is None or mark_price is None:
        return
    excursion.update(symbol, entry_price=entry_info.get("entry_price"),
                      stop_loss_price=entry_info.get("stop_loss_price"),
                      mark_price=mark_price, side=side, path=excursion_path)


def _reconcile_symbol(client, symbol: str, position: dict | None, journal_path: str = None,
                       last_trade_path: str = None, excursion_path: str = None) -> None:
    """신규 진입 판단과 별개로, 거래소의 실제 상태(포지션 유무)와 우리 저널/주문을 항상
    동기화한다 — 서킷브레이커 발동 여부와 무관하게 매 사이클 돌아야 한다.

    예전엔 이 로직이 _evaluate_symbol 안에 있어서 run_once가 서킷브레이커로 막힐 때 심볼 평가
    자체를 건너뛰면 이것도 같이 건너뛰어졌다 — 그 결과 서킷브레이커가 걸려있는 동안 실제로 일어난
    청산(손절/익절 체결)을 봇이 전혀 모른 채 방치하는 사고가 있었다(2026-08-22 실전 확인 — 실계좌
    ETH/SOL이 진입 직후 손절됐는데 서킷브레이커가 그 뒤로도 계속 걸려있어서 저널에 "청산"으로
    기록되기까지 30분 가까이 걸림, 그동안 고아 주문 정리도 텔레그램 알림도 전부 지연됨). 이제는
    run_once가 서킷브레이커 통과 여부와 무관하게 매 사이클 모든 심볼에 대해 이 함수를 먼저 호출."""
    journal_path = journal_path or JOURNAL_PATH
    excursion_path = excursion_path or EXCURSION_PATH
    if position is not None:
        check_and_log_untracked_position(client, symbol, position, journal_path=journal_path)
        # 무보호 감지와 최고점 추적은 둘 다 "포지션이 살아있는 동안 매 사이클" 돌아야 의미가 있다.
        check_position_protection(client, symbol, journal_path=journal_path)
        _track_excursion(symbol, position, journal_path, excursion_path)
    else:
        check_and_log_closed_trade(client, symbol, journal_path=journal_path,
                                    last_trade_path=last_trade_path, excursion_path=excursion_path)
        cleanup_stale_orders(client, symbol)


def _evaluate_symbol(client, symbol: str, position: dict | None, margin_equity: float,
                      open_position_count: int, leverage: int = LEVERAGE, env: str = "demo",
                      journal_path: str = None, last_trade_path: str = None) -> dict:
    """종목 하나에 대한 신규 진입 판단. 새 포지션을 열면 result["entered"]=True로 호출자에게
    알려서 같은 사이클 안에서 MAX_CONCURRENT_POSITIONS 카운트를 즉시 반영할 수 있게 한다.
    청산 감지/고아 주문 정리/미기록 포지션 백필은 run_once가 이 함수를 부르기 전에
    _reconcile_symbol로 먼저 처리한다(서킷브레이커 여부와 무관하게 항상 돌아야 해서 분리함,
    2026-08-22).

    leverage: initialize()가 이 심볼에 실제로 적용한 레버리지(거래소 한도 때문에 설정값보다
    낮을 수 있음 — 예: TSLA는 5배). 청산가 추정에 실제 레버리지를 써야 정확하다.

    env="live"면 open_position_with_bracket에 confirm_live=True를 같이 넘긴다 — 이 프로세스가
    애초에 실계좌 client(get_futures_client(env="live"))로 호출되고 있다는 것 자체가 명시적
    의도이므로(2026-08-22), 매 진입마다 다시 물어볼 필요는 없다. env="demo"는 그대로 확인 없이도
    허용된다(_guard_live가 env="live"일 때만 막음)."""
    if position is not None:
        return {"symbol": symbol, "has_position": True, "event": "holding_position",
                "position": position, "entered": False}

    if open_position_count >= MAX_CONCURRENT_POSITIONS:
        return {"symbol": symbol, "has_position": False, "event": "skipped_max_positions", "entered": False}

    market_client = get_futures_market_data_client()
    # limit+1을 받아서 마지막 한 봉(아직 마감 안 된, 진행 중인 캔들)을 버린다 — 바이낸스
    # fetch_ohlcv는 이번 시간봉이 끝나기 전까지도 그 봉을 계속 갱신되는 종가로 포함시켜서 돌려주는데
    # (실전 확인: 마감까지 24분 남은 봉도 이미 종가값이 들어있고 계속 바뀜), POLL_INTERVAL_SECONDS
    # (30초)마다 이 값으로 신호를 계산하면 그 시간 안에 반전될 일시적 스파이크에도 SMA 돌파처럼
    # 반응해버린다. 반면 백테스트(run_backtest)는 항상 이미 마감된 과거 캔들만 순회하므로 이
    # 노이즈를 원천적으로 볼 수 없다 — 실제로 2026-08-22 실계좌 3연패 전부, 그 시간봉이 실제
    # 마감됐을 때의 종가로 다시 계산하면 신호 자체가 안 떴어야 했던 것으로 확인됨(UPDATE_LOG.md
    # 참고). 마지막 봉을 버려서 항상 "확실히 마감된" 캔들만 쓰게 하면 백테스트가 실제로 검증한
    # 조건과 정확히 같아진다 — 대신 신호 확정이 최대 한 시간봉만큼 늦어진다.
    # 신호 계산엔 100봉이면 충분하지만, 레짐 필터를 쓰면 장기 SMA만큼, 상위봉 필터를 쓰면 상위봉
    # 100개만큼 더 필요하다. +1은 아래에서 버릴 "아직 마감 안 된" 마지막 봉 몫.
    limit = closed_bars_needed(RULE_REGIME_SMA_PERIOD, RULE_HTF_HOURS) + 1
    raw_df = fetch_ohlcv_df(market_client, symbol, timeframe=RULE_TIMEFRAME, limit=limit)
    # 진행 중인 봉의 종가 = 사실상 현재가. 아래 괴리 검사에 쓰려고 버리기 전에 챙겨둔다
    # (티커를 따로 조회하지 않아도 되므로 API 호출이 안 늘어난다).
    live_price = float(raw_df["close"].iloc[-1])
    df = raw_df.iloc[:-1]
    raw_signal = detect_signal(df)
    signal = apply_regime_filter(raw_signal, is_above_long_sma(df, RULE_REGIME_SMA_PERIOD))

    result = {"symbol": symbol, "has_position": False, "entered": False}
    # 신호를 만든 마감 봉의 시각. "같은 봉으로 두 번 진입하지 않는다"의 판정 키이자, 차단 통계
    # (filter_stats)가 30초마다 재발생하는 같은 차단을 중복해서 세지 않게 하는 키이기도 해서
    # 모든 차단 경로가 이 값을 결과에 실어 보낸다. fetch_ohlcv_df는 항상 timestamp 열을 주지만,
    # 없으면 인덱스로 대체한다(테스트용 최소 df 등).
    signal_bar_timestamp = str(df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1])

    if signal is None:
        if raw_signal is not None:
            # 레짐 필터가 숏을 버린 경우. no_signal과 뭉뚱그리면 "상승장이라 숏을 안 잡은 것"과
            # "애초에 신호 자체가 없는 것"이 구별되지 않아, 필터가 실제로 몇 번 일했는지 알 방법이
            # 사라진다 — 저널엔 안 남기되(매 사이클 반복) 통계로는 세려고 이벤트를 나눈다.
            result.update({"event": "skipped_regime", "signal": raw_signal,
                            "signal_bar_timestamp": signal_bar_timestamp})
        else:
            result["event"] = "no_signal"
        return result

    # 상위봉(RULE_HTF_HOURS) 흐름이 신호와 반대면 진입하지 않는다 — 규칙은
    # futures_strategy.apply_htf_filter 한 곳이고 백테스트(engine.gated_signals)도 같은 순서로 부른다.
    # 꺼져 있으면(0) latest_htf_direction이 None이라 그대로 통과한다.
    htf_direction = latest_htf_direction(df, RULE_HTF_HOURS)
    if apply_htf_filter(signal, htf_direction) is None:
        result.update({
            "event": "skipped_htf", "signal": signal, "htf_direction": htf_direction,
            "signal_bar_timestamp": signal_bar_timestamp,
            "reason": f"{RULE_HTF_HOURS}시간봉 방향이 신호와 반대({'+DI>-DI' if htf_direction > 0 else '-DI>+DI'})",
        })
        return result

    # 저변동 구간이면 진입하지 않는다 — 손절폭 대비 ATR이 너무 작으면 익절까지 가야 할 거리가
    # ATR 몇 배가 되어 사실상 도달이 어렵고, 대신 시간이 흐르며 손절로 흘러간다
    # (UPDATE_LOG.md 2026-09-09: 이 구간이 일관된 손실 구간이었다).
    atr_ratio = atr_to_stop_ratio(df)
    if not passes_volatility_floor(atr_ratio):
        result.update({
            "event": "skipped_low_volatility", "signal": signal, "atr_to_stop_ratio": atr_ratio,
            "signal_bar_timestamp": signal_bar_timestamp,
            "reason": f"ATR이 손절폭의 {atr_ratio:.2f}배로 하한({MIN_ATR_TO_STOP_RATIO})에 미달",
        })
        return result

    last_entry = _find_last_entry_journal(symbol, journal_path=journal_path or JOURNAL_PATH)
    if last_entry is not None and last_entry.get("signal_bar_timestamp") == signal_bar_timestamp:
        # 백테스트(run_backtest)는 봉 하나당 최대 한 번만 진입하고 청산된 봉 다음 봉부터 다시
        # 신호를 본다. 반면 실거래는 마감 봉이 그 시간봉 내내 고정이라, 포지션이 도중에 청산되면
        # POLL_INTERVAL_SECONDS마다 같은 신호로 계속 재진입한다 — 2026-09-07 실계좌에서 ZEC가
        # 한 시간 안에 같은 값으로 5번 진입해 수수료만 태운 유령 왕복을 만들고 3분 만에 연속손실
        # 5회로 서킷브레이커를 걸었다(UPDATE_LOG.md 참고). 봉 단위로 잠가서 백테스트와 맞춘다.
        result["event"] = "skipped_same_signal_bar"
        result["signal_bar_timestamp"] = signal_bar_timestamp
        return result

    entry_price = float(df["close"].iloc[-1])
    side = "long" if signal == "LONG" else "short"
    stop_loss_price, take_profit_price = compute_bracket_prices(entry_price, side)

    # 손절/익절가가 전부 entry_price(마감 봉 종가) 기준이라, 현재가가 거기서 너무 멀어졌으면
    # 그 브라켓은 이미 무의미하다 — 진입하자마자 손절되거나 브라켓 주문이 -2021
    # "Order would immediately trigger"로 거부된다(둘 다 실계좌에서 확인).
    drift = abs(live_price - entry_price)
    max_drift = MAX_ENTRY_PRICE_DRIFT_R * abs(entry_price - stop_loss_price)
    if max_drift > 0 and drift > max_drift:
        result.update({
            "event": "skipped_price_drift", "signal": signal, "entry_price": entry_price,
            "live_price": live_price, "signal_bar_timestamp": signal_bar_timestamp,
            "reason": f"현재가가 신호 봉 종가에서 손절폭의 {drift / max_drift * MAX_ENTRY_PRICE_DRIFT_R:.2f}배 이탈",
        })
        return result

    liquidation_estimate = estimate_liquidation_price(entry_price, leverage, side)
    safety = check_stop_before_liquidation(entry_price, stop_loss_price, side, liquidation_estimate)

    result.update({
        "signal": signal, "entry_price": entry_price, "signal_bar_timestamp": signal_bar_timestamp,
        "atr_to_stop_ratio": atr_ratio,
        "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
    })

    if not safety.allowed:
        result["event"] = "rejected_unsafe_stop"
        result["reason"] = safety.reason
        return result

    try:
        max_notional = get_notional_cap(client, symbol, leverage)
    except Exception:
        # 조회 실패는 흔치 않은 일시적 오류일 뿐 이 상한이 없다고 확정할 근거가 아니다 — 다만
        # 이 사이클에서 진입 자체를 막느니, 상한 없이(기존 동작대로) 진행하고 거래소가 최종
        # 검증하게 둔다. -2027로 거부되면 위쪽 run_once의 심볼별 예외 격리가 잡아준다.
        logger.exception("symbol %s failed to fetch notional cap — proceeding without it", symbol)
        max_notional = None

    quantity = leveraged_position_size(margin_equity, entry_price, stop_loss_price, leverage,
                                        FUTURES_RISK_PER_TRADE, max_notional)
    if quantity <= 0:
        result["event"] = "rejected_zero_quantity"
        return result

    result["execution"] = open_position_with_bracket(
        client, symbol, side, quantity, stop_loss_price, take_profit_price,
        env=env, confirm_live=(env == "live"), take_profit_order_type=TAKE_PROFIT_ORDER_TYPE,
    )
    result["event"] = "entered"
    result["entered"] = True
    return result


def run_once(client, env: str = "demo", consecutive_losses: int = None, daily_pnl_pct: float = None,
             symbols: list[str] = None, leverage_by_symbol: dict[str, int] = None,
             journal_path: str = None, state_path: str = None, last_trade_path: str = None,
             filter_stats_path: str = None, excursion_path: str = None) -> dict:
    """감시 루프 한 사이클. symbols(기본값 FUTURES_SYMBOLS 전체)를 순회하며 판단하되, 동시 보유
    포지션이 MAX_CONCURRENT_POSITIONS에 도달하면 나머지 종목은 신규 진입을 건너뛴다
    (skipped_max_positions). symbols는 보통 initialize()가 돌려준 {심볼: 적용레버리지} 맵의
    키 목록을 그대로 넘겨받는다.

    leverage_by_symbol: initialize()가 돌려준 {심볼: 적용레버리지} 맵을 그대로 넘기면 심볼별로
    실제 설정된 레버리지를 청산가 추정/수량 계산에 쓴다. 안 넘기거나 맵에 없는 심볼은 config의
    기본 LEVERAGE를 쓴다.

    consecutive_losses를 호출자가 안 넘기면(None) 저널에서 직접 계산한다 — 예전엔 호출자
    (run_futures_bot.py)가 이 값을 아예 안 넘겨서 항상 0으로 고정되고, 서킷브레이커의 "연속
    3연패 시 중단" 조건이 실전에서 계속 죽어있던 사고가 있었다(2026-08-12, UPDATE_LOG.md 참고).
    기본값을 0이 아니라 None으로 두고 여기서 자동 계산하게 해서, 앞으로 호출자가 깜빡 잊고
    안 넘겨도 같은 사고가 재발하지 않게 했다.

    env="live"면 실계좌 client로 호출되고 있다는 뜻이며(2026-08-22, 데모/실계좌 동시 운영),
    자동 진입 시 confirm_live=True가 같이 전달되고(_evaluate_symbol 참고) journal_path/state_path/
    last_trade_path도 안 넘기면 이 env에 맞는 LIVE_* 상수로 자동 해석된다 — 데모 저널/상태
    파일은 절대 안 건드린다."""
    symbols = FUTURES_SYMBOLS if symbols is None else symbols
    leverage_by_symbol = leverage_by_symbol or {}
    if journal_path is None:
        journal_path = LIVE_JOURNAL_PATH if env == "live" else JOURNAL_PATH
    if state_path is None:
        state_path = LIVE_STATE_PATH if env == "live" else STATE_PATH
    if last_trade_path is None:
        last_trade_path = LIVE_LAST_TRADE_STATE_PATH if env == "live" else LAST_TRADE_STATE_PATH
    if filter_stats_path is None:
        filter_stats_path = LIVE_FILTER_STATS_PATH if env == "live" else FILTER_STATS_PATH
    if excursion_path is None:
        excursion_path = LIVE_EXCURSION_PATH if env == "live" else EXCURSION_PATH

    balance = get_futures_balance(client)
    margin_equity = (balance.get("USDT") or {}).get("total") or 0.0

    if daily_pnl_pct is None:
        daily_pnl_pct = get_daily_pnl_pct(margin_equity, path=state_path)
    if consecutive_losses is None:
        consecutive_losses = compute_consecutive_losses(read_entries(path=journal_path))

    cycle = {"margin_equity": margin_equity, "daily_pnl_pct": daily_pnl_pct, "symbols": {}}

    # 한 번의 요청으로 전 종목 — 종목마다 부르면 사이클당 12번이고, 같은 IP를 쓰는 대시보드와
    # 합쳐 바이낸스 요청 한도를 넘겨 IP가 차단된 적이 있다(2026-09-28, 418 -1003).
    positions = get_positions(client, list(symbols))
    open_count = sum(1 for p in positions.values() if p is not None)

    # 청산 감지/고아 주문 정리/미기록 포지션 백필은 서킷브레이커 통과 여부와 무관하게 항상
    # 수행한다 — 아래 신규 진입 평가 루프와 달리 "지금 거래소 상태가 실제로 어떤지"를 우리 저널과
    # 맞추는 작업이라, 진입을 막는 것과는 별개로 매 사이클 돌아야 한다(2026-08-22 실전 확인 —
    # 서킷브레이커가 걸린 동안 실제 손절 체결이 30분 가까이 저널에 반영 안 됐던 사고, 상세 사유는
    # _reconcile_symbol 참고).
    for symbol in symbols:
        try:
            _reconcile_symbol(client, symbol, positions[symbol], journal_path=journal_path,
                               last_trade_path=last_trade_path, excursion_path=excursion_path)
        except Exception:
            logger.exception("symbol %s failed to reconcile against exchange state — continuing", symbol)

    circuit_breaker = check_circuit_breaker(daily_pnl_pct, consecutive_losses)
    if not circuit_breaker.allowed:
        # 서킷브레이커가 걸린 동안은(위 재조정은 끝냈지만) 신규 진입 평가 없이 매 사이클 바로
        # 여기서 return하므로, 직전 기록과 무조건 비교만으로 충분하다(그 사이 다른 이벤트가 끼어들
        # 수 없음). reason은 손실률(daily_pnl_pct)이 미실현손익 변동으로 사이클마다 값이 미세하게
        # 달라져 문자열까지 비교하면 사실상 항상 "다른 사유"로 오인되므로 event만 비교한다 — 안
        # 그러면 서킷브레이커가 걸려있는 내내 30초(POLL_INTERVAL_SECONDS)마다 저널에 새로 쌓이고,
        # 텔레그램 봇이 그걸 전부 "새 이벤트"로 보고 알림을 계속 쏘는 사고로 이어진다(2026-08-22
        # 실전 확인 — 실계좌가 일일 손실 한도 초과로 30초마다 반복 알림).
        existing_entries = read_entries(path=journal_path)
        if not existing_entries or existing_entries[-1].get("event") != "circuit_breaker_blocked":
            append_entry({"event": "circuit_breaker_blocked", "reason": circuit_breaker.reason}, path=journal_path)
        cycle["event"] = "circuit_breaker_blocked"
        cycle["reason"] = circuit_breaker.reason
        return cycle

    for symbol in symbols:
        leverage = leverage_by_symbol.get(symbol, LEVERAGE)
        try:
            result = _evaluate_symbol(client, symbol, positions[symbol], margin_equity, open_count, leverage,
                                       env=env, journal_path=journal_path, last_trade_path=last_trade_path)
        except Exception as exc:
            # 한 심볼의 거래소 쪽 오류(예: TSLA/CRCL 같은 토큰화 주식형 심볼이 계정에서 아직
            # TradFi-Perps 약관에 동의가 안 돼 주문이 거부되는 경우, 2026-08-14 실전 확인)로
            # 사이클 전체가 죽어서 이후 순서의 다른 심볼들(예: 목록 맨 끝의 BNB)이 그 사이클에서
            # 아예 평가조차 안 되는 사고가 있었다. 심볼 하나의 실패가 나머지를 막지 않도록 격리한다.
            logger.exception("symbol %s raised an error during evaluation — skipping this cycle", symbol)
            result = {"symbol": symbol, "has_position": False, "entered": False,
                      "event": "rejected_exchange_error", "reason": str(exc)}
        if result["entered"]:
            open_count += 1
        should_log = result["event"] not in _SILENT_EVENTS
        if should_log and result["event"] == "rejected_exchange_error":
            # TSLA/CRCL처럼 계정 설정 문제(약관 미동의 등)로 매 사이클 계속 거부되는 심볼은
            # 고칠 때까지 똑같은 오류가 몇 시간이고 반복된다 — 매번 저널에 남기면(2026-08-14
            # 실전에서 TSLA 하나로 60줄 넘게 쌓인 적 있음) 대시보드 "최근 거래 내역"의 고정
            # 표시 개수(last 30) 안에서 실제 체결 기록이 밀려나 안 보이는 사고로 이어진다
            # (2026-08-20 실전 확인 — 손절 4건 중 2건이 이 노이즈에 밀려 화면에 안 보였음).
            # 같은 심볼에서 바로 직전 기록과 이벤트+사유가 완전히 같으면(=상태 변화 없음) 또
            # 남기지 않는다 — 최초 발생/사유가 바뀐 경우는 여전히 남긴다.
            last = _find_last_symbol_entry(symbol, journal_path=journal_path)
            if (last is not None and last.get("event") == "rejected_exchange_error"
                    and last.get("reason") == result.get("reason")):
                should_log = False
        if should_log:
            append_entry({k: v for k, v in result.items() if k != "position"}, path=journal_path)
        else:
            # 저널에 안 남는 차단 사유는 카운터에만 누적한다 — 신호 봉 단위로 중복 제거되므로
            # 30초마다 같은 차단이 반복돼도 하루 집계는 "차단된 신호 개수"로 남는다.
            filter_stats.record(result["event"], symbol, result.get("signal_bar_timestamp"),
                                 path=filter_stats_path)
        cycle["symbols"][symbol] = result

    cycle["open_position_count"] = open_count
    return cycle
