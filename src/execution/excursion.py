import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "state/futures_rule_excursion.json"
LIVE_DEFAULT_PATH = "state/futures_rule_excursion.live.json"  # 실계좌 봇 전용


def _load(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("excursion file %s is unreadable — starting over", path)
        return {}


def _save(data: dict, path: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def to_r(price: float, entry_price: float, stop_loss_price: float, side: str) -> float | None:
    """가격을 R배수로 — 손절가까지가 정확히 -1R. 손익($)이 아니라 가격 기준으로 재는 이유는
    수량/레버리지와 무관해서 백테스트의 R과 곧바로 비교되기 때문이다."""
    try:
        risk = abs(float(entry_price) - float(stop_loss_price))
    except (TypeError, ValueError):
        return None
    if risk <= 0:
        return None
    move = float(price) - float(entry_price)
    return move / risk if side == "long" else -move / risk


def update(symbol: str, *, entry_price, stop_loss_price, mark_price, side: str,
           path: str = DEFAULT_PATH) -> dict | None:
    """보유 중인 포지션의 최고 유리/불리 지점(MFE/MAE)을 R배수로 갱신한다. 감시 루프가 매
    사이클 호출하므로 30초 해상도로 기록된다.

    "익절 코앞까지 갔다가 손절났다"는 체감을 사후에 확인하려면 원래 5분봉을 다시 받아 경로를
    재구성해야 했다(2026-09-07에 실제로 그렇게 분석했음) — 어차피 매 사이클 현재가를 이미
    받고 있으니 그때그때 최고점만 굴려두면 청산 기록에 그대로 남길 수 있다.

    진입가/손절가가 직전과 다르면(=같은 심볼에 새 포지션이 열렸으면) 이전 기록을 버리고 새로
    시작한다 — 청산 시 pop()이 지우지만, 그게 실패했거나 봇이 모르는 사이 재진입된 경우에도
    이전 포지션의 최고점이 새 포지션에 섞이지 않게 하는 안전장치."""
    favorable = to_r(mark_price, entry_price, stop_loss_price, side)
    if favorable is None:
        return None

    data = _load(path)
    record = data.get(symbol)
    if (record is None or record.get("entry_price") != entry_price
            or record.get("stop_loss_price") != stop_loss_price):
        record = {"entry_price": entry_price, "stop_loss_price": stop_loss_price, "side": side,
                  "max_favorable_r": favorable, "max_adverse_r": favorable}
    else:
        record["max_favorable_r"] = max(record.get("max_favorable_r", favorable), favorable)
        record["max_adverse_r"] = min(record.get("max_adverse_r", favorable), favorable)

    record["current_r"] = favorable
    record["updated"] = datetime.now(timezone.utc).isoformat()
    data[symbol] = record
    _save(data, path)
    return record


def pop(symbol: str, path: str = DEFAULT_PATH) -> dict | None:
    """청산이 확정된 심볼의 기록을 꺼내면서 지운다 — 청산 저널에 옮겨 적고 나면 이 파일에
    남겨둘 이유가 없고, 남아 있으면 다음 포지션의 시작값을 오염시킨다."""
    data = _load(path)
    record = data.pop(symbol, None)
    if record is not None:
        _save(data, path)
    return record


def read_all(path: str = DEFAULT_PATH) -> dict:
    return _load(path)
