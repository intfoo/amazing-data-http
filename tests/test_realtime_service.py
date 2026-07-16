from dataclasses import dataclass
from datetime import datetime

from app.realtime_service import RealtimeService
from tests.conftest import FakeGateway


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


def test_snapshot_to_dict_plain_object_with_dict():
    """非 dataclass 的普通对象（有 __dict__）应走第二级 vars() 降级。"""
    class PlainSnapshot:
        def __init__(self):
            self.code = "000001.SZ"
            self.last = 10.3
            self.trade_time = datetime(2024, 1, 2, 9, 30)
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(PlainSnapshot())
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["code"] == "000001.SZ"
    assert data[0]["last"] == 10.3
    assert data[0]["trade_time"] == "2024-01-02T09:30:00"


def test_snapshot_to_dict_slots_object():
    """用 __slots__ 的对象应走第三级 MRO slots 遍历降级。"""
    class SlotsSnapshot:
        __slots__ = ("code", "last", "trade_time")
        def __init__(self):
            self.code = "600000.SH"
            self.last = 20.5
            self.trade_time = datetime(2024, 1, 2, 10, 0)
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(SlotsSnapshot())
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["code"] == "600000.SH"
    assert data[0]["last"] == 20.5
    assert data[0]["trade_time"] == "2024-01-02T10:00:00"


# ---------------------------------------------------------------------------
# 指数实时数据支持测试（设计文档 §10 测试策略）
# ---------------------------------------------------------------------------


@dataclass
class FakeIndexSnapshot:
    """模拟 SDK 指数快照对象（11 字段，无 5 档 ask/bid）。"""

    code: str
    trade_time: datetime
    last: float
    pre_close: float
    high: float
    open: float
    low: float
    close: float
    volume: int
    amount: float
    trading_phase_code: str


def _index_snap(code="000001.SH", last=3200.0):
    return FakeIndexSnapshot(
        code=code,
        trade_time=datetime(2024, 1, 2, 9, 30),
        last=last,
        pre_close=3150.0,
        high=3210.0,
        open=3155.0,
        low=3148.0,
        close=last,
        volume=0,
        amount=0.0,
        trading_phase_code="T",
    )


# ---- 用例 1：指数快照能被 on_snapshot 缓存 ----

def test_on_snapshot_caches_index_snapshot():
    """on_snapshot 收到指数风格对象（11 字段）应正确缓存，字段齐全。"""
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_index_snap(last=3200.5))
    data = svc.snapshot()
    assert len(data) == 1
    rec = data[0]
    assert rec["code"] == "000001.SH"
    assert rec["last"] == 3200.5
    # 验证指数特有的 trading_phase_code 字段存在
    assert rec["trading_phase_code"] == "T"
    # 验证 11 个字段全部存在
    expected_fields = {
        "code", "trade_time", "last", "pre_close", "high", "open",
        "low", "close", "volume", "amount", "trading_phase_code",
    }
    assert expected_fields.issubset(rec.keys())
    # 验证指数不应有的字段确实不存在
    assert "ask_price1" not in rec
    assert "bid_volume5" not in rec


# ---- 用例 2：snapshot 混合返回股票+指数 ----

def test_snapshot_mixed_stock_and_index():
    """snapshot() 应能同时返回股票（35 字段）和指数（11 字段）混合数据。"""
    svc = RealtimeService(gateway=None)
    # 手动往 _cache 塞一个股票 dict（含 ask_price1 等 35 字段）
    stock_dict = {
        "code": "000001.SZ", "trade_time": "2024-01-02T09:30:00",
        "pre_close": 9.9, "last": 10.3, "open": 9.9, "high": 10.5,
        "low": 9.8, "close": 10.3, "volume": 1000, "amount": 10300.0,
        "num_trades": 50, "high_limited": 10.89, "low_limited": 8.91,
        "ask_price1": 10.3, "ask_price2": 10.31, "ask_price3": 10.32,
        "ask_price4": 10.33, "ask_price5": 10.34,
        "ask_volume1": 100, "ask_volume2": 200, "ask_volume3": 300,
        "ask_volume4": 400, "ask_volume5": 500,
        "bid_price1": 10.29, "bid_price2": 10.28, "bid_price3": 10.27,
        "bid_price4": 10.26, "bid_price5": 10.25,
        "bid_volume1": 110, "bid_volume2": 220, "bid_volume3": 330,
        "bid_volume4": 440, "bid_volume5": 550,
        "iopv": 0.0, "trading_phase_code": "T",
    }
    # 指数 dict（11 字段）
    index_dict = {
        "code": "000001.SH", "trade_time": "2024-01-02T09:30:00",
        "last": 3200.0, "pre_close": 3150.0, "high": 3210.0,
        "open": 3155.0, "low": 3148.0, "close": 3200.0,
        "volume": 0, "amount": 0.0, "trading_phase_code": "T",
    }
    with svc._lock:
        svc._cache["000001.SZ"] = stock_dict
        svc._cache["000001.SH"] = index_dict

    # 不传 codes 返回两条
    data = svc.snapshot()
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "000001.SH"}

    # 传指数 code 只返回指数那条
    data_index = svc.snapshot(["000001.SH"])
    assert len(data_index) == 1
    assert data_index[0]["code"] == "000001.SH"
    # 确认指数记录没有 ask_price1
    assert "ask_price1" not in data_index[0]

    # 传股票 code 只返回股票那条
    data_stock = svc.snapshot(["000001.SZ"])
    assert len(data_stock) == 1
    assert data_stock[0]["code"] == "000001.SZ"
    assert "ask_price1" in data_stock[0]


# ---- 用例 3：set_combined_code_list 注入后 fallback 用缓存列表 ----

def test_fallback_uses_combined_code_list_when_set():
    """set_combined_code_list 注入后 fallback_snapshot(codes=None) 应使用缓存列表，不调 get_realtime_code_list。"""
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    combined = ["000001.SZ", "000001.SH"]
    svc.set_combined_code_list(combined)
    svc.fallback_snapshot()
    # query_snapshot 被调用时传入的 code_list 应为 combined
    assert hasattr(gw, "snapshot_query_calls")
    assert len(gw.snapshot_query_calls) == 1
    assert gw.snapshot_query_calls[0]["codes"] == combined


# ---- 用例 4：fallback_snapshot codes=None 且未 set_combined_code_list 时调 get_realtime_code_list ----

def test_fallback_calls_get_realtime_code_list_without_combined():
    """未调 set_combined_code_list 时 fallback_snapshot(codes=None) 应调 get_realtime_code_list()。"""
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    svc.fallback_snapshot()
    # query_snapshot 应收到 get_realtime_code_list() 的结果（4 个代码）
    assert hasattr(gw, "snapshot_query_calls")
    assert len(gw.snapshot_query_calls) == 1
    expected = gw.get_realtime_code_list()
    assert gw.snapshot_query_calls[0]["codes"] == expected
    assert set(expected) == {"000001.SZ", "600000.SH", "000001.SH", "399001.SZ"}


# ---- 用例 5：fallback_snapshot codes 非空时用用户 codes ----

def test_fallback_uses_user_codes_when_provided():
    """fallback_snapshot(codes=["000001.SZ"]) 应使用用户传入 codes，不走 combined 或 get_realtime_code_list。"""
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    # 即使设置了 combined_code_list，codes 非空时应优先用用户 codes
    svc.set_combined_code_list(["000001.SZ", "000001.SH", "399001.SZ"])
    svc.fallback_snapshot(["000001.SZ"])
    assert hasattr(gw, "snapshot_query_calls")
    assert len(gw.snapshot_query_calls) == 1
    assert gw.snapshot_query_calls[0]["codes"] == ["000001.SZ"]
