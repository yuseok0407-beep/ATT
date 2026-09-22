from src.execution import strategy_versions as sv

N_HIST = len(sv.HISTORICAL_VERSIONS)


def _config(ts, changes=None, first=False, config=None):
    return {"timestamp": ts, "event": "config_changed", "changes": changes or {},
            "first_record": first, "config": config or {"stop_loss_pct": 0.0125}}


def _trade(entry_ts, exit_ts, r, pnl, symbol="BTC/USDT:USDT"):
    return [
        {"timestamp": entry_ts, "event": "entered", "symbol": symbol, "signal": "LONG",
         "entry_price": 100.0, "stop_loss_price": 98.75},
        {"timestamp": exit_ts, "event": "closed", "symbol": symbol, "reason": "take_profit",
         "side": "long", "entry_price": 100.0, "stop_loss_price": 98.75,
         "realized_r": r, "realized_pnl": pnl, "entry_timestamp": entry_ts},
    ]


def test_first_record_only_attaches_a_snapshot_and_does_not_bump_the_version():
    """추적을 시작한 기록은 규칙이 바뀐 게 아니다 — 버전을 올리면 개정 횟수가 부풀려진다."""
    timeline = sv.version_timeline([_config("2026-09-12T11:00:00+00:00", first=True)])
    assert len(timeline) == N_HIST
    assert timeline[-1]["config"] == {"stop_loss_pct": 0.0125}


def test_a_real_value_change_is_a_new_version():
    entries = [
        _config("2026-09-12T11:00:00+00:00", first=True),
        _config("2026-09-22T05:37:00+00:00", {"max_entry_price_drift_r": {"from": 0.5, "to": 0.1}}),
    ]
    timeline = sv.version_timeline(entries)
    assert len(timeline) == N_HIST + 1
    assert timeline[-1]["label"] == f"v{N_HIST + 1}"
    assert "0.5→0.1" in timeline[-1]["detail"]


def test_adding_a_tracked_key_is_not_a_rule_change():
    """current_strategy_config()에 항목을 새로 넣으면 from=None 기록이 생긴다 — 규칙 변경이 아니다."""
    entries = [_config("2026-09-23T00:00:00+00:00", {"logic_revision": {"from": None, "to": 3}})]
    assert len(sv.version_timeline(entries)) == N_HIST


def test_trades_are_assigned_by_entry_time_not_exit_time():
    """청산이 재시작 뒤여도 그 거래를 만든 건 진입 때의 규칙이다."""
    change = "2026-09-22T05:37:00+00:00"
    entries = (_trade("2026-09-21T00:00:00+00:00", "2026-09-22T09:00:00+00:00", 2.0, 20.0)
               + [_config(change, {"take_profit_rr": {"from": 2.0, "to": 1.5}})]
               + _trade("2026-09-22T10:00:00+00:00", "2026-09-22T12:00:00+00:00", -1.0, -10.0, "ETH/USDT:USDT"))
    summary = sv.summarize_by_version(entries)
    by_label = {v["label"]: v for v in summary["versions"]}

    before, after = by_label[f"v{N_HIST}"], by_label[f"v{N_HIST + 1}"]
    assert (before["trades"], before["total_r"]) == (1, 2.0)
    assert (after["trades"], after["total_r"]) == (1, -1.0)
    assert after["win_rate"] == 0.0
    assert summary["current"] == f"v{N_HIST + 1}"
    assert summary["revisions"] == N_HIST


def test_net_r_subtracts_fees_per_version():
    entries = _trade("2026-09-21T00:00:00+00:00", "2026-09-21T02:00:00+00:00", 2.0, 20.0)
    entries[1]["net_realized_r"] = 1.93
    v = next(v for v in sv.summarize_by_version(entries)["versions"] if v["trades"])
    assert v["total_net_r"] == 1.93 and v["avg_net_r"] == 1.93


def test_trades_before_the_first_version_are_counted_as_unassigned():
    entries = _trade("2026-08-01T00:00:00+00:00", "2026-08-01T02:00:00+00:00", 2.0, 20.0)
    assert sv.summarize_by_version(entries)["unassigned_trades"] == 1


def test_old_closes_without_entry_timestamp_use_the_entered_record():
    """2026-09-09 이전 청산엔 entry_timestamp가 없다 — 진입 기록에서 채워야 버전이 맞게 붙는다."""
    change = "2026-09-22T05:37:00+00:00"
    entries = _trade("2026-09-21T00:00:00+00:00", "2026-09-22T09:00:00+00:00", 2.0, 20.0)
    del entries[1]["entry_timestamp"]
    entries = entries[:1] + [_config(change, {"take_profit_rr": {"from": 2.0, "to": 1.5}})] + entries[1:]
    by_label = {v["label"]: v for v in sv.summarize_by_version(entries)["versions"]}
    assert by_label[f"v{N_HIST}"]["trades"] == 1


def test_historical_versions_are_in_time_order():
    starts = [v["start"] for v in sv.HISTORICAL_VERSIONS]
    assert starts == sorted(starts)
