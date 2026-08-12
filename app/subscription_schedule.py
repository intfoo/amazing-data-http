"""订阅时段判定：决定是否启动 SDK 快照订阅。

非交易时段不持有订阅会话，CPU 降 ~60%（每日省 ~16 小时 × 1 核）。
窗口时间可通过 Config 的 subscription_open / subscription_close 配置。

日历兜底：SDK get_calendar() 返回的交易日历可能不含今天（数据延迟），
calendar_fallback_weekday=True 时用 weekday 兜底（周一~周五视为交易日）。
无法识别节假日，但避免日历延迟导致交易时段订阅永不启动。
"""

import datetime
from zoneinfo import ZoneInfo

_CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime.datetime:
    """当前时间（Asia/Shanghai，tz-aware）。窗口判定统一用显式时区，不依赖系统 TZ。"""
    return datetime.datetime.now(_CN_TZ)


def parse_hhmm(s: str) -> datetime.time:
    """'09:00' → datetime.time(9, 0)"""
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def is_subscription_window(
    now: datetime.datetime,
    calendar: list[int] | None,
    open_time: str = "09:00",
    close_time: str = "15:20",
    calendar_fallback_weekday: bool = True,
) -> bool:
    """是否应启动订阅：交易日 且 在 [open_time, close_time] 窗口内。

    calendar 为 None/空时走 weekday 兜底（calendar_fallback_weekday=False 时返回 False）。
    日历不含今天时：
    - calendar_fallback_weekday=True（默认）：用 weekday 兜底，周一~周五视为交易日
      （无法识别节假日，但避免 SDK 日历数据延迟导致交易时段订阅永不启动）
    - calendar_fallback_weekday=False：返回 False（严格按日历）
    """
    if not calendar:
        # calendar 为 None（登录前/重连失败保留期）：weekday 兜底，
        # 避免日历缺失导致盘中误判"不在窗口"杀订阅清缓存（2026-08-12 事故）。
        if not calendar_fallback_weekday:
            return False
        if now.weekday() >= 5:
            return False
        t = now.time()
        return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
    today_int = int(now.strftime("%Y%m%d"))
    if today_int not in calendar:
        if not calendar_fallback_weekday:
            return False
        # SDK 日历可能不含今天（数据延迟），用 weekday 兜底
        # 周一=0..周日=6，周六日非交易日（节假日无法识别，但优于完全不启动）
        if now.weekday() >= 5:
            return False
        # 落入工作日，继续检查时间窗口
    t = now.time()
    return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
