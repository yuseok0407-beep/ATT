from unittest.mock import MagicMock, patch

from src import futures_pipeline as fp


def _base_patches(balance_usdt_total=10_000, position=None):
    return [
        patch("src.futures_pipeline.get_futures_client"),
        patch("src.futures_pipeline.futures_decision.get_client"),
        patch("src.futures_pipeline.set_margin_mode"),
        patch("src.futures_pipeline.set_leverage"),
        patch("src.futures_pipeline.get_futures_balance", return_value={"USDT": {"total": balance_usdt_total}}),
        patch("src.futures_pipeline.get_position", return_value=position),
    ]


def _apply(patches):
    started = [p.start() for p in patches]
    return started, patches


def _stop(patches):
    for p in patches:
        p.stop()


def test_run_futures_cycle_blocked_by_circuit_breaker(tmp_path):
    patches = _base_patches()
    _apply(patches)
    try:
        with patch("src.futures_pipeline.append_entry") as mock_append, \
             patch("src.futures_pipeline.build_snapshot") as mock_snapshot:
            cycle = fp.run_futures_cycle(consecutive_losses=0, daily_pnl_pct=-0.10)
            mock_snapshot.assert_not_called()  # 서킷 브레이커에 걸리면 시세 조회까지 갈 필요 없음
        assert cycle["results"][0]["event"] == "circuit_breaker_blocked"
        mock_append.assert_called_once()
    finally:
        _stop(patches)


def test_run_futures_cycle_opens_long_with_attached_stop():
    patches = _base_patches(balance_usdt_total=10_000, position=None)
    _apply(patches)
    fake_decision = {"decision": "LONG", "confidence": 0.9, "reasoning": "clean breakout"}
    try:
        with patch("src.futures_pipeline.append_entry"), \
             patch("src.futures_pipeline.build_snapshot", return_value={"symbol": "BTC/USDT", "price": 65000.0}), \
             patch("src.futures_pipeline.futures_decision.decide", return_value=fake_decision), \
             patch("src.futures_pipeline.open_position") as mock_open:
            mock_open.return_value = {"status": "opened"}
            cycle = fp.run_futures_cycle(consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["results"][0]["decision"]["decision"] == "LONG"
        mock_open.assert_called_once()
        args, _ = mock_open.call_args
        # (client, symbol, side, quantity, stop_loss_price)
        assert args[2] == "long"
        assert args[3] > 0
        assert args[4] < 65000.0  # 롱 손절가는 진입가보다 낮아야 함
    finally:
        _stop(patches)


def test_run_futures_cycle_closes_existing_position():
    open_position_state = {"side": "long", "contracts": 0.02}
    patches = _base_patches(balance_usdt_total=10_000, position=open_position_state)
    _apply(patches)
    fake_decision = {"decision": "CLOSE", "confidence": 0.9, "reasoning": "target hit"}
    try:
        with patch("src.futures_pipeline.append_entry"), \
             patch("src.futures_pipeline.build_snapshot", return_value={"symbol": "BTC/USDT", "price": 66000.0}), \
             patch("src.futures_pipeline.futures_decision.decide", return_value=fake_decision), \
             patch("src.futures_pipeline.close_position") as mock_close:
            mock_close.return_value = {"status": "closed"}
            cycle = fp.run_futures_cycle(consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["results"][0]["decision"]["decision"] == "CLOSE"
        mock_close.assert_called_once()
        args, _ = mock_close.call_args
        assert args[2] == "long"
        assert args[3] == 0.02
    finally:
        _stop(patches)


def test_run_futures_cycle_rejects_unsafe_stop_without_opening(monkeypatch):
    # STOP_LOSS_PCT를 손절가가 청산가를 넘어설 정도로 크게 설정 -> 안전검증에서 걸려야 함
    monkeypatch.setattr("src.futures_pipeline.STOP_LOSS_PCT", 0.5)
    patches = _base_patches(balance_usdt_total=10_000, position=None)
    _apply(patches)
    fake_decision = {"decision": "LONG", "confidence": 0.9, "reasoning": "..."}
    try:
        with patch("src.futures_pipeline.append_entry"), \
             patch("src.futures_pipeline.build_snapshot", return_value={"symbol": "BTC/USDT", "price": 65000.0}), \
             patch("src.futures_pipeline.futures_decision.decide", return_value=fake_decision), \
             patch("src.futures_pipeline.open_position") as mock_open:
            cycle = fp.run_futures_cycle(consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["results"][0]["event"] == "rejected_unsafe_stop"
        mock_open.assert_not_called()
    finally:
        _stop(patches)
