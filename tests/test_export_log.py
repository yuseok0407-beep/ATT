import csv
import json

from src.core.config import FEE_PCT_PER_SIDE
from src.execution import export_log
from src.execution.export_log import (
    build_config_rows,
    build_event_rows,
    build_trade_rows,
    export_all,
    export_env,
    write_csv,
)


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _entered(symbol, timestamp, signal="LONG", entry=100.0, stop=98.0, take=104.0, **extra):
    return {"timestamp": timestamp, "symbol": symbol, "event": "entered", "entered": True,
            "has_position": False, "signal": signal, "entry_price": entry,
            "stop_loss_price": stop, "take_profit_price": take, **extra}


def _closed(symbol, timestamp, exit_price, pnl, reason="take_profit", **extra):
    return {"timestamp": timestamp, "symbol": symbol, "event": "closed", "reason": reason,
            "exit_price": exit_price, "realized_pnl": pnl, **extra}


def test_build_trade_rows_joins_entry_context():
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00",
                 atr_to_stop_ratio=0.9, signal_bar_timestamp="2026-09-20T00:00:00+00:00",
                 execution={"quantity": 2.0}),
        _closed("BTC/USDT:USDT", "2026-09-20T03:30:00+00:00", 104.0, 8.0,
                side="long", stop_loss_price=98.0, entry_price=100.0, realized_r=2.0,
                entry_timestamp="2026-09-20T01:00:00+00:00"),
    ]
    row, = build_trade_rows(entries, "demo")

    assert row["env"] == "demo"
    assert row["symbol"] == "BTC/USDT:USDT"
    assert row["side"] == "long"
    assert row["realized_r"] == 2.0
    assert row["r_reliable"] == "yes"
    # 진입 기록에만 있는 맥락이 청산 줄로 따라와야 한다 — CSV에서 "어떤 신호였나"를 세려면 필수.
    assert row["signal"] == "LONG"
    assert row["atr_to_stop_ratio"] == 0.9
    assert row["signal_bar_timestamp"] == "2026-09-20T00:00:00+00:00"
    assert row["holding_minutes"] == 150.0
    assert row["quantity"] == 2.0
    assert row["notional"] == 200.0
    assert row["stop_loss_pct"] == 0.02


def test_build_trade_rows_marks_discarded_r_as_unreliable():
    """resolve_closed_trades가 부호 모순으로 R을 버린 거래는 빈 칸이 아니라 no로 드러나야 한다 —
    2026-09-07 ZEC 재진입 루프처럼 진입가가 안 맞는 기록이 실제로 있고, 빈 칸만 보면 옛 기록과
    구분이 안 돼서 "R이 없는 이유"를 CSV에서 판단할 수 없다."""
    entries = [
        _entered("ZEC/USDT:USDT", "2026-09-07T01:00:00+00:00", entry=100.0, stop=98.0),
        # 가격으로 재면 +2R인데 실현손익은 마이너스 — 저널의 진입가가 이 청산과 안 맞는 경우.
        _closed("ZEC/USDT:USDT", "2026-09-07T02:00:00+00:00", 104.0, -1.53,
                side="long", entry_price=100.0, stop_loss_price=98.0),
    ]
    row, = build_trade_rows(entries, "live")

    assert row["realized_r"] is None
    assert row["r_reliable"] == "no"
    # 달러 손익은 그대로 남는다(대시보드와 같은 기준).
    assert row["realized_pnl"] == -1.53


def test_build_trade_rows_flags_unknown_r_for_legacy_records():
    """2026-09-09 이전 청산 기록은 방향도 손절가도 없어 R 복원 자체가 불가능하다 → unknown."""
    entries = [_closed("ETH/USDT:USDT", "2026-08-01T00:00:00+00:00", 2400.0, -12.0,
                       reason="stop_loss")]
    row, = build_trade_rows(entries, "demo")

    assert row["realized_r"] is None
    assert row["r_reliable"] == "unknown"


def test_build_trade_rows_excludes_manual_trades():
    """summarize_performance와 같은 기준 — 전략이 판단조차 안 한 수동 거래는 표에서도 빠진다."""
    entries = [
        _entered("SOL/USDT:USDT", "2026-09-20T01:00:00+00:00"),
        _closed("SOL/USDT:USDT", "2026-09-20T02:00:00+00:00", 104.0, 5.0, reason="manual"),
        _closed("SOL/USDT:USDT", "2026-09-20T03:00:00+00:00", 98.0, -3.0, reason="stop_loss"),
    ]
    rows = build_trade_rows(entries, "demo")

    assert [row["reason"] for row in rows] == ["stop_loss"]


def test_build_trade_rows_pairs_context_in_order_across_symbols():
    """심볼이 섞여 들어와도 각 청산에 자기 심볼의 진입 맥락이 붙어야 한다(인덱스 짝짓기가
    resolve_closed_trades의 순회 순서와 어긋나면 신호/수량이 다른 거래로 새어 들어간다)."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", signal="LONG",
                 execution={"quantity": 1.0}),
        _entered("ETH/USDT:USDT", "2026-09-20T01:30:00+00:00", signal="SHORT",
                 execution={"quantity": 5.0}),
        _closed("ETH/USDT:USDT", "2026-09-20T02:00:00+00:00", 96.0, 4.0),
        _closed("BTC/USDT:USDT", "2026-09-20T02:30:00+00:00", 104.0, 9.0),
    ]
    eth, btc = build_trade_rows(entries, "demo")

    assert (eth["symbol"], eth["signal"], eth["quantity"]) == ("ETH/USDT:USDT", "SHORT", 5.0)
    assert (btc["symbol"], btc["signal"], btc["quantity"]) == ("BTC/USDT:USDT", "LONG", 1.0)


def test_build_trade_rows_stamps_active_config_boundary():
    """거래마다 그 시점에 유효했던 설정 경계가 붙어야 한다 — 없으면 CSV의 총R이 필터 전후를
    섞은 숫자인지 알 수 없다(config_changed를 남기기 시작한 이유와 같다)."""
    entries = [
        _closed("BTC/USDT:USDT", "2026-09-10T00:00:00+00:00", 104.0, 5.0),
        {"timestamp": "2026-09-12T11:00:00+00:00", "event": "config_changed",
         "config": {"sma_period": 10, "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
         "changes": {"sma_period": [8, 10]}},
        _closed("BTC/USDT:USDT", "2026-09-13T00:00:00+00:00", 104.0, 5.0),
    ]
    before, after = build_trade_rows(entries, "demo")

    assert before["config_timestamp"] is None
    assert after["config_timestamp"] == "2026-09-12T11:00:00+00:00"


def test_build_event_rows_keeps_non_trade_events():
    """거래소 거부나 서킷브레이커는 청산 표에 안 나오므로 events 쪽에서 세야 한다."""
    entries = [
        {"timestamp": "2026-09-20T01:00:00+00:00", "symbol": "TSLA/USDT:USDT",
         "event": "rejected_exchange_error", "reason": '{"code":-2027}'},
        {"timestamp": "2026-09-20T01:05:00+00:00", "event": "circuit_breaker_blocked",
         "reason": "consecutive_losses"},
    ]
    rows = build_event_rows(entries, "live")

    assert [row["event"] for row in rows] == ["rejected_exchange_error", "circuit_breaker_blocked"]
    assert rows[0]["env"] == "live"
    assert rows[0]["reason"] == '{"code":-2027}'


def test_build_config_rows_flattens_symbol_list():
    entries = [{"timestamp": "2026-09-12T11:00:00+00:00", "event": "config_changed",
                "config": {"sma_period": 10, "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
                "changes": {"sma_period": [8, 10]}, "first_record": True}]
    row, = build_config_rows(entries, "demo")

    assert row["sma_period"] == 10
    assert row["symbols"] == "BTC/USDT:USDT,ETH/USDT:USDT"
    assert row["changed_keys"] == "sma_period"
    assert row["first_record"] is True


def test_write_csv_writes_header_only_when_no_rows(tmp_path):
    """행이 없어도 헤더는 남아야 한다 — 파일이 없으면 실패와 "거래 없음"이 구분되지 않는다."""
    path = write_csv([], tmp_path / "trades_live.csv", export_log.TRADE_COLUMNS)

    assert _read_csv(path) == []
    with open(path, encoding="utf-8-sig") as f:
        assert f.readline().strip().split(",") == export_log.TRADE_COLUMNS


def test_export_env_writes_all_files_and_copies_raw_journal(tmp_path, monkeypatch):
    journal_path = tmp_path / "futures_rule_trades.live.jsonl"
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", execution={"quantity": 2.0}),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 104.0, 8.0,
                side="long", entry_price=100.0, stop_loss_price=98.0, realized_r=2.0),
    ]
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    stats_path = tmp_path / "futures_rule_filter_stats.live.json"
    stats_path.write_text('{"2026-09-20": {"skipped_low_volatility": 3}}', encoding="utf-8")
    monkeypatch.setitem(export_log.FILTER_STATS, "live", str(stats_path))

    out = tmp_path / "out"
    summary = export_env("live", out, journal_path=str(journal_path))

    assert summary["events"] == 2
    assert summary["trades"] == 1
    assert summary["trades_with_r"] == 1
    assert summary["total_r"] == 2.0
    assert summary["realized_pnl"] == 8.0
    assert summary["last_trade"] == "2026-09-20T02:00:00+00:00"

    assert len(_read_csv(out / "trades_live.csv")) == 1
    assert len(_read_csv(out / "events_live.csv")) == 2
    # 원본은 손실 없이 그대로 복사돼야 한다(CSV는 중첩 필드를 버리므로 재생성 원본이 필요하다).
    assert (out / "raw" / journal_path.name).read_text(encoding="utf-8") == \
        journal_path.read_text(encoding="utf-8")
    assert (out / "raw" / stats_path.name).exists()


def test_export_all_writes_combined_file_sorted_by_exit_time(tmp_path, monkeypatch):
    demo = tmp_path / "demo.jsonl"
    live = tmp_path / "live.jsonl"
    demo.write_text(json.dumps(
        _closed("BTC/USDT:USDT", "2026-09-20T05:00:00+00:00", 104.0, 8.0)) + "\n", encoding="utf-8")
    live.write_text(json.dumps(
        _closed("ETH/USDT:USDT", "2026-09-20T01:00:00+00:00", 96.0, -4.0)) + "\n", encoding="utf-8")
    monkeypatch.setattr(export_log, "JOURNALS", {"demo": str(demo), "live": str(live)})
    monkeypatch.setattr(export_log, "FILTER_STATS", {})

    out = tmp_path / "out"
    manifest = export_all(out)

    rows = _read_csv(out / "trades_all.csv")
    assert [(row["env"], row["symbol"]) for row in rows] == [
        ("live", "ETH/USDT:USDT"), ("demo", "BTC/USDT:USDT")]
    assert [s["env"] for s in manifest["envs"]] == ["demo", "live"]
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["envs"][0]["trades"] == 1


def test_export_all_handles_missing_journal(tmp_path, monkeypatch):
    """실계좌를 아직 한 번도 안 돌린 상태에서도 내보내기가 죽지 않아야 한다."""
    monkeypatch.setattr(export_log, "JOURNALS",
                        {"demo": str(tmp_path / "nope.jsonl"), "live": str(tmp_path / "nope2.jsonl")})
    monkeypatch.setattr(export_log, "FILTER_STATS", {})

    manifest = export_all(tmp_path / "out")

    assert all(s["trades"] == 0 and s["total_r"] is None for s in manifest["envs"])
    assert (tmp_path / "out" / "trades_all.csv").exists()


# ------------------------------------------- 실제 거래비용 칼럼 (2026-09-22)


def test_trade_rows_carry_net_r_and_mark_estimated_fees():
    """수수료 실측이 없는 옛 기록은 설정 수수료율로 추정하되, 추정임을 표에 밝힌다."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", entry=100.0, stop=98.75),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 102.5, 20.0,
                side="long", entry_price=100.0, stop_loss_price=98.75, realized_r=2.0),
    ]
    row, = build_trade_rows(entries, "demo")

    assert row["realized_r"] == 2.0
    assert row["net_realized_r"] == round(2.0 - 2 * FEE_PCT_PER_SIDE / 0.0125, 4)
    assert row["fee_estimated"] == "yes"


def test_trade_rows_use_the_recorded_fee_when_present():
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", entry=100.0, stop=98.75),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 102.5, 20.0,
                side="long", entry_price=100.0, stop_loss_price=98.75, realized_r=2.0,
                total_fee=0.8, entry_fee=0.4, exit_fee=0.4, fee_r=0.05,
                net_realized_r=1.95, net_realized_pnl=19.2),
    ]
    row, = build_trade_rows(entries, "live")

    assert row["total_fee"] == 0.8
    assert row["fee_r"] == 0.05
    assert row["net_realized_r"] == 1.95
    assert row["net_realized_pnl"] == 19.2
    assert row["fee_estimated"] == "no"


def test_trade_rows_measure_entry_slippage_against_the_signal_bar_close():
    """저널의 entry_price는 신호 봉 종가이고 actual_entry_price가 실제 체결가다 — 그 차이가
    realized_r에 빠져 있는 또 하나의 비용이므로 R로 재서 싣는다."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", entry=100.0, stop=98.75),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 102.5, 20.0,
                side="long", entry_price=100.0, stop_loss_price=98.75, realized_r=2.0,
                actual_entry_price=100.25),
    ]
    row, = build_trade_rows(entries, "demo")

    # 롱인데 0.25 비싸게 샀다 -> 리스크 1.25 대비 +0.2R 불리.
    assert row["entry_slippage_r"] == 0.2
    assert row["actual_entry_price"] == 100.25


def test_entry_slippage_sign_flips_for_shorts():
    """숏은 더 낮은 가격에 팔면 불리하다 — 부호를 방향에 맞춰 뒤집어야 한다."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", signal="SHORT",
                 entry=100.0, stop=101.25),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 97.5, 20.0,
                side="short", entry_price=100.0, stop_loss_price=101.25, realized_r=2.0,
                actual_entry_price=99.75),
    ]
    row, = build_trade_rows(entries, "live")

    assert row["entry_slippage_r"] == 0.2


def test_export_env_summary_reports_net_r_alongside_gross(tmp_path, monkeypatch):
    journal_path = tmp_path / "j.jsonl"
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", entry=100.0, stop=98.75),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 102.5, 20.0,
                side="long", entry_price=100.0, stop_loss_price=98.75, realized_r=2.0),
    ]
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    monkeypatch.setattr(export_log, "FILTER_STATS", {})

    summary = export_env("demo", tmp_path / "out", journal_path=str(journal_path))

    assert summary["total_r"] == 2.0
    assert summary["total_net_r"] == round(2.0 - 2 * FEE_PCT_PER_SIDE / 0.0125, 4)


def test_exit_slippage_measures_take_profit_fill_against_the_trigger_price():
    """익절 조건주문은 발동 뒤 시장가로 나간다 — 가격이 스치고 되돌아가면 발동가보다 나쁘게
    체결된다(2026-09-27 실계좌 SUI). 백테스트는 익절가 정확히 체결을 가정하므로 그 차이를 잰다."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", entry=100.0, stop=98.0, take=104.0),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 103.5, 7.0,
                side="long", entry_price=100.0, stop_loss_price=98.0, take_profit_price=104.0,
                realized_r=1.75, actual_entry_price=100.0, fee_r=0.05),
    ]
    row, = build_trade_rows(entries, "live")

    # 롱 청산은 파는 주문 — 104에 팔려야 했는데 103.5에 팔렸다 -> 리스크 2 대비 +0.25R 불리.
    assert row["exit_slippage_r"] == 0.25
    # 총비용 = 진입 0 + 청산 0.25 + 수수료 0.05
    assert row["execution_cost_r"] == 0.3


def test_exit_slippage_sign_flips_for_short_stop_losses():
    """숏 손절은 사는 주문 — 손절가보다 비싸게 사면 불리하다."""
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00", signal="SHORT",
                 entry=100.0, stop=102.0, take=96.0),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 102.1, -4.2, reason="stop_loss",
                side="short", entry_price=100.0, stop_loss_price=102.0, take_profit_price=96.0,
                realized_r=-1.05),
    ]
    row, = build_trade_rows(entries, "live")

    assert row["exit_slippage_r"] == 0.05
    # 진입 체결가가 없는 옛 기록은 총비용을 내지 않는다 — 한쪽만 더하면 과소평가다.
    assert row["execution_cost_r"] is None


def test_exit_slippage_is_blank_when_the_exit_reason_is_unknown():
    entries = [
        _entered("BTC/USDT:USDT", "2026-09-20T01:00:00+00:00"),
        _closed("BTC/USDT:USDT", "2026-09-20T02:00:00+00:00", 101.0, 2.0, reason="unknown",
                side="long", entry_price=100.0, stop_loss_price=98.0, take_profit_price=104.0,
                realized_r=0.5),
    ]
    row, = build_trade_rows(entries, "demo")

    assert row["exit_slippage_r"] is None
