# Gateway 拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把 `app/gateway.py`（1018 行）机械拆分为 `app/gateway/` mixin 包，行为零变化、导入面不变、335 测试零改动全绿。

**Architecture:** 见 spec `docs/superpowers/specs/2026-08-11-gateway-split-design.md`（已评审修订，含 PERIOD_MAP/logger 归属、__file__ 偏移修正）。单向依赖：mixin 只 import `base.py`/`paths.py`，mixin 间走 `self`。

**Tech Stack:** Python 3.13 / pytest

## Global Constraints

- **方法体零改动**（docstring/注释/日志随行平移）。仅允许的改动：①`_resolve_*_local_path` 实例方法 → `paths.py` 模块函数（`self._config.xxx` → 参数 `configured`）；②`__file__` 路径 `parent.parent` → `parents[2]`；③`__init__` 两处调用点改调模块函数
- `logger` 唯一定义在 `base.py`（`logging.getLogger("amazingdata.gateway")`），各 mixin `from app.gateway.base import logger`
- re-export 全清单：`AmazingDataGateway, Gateway, GatewayError, GatewayNotReadyError, GatewayQueryError, PERIOD_MAP, _is_connection_error, _is_sdk_corruption`
- 各 mixin 模块头部按需携带 stdlib import + `from app.gateway.base import ...`
- 包优先于模块：`app/gateway/__init__.py` 一旦存在即遮蔽 `app/gateway.py`，旧文件删除与 `__init__.py` 落地必须在同一 commit
- 不新增/不修改任何测试文件
- pytest 合并执行；输出被吞则落盘读取后删除
- commit 只 add 本任务文件，严禁 git add -A

---

### Task 1: base.py + paths.py + resilience.py + subscription.py

**Files:**
- Create: `app/gateway/base.py` — logger、7 个常量（PERIOD_MAP/_CONNECTION_KEYWORDS/_SDK_CORRUPTION_KEYWORDS/_RECONNECT_COOLDOWN_SEC/_DISCONNECT_DEDUP_SEC/ADJ_FACTOR_TIMEOUT_SEC/SDK_LOCK_TIMEOUT_SEC）、_is_connection_error、_is_sdk_corruption、Gateway Protocol、GatewayError/GatewayNotReadyError/GatewayQueryError
- Create: `app/gateway/paths.py` — `resolve_adj_factor_local_path(configured: str) -> str`、`resolve_fund_local_path(configured: str) -> str`（从实例方法平移，`Path(__file__).resolve().parent.parent` → `Path(__file__).resolve().parents[2]`，docstring 内"gateway.py 在 app/，parent.parent = 项目根"表述同步改为 parents[2]）
- Create: `app/gateway/resilience.py` — `ResilienceMixin`：`_call_sdk_with_timeout`（staticmethod）、`_sdk_lock`（contextmanager）；import：`threading`、`from contextlib import contextmanager`、`from app.gateway.base import logger, GatewayQueryError, SDK_LOCK_TIMEOUT_SEC`
- Create: `app/gateway/subscription.py` — `SubscriptionMixin`：`start_snapshot_subscription`、`stop_subscription`；import：`threading`、`from app.gateway.base import logger, GatewayNotReadyError, GatewayQueryError`

**源：** `app/gateway.py`（1018 行，只读）。常量/函数在 25-79 行；Protocol 82-122；异常 124-137；_resolve_* 166-225；_call_sdk_with_timeout 488-514；_sdk_lock 516-527；start_snapshot_subscription 762-812；stop_subscription 814-831。

- [ ] Step 1: 读 `app/gateway.py` 对应行段，逐字平移到 4 个新文件（每个 mixin 类带一句中文 docstring 说明职责）
- [ ] Step 2: 语法检查 `python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) for f in ['app/gateway/base.py','app/gateway/paths.py','app/gateway/resilience.py','app/gateway/subscription.py']]; print('OK')"`
- [ ] Step 3: 不 commit（由 Task 4 统一 commit；工作区留新文件即可）

---

### Task 2: session.py + tgw_events.py

**Files:**
- Create: `app/gateway/session.py` — `SessionMixin`：`login`/`_do_login`/`logout`/`_safe_logout`/`is_ready`/`calendar` property/`refresh_calendar`；import：`threading`（如用到）、`from app.gateway.base import logger, GatewayNotReadyError`
- Create: `app/gateway/tgw_events.py` — `TgwEventMixin`：`_schedule_reconnect`/`_should_log_disconnect`/`_install_tgw_event_logger`（含 logged_on_log/logged_on_event/logged_on_logon 三闭包）；import：`sys`、`threading`、`time`、`from app.gateway.base import logger, _RECONNECT_COOLDOWN_SEC, _DISCONNECT_DEDUP_SEC`

**源：** `app/gateway.py` 只读。login/logout 227-231/439-443；_do_login 232-276；_safe_logout 444-456；is_ready/calendar 458-465；refresh_calendar 467-486；_schedule_reconnect 278-305；_should_log_disconnect 307-317；_install_tgw_event_logger 319-437。

**注意：** `_do_login` 调 `self._install_tgw_event_logger()`（在 TgwEventMixin）、`login`/`refresh_calendar` 调 `self._sdk_lock()`（在 ResilienceMixin）——保持 `self.xxx` 调用原样，不 import 其他 mixin。

- [ ] Step 1: 读源行段，逐字平移到 2 个新文件
- [ ] Step 2: 语法检查（同 Task 1 模式）
- [ ] Step 3: 不 commit

---

### Task 3: query_market.py + query_basedata.py

**Files:**
- Create: `app/gateway/query_market.py` — `QueryMarketMixin`：`get_code_list`/`get_code_info`/`get_realtime_code_list`/`query_snapshot`/`query_kline`；import：`time`、`typing.Any`、`from app.gateway.base import logger, PERIOD_MAP, GatewayNotReadyError, GatewayQueryError, _is_connection_error, _is_sdk_corruption`
- Create: `app/gateway/query_basedata.py` — `QueryBaseDataMixin`：`get_adj_factor`/`get_fund_share`/`get_fund_nav`；import：`from app.gateway.base import logger, ADJ_FACTOR_TIMEOUT_SEC, GatewayNotReadyError, GatewayQueryError, _is_connection_error, _is_sdk_corruption`

**源：** `app/gateway.py` 只读。get_code_list 529-558；get_code_info 560-589；get_realtime_code_list 591-622；query_snapshot 624-686；query_kline 688-760；get_adj_factor 833-909；get_fund_share 911-964；get_fund_nav 966-1018。

**注意：** `self._sdk_lock()`/`self._call_sdk_with_timeout()`/`self._do_login()`/`self.refresh_calendar()` 保持 self 调用原样；`query_kline` 里的日历前置刷新段（调 `self.refresh_calendar()`）原样平移。

- [ ] Step 1: 读源行段，逐字平移到 2 个新文件
- [ ] Step 2: 语法检查（同 Task 1 模式）
- [ ] Step 3: 不 commit

---

### Task 4: __init__.py 组合 + 删旧文件 + 全量验证（必须在 Task 1-3 完成后）

**Files:**
- Create: `app/gateway/__init__.py`
- Delete: `app/gateway.py`

**`__init__.py` 内容：**

```python
"""AmazingData SDK 网关包：AmazingDataGateway 由各职责 Mixin 组合而成。

拆分自原 app/gateway.py（1018 行超单文件可维护阈值），行为零变化。
公共导入面（from app.gateway import ...）与本模块 re-export 保持一致。
"""

from app.config import Config
from app.gateway.base import (
    PERIOD_MAP,
    Gateway,
    GatewayError,
    GatewayNotReadyError,
    GatewayQueryError,
    _is_connection_error,
    _is_sdk_corruption,
)
from app.gateway.paths import (
    resolve_adj_factor_local_path,
    resolve_fund_local_path,
)
from app.gateway.query_basedata import QueryBaseDataMixin
from app.gateway.query_market import QueryMarketMixin
from app.gateway.resilience import ResilienceMixin
from app.gateway.session import SessionMixin
from app.gateway.subscription import SubscriptionMixin
from app.gateway.tgw_events import TgwEventMixin

__all__ = [
    "AmazingDataGateway",
    "Gateway",
    "GatewayError",
    "GatewayNotReadyError",
    "GatewayQueryError",
    "PERIOD_MAP",
    "_is_connection_error",
    "_is_sdk_corruption",
]


class AmazingDataGateway(
    SessionMixin,
    TgwEventMixin,
    ResilienceMixin,
    QueryMarketMixin,
    QueryBaseDataMixin,
    SubscriptionMixin,
):
    """AmazingData SDK 的真实封装实现（组合各职责 Mixin，原 gateway.py 文档注释随行）。

    管理进程级 SDK 会话：启动时 login + 创建 MarketData，
    后续请求复用同一会话，避免重复登录。SDK 对象非线程安全，
    所有操作通过 _lock 串行化（_sdk_lock 带 30s 竞争超时）。
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._ad = None           # AmazingData 模块引用
        self._market_data = None  # ad.MarketData 实例（含交易日历）
        self._ready = False       # 是否已登录且 MarketData 就绪
        self._base_data = None      # ad.BaseData 实例（供 get_code_list）
        self._calendar = None       # 交易日历 list[int]（供 query_snapshot 默认日期）
        self._subscribe_data = None  # ad.SubscribeData 实例
        self._sub_thread = None      # 订阅 daemon 线程
        self._adj_factor_local_path = resolve_adj_factor_local_path(config.adj_factor_local_path)
        self._info_data = None  # ad.InfoData 实例（供 get_fund_share/get_fund_nav）
        self._fund_local_path = resolve_fund_local_path(config.fund_local_path)
        # 主动重连状态（tgw 断线回调触发，后台线程执行）
        self._reconnect_lock = threading.Lock()
        self._reconnect_in_progress = False
        self._last_reconnect_attempt = 0.0
        self._last_disconnect_log: dict = {"msg": None, "ts": 0.0}
```

（顶部需补 `import threading`。`__init__` 状态清单以当前 `gateway.py:147-164` 为准逐字核对，上方为示意；两个 resolve 调用签名以 paths.py 实际为准。）

- [ ] Step 1: 核对 Task 1-3 落地的 8 个模块存在且 mixin 类名/方法齐全（对照 spec §2.1 清单 40 个符号逐一打勾）
- [ ] Step 2: 写 `__init__.py`（import threading + 组合类 + __init__ + re-export）
- [ ] Step 3: import 冒烟 + 全量测试：`python -c "from app.gateway import AmazingDataGateway, Gateway, GatewayError, GatewayNotReadyError, GatewayQueryError, PERIOD_MAP, _is_connection_error, _is_sdk_corruption; print('import OK')"` && `python -m pytest tests/ -q`（预期 335 passed）
- [ ] Step 4: 删除 `app/gateway.py`，再跑一次全量测试确认
- [ ] Step 5: Commit：`git add app/gateway/ app/gateway.py`（git 会自动记录删除）+ `git commit -m "refactor(gateway): 1018 行 gateway.py 拆分为 app/gateway/ mixin 包，行为零变化"`

---

## 验证

- 335 测试全绿（零改动）；import 冒烟通过；`app/gateway.py` 不存在
- 最终代码评审核对 diff 为纯搬家（除 Global Constraints 声明的 3 处允许改动）
