from dataclasses import dataclass
from datetime import datetime
import time

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


def test_clear_cache_empties_cache():
    """clear_cache 清空订阅缓存。"""
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(_snap(last=10.0))
    assert len(svc.snapshot()) == 1
    svc.clear_cache()
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


# ---- 用例 3：无 codes 时不触发全市场 query_snapshot（避免阻塞服务）----

def test_fallback_no_codes_does_not_query_all_market():
    """fallback_snapshot(codes=None) 不触发全市场 query_snapshot，返回空。

    全市场查询耗时数分钟且持 gateway._lock 阻塞 /daily /minute，故禁用。
    无 codes 时直接返回空（或旧缓存），不查 SDK。
    """
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    result = svc.fallback_snapshot()
    assert result == []
    # 不应调用 query_snapshot
    assert not hasattr(gw, "snapshot_query_calls") or len(gw.snapshot_query_calls) == 0


# ---- 用例 5：fallback_snapshot codes 非空时用用户 codes ----

def test_fallback_uses_user_codes_when_provided():
    """fallback_snapshot(codes=["000001.SZ"]) 应使用用户传入 codes，并透传收盘窄窗口时间参数。"""
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    svc.fallback_snapshot(["000001.SZ"])
    assert hasattr(gw, "snapshot_query_calls")
    assert len(gw.snapshot_query_calls) == 1
    assert gw.snapshot_query_calls[0]["codes"] == ["000001.SZ"]
    # 验证 begin_time/end_time 收盘窄窗口被透传（避免拉全量逐笔）
    assert gw.snapshot_query_calls[0]["begin_time"] == 145900000
    assert gw.snapshot_query_calls[0]["end_time"] == 150100000


# ---- 用例 6：singleflight 并发去重 ----

def test_fallback_singleflight_dedupes_concurrent_queries():
    """并发调 fallback_snapshot 时，singleflight 应保证只发起一次 query_snapshot。

    第一个查询进行中时，其他并发请求返回旧缓存/空，不重复查询、不堆积抢 gateway._lock。
    """
    import threading
    import time as _time

    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    # 用带延迟的 query_snapshot 模拟慢查询
    original = gw.query_snapshot
    call_count = [0]
    cnt_lock = threading.Lock()

    def slow_query(codes, trade_date=None, begin_time=None, end_time=None):
        with cnt_lock:
            call_count[0] += 1
        _time.sleep(0.2)  # 模拟 SDK 慢查询
        return original(codes, trade_date, begin_time, end_time)

    gw.query_snapshot = slow_query

    results = [None, None]
    threads = []
    for i in range(2):
        def worker(idx=i):
            results[idx] = svc.fallback_snapshot(["000001.SZ"])
        t = threading.Thread(target=worker)
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # 只应有一次 query_snapshot 调用（singleflight 去重）
    assert call_count[0] == 1, f"expected 1 query, got {call_count[0]}"
    # 两个线程都应拿到结果（空列表，FakeGateway 返回 {}）
    for r in results:
        assert r == []


def test_snapshot_large_cache_returns_correct_count():
    """大量 code 缓存时 snapshot 返回数量正确（验证移出锁后不丢数据）。"""
    svc = RealtimeService(gateway=None)
    for i in range(100):
        svc.on_snapshot(_snap(code=f"{i:06d}.SZ"))
    result = svc.snapshot()
    assert len(result) == 100


def test_fallback_concat_takes_last_row_per_code():
    """fallback 应对每只股票取最后一行（最新快照），多只合并序列化。"""
    import pandas as pd
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    gw.query_snapshot = lambda codes, **kw: {
        "000001.SZ": pd.DataFrame({
            "code": ["000001.SZ", "000001.SZ"],
            "last": [10.0, 10.3],
            "trade_time": [pd.Timestamp("2024-01-02T09:30"), pd.Timestamp("2024-01-02T15:00")],
        }),
        "600000.SH": pd.DataFrame({
            "code": ["600000.SH"],
            "last": [20.5],
            "trade_time": [pd.Timestamp("2024-01-02T15:00")],
        }),
    }
    result = svc.fallback_snapshot(["000001.SZ", "600000.SH"])
    assert len(result) == 2
    by_code = {r["code"]: r for r in result}
    assert by_code["000001.SZ"]["last"] == 10.3  # 取最后一行
    assert by_code["600000.SH"]["last"] == 20.5


def test_snapshot_to_dict_caches_per_type():
    """同类型多次调用应复用提取函数，混合类型（股票+指数）不串类型。"""
    svc = RealtimeService(gateway=None)
    # 交替推送股票和指数快照，验证类型缓存不混淆
    for i in range(10):
        svc.on_snapshot(_snap(code=f"{i:06d}.SZ"))
        svc.on_snapshot(_index_snap(code=f"{i:06d}.SH"))
    result = svc.snapshot()
    stock = [r for r in result if r["code"].endswith(".SZ")]
    index = [r for r in result if r["code"].endswith(".SH")]
    assert len(stock) == 10
    assert len(index) == 10
    # 指数快照含 trading_phase_code，股票不含
    assert "trading_phase_code" in index[0]
    assert "trading_phase_code" not in stock[0]


# ---------------------------------------------------------------------------
# Watchdog 测试
# ---------------------------------------------------------------------------

def test_on_snapshot_auto_recovery():
    """订阅曾被标记 inactive 但收到数据 → 自动恢复 active。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(False)
    svc._deactivation_reason = "stale"
    assert svc.is_active() is False
    svc.on_snapshot(_snap(last=10.0))
    assert svc.is_active() is True
    assert svc.deactivation_reason() is None


def test_on_subscription_error_sets_reason():
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.on_subscription_error(Exception("test"))
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "error"


def test_set_active_true_clears_reason():
    svc = RealtimeService(gateway=None)
    svc._deactivation_reason = "stale"
    svc.set_active(True)
    assert svc.deactivation_reason() is None


def test_deactivation_reason_default_none():
    svc = RealtimeService(gateway=None)
    assert svc.deactivation_reason() is None


def test_last_snapshot_ts_default_zero():
    svc = RealtimeService(gateway=None)
    assert svc.last_snapshot_ts() == 0.0


def test_on_snapshot_updates_timestamp():
    import time as _time
    svc = RealtimeService(gateway=None)
    t_before = _time.time()
    svc.on_snapshot(_snap(last=10.0))
    t_after = _time.time()
    assert t_before <= svc.last_snapshot_ts() <= t_after


def test_watchdog_stale_marks_inactive():
    """窗口期内超过 stale_threshold 无数据 → 标记 inactive + reason=stale。"""
    import datetime
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.on_snapshot(_snap())  # 收到一次数据，设置 timestamp
    svc._last_snapshot_ts = time.time() - 200  # 回拨到200秒前（超过90s阈值）
    cal = [int(datetime.datetime.now().strftime("%Y%m%d"))]
    svc._watchdog_loop(cal, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "stale"


def test_watchdog_first_data_timeout_marks_inactive():
    """订阅启动后 stale_threshold 内未收到任何数据 → 标记 inactive。"""
    import datetime
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = 0.0
    svc._watchdog_start_ts = time.time() - 200
    cal = [int(datetime.datetime.now().strftime("%Y%m%d"))]
    svc._watchdog_loop(cal, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "stale"


def test_watchdog_non_window_does_not_trigger():
    """非窗口期不判 stale（线程方式：watchdog 空转 continue，stop 可退出）。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = time.time() - 200
    svc.start_watchdog([20231231], stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="09:00", close_time="15:20",
                       calendar_fallback_weekday=False)
    time.sleep(0.05)  # 跑若干次迭代（非窗口期 continue，不判 stale）
    assert svc.is_active() is True
    svc.stop_watchdog()
    svc._watchdog_thread.join(timeout=2)


def test_watchdog_calendar_none_does_not_trigger():
    """calendar 为 None 时不触发（线程方式，避免同步调用死循环）。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = time.time() - 200
    svc.start_watchdog(None, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    time.sleep(0.05)
    assert svc.is_active() is True
    svc.stop_watchdog()
    svc._watchdog_thread.join(timeout=2)


def test_stop_watchdog_sets_stop_flag():
    """stop_watchdog 设置 Event，watchdog 线程应能退出。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    svc.stop_watchdog()
    assert svc._stop_flag.is_set()
    if svc._watchdog_thread:
        svc._watchdog_thread.join(timeout=2)
        assert not svc._watchdog_thread.is_alive()


def test_start_watchdog_idempotent():
    """重复调用 start_watchdog 不启动多个线程。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    t1 = svc._watchdog_thread
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    assert svc._watchdog_thread is t1
    svc.stop_watchdog()
    if t1:
        t1.join(timeout=2)
