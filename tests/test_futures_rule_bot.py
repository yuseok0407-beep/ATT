from datetime import datetime
from unittest.mock import MagicMock, patch

import ccxt
import numpy as np
import pandas as pd
import pytest

from src import futures_rule_bot as bot
from src.execution import excursion, filter_stats
from src.core.config import MIN_ATR_TO_STOP_RATIO, RULE_REGIME_SMA_PERIOD
from src.core.risk import MAX_CONSECUTIVE_LOSSES

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]


# 고가/저가 폭은 ATR이 손절폭 하한(MIN_ATR_TO_STOP_RATIO x STOP_LOSS_PCT)을 넘도록 잡는다 —
# 너무 좁으면 저변동 필터에 막혀 모든 테스트가 "진입 안 함"이 되어버린다.
_HL = 0.6

def _flat_df(n=60):
    closes = np.full(n, 100.0)
    return pd.DataFrame({"high": closes + _HL, "low": closes - _HL, "close": closes})


@pytest.fixture(autouse=True)
def _patch_symbols(monkeypatch):
    monkeypatch.setattr(bot, "FUTURES_SYMBOLS", SYMBOLS)
    monkeypatch.setattr(bot, "MAX_CONCURRENT_POSITIONS", 2)


def _base_patches(balance_total=10_000, positions=None):
    positions = positions or {}
    return [
        patch("src.futures_rule_bot.get_futures_balance", return_value={"USDT": {"total": balance_total}}),
        patch("src.futures_rule_bot.get_positions",
              side_effect=lambda client, symbols: {s: positions.get(s) for s in symbols}),
        patch("src.futures_rule_bot.cleanup_stale_orders"),
        patch("src.futures_rule_bot.check_and_log_closed_trade"),
        patch("src.futures_rule_bot.check_and_log_untracked_position"),
        patch("src.futures_rule_bot.append_entry"),
        # rejected_exchange_error 중복 억제 로직(run_once)이 심볼별 마지막 저널 기록을 조회하느라
        # read_entries를 호출한다 — 기본값을 빈 목록으로 둬서 테스트가 실제 저널 파일을 읽지 않게
        # 격리한다(각 테스트는 필요하면 이 patch를 안쪽 with로 덮어써서 원하는 이력을 준다).
        patch("src.futures_rule_bot.read_entries", return_value=[]),
        patch("src.futures_rule_bot.get_futures_market_data_client"),
        patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_flat_df()),
    ]


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in patches:
        p.stop()


def test_run_once_blocked_by_circuit_breaker():
    patches = _base_patches()
    _start(patches)
    try:
        cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)
        assert cycle["event"] == "circuit_breaker_blocked"
        assert "symbols" not in cycle or cycle["symbols"] == {}
        # 하트비트가 이 사유를 실어 텔레그램 정지 지속 알림이 쓴다
        assert "일일 손실 한도" in cycle["reason"]
    finally:
        _stop(patches)


def test_run_once_does_not_relog_circuit_breaker_when_already_last_entry():
    """실전 버그 재현(2026-08-22): 서킷브레이커가 걸린 동안 run_once가 매 사이클(30초마다)
    circuit_breaker_blocked를 저널에 계속 새로 남겨서, 텔레그램 봇이 그걸 전부 "새 이벤트"로
    보고 알림을 반복 전송했다. 직전 저널 기록이 이미 circuit_breaker_blocked면 다시 남기면 안
    된다."""
    patches = _base_patches()
    _start(patches)
    try:
        history = [{"event": "circuit_breaker_blocked", "reason": "일일 손실 한도 초과 (-6.4% <= -5%)"}]
        with patch("src.futures_rule_bot.read_entries", return_value=history), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.066)

        assert cycle["event"] == "circuit_breaker_blocked"
        mock_append.assert_not_called()
    finally:
        _stop(patches)


def test_run_once_still_reconciles_closed_trades_and_stale_orders_while_circuit_breaker_blocked():
    """실전 버그 재현(2026-08-22): 서킷브레이커가 걸린 동안 심볼 평가 자체를 건너뛰다 보니
    청산 감지(check_and_log_closed_trade)/고아 주문 정리(cleanup_stale_orders)/미기록 포지션
    백필(check_and_log_untracked_position)도 같이 건너뛰어져서, 실계좌에서 실제로 일어난 손절
    체결이 저널에 30분 가까이 반영이 안 된 사고가 있었다. 이제는 서킷브레이커 여부와 무관하게
    매 사이클 전 심볼에 대해 돌아야 한다."""
    positions = {"BTC/USDT:USDT": {"side": "long", "contracts": 0.01}}  # 나머지는 flat
    patches = _base_patches(positions=positions)
    _start(patches)
    try:
        cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)
        assert cycle["event"] == "circuit_breaker_blocked"

        # flat인 3개 심볼(ETH/SOL/XRP)은 청산 감지 + 고아 주문 정리가 호출돼야 한다
        flat_symbols = {c.args[1] for c in bot.check_and_log_closed_trade.call_args_list}
        assert flat_symbols == {"ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"}
        assert bot.cleanup_stale_orders.call_count == 3

        # 포지션이 있는 BTC는 미기록 포지션 백필 확인이 호출돼야 한다
        bot.check_and_log_untracked_position.assert_called_once()
        assert bot.check_and_log_untracked_position.call_args.args[1] == "BTC/USDT:USDT"
    finally:
        _stop(patches)


def test_run_once_isolates_one_symbols_reconcile_error_while_circuit_breaker_blocked():
    """재조정 중 한 심볼에서 예외가 나도(예: 거래소 일시적 오류) 나머지 심볼의 재조정은 계속
    돼야 하고, 서킷브레이커 판단 자체도 죽으면 안 된다."""
    patches = _base_patches()
    _start(patches)
    try:
        def _raise_for_eth(client, symbol, journal_path=None, last_trade_path=None,
                            excursion_path=None):
            if symbol == "ETH/USDT:USDT":
                raise Exception("temporary exchange error")

        with patch("src.futures_rule_bot.check_and_log_closed_trade", side_effect=_raise_for_eth):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)

        assert cycle["event"] == "circuit_breaker_blocked"
        assert bot.cleanup_stale_orders.call_count == 3  # ETH만 재조정 실패, 나머지 3개는 정상 진행
    finally:
        _stop(patches)


def test_run_once_logs_circuit_breaker_on_first_occurrence_and_after_it_clears():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.read_entries", return_value=[]), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)
        mock_append.assert_called_once()
        assert mock_append.call_args.args[0]["event"] == "circuit_breaker_blocked"

        # 서킷브레이커가 풀렸다가(직전 기록이 다른 이벤트) 다시 걸리면 새로 남겨야 한다
        history = [{"event": "closed", "realized_pnl": 5.0}]
        with patch("src.futures_rule_bot.read_entries", return_value=history), \
             patch("src.futures_rule_bot.append_entry") as mock_append2:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)
        mock_append2.assert_called_once()
    finally:
        _stop(patches)


def test_run_once_computes_consecutive_losses_from_journal_when_not_passed():
    """실전 버그 재현: run_futures_bot.py가 consecutive_losses를 안 넘겨서 서킷브레이커가
    죽어있던 걸 고침 — 이제 안 넘기면(None) 저널에서 직접 계산해야 한다."""
    patches = _base_patches()
    _start(patches)
    try:
        losing_streak = [{"event": "closed", "realized_pnl": -float(i + 1)}
                          for i in range(MAX_CONSECUTIVE_LOSSES)]
        with patch("src.futures_rule_bot.read_entries", return_value=losing_streak):
            cycle = bot.run_once(MagicMock(), daily_pnl_pct=0.0)
        assert cycle["event"] == "circuit_breaker_blocked"
    finally:
        _stop(patches)


def test_run_once_does_not_block_when_journal_losing_streak_is_below_threshold():
    patches = _base_patches()
    _start(patches)
    try:
        losing_streak = [{"event": "closed", "realized_pnl": -float(i + 1)}
                          for i in range(MAX_CONSECUTIVE_LOSSES - 1)]
        with patch("src.futures_rule_bot.read_entries", return_value=losing_streak):
            cycle = bot.run_once(MagicMock(), daily_pnl_pct=0.0)
        assert cycle.get("event") != "circuit_breaker_blocked"
    finally:
        _stop(patches)


def test_run_once_checks_every_symbol_independently():
    patches = _base_patches()
    _start(patches)
    try:
        cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert set(cycle["symbols"].keys()) == set(SYMBOLS)
        for symbol in SYMBOLS:
            assert cycle["symbols"][symbol]["event"] == "no_signal"
        assert cycle["open_position_count"] == 0
    finally:
        _stop(patches)


def test_run_once_reports_holding_position_without_touching_it():
    positions = {"BTC/USDT:USDT": {"side": "long", "contracts": 0.01}}
    patches = _base_patches(positions=positions)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value=None):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "holding_position"
        assert cycle["symbols"]["ETH/USDT:USDT"]["event"] == "no_signal"
        assert cycle["open_position_count"] == 1
    finally:
        _stop(patches)


def test_evaluate_symbol_drops_the_still_forming_last_candle_before_signal_detection():
    """실전 버그 재현(2026-08-22): fetch_ohlcv_df가 돌려주는 마지막 봉은 바이낸스가 아직 마감
    안 된(진행 중인) 캔들을 실시간 종가로 계속 갱신해서 주는 것이라, 이걸 그대로 신호 계산에
    쓰면 그 시간 안에 반전될 일시적 스파이크에도 반응해버린다(실계좌 3연패 원인으로 실제 확인,
    UPDATE_LOG.md 참고). fetch는 limit+1로 받아서 마지막 한 봉을 버리고 확실히 마감된 캔들만
    신호 계산에 써야 한다."""
    raw_df = _flat_df(n=61)
    raw_df.loc[raw_df.index[-1], "close"] = 99999.0  # 마지막(진행중) 봉만 티나게 다른 값

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=raw_df) as mock_fetch, \
             patch("src.futures_rule_bot.detect_signal", return_value=None) as mock_detect:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        # 버릴 여유분(+1)을 항상 확보해야 한다. 레짐 필터가 켜져 있으면 장기 SMA 몫까지 더 받는다
        # (하드코딩 대신 config에서 계산 — 필터 기간을 바꿔도 이 테스트가 같이 따라간다).
        expected_limit = max(101, RULE_REGIME_SMA_PERIOD + 2) if RULE_REGIME_SMA_PERIOD > 0 else 101
        assert mock_fetch.call_args.kwargs.get("limit") == expected_limit

        # detect_signal에 넘어간 데이터의 마지막 행은 원본의 "진행중" 스파이크 행이면 안 된다
        passed_df = mock_detect.call_args.args[0]
        assert passed_df["close"].iloc[-1] != 99999.0
        assert len(passed_df) == len(raw_df) - 1
    finally:
        _stop(patches)


def test_run_once_isolates_one_symbols_exchange_error_from_the_rest():
    """실전 버그 재현(2026-08-14): TSLA/CRCL 같은 토큰화 주식형 심볼이 계정에서 TradFi-Perps
    약관 미동의로 주문이 거부되면 예외가 사이클 전체를 죽여서, 그 뒤 순서의 다른 심볼(예: 목록
    맨 끝의 BNB)이 그 사이클에서 아예 평가조차 안 되고 10분 넘게 반복됐다. 한 심볼의 실패가
    나머지 심볼 평가를 막지 않아야 한다."""
    patches = _base_patches()
    _start(patches)
    try:
        def _fail_for_btc(client, symbol, side, quantity, stop_loss_price, take_profit_price, **kwargs):
            if symbol == "BTC/USDT:USDT":
                raise Exception('binance {"code":-4411,"msg":"Please sign TradFi-Perps agreement contract fapi."}')
            return {"status": "opened"}

        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", side_effect=_fail_for_btc):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "rejected_exchange_error"
        assert "TradFi-Perps" in cycle["symbols"]["BTC/USDT:USDT"]["reason"]
        # BTC가 실패해도 나머지 심볼은 계속 평가되어야 한다(그 중 2개는 MAX_CONCURRENT_POSITIONS=2
        # 한도까지 정상 진입).
        assert cycle["symbols"]["ETH/USDT:USDT"]["event"] == "entered"
        assert cycle["symbols"]["SOL/USDT:USDT"]["event"] == "entered"
        assert cycle["symbols"]["XRP/USDT:USDT"]["event"] == "skipped_max_positions"
    finally:
        _stop(patches)


def test_run_once_enters_on_signal_and_counts_toward_cap():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", return_value={"status": "opened"}) as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        entered = [s for s, r in cycle["symbols"].items() if r["event"] == "entered"]
        skipped = [s for s, r in cycle["symbols"].items() if r["event"] == "skipped_max_positions"]

        # MAX_CONCURRENT_POSITIONS=2 이므로 4종목 다 신호가 나도 2개만 진입하고 나머지는 스킵되어야 한다
        assert len(entered) == 2
        assert len(skipped) == 2
        assert mock_open.call_count == 2
        assert cycle["open_position_count"] == 2
    finally:
        _stop(patches)


def test_run_once_env_live_passes_confirm_live_to_entries():
    """실계좌(env="live")에서 자동 진입이 실제로 주문을 낼 수 있으려면 open_position_with_bracket에
    confirm_live=True가 같이 전달돼야 한다(안 그러면 _guard_live가 막음, 2026-08-22)."""
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", return_value={"status": "opened"}) as mock_open:
            bot.run_once(MagicMock(), env="live", consecutive_losses=0, daily_pnl_pct=0.0)

        assert mock_open.call_count == 2
        for c in mock_open.call_args_list:
            assert c.kwargs["env"] == "live"
            assert c.kwargs["confirm_live"] is True
    finally:
        _stop(patches)


def test_run_once_env_demo_does_not_confirm_live():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", return_value={"status": "opened"}) as mock_open:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        for c in mock_open.call_args_list:
            assert c.kwargs["env"] == "demo"
            assert c.kwargs["confirm_live"] is False
    finally:
        _stop(patches)


def test_run_once_uses_live_path_constants_when_env_live_and_paths_not_given():
    """journal_path/state_path/last_trade_path를 명시하지 않고 env="live"만 넘기면 LIVE_* 상수로
    자동 해석돼야 한다 — 데모 파일과 완전히 분리(2026-08-22)."""
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.get_daily_pnl_pct", return_value=0.0) as mock_pnl, \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", return_value={"status": "opened"}), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            bot.run_once(MagicMock(), env="live", consecutive_losses=0)

        mock_pnl.assert_called_once_with(10_000, path=bot.LIVE_STATE_PATH)
        # "entered" 이벤트 2건(MAX_CONCURRENT_POSITIONS=2)이 전부 라이브 저널 경로로 기록됐는지 확인
        entered_calls = [c for c in mock_append.call_args_list if c.args[0].get("event") == "entered"]
        assert len(entered_calls) == 2
        for c in entered_calls:
            assert c.kwargs["path"] == bot.LIVE_JOURNAL_PATH
    finally:
        _stop(patches)


def test_run_once_respects_existing_positions_when_capping():
    # 이미 2개(cap) 보유 중이면 나머지 종목은 신호가 나도 전부 스킵돼야 한다
    positions = {
        "BTC/USDT:USDT": {"side": "long", "contracts": 0.01},
        "ETH/USDT:USDT": {"side": "short", "contracts": 0.1},
    }
    patches = _base_patches(positions=positions)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["SOL/USDT:USDT"]["event"] == "skipped_max_positions"
        assert cycle["symbols"]["XRP/USDT:USDT"]["event"] == "skipped_max_positions"
        mock_open.assert_not_called()
        assert cycle["open_position_count"] == 2
    finally:
        _stop(patches)


def test_run_once_suppresses_a_repeated_identical_rejected_exchange_error():
    """실전 재현(2026-08-14, TSLA -2027이 몇 시간이고 그대로 반복): 계정 설정 문제로 매 사이클
    똑같은 거래소 거부가 반복되면 예전엔 매번 저널에 남아 60줄 넘게 쌓였고, 이 노이즈가 대시보드
    "최근 거래 내역"(고정 30개)에서 진짜 체결 기록을 밀어내는 사고로 이어졌다(2026-08-20 실전
    확인). 직전 기록과 이벤트+사유가 완전히 같으면 다시 남기지 않아야 한다."""
    same_error = 'binance {"code":-2027,"msg":"Exceeded the maximum allowable position at current leverage."}'
    history = [{"symbol": "BTC/USDT:USDT", "event": "rejected_exchange_error", "reason": same_error}]

    def _fail(client, symbol, side, quantity, stop_loss_price, take_profit_price, **kwargs):
        raise Exception(same_error)

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", side_effect=_fail), \
             patch("src.futures_rule_bot.read_entries", return_value=history), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "rejected_exchange_error"
        btc_appends = [c for c in mock_append.call_args_list if c.args[0].get("symbol") == "BTC/USDT:USDT"]
        assert btc_appends == []
    finally:
        _stop(patches)


def test_run_once_logs_a_new_rejected_exchange_error_when_it_actually_changes():
    """반복 억제가 오류를 영영 숨겨버리면 안 된다 — 사유가 바뀌거나(예: 다른 오류 코드) 처음
    발생한 경우는 정상적으로 저널에 남아야 한다."""
    history = [{"symbol": "BTC/USDT:USDT", "event": "rejected_exchange_error", "reason": "이전과 다른 옛날 오류"}]
    new_error = 'binance {"code":-2027,"msg":"Exceeded the maximum allowable position at current leverage."}'

    def _fail(client, symbol, side, quantity, stop_loss_price, take_profit_price, **kwargs):
        raise Exception(new_error)

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", side_effect=_fail), \
             patch("src.futures_rule_bot.read_entries", return_value=history), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        btc_appends = [c for c in mock_append.call_args_list if c.args[0].get("symbol") == "BTC/USDT:USDT"]
        assert len(btc_appends) == 1
        assert btc_appends[0].args[0]["reason"] == new_error
    finally:
        _stop(patches)


def test_run_once_caps_quantity_by_the_symbols_notional_cap():
    """실전 재현(2026-08-18): TSLA는 5배에서 명목가치 상한이 $5000인데, 리스크 기반 수량
    계산이 이를 몰라서 상한을 넘는 주문을 시도해 거래소가 -2027로 거부했다. run_once가 이
    상한을 실제로 open_position_with_bracket에 넘길 수량에 반영하는지 확인한다."""
    patches = _base_patches(balance_total=10_000)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.get_notional_cap", return_value=500.0), \
             patch("src.futures_rule_bot.open_position_with_bracket",
                   return_value={"status": "opened"}) as mock_open:
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0, symbols=["BTC/USDT:USDT"])

        assert mock_open.call_count == 1
        _, _, _, quantity, _, _ = mock_open.call_args.args
        entry_price = 100.0  # _flat_df()의 종가
        assert quantity * entry_price == pytest.approx(500.0)
    finally:
        _stop(patches)


def test_run_once_proceeds_without_cap_when_notional_lookup_fails():
    """명목가치 상한 조회 자체가 실패해도(일시적 거래소 오류 등) 진입을 막지 않아야 한다 —
    상한 없이 기존 로직대로 진행하고, 정말 초과하면 거래소가 최종 거부하며 그건 심볼별
    예외 격리(rejected_exchange_error)가 잡아준다."""
    patches = _base_patches(balance_total=10_000)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.get_notional_cap", side_effect=Exception("timeout")), \
             patch("src.futures_rule_bot.open_position_with_bracket",
                   return_value={"status": "opened"}) as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                                  symbols=["BTC/USDT:USDT"])

        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "entered"
        mock_open.assert_called_once()
    finally:
        _stop(patches)


def test_run_once_rejects_unsafe_stop_for_one_symbol():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.compute_bracket_prices", return_value=(1.0, 200.0)), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        for symbol in SYMBOLS:
            assert cycle["symbols"][symbol]["event"] == "rejected_unsafe_stop"
        mock_open.assert_not_called()
    finally:
        _stop(patches)


def test_run_once_uses_explicit_symbols_list_over_config_default():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value=None):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                                  symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"])
        assert set(cycle["symbols"].keys()) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    finally:
        _stop(patches)


class TestInitialize:
    """실전에서 발견된 문제: SOXL이 실거래 선물엔 있지만 바이낸스 데모 트레이딩 환경엔 없어서
    initialize()가 BadSymbol로 죽어 봇 자체가 못 뜨던 것, 그리고 TSLA처럼 거래소 레버리지 한도가
    설정값(10배)보다 낮은 심볼에서 set_leverage가 거부돼 똑같이 죽던 것을 고친 부분. 이제
    {심볼: 실제 적용된 레버리지} 맵을 반환한다."""

    def test_skips_symbol_not_in_client_markets_and_returns_leverage_map(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS if s != "SOL/USDT:USDT"}  # SOL만 이 거래소에 없다고 가정

        with patch("src.futures_rule_bot.set_margin_mode") as mock_margin, \
             patch("src.futures_rule_bot.set_leverage") as mock_leverage:
            result = bot.initialize(client)

        assert result == {"BTC/USDT:USDT": 10, "ETH/USDT:USDT": 10, "XRP/USDT:USDT": 10}
        client.load_markets.assert_called_once()
        assert mock_margin.call_count == 3
        assert mock_leverage.call_count == 3

    def test_all_symbols_available_returns_full_map(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS}

        with patch("src.futures_rule_bot.set_margin_mode"), patch("src.futures_rule_bot.set_leverage"):
            result = bot.initialize(client)

        assert result == {s: 10 for s in SYMBOLS}

    def test_retries_with_exchange_max_leverage_when_configured_leverage_is_rejected(self):
        """실전 버그 재현: TSLA는 거래소 레버리지 한도가 5배라 LEVERAGE=10으로 set_leverage를
        호출하면 거래소가 거부한다. 리스크 기반 수량 계산은 레버리지와 무관하므로(포지션 상한
        으로만 쓰임) 건너뛰는 대신 거래소가 허용하는 한도로 자동 재시도해야 한다."""
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS}

        def _reject_configured_leverage(client, symbol, leverage):
            if symbol == "ETH/USDT:USDT" and leverage == 10:
                raise ccxt.BadRequest('binance {"code":-4028,"msg":"Leverage 10 is not valid"}')

        with patch("src.futures_rule_bot.set_margin_mode") as mock_margin, \
             patch("src.futures_rule_bot.set_leverage", side_effect=_reject_configured_leverage), \
             patch("src.futures_rule_bot.get_max_leverage", return_value=5) as mock_max_leverage:
            result = bot.initialize(client)

        assert result == {"BTC/USDT:USDT": 10, "ETH/USDT:USDT": 5, "SOL/USDT:USDT": 10, "XRP/USDT:USDT": 10}
        mock_max_leverage.assert_called_once_with(client, "ETH/USDT:USDT")
        assert mock_margin.call_count == 4  # 재시도로 성공한 종목도 정상적으로 마진모드까지 설정됨

    def test_skips_symbol_when_it_rejects_even_its_own_reported_max_leverage(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS}

        def _always_reject(client, symbol, leverage):
            if symbol == "ETH/USDT:USDT":
                raise ccxt.BadRequest('binance {"code":-4028,"msg":"Leverage is not valid"}')

        with patch("src.futures_rule_bot.set_margin_mode") as mock_margin, \
             patch("src.futures_rule_bot.set_leverage", side_effect=_always_reject), \
             patch("src.futures_rule_bot.get_max_leverage", return_value=5):
            result = bot.initialize(client)

        assert "ETH/USDT:USDT" not in result
        assert set(result) == {"BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"}
        assert mock_margin.call_count == 3

    def test_skips_symbol_when_no_max_leverage_tier_is_found(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS}

        def _reject_configured_leverage(client, symbol, leverage):
            if symbol == "ETH/USDT:USDT":
                raise ccxt.BadRequest('binance {"code":-4028,"msg":"Leverage 10 is not valid"}')

        with patch("src.futures_rule_bot.set_margin_mode"), \
             patch("src.futures_rule_bot.set_leverage", side_effect=_reject_configured_leverage), \
             patch("src.futures_rule_bot.get_max_leverage", return_value=0):
            result = bot.initialize(client)

        assert "ETH/USDT:USDT" not in result


class TestCheckAndLogClosedTrade:
    """실전에서 발견된 문제(청산 이벤트가 저널에 전혀 안 남던 것)를 고친 부분."""

    SYMBOL = "ETH/USDT:USDT"

    def _paths(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        state_path = str(tmp_path / "last_trade.json")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", state_path)
        return journal_path, state_path

    def test_no_trades_does_nothing(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = []
        assert bot.check_and_log_closed_trade(client, self.SYMBOL) is None

    def test_already_seen_trade_id_does_nothing(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot._save_last_trade_id(self.SYMBOL, "trade-1", path=state_path)
        client = MagicMock()
        client.fetch_my_trades.return_value = [{"id": "trade-1", "price": 100.0, "info": {}}]
        assert bot.check_and_log_closed_trade(client, self.SYMBOL) is None

    def test_no_matching_entry_journal_just_remembers_trade_id(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = [{"id": "trade-2", "price": 100.0, "info": {}}]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result is None
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "trade-2"
        assert bot.read_entries(path=journal_path) == []

    def test_detects_stop_loss_exit_and_logs_realized_pnl(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1917.99,
            "stop_loss_price": 1893.72, "take_profit_price": 1965.63,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-entry", "price": 1917.99, "info": {"realizedPnl": "0"}},
            {"id": "trade-exit", "price": 1893.72, "info": {"realizedPnl": "-101.06028000"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["event"] == "closed"
        assert result["reason"] == "stop_loss"
        assert result["realized_pnl"] == pytest.approx(-101.06028)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "trade-exit"

    def test_detects_take_profit_exit(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-exit-tp", "price": 1947.5, "info": {"realizedPnl": "197.60"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["reason"] == "take_profit"
        assert result["realized_pnl"] == pytest.approx(197.60)

    def test_seeing_entry_fill_itself_does_not_log_a_close(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-entry-only", "price": 1900.0, "info": {"realizedPnl": "0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)
        assert result is None
        entries = bot.read_entries(path=journal_path)
        assert all(e.get("event") != "closed" for e in entries)

    def test_fetches_trades_since_entry_timestamp_not_a_bare_limit(self, tmp_path, monkeypatch):
        """실전 버그: since 없이 limit만 주면 거래소가 "최신 N개"가 아니라 "가장 오래된 N개"를
        돌려줘서, 체결이 limit개를 넘어가면 이 함수가 옛날 체결(진입 체결)에 영원히 멈춰버렸다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)
        entry_timestamp = bot.read_entries(path=journal_path)[0]["timestamp"]
        expected_since_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000) - 60_000

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-exit-tp", "price": 1947.5, "amount": 1.0, "info": {"realizedPnl": "47.5"}},
        ]

        bot.check_and_log_closed_trade(client, self.SYMBOL)

        client.fetch_my_trades.assert_called_once_with(self.SYMBOL, since=expected_since_ms, limit=50)

    def test_aggregates_multiple_partial_fill_trades_into_one_closed_entry(self, tmp_path, monkeypatch):
        """실전 버그 재현: TAKE_PROFIT_MARKET 주문 하나가 부분 체결 3번으로 쪼개졌는데,
        마지막 한 건의 realizedPnl만 보면 나머지 두 건의 손익이 통째로 누락됐다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 65023.89,
            "stop_loss_price": 65836.688625, "take_profit_price": 63398.29275,
            "execution": {"entry_order": {"id": "28535168719"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 65061.0, "amount": 0.1228, "info": {"realizedPnl": "0", "orderId": 28535168719}},
            {"id": "t-tp-1", "price": 63467.7, "amount": 0.005, "info": {"realizedPnl": "7.9665", "orderId": 28538022498}},
            {"id": "t-tp-2", "price": 63489.1, "amount": 0.0008, "info": {"realizedPnl": "1.25752", "orderId": 28538022498}},
            {"id": "t-tp-3", "price": 63501.0, "amount": 0.117, "info": {"realizedPnl": "182.52", "orderId": 28538022498}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["reason"] == "take_profit"
        assert result["realized_pnl"] == pytest.approx(7.9665 + 1.25752 + 182.52)
        assert result["exit_price"] == pytest.approx(63499.567, abs=0.01)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "t-tp-3"

    def test_excludes_entry_fill_by_order_id_even_when_price_differs_from_recorded_entry_price(self, tmp_path, monkeypatch):
        """실전 버그: 진입가는 신호가 뜬 캔들 종가로 기록되는데 실제 체결가는 슬리피지로 조금
        다를 수 있다(65023.89 기록 vs 65061.0 실체결). 가격 비교만으로는 진입 체결을 걸러내지
        못했던 걸 주문ID 비교로 고쳤다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 65023.89,
            "stop_loss_price": 65836.688625, "take_profit_price": 63398.29275,
            "execution": {"entry_order": {"id": "28535168719"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 65061.0, "amount": 0.1228, "info": {"realizedPnl": "0", "orderId": 28535168719}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)
        assert result is None

    def test_excludes_a_trade_already_attributed_to_the_previous_position(self, tmp_path, monkeypatch):
        """실전 버그 재현(2026-08-20, SOL): 직전 포지션 청산 후 60초 안에 새 포지션이 재진입되면
        since 버퍼(진입시각-60초)가 "이미 처리되어 저널에 기록까지 끝난 직전 포지션의 청산 체결"을
        다시 끌어와 이번 포지션의 실현손익에 합산해버렸다. 실제로 -16.401 손실이던 포지션이
        직전 포지션의 +63.6659 이익과 합쳐져 +47.2649 흑자로 기록됐고, reason은 "stop_loss"였다
        (손절인데 흑자라는 모순된 기록). LAST_TRADE_STATE_PATH에 이미 기록된 거래ID 이하는
        항상 직전 포지션 몫이므로 제외해야 한다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot._save_last_trade_id(self.SYMBOL, "500", path=state_path)  # 직전 포지션의 청산 체결(이미 처리 완료)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 87.35,
            "stop_loss_price": 86.26, "take_profit_price": 89.53,
            "execution": {"entry_order": {"id": "600"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "500", "price": 87.24, "amount": 106.1, "info": {"realizedPnl": "63.6659", "orderId": 999}},
            {"id": "600", "price": 87.39, "amount": 109.34, "info": {"realizedPnl": "0", "orderId": 600}},
            {"id": "700", "price": 87.24, "amount": 109.34, "info": {"realizedPnl": "-16.401", "orderId": 601}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["realized_pnl"] == pytest.approx(-16.401)
        assert result["exit_price"] == pytest.approx(87.24)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "700"

    def test_reason_falls_back_to_unknown_when_pnl_sign_contradicts_the_guess(self, tmp_path, monkeypatch):
        """체결가 거리만으로 추정한 reason이 실제 손익 부호와 모순되면(예: "stop_loss"로
        추정했는데 흑자) 그 추정을 믿지 않고 unknown으로 남긴다 — 위 오염 버그가 실제로
        "손절인데 흑자"라는 모순된 기록을 남겼었다(2026-08-20)."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            # exit_price(1880.0)는 손절가(1876.25)에 더 가깝지만 realized_pnl은 흑자 -> 모순
            {"id": "trade-exit", "price": 1880.0, "amount": 1.0, "info": {"realizedPnl": "50.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)
        assert result["reason"] == "unknown"


class TestRecordManualClose:
    """실전 버그 재현(2026-08-14): fetch_my_trades를 since 없이 limit=5로만 부르면 진입 체결
    자체를 청산으로 오인해 손익 0으로 기록하고, 그 직후 다음 사이클의 check_and_log_closed_trade가
    진짜 청산을 또 감지해서 같은 포지션이 두 번(0원짜리 가짜 + 진짜) 기록되는 사고로 이어졌다."""

    SYMBOL = "BTC/USDT:USDT"

    def _paths(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        state_path = str(tmp_path / "last_trade.json")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", state_path)
        return journal_path, state_path

    def test_aggregates_partial_fills_since_entry_when_entry_info_exists(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 62743.1,
            "execution": {"entry_order": {"id": "28540959760"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 62743.1, "amount": 0.1156, "info": {"realizedPnl": "0", "orderId": 28540959760}},
            {"id": "t-close-1", "price": 62817.5, "amount": 0.0815, "info": {"realizedPnl": "-6.0636", "orderId": 99}},
            {"id": "t-close-2", "price": 62824.5, "amount": 0.0341, "info": {"realizedPnl": "-2.7702", "orderId": 99}},
        ]

        result = bot.record_manual_close(client, self.SYMBOL)

        assert result["reason"] == "manual"
        assert result["realized_pnl"] == pytest.approx(-8.8338)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "t-close-2"

    def test_falls_back_to_last_trade_when_no_entry_info(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-close", "price": 62817.5, "info": {"realizedPnl": "-8.83"}},
        ]

        result = bot.record_manual_close(client, self.SYMBOL)

        assert result["reason"] == "manual"
        assert result["entry_price"] is None
        assert result["realized_pnl"] == pytest.approx(-8.83)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "t-close"

    def test_falls_back_when_aggregation_finds_no_closing_trades(self, tmp_path, monkeypatch):
        """진입 기록은 있지만(예: 봇 재시작 등으로 since 창 안에 진입 체결만 보이는 경우)
        청산 체결을 못 찾으면, 마지막 체결 하나를 그대로 쓰는 예전 방식으로 대체해야 한다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 62743.1,
            "execution": {"entry_order": {"id": "28540959760"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 62743.1, "amount": 0.1156, "info": {"realizedPnl": "0", "orderId": 28540959760}},
        ]

        result = bot.record_manual_close(client, self.SYMBOL)

        assert result["reason"] == "manual"
        assert result["realized_pnl"] == pytest.approx(0.0)


class TestResetConsecutiveLosses:
    def test_appends_reset_event_to_given_journal_path(self, tmp_path):
        journal_path = str(tmp_path / "journal.jsonl")

        result = bot.reset_consecutive_losses(journal_path=journal_path)

        assert result == {"event": "consecutive_loss_reset"}
        entries = bot.read_entries(path=journal_path)
        assert entries[-1]["event"] == "consecutive_loss_reset"

    def test_defaults_to_module_journal_path(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)

        bot.reset_consecutive_losses()

        entries = bot.read_entries(path=journal_path)
        assert entries[-1]["event"] == "consecutive_loss_reset"


class TestCheckAndLogUntrackedPosition:
    """실전 버그 재현(2026-08-14): 사용자가 대시보드/봇을 거치지 않고 직접 주문을 넣으면 종목별
    상태(거래소 직접 조회)엔 바로 보이는데 거래 내역(저널 기반)엔 전혀 안 남았다."""

    SYMBOL = "BTC/USDT:USDT"

    def _paths(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        return journal_path

    def test_logs_entry_when_journal_has_no_record_for_this_symbol(self, tmp_path, monkeypatch):
        journal_path = self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_open_orders.return_value = [
            {"info": {"orderType": "STOP_MARKET"}, "triggerPrice": 61000},
            {"info": {"orderType": "TAKE_PROFIT_MARKET"}, "triggerPrice": 68000},
        ]
        position = {"entryPrice": 63000.0, "side": "short"}

        result = bot.check_and_log_untracked_position(client, self.SYMBOL, position)

        assert result["event"] == "entered"
        assert result["entry_price"] == 63000.0
        assert result["stop_loss_price"] == 61000
        assert result["take_profit_price"] == 68000
        assert result["reason"] == "manual_position_detected"
        entries = bot.read_entries(path=journal_path)
        assert len(entries) == 1 and entries[0]["event"] == "entered"

    def test_logs_entry_when_last_journal_record_for_symbol_is_closed(self, tmp_path, monkeypatch):
        """봇이 예전에 이 심볼로 거래하고 청산까지 기록했는데, 그 뒤 사용자가 새로 수동 진입한
        경우 — 마지막 기록이 "entered"가 아니라 "closed"이므로 새 포지션으로 취급해야 한다."""
        journal_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({"symbol": self.SYMBOL, "event": "entered", "entry_price": 60000.0}, path=journal_path)
        bot.append_entry({"symbol": self.SYMBOL, "event": "closed", "reason": "take_profit"}, path=journal_path)

        client = MagicMock()
        client.fetch_open_orders.return_value = []
        position = {"entryPrice": 63000.0, "side": "long"}

        result = bot.check_and_log_untracked_position(client, self.SYMBOL, position)

        assert result is not None
        assert result["entry_price"] == 63000.0

    def test_does_nothing_when_already_tracked_as_entered(self, tmp_path, monkeypatch):
        journal_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({"symbol": self.SYMBOL, "event": "entered", "entry_price": 63000.0}, path=journal_path)

        client = MagicMock()
        result = bot.check_and_log_untracked_position(client, self.SYMBOL, {"entryPrice": 63000.0})

        assert result is None
        client.fetch_open_orders.assert_not_called()
        entries = bot.read_entries(path=journal_path)
        assert len(entries) == 1  # 중복 기록 안 됨

    def test_ignores_other_symbols_when_checking_last_event(self, tmp_path, monkeypatch):
        journal_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({"symbol": "ETH/USDT:USDT", "event": "entered", "entry_price": 1900.0}, path=journal_path)

        client = MagicMock()
        client.fetch_open_orders.return_value = []
        result = bot.check_and_log_untracked_position(client, self.SYMBOL, {"entryPrice": 63000.0})

        assert result is not None  # BTC엔 기록이 없으니 ETH 기록과 무관하게 새로 남겨야 함


def _signal_df(n=61, close=100.0, live_close=None):
    """timestamp 열이 있는 최소 df — 마지막 행은 '아직 마감 안 된' 진행중 봉 몫이다."""
    closes = np.full(n, close)
    if live_close is not None:
        closes[-1] = live_close
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-09-07", periods=n, freq="h"),
        "high": closes + _HL, "low": closes - _HL, "close": closes,
    })


def test_evaluate_symbol_skips_reentry_on_the_same_signal_candle():
    """실전 버그 재현(2026-09-07): 마감 봉이 그 시간봉 내내 고정이라, 포지션이 도중에 청산되면
    같은 신호로 30초마다 계속 재진입한다 — 실계좌 ZEC가 한 시간에 같은 값으로 5번 진입해
    수수료만 태운 왕복을 만들고 3분 만에 연속손실 5회로 서킷브레이커를 걸었다. 백테스트는 봉
    하나당 한 번만 진입하므로, 실거래도 같은 봉으로는 재진입하지 않아야 한다."""
    df = _signal_df()
    signal_bar = str(df.iloc[:-1]["timestamp"].iloc[-1])
    already_entered = [{"symbol": "BTC/USDT:USDT", "event": "entered",
                        "signal_bar_timestamp": signal_bar}]

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.read_entries", return_value=already_entered), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "skipped_same_signal_bar"
        # 같은 봉 이력이 없는 다른 심볼은 정상 진입해야 한다(잠금이 전역이 아니라 심볼별)
        assert cycle["symbols"]["ETH/USDT:USDT"]["event"] == "entered"
        assert mock_open.called
    finally:
        _stop(patches)


def test_evaluate_symbol_enters_when_last_entry_used_a_different_candle():
    df = _signal_df()
    stale = [{"symbol": "BTC/USDT:USDT", "event": "entered",
              "signal_bar_timestamp": "2020-01-01 00:00:00"}]

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.read_entries", return_value=stale), \
             patch("src.futures_rule_bot.open_position_with_bracket"):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        result = cycle["symbols"]["BTC/USDT:USDT"]
        assert result["event"] == "entered"
        # 다음 사이클에서 잠글 수 있도록 저널에 남길 결과에 봉 시각이 실려야 한다
        assert result["signal_bar_timestamp"] == str(df.iloc[:-1]["timestamp"].iloc[-1])
    finally:
        _stop(patches)


def test_evaluate_symbol_skips_entry_when_live_price_drifted_past_the_bracket():
    """손절/익절가는 전부 신호 봉 종가 기준이라, 현재가가 이미 손절선 쪽으로 크게 이동했으면
    진입하자마자 손절되거나 브라켓이 -2021로 거부된다. 진입 전에 걸러야 한다."""
    # 손절폭 = 100 * STOP_LOSS_PCT(1.25%) = 1.25, 허용 이탈 = 그 절반 = 0.625
    df = _signal_df(close=100.0, live_close=99.0)  # 현재가가 1.0 아래로 이탈 > 0.625

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        result = cycle["symbols"]["BTC/USDT:USDT"]
        assert result["event"] == "skipped_price_drift"
        assert result["entry_price"] == 100.0 and result["live_price"] == 99.0
        assert not mock_open.called
    finally:
        _stop(patches)


def test_evaluate_symbol_enters_when_live_price_drift_is_within_tolerance():
    # 이탈 0.1 < 허용 0.125 (= 손절폭 1.25% x MAX_ENTRY_PRICE_DRIFT_R 0.1).
    # 허용치는 2026-09-22에 0.5 -> 0.1로 좁혔다 — 이 전략은 편도 0.05R 슬리피지에서 이미
    # 무너지므로 0.5는 죽는 수준의 10배를 허용하는 값이었다. 그래서 이 테스트의 괴리도
    # 같이 좁혔다(옛 값 0.3은 이제 skipped_price_drift로 막히는 게 정상이다).
    df = _signal_df(close=100.0, live_close=100.1)

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "entered"
        assert mock_open.called
    finally:
        _stop(patches)


def test_evaluate_symbol_blocks_shorts_in_an_uptrend_regime():
    """상승 레짐(종가가 장기 SMA 위)에서는 숏 진입을 막고 롱은 그대로 통과시킨다."""
    n = RULE_REGIME_SMA_PERIOD + 2
    closes = np.linspace(100.0, 200.0, n)  # 꾸준한 상승 -> 종가가 장기 SMA 위
    # 가격이 100 -> 200으로 변하므로 고가/저가 폭도 비율로 잡아야 ATR%가 일정하게 유지된다
    # (고정폭으로 두면 뒤로 갈수록 ATR%가 작아져 저변동 필터에 걸린다)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "high": closes * 1.006, "low": closes * 0.994, "close": closes,
    })

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="SHORT"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        # 레짐 필터가 버린 숏은 skipped_regime으로 구분해서 남긴다 — no_signal과 뭉뚱그리면
        # 필터가 실제로 몇 번 일했는지 셀 수 없다(2026-09-09 차단 통계 추가).
        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "skipped_regime"
        assert not mock_open.called

        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "entered"
        assert mock_open.called
    finally:
        _stop(patches)


def test_new_skip_events_are_not_written_to_the_journal():
    """진입 잠금/괴리 스킵은 신호가 살아있는 동안 매 사이클 반복된다 — 저널에 남기면 예전
    rejected_exchange_error 스팸(2026-08-20, 실제 체결 기록이 대시보드 last 30에서 밀려남)과
    같은 사고가 난다. 사이클 결과에는 남되 저널에는 안 남아야 한다."""
    df = _signal_df()
    signal_bar = str(df.iloc[:-1]["timestamp"].iloc[-1])
    already = [{"symbol": s, "event": "entered", "signal_bar_timestamp": signal_bar} for s in SYMBOLS]

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.read_entries", return_value=already), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert all(r["event"] == "skipped_same_signal_bar" for r in cycle["symbols"].values())
        assert not mock_append.called

    finally:
        _stop(patches)

    drift_df = _signal_df(close=100.0, live_close=99.0)
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=drift_df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert all(r["event"] == "skipped_price_drift" for r in cycle["symbols"].values())
        assert not mock_append.called
    finally:
        _stop(patches)


def _low_vol_df(n=61, close=100.0, hl=0.05):
    """ATR이 손절폭 하한에 한참 못 미치는 저변동 시계열."""
    closes = np.full(n, close)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-09-07", periods=n, freq="h"),
        "high": closes + hl, "low": closes - hl, "close": closes,
    })


def test_evaluate_symbol_skips_entry_in_a_low_volatility_regime():
    """손절 1.25%/익절 2.5%인데 ATR이 그보다 훨씬 작으면 익절까지 ATR 몇 배를 가야 해서 사실상
    도달이 어렵고, 대신 시간이 흐르며 손절로 흘러간다 — 후보 신호를 특성별로 쪼개보면 이 구간이
    일관된 손실 구간이었다(UPDATE_LOG.md 2026-09-09)."""
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_low_vol_df()), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        result = cycle["symbols"]["BTC/USDT:USDT"]
        assert result["event"] == "skipped_low_volatility"
        assert result["atr_to_stop_ratio"] < MIN_ATR_TO_STOP_RATIO
        assert not mock_open.called
    finally:
        _stop(patches)


def test_evaluate_symbol_enters_when_volatility_clears_the_floor():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_signal_df()), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        result = cycle["symbols"]["BTC/USDT:USDT"]
        assert result["event"] == "entered"
        # 나중에 저널만으로 필터 효과를 검증할 수 있도록 진입 기록에 비율을 남긴다
        assert result["atr_to_stop_ratio"] >= MIN_ATR_TO_STOP_RATIO
        assert mock_open.called
    finally:
        _stop(patches)


def test_low_volatility_skip_is_not_written_to_the_journal():
    """신호가 살아있는 동안 매 사이클 반복되므로 저널에 남기면 안 된다."""
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_low_vol_df()), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert all(r["event"] == "skipped_low_volatility" for r in cycle["symbols"].values())
        assert not mock_append.called
    finally:
        _stop(patches)



# ---------- 무보호 포지션 감지 (2026-09-09) ----------

class TestCheckPositionProtection:
    """손절 주문 없이 열려있는 레버리지 포지션을 감지해 저널(=텔레그램 알림 경로)에 남긴다."""

    SYMBOL = "BTC/USDT:USDT"

    def _journal_with_entry(self, tmp_path):
        journal_path = str(tmp_path / "journal.jsonl")
        bot.append_entry({"symbol": self.SYMBOL, "event": "entered", "signal": "LONG",
                          "entry_price": 100.0, "stop_loss_price": 99.0}, path=journal_path)
        return journal_path

    def test_logs_once_when_the_stop_order_is_missing(self, tmp_path):
        journal_path = self._journal_with_entry(tmp_path)
        client = MagicMock()

        with patch("src.futures_rule_bot.get_bracket_prices", return_value=(None, 102.0)):
            first = bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path)
            second = bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path)

        assert first["event"] == "unprotected_position"
        assert second is None  # 30초마다 같은 경보를 쌓지 않는다
        events = [e["event"] for e in bot.read_entries(path=journal_path)]
        assert events.count("unprotected_position") == 1

    def test_says_nothing_while_the_stop_order_is_in_place(self, tmp_path):
        journal_path = self._journal_with_entry(tmp_path)
        client = MagicMock()

        with patch("src.futures_rule_bot.get_bracket_prices", return_value=(99.0, 102.0)):
            assert bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path) is None

        assert [e["event"] for e in bot.read_entries(path=journal_path)] == ["entered"]

    def test_reports_recovery_after_an_alert(self, tmp_path):
        """경보만 있고 해제 알림이 없으면 사용자가 아직 위험한지 계속 확인해야 한다."""
        journal_path = self._journal_with_entry(tmp_path)
        client = MagicMock()

        with patch("src.futures_rule_bot.get_bracket_prices", return_value=(None, None)):
            bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path)
        with patch("src.futures_rule_bot.get_bracket_prices", return_value=(99.0, 102.0)):
            recovered = bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path)
            again = bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path)

        assert recovered["event"] == "position_protected"
        assert recovered["stop_loss_price"] == 99.0
        assert again is None  # 복구 알림도 한 번만

    def test_stays_quiet_when_the_bracket_query_itself_fails(self, tmp_path):
        """일시적 API 오류와 "정말로 무보호"를 구별해야 한다 — 잘못된 경보는 진짜 경보를
        무시하게 만들어서 없느니만 못하다. get_bracket_prices(strict=True)가 예외를 올려준다."""
        journal_path = self._journal_with_entry(tmp_path)
        client = MagicMock()

        with patch("src.futures_rule_bot.get_bracket_prices", side_effect=Exception("timeout")):
            assert bot.check_position_protection(client, self.SYMBOL, journal_path=journal_path) is None

        assert [e["event"] for e in bot.read_entries(path=journal_path)] == ["entered"]

    def test_asks_the_exchange_strictly(self, tmp_path):
        journal_path = self._journal_with_entry(tmp_path)
        with patch("src.futures_rule_bot.get_bracket_prices", return_value=(99.0, None)) as mock_get:
            bot.check_position_protection(MagicMock(), self.SYMBOL, journal_path=journal_path)
        assert mock_get.call_args.kwargs["strict"] is True


def test_untracked_position_backfill_is_not_repeated_after_an_unprotected_record(tmp_path):
    """회귀 방지: unprotected_position이 그 심볼의 마지막 기록이 되어도 "봇이 모르는 포지션"으로
    오인하면 안 된다 — 오인하면 매 사이클 진입 기록을 중복 백필하게 된다."""
    journal_path = str(tmp_path / "journal.jsonl")
    bot.append_entry({"symbol": "BTC/USDT:USDT", "event": "entered", "signal": "LONG",
                      "entry_price": 100.0, "stop_loss_price": 99.0}, path=journal_path)
    bot.append_entry({"symbol": "BTC/USDT:USDT", "event": "unprotected_position"}, path=journal_path)

    with patch("src.futures_rule_bot.get_bracket_prices", return_value=(None, None)):
        result = bot.check_and_log_untracked_position(
            MagicMock(), "BTC/USDT:USDT", {"entryPrice": 100.0}, journal_path=journal_path)

    assert result is None
    assert [e["event"] for e in bot.read_entries(path=journal_path)].count("entered") == 1


# ---------- 청산 기록의 R배수/최고점 (2026-09-09) ----------

class TestClosedEntryCarriesTradeContext:
    """$ 금액만으로는 "계획 대비 어땠는지"를 알 수 없어서 방향/손절가/R을 같이 남긴다."""

    SYMBOL = "ETH/USDT:USDT"

    def _setup(self, tmp_path, monkeypatch, signal="LONG"):
        journal_path = str(tmp_path / "journal.jsonl")
        last_trade_path = str(tmp_path / "last_trade.json")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", last_trade_path)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "signal": signal, "entry_price": 100.0,
            "stop_loss_price": 99.0, "take_profit_price": 102.0,
        }, path=journal_path)
        return journal_path, last_trade_path

    def test_records_side_stop_and_realized_r(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 1.0, "info": {"realizedPnl": "0"}},
            {"id": "2", "price": 102.0, "amount": 1.0, "info": {"realizedPnl": "2.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL,
                                                 excursion_path=str(tmp_path / "exc.json"))

        assert result["side"] == "long"
        assert result["stop_loss_price"] == 99.0
        assert result["realized_r"] == pytest.approx(2.0)  # 손절폭 1.0, +2.0 이동

    def test_records_realized_r_for_a_short(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, signal="SHORT")
        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 1.0, "info": {"realizedPnl": "0"}},
            {"id": "2", "price": 101.0, "amount": 1.0, "info": {"realizedPnl": "-1.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL,
                                                 excursion_path=str(tmp_path / "exc.json"))

        assert result["side"] == "short"
        assert result["realized_r"] == pytest.approx(-1.0)  # 숏은 가격이 오르면 손실

    def test_carries_the_best_and_worst_points_reached_while_open(self, tmp_path, monkeypatch):
        """"익절 코앞까지 갔다가 손절났다"를 5분봉 재구성 없이 바로 볼 수 있게 한다."""
        self._setup(tmp_path, monkeypatch)
        excursion_path = str(tmp_path / "exc.json")
        excursion.update(self.SYMBOL, entry_price=100.0, stop_loss_price=99.0,
                         mark_price=101.8, side="long", path=excursion_path)
        excursion.update(self.SYMBOL, entry_price=100.0, stop_loss_price=99.0,
                         mark_price=99.0, side="long", path=excursion_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 1.0, "info": {"realizedPnl": "0"}},
            {"id": "2", "price": 99.0, "amount": 1.0, "info": {"realizedPnl": "-1.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL, excursion_path=excursion_path)

        assert result["max_favorable_r"] == pytest.approx(1.8)
        assert result["max_adverse_r"] == pytest.approx(-1.0)
        # 기록을 소비했으면 지워야 다음 포지션의 시작값이 오염되지 않는다
        assert excursion.read_all(path=excursion_path) == {}

    def test_infers_the_side_for_a_manually_opened_position(self, tmp_path, monkeypatch):
        """사용자가 직접 넣은 포지션은 백필 기록에 signal이 없다 — 손절가 위치로 방향을 읽는다."""
        journal_path = str(tmp_path / "journal.jsonl")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", str(tmp_path / "last_trade.json"))
        bot.append_entry({"symbol": self.SYMBOL, "event": "entered", "signal": None,
                          "entry_price": 100.0, "stop_loss_price": 101.0}, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 1.0, "info": {"realizedPnl": "0"}},
            {"id": "2", "price": 98.0, "amount": 1.0, "info": {"realizedPnl": "2.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL,
                                                 excursion_path=str(tmp_path / "exc.json"))

        assert result["side"] == "short"  # 손절가가 진입가보다 위 -> 숏


def test_track_excursion_updates_while_a_position_is_open(tmp_path):
    journal_path = str(tmp_path / "journal.jsonl")
    excursion_path = str(tmp_path / "exc.json")
    bot.append_entry({"symbol": "BTC/USDT:USDT", "event": "entered", "signal": "LONG",
                      "entry_price": 100.0, "stop_loss_price": 99.0}, path=journal_path)

    bot._track_excursion("BTC/USDT:USDT", {"markPrice": 101.5, "side": "long"},
                         journal_path, excursion_path)

    assert excursion.read_all(path=excursion_path)["BTC/USDT:USDT"]["max_favorable_r"] == pytest.approx(1.5)


def test_track_excursion_does_nothing_without_an_entry_record(tmp_path):
    excursion_path = str(tmp_path / "exc.json")
    bot._track_excursion("BTC/USDT:USDT", {"markPrice": 101.5, "side": "long"},
                         str(tmp_path / "journal.jsonl"), excursion_path)
    assert excursion.read_all(path=excursion_path) == {}


# ---------- 차단 통계 (2026-09-09) ----------

def test_run_once_counts_silently_blocked_signals(tmp_path):
    """저널에 안 남는 차단 사유도 집계는 남아야 한다 — 안 그러면 필터가 도는지조차 알 수 없다."""
    n = RULE_REGIME_SMA_PERIOD + 2
    closes = np.linspace(100.0, 200.0, n)  # 꾸준한 상승 -> 종가가 장기 SMA 위
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "high": closes * 1.006, "low": closes * 0.994, "close": closes,
    })
    stats_path = str(tmp_path / "stats.json")

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="SHORT"), \
             patch("src.futures_rule_bot.open_position_with_bracket"):
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                         filter_stats_path=stats_path)
            # 같은 봉으로 한 사이클 더 돌아도 집계는 안 늘어야 한다
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                         filter_stats_path=stats_path)
    finally:
        _stop(patches)

    counts = next(iter(filter_stats.read_counts(path=stats_path).values()))
    assert counts["skipped_regime"] == len(SYMBOLS)


def test_run_once_does_not_count_plain_no_signal(tmp_path):
    """no_signal은 "필터가 일한 것"이 아니라 그냥 신호가 없는 것 — 집계 대상이 아니다."""
    stats_path = str(tmp_path / "stats.json")
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value=None):
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                         filter_stats_path=stats_path)
    finally:
        _stop(patches)

    assert filter_stats.read_counts(path=stats_path) == {}


def test_run_once_uses_live_state_paths_for_live_env(tmp_path, monkeypatch):
    """데모/실계좌가 통계·최고점 파일도 완전히 분리돼야 한다."""
    monkeypatch.setattr(bot, "LIVE_FILTER_STATS_PATH", str(tmp_path / "live_stats.json"))
    monkeypatch.setattr(bot, "LIVE_EXCURSION_PATH", str(tmp_path / "live_exc.json"))
    captured = {}

    def _reconcile(client, symbol, position, journal_path=None, last_trade_path=None,
                    excursion_path=None):
        captured["excursion_path"] = excursion_path

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot._reconcile_symbol", side_effect=_reconcile):
            bot.run_once(MagicMock(), env="live", consecutive_losses=0, daily_pnl_pct=0.0)
    finally:
        _stop(patches)

    assert captured["excursion_path"] == str(tmp_path / "live_exc.json")


# --- 설정 스냅샷(config_changed) -------------------------------------------------
# 거래가 어떤 규칙으로 나왔는지를 저널 자체에 남겨두려는 것 — 없으면 필터를 넣기 전후의 거래가
# 한 숫자로 뭉뚱그려져서 "지금 규칙이 통하는지"를 화면에서 판단할 수 없다(2026-09-12).

def test_log_config_change_records_the_first_snapshot(tmp_path):
    journal = str(tmp_path / "j.jsonl")

    entry = bot.log_config_change(journal_path=journal)

    assert entry["event"] == "config_changed"
    assert entry["first_record"] is True
    assert entry["config"] == bot.current_strategy_config()


def test_log_config_change_is_silent_when_nothing_changed(tmp_path):
    """봇을 하루에 몇 번씩 재시작해도 설정이 그대로면 저널에 아무것도 안 남아야 한다."""
    journal = str(tmp_path / "j.jsonl")
    bot.log_config_change(journal_path=journal)

    assert bot.log_config_change(journal_path=journal) is None
    assert len(bot.read_entries(path=journal)) == 1


def test_log_config_change_records_only_what_changed(tmp_path):
    journal = str(tmp_path / "j.jsonl")
    first = dict(bot.current_strategy_config())
    bot.log_config_change(journal_path=journal, config=first)

    changed = {**first, "regime_sma_period": 700}
    entry = bot.log_config_change(journal_path=journal, config=changed)

    assert entry["first_record"] is False
    assert entry["changes"] == {"regime_sma_period": {"from": first["regime_sma_period"], "to": 700}}


def test_config_snapshot_does_not_disturb_position_lifecycle(tmp_path):
    """설정 기록은 심볼이 없는 이벤트라, 심볼별 "마지막 생애주기 기록" 판정에 끼어들면 안 된다
    (끼어들면 진입 기록이 매 사이클 중복 백필된다 — unprotected_position 때 겪은 문제)."""
    journal = str(tmp_path / "j.jsonl")
    bot.append_entry({"symbol": "BTC/USDT:USDT", "event": "entered", "entry_price": 100}, path=journal)
    bot.log_config_change(journal_path=journal)

    client = MagicMock()
    assert bot.check_and_log_untracked_position(
        client, "BTC/USDT:USDT", {"entryPrice": 100}, journal_path=journal) is None


class TestClosedTradeRecordsRealCosts:
    """저널의 realized_pnl/realized_r은 둘 다 **수수료 이전** 값이었다(2026-09-22).

    거래소의 realizedPnl에 수수료가 안 들어있고(commission이 별개 필드), R은 신호 봉 종가를
    진입가로 써서 슬리피지도 안 들어있다. 손절폭 1.25%에서 왕복 수수료는 약 0.04~0.064R인데
    이 전략의 건당 기대값이 +0.02~0.06R이라 **수수료가 기대값과 같은 크기**다 — 실제 값을
    저널에 남겨야 사후에 순성과를 낼 수 있다.
    """

    SYMBOL = "ETH/USDT:USDT"

    def _paths(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", str(tmp_path / "last_trade.json"))
        return journal_path

    def _entered(self, journal_path, quantity=10.0):
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "signal": "LONG", "entry_price": 100.0,
            "stop_loss_price": 99.0, "take_profit_price": 102.0,
            "execution": {"quantity": quantity, "entry_order": {"id": "entry-order"}},
        }, path=journal_path)

    def test_records_entry_and_exit_commission_separately_and_nets_pnl(self, tmp_path, monkeypatch):
        journal_path = self._paths(tmp_path, monkeypatch)
        self._entered(journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 10.0,
             "info": {"orderId": "entry-order", "realizedPnl": "0", "commission": "0.40",
                       "commissionAsset": "USDT"}},
            {"id": "2", "price": 99.0, "amount": 10.0,
             "info": {"orderId": "stop-order", "realizedPnl": "-10.0", "commission": "0.396",
                       "commissionAsset": "USDT"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["entry_fee"] == pytest.approx(0.40)
        assert result["exit_fee"] == pytest.approx(0.396)
        assert result["total_fee"] == pytest.approx(0.796)
        assert result["fee_assets"] == ["USDT"]
        # 거래소가 준 realizedPnl은 수수료 이전 값이라 그대로 남기고, 순손익을 따로 계산한다.
        assert result["realized_pnl"] == pytest.approx(-10.0)
        assert result["net_realized_pnl"] == pytest.approx(-10.796)

    def test_records_fee_in_r_so_it_can_be_compared_with_the_backtest(self, tmp_path, monkeypatch):
        """R로 환산해 둬야 백테스트의 fee_pct_per_side 차감과 같은 의미로 비교된다."""
        journal_path = self._paths(tmp_path, monkeypatch)
        self._entered(journal_path, quantity=10.0)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.0, "amount": 10.0,
             "info": {"orderId": "entry-order", "realizedPnl": "0", "commission": "0.40"}},
            {"id": "2", "price": 102.0, "amount": 10.0,
             "info": {"orderId": "tp-order", "realizedPnl": "20.0", "commission": "0.408"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        # 리스크 금액 = |100 - 99| x 10주 = 10 USDT. 수수료 0.808 / 10 = 0.0808R.
        assert result["fee_r"] == pytest.approx(0.0808)
        assert result["realized_r"] == pytest.approx(2.0)
        assert result["net_realized_r"] == pytest.approx(2.0 - 0.0808)

    def test_records_the_actual_entry_fill_price_which_the_order_response_lacks(self, tmp_path, monkeypatch):
        """주문 생성 응답에는 체결가가 안 담겨 온다(avgPrice가 "0.00") — 진입 체결에서 뽑는다.
        저널의 entry_price는 신호 봉 종가이므로, 이 둘의 차이가 실제 진입 슬리피지다."""
        journal_path = self._paths(tmp_path, monkeypatch)
        self._entered(journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "1", "price": 100.5, "amount": 4.0,
             "info": {"orderId": "entry-order", "realizedPnl": "0", "commission": "0.16"}},
            {"id": "2", "price": 100.7, "amount": 6.0,
             "info": {"orderId": "entry-order", "realizedPnl": "0", "commission": "0.24"}},
            {"id": "3", "price": 99.0, "amount": 10.0,
             "info": {"orderId": "stop-order", "realizedPnl": "-15.0", "commission": "0.396"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        # 부분 체결의 수량가중평균: (100.5*4 + 100.7*6)/10 = 100.62
        assert result["actual_entry_price"] == pytest.approx(100.62)
        assert result["entry_price"] == pytest.approx(100.0)  # 신호 봉 종가는 그대로 남는다
        assert result["entry_fee"] == pytest.approx(0.40)

    def test_omits_cost_fields_when_the_exchange_reports_no_commission(self, tmp_path, monkeypatch):
        """옛 기록이나 수수료 정보가 없는 응답에서 0을 지어내면 "수수료가 없었다"로 읽힌다 —
        없으면 칼럼 자체를 안 남긴다."""
        journal_path = self._paths(tmp_path, monkeypatch)
        self._entered(journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "2", "price": 99.0, "amount": 10.0,
             "info": {"orderId": "stop-order", "realizedPnl": "-10.0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert "total_fee" not in result
        assert "net_realized_pnl" not in result
        assert "fee_r" not in result
        assert result["realized_pnl"] == pytest.approx(-10.0)


# ---------- 봉 마감에 맞춰 깨기 (2026-09-28) ----------

def test_next_cycle_waits_the_normal_poll_when_no_bar_closes_soon():
    now = 1_790_600_000 + 600  # 정각 10분 뒤 (1_790_600_000 = 정각이 아님 → 아래서 정각으로 맞춘다)
    hour = (now // 3600) * 3600
    assert bot.seconds_until_next_cycle(hour + 600, 30, "1h", 3) == 30


def test_next_cycle_wakes_just_after_the_bar_closes():
    """정각 10초 전에 사이클이 끝났으면 30초를 다 기다리지 않고 정각+3초에 깬다 — 새 신호는 봉이
    마감되는 순간에만 생기므로, 거기서 기다리는 시간이 곧 진입 슬리피지의 원인이다."""
    next_hour = ((1_790_600_000 // 3600) + 1) * 3600
    assert bot.seconds_until_next_cycle(next_hour - 10, 30, "1h", 3) == pytest.approx(13)


def test_next_cycle_catches_a_close_that_just_happened():
    """정각 1초 뒤(아직 +3초 전)면 2초 뒤에 깬다."""
    hour = (1_790_600_000 // 3600) * 3600
    assert bot.seconds_until_next_cycle(hour + 1, 30, "1h", 3) == pytest.approx(2)


def test_next_cycle_after_the_aligned_wake_goes_back_to_the_normal_poll():
    hour = (1_790_600_000 // 3600) * 3600
    assert bot.seconds_until_next_cycle(hour + 4, 30, "1h", 3) == 30


def test_next_cycle_never_busy_loops():
    hour = (1_790_600_000 // 3600) * 3600
    assert bot.seconds_until_next_cycle(hour + 2.99, 30, "1h", 3) >= 0.5


def test_evaluate_symbol_skips_a_signal_against_the_higher_timeframe():
    """상위봉(RULE_HTF_HOURS) 방향이 신호와 반대면 진입하지 않고, 그 차단은 skipped_htf로
    구분해 남긴다(차단 통계가 필터가 몇 번 일했는지 셀 수 있게). 판정에는 **마감된 봉만** 넘긴다."""
    df = _signal_df()
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.RULE_HTF_HOURS", 4), \
             patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=df), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.latest_htf_direction", return_value=-1.0) as mock_htf, \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open, \
             patch("src.futures_rule_bot.append_entry") as mock_append:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        result = cycle["symbols"]["BTC/USDT:USDT"]
        assert result["event"] == "skipped_htf"
        assert result["signal_bar_timestamp"] == str(df["timestamp"].iloc[-2])
        assert not mock_open.called
        # 매 사이클 반복되는 차단이라 저널에는 안 남는다
        assert not any(c.args and c.args[0].get("event") == "skipped_htf"
                       for c in mock_append.call_args_list)
        passed_df, hours = mock_htf.call_args.args
        assert hours == 4
        assert len(passed_df) == len(df) - 1  # 진행 중인 봉을 버린 뒤
    finally:
        _stop(patches)


def test_evaluate_symbol_enters_when_the_higher_timeframe_agrees():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.RULE_HTF_HOURS", 4), \
             patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_signal_df()), \
             patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.latest_htf_direction", return_value=1.0), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "entered"
        assert mock_open.called
    finally:
        _stop(patches)


def test_candle_request_covers_the_higher_timeframe_warmup():
    """상위봉 필터를 켜면 상위봉 100개(+반쪽 묶음 몫)만큼 신호봉을 더 받아야 한다 — 모자라면
    DI의 지수평활이 백테스트(전체 이력)와 다른 값에서 출발한다."""
    from src.core.futures_strategy import closed_bars_needed

    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.RULE_HTF_HOURS", 4), \
             patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_signal_df()) as mock_fetch, \
             patch("src.futures_rule_bot.detect_signal", return_value=None):
            bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        expected = closed_bars_needed(RULE_REGIME_SMA_PERIOD, 4) + 1
        assert mock_fetch.call_args.kwargs["limit"] == expected
        assert expected > RULE_REGIME_SMA_PERIOD + 2
    finally:
        _stop(patches)


def test_strategy_config_tracks_the_higher_timeframe_setting():
    """이 설정이 바뀌면 전략 버전 경계가 생겨야 전후 성과를 나눠 볼 수 있다."""
    assert "htf_hours" in bot.current_strategy_config()
