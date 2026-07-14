from dataclasses import dataclass
from datetime import datetime

from app.realtime_service import RealtimeService


@dataclass
class FakeSnapshot:
    code: str
    trade_time: datetime
    last: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int
    amount: float


def _snap(last=10.0, code="000001.SZ"):
    return FakeSnapshot(code=code, trade_time=datetime(2024, 1, 2, 9, 30),
                        last=last, pre_close=9.9, open=9.9, high=10.1,
                        low=9.8, volume=100, amount=1000.0)


def test_on_snapshot_stores_in_cache():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=10.3))
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["code"] == "000001.SZ"
    assert data[0]["last"] == 10.3


def test_snapshot_empty_cache():
    svc = RealtimeService(gateway=None)
    assert svc.snapshot() == []


def test_snapshot_overwrites_same_code():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=10.0))
    svc.on_snapshot(_snap(last=10.5))
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["last"] == 10.5


def test_snapshot_returns_shallow_copy():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=10.0))
    data = svc.snapshot()
    data[0]["last"] = 999.0
    assert svc.snapshot()[0]["last"] == 10.0


def test_on_subscription_error_deactivates():
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    assert svc.is_active() is True
    svc.on_subscription_error()
    assert svc.is_active() is False


def test_snapshot_to_dict_handles_nan():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=float("nan")))
    data = svc.snapshot()
    assert data[0]["last"] is None


def test_snapshot_to_dict_datetime_isoformat():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap())
    data = svc.snapshot()
    assert data[0]["trade_time"] == "2024-01-02T09:30:00"


def test_snapshot_multiple_codes():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=10.0, code="000001.SZ"))
    svc.on_snapshot(_snap(last=20.0, code="600000.SH"))
    data = svc.snapshot()
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "600000.SH"}
