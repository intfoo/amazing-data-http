"""订阅时段判定：决定是否启动 SDK 快照订阅。

非交易时段不持有订阅会话，CPU 降 ~60%（每日省 ~16 小时 × 1 核）。
窗口时间可通过 Config 的 subscription_open / subscription_close 配置。
"""

import datetime


def parse_hhmm(s: str) -> datetime.time:
    """'09:00' → datetime.time(9, 0)"""
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def is_subscription_window(
    now: datetime.datetime,
    calendar: list[int] | None,
    open_time: str = "09:00",
    close_time: str = "15:20",
) -> bool:
    """是否应启动订阅：交易日 且 在 [open_time, close_time] 窗口内。

    calendar 为 None 或空时返回 False（login 前 / 无日历数据）。
    """
    if not calendar:
        return False
    today_int = int(now.strftime("%Y%m%d"))
    if today_int not in calendar:
        return False
    t = now.time()
    return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
