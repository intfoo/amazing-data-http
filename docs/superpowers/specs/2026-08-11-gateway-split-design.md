# Gateway 拆分设计（gateway.py → app/gateway/ 包）

**日期：** 2026-08-11
**分支：** fix/gateway-stability-20260811
**状态：** 已批准（brainstorming 结论：方案 C1 mixin 包，query 拆两个文件）

## 1. 背景与目标

`app/gateway.py` 经稳定性修复后达 1018 行 / 48KB，是项目第二大文件的 2 倍多，AI 读取和编辑成本高、易出错。本次为**纯机械重构**：文件变小、边界归位，行为零变化。

**目标：**
- 单文件 ≤ ~250 行（query_market.py 允许到 ~280）
- `from app.gateway import ...` 导入面完全不变，`http_app.py` / `tests/` / `conftest.py` 零改动
- 既有 335 个测试零改动全绿是唯一验收标准

**非目标（YAGNI）：**
- 不改任何方法体逻辑、日志文案、异常语义
- 不拆分成协作对象（SessionManager/QueryExecutor 等），不引入依赖注入
- 不为 mixin 间的 `self.xxx` 引用补类型协议
- 不动 `app/` 其他文件（realtime_service.py 15KB、http_app.py 21KB 尚可接受）

## 2. 架构

`app/gateway.py` → `app/gateway/` 包。单向依赖规则：**mixin 模块只允许 import `base.py` / `paths.py`；mixin 之间禁止互相 import**；跨 mixin 调用一律走 `self`（鸭子类型），杜绝循环 import。

```
app/gateway/
├── __init__.py        # AmazingDataGateway(各Mixin) 组合类 + __init__ 状态清单 + re-export（~70行）
├── base.py            # 常量、判定函数、Protocol、异常（~130行）
├── paths.py           # 两个 local_path 解析模块函数（~70行）
├── session.py         # SessionMixin（~150行）
├── tgw_events.py      # TgwEventMixin（~160行）
├── resilience.py      # ResilienceMixin（~60行）
├── query_market.py    # QueryMarketMixin（~230行）
├── query_basedata.py  # QueryBaseDataMixin（~200行）
└── subscription.py    # SubscriptionMixin（~80行）
```

### 2.1 各模块职责与内容清单

**`base.py`** — 无状态共享层：
- 模块 logger：`logger = logging.getLogger("amazingdata.gateway")`（**所有 mixin 模块 `from app.gateway.base import logger` 复用同一个**，保持日志标签不变）
- 常量：`PERIOD_MAP`、`_CONNECTION_KEYWORDS`、`_SDK_CORRUPTION_KEYWORDS`、`_RECONNECT_COOLDOWN_SEC`、`_DISCONNECT_DEDUP_SEC`、`ADJ_FACTOR_TIMEOUT_SEC`、`SDK_LOCK_TIMEOUT_SEC`
- 函数：`_is_connection_error`、`_is_sdk_corruption`
- 接口：`Gateway` Protocol；异常：`GatewayError` / `GatewayNotReadyError` / `GatewayQueryError`

**`paths.py`** — 模块级函数（从实例方法改造，仅允许的代码改动点）：
- `resolve_adj_factor_local_path(configured: str) -> str`
- `resolve_fund_local_path(configured: str) -> str`
- 逻辑从 `AmazingDataGateway._resolve_*_local_path` 平移，`self._config.xxx` 改为参数传入；docstring 中"本方法"等表述同步微调
- **⚠️ `__file__` 路径偏移修正**：当前 `app/gateway.py` 里 `Path(__file__).resolve().parent.parent / "data"` 指向项目根；搬到 `app/gateway/paths.py` 后目录多一层，必须改为 `Path(__file__).resolve().parents[2] / "data"`（parents[0]=gateway/、parents[1]=app/、parents[2]=项目根），否则默认兜底路径错指 `app/data`，违反行为零变化

**`session.py`** — `SessionMixin`：`login` / `_do_login` / `_safe_logout` / `logout` / `is_ready` / `calendar` property / `refresh_calendar`

**`tgw_events.py`** — `TgwEventMixin`：`_install_tgw_event_logger`（含 `logged_on_log` / `logged_on_event` / `logged_on_logon` 三个闭包）/ `_schedule_reconnect` / `_should_log_disconnect`

**`resilience.py`** — `ResilienceMixin`：`_call_sdk_with_timeout`（staticmethod）/ `_sdk_lock`（contextmanager）

**`query_market.py`** — `QueryMarketMixin`（MarketData 系 + 代码列表）：`query_kline` / `query_snapshot` / `get_code_list` / `get_code_info` / `get_realtime_code_list`

**`query_basedata.py`** — `QueryBaseDataMixin`（BaseData/InfoData 系）：`get_adj_factor` / `get_fund_share` / `get_fund_nav`

**`subscription.py`** — `SubscriptionMixin`：`start_snapshot_subscription` / `stop_subscription`

**`__init__.py`** — 组合与门面：
- `class AmazingDataGateway(SessionMixin, TgwEventMixin, ResilienceMixin, QueryMarketMixin, QueryBaseDataMixin, SubscriptionMixin)`
- `__init__`：全部实例状态一处定义（`_config`/`_lock`/`_ad`/`_market_data`/`_ready`/`_base_data`/`_calendar`/`_subscribe_data`/`_sub_thread`/`_adj_factor_local_path`/`_info_data`/`_fund_local_path`/`_reconnect_lock`/`_reconnect_in_progress`/`_last_reconnect_attempt`/`_last_disconnect_log`），其中两个 local_path 改调 `paths.resolve_*` 模块函数
- re-export（全清单，一个不能少）：`AmazingDataGateway`、`Gateway`、`GatewayError`、`GatewayNotReadyError`、`GatewayQueryError`、`PERIOD_MAP`、`_is_connection_error`、`_is_sdk_corruption`（下划线名与 `PERIOD_MAP` 均为测试直接导入，必须显式导出）
- 另需 `from app.config import Config`（`__init__(self, config: Config)` 签名注解）
- 各 mixin 模块按需携带方法体引用的 stdlib import（`threading`/`time`/`sys`/`contextlib`/`typing.Any` 等）+ `from app.gateway.base import logger, ...`（用到的异常/常量/判定函数）

## 3. 关键机制

1. **Mixin 全部无 `__init__`**，只带方法；`self` 共享状态由最终类 `__init__` 单点定义，一处可览全部状态清单
2. **方法体一行不改**（docstring/注释/日志随行平移）；唯一代码改动是 `__init__` 中两处 `self._resolve_*_local_path()` → `paths.resolve_*_local_path(self._config.*_local_path)`，以及两个 `_resolve_*` 实例方法变为模块函数（`self._config.xxx` → 参数）
3. **import 面不变**：`__init__.py` re-export 所有被外部 import 的名字。外部已知 import 点（需逐个核对）：
   - `app/http_app.py`：`from app.gateway import ...`
   - `app/realtime_service.py`：`from app.gateway import GatewayNotReadyError`
   - `app/kline_service.py`：`from app.gateway import Gateway`
   - `app/subscription_scheduler.py`：`from app.gateway import Gateway`
   - `app/adj_factor_service.py`（Gateway）、`app/etf_flow_service.py`（Gateway）、`app/health.py`（Gateway）
   - `tests/conftest.py`、`tests/test_amazingdata_gateway.py`（含 `_is_connection_error`/`_is_sdk_corruption`/`AmazingDataGateway` 多处函数内导入）、`tests/test_adj_factor.py`、`tests/test_gateway_interface.py`（含 `PERIOD_MAP`/`GatewayError`）、`tests/test_kline_service.py`、`tests/test_etf_flow_service.py`
4. **Mixin 间调用走 self**：如 `session._do_login` 调 `self._install_tgw_event_logger()`（TgwEventMixin）、`tgw_events._schedule_reconnect._do` 调 `self._sdk_lock()`（ResilienceMixin）、`query_*` 调 `self._sdk_lock()`/`self._call_sdk_with_timeout()`/`self._do_login()`——`self` 即组合类实例，无 import 依赖
5. **错误处理不变**：异常类搬入 `base.py`，语义不动；`_is_connection_error`/`_is_sdk_corruption` 同名同行数平移

## 4. 实施步骤

1. 建包骨架：`base.py` → `paths.py`（无依赖，先落地）
2. 各 mixin 模块平移（互相独立，可并行）：`resilience.py`、`subscription.py`、`session.py`、`tgw_events.py`、`query_market.py`、`query_basedata.py`
3. `__init__.py` 组合类 + re-export；删除旧 `app/gateway.py`
4. 全量测试 + import 冒烟（`python -c "from app.gateway import AmazingDataGateway, Gateway, GatewayError, GatewayNotReadyError, GatewayQueryError, PERIOD_MAP, _is_connection_error, _is_sdk_corruption"`）

## 5. 测试策略

- **零新增测试、零测试改动**；335 个既有测试全绿即验收
- import 冒烟脚本验证公共名一个不少
- 用 codespelunker 全仓搜 `from app.gateway import` / `app.gateway.` 确认无遗漏引用点
- commit 策略：2 个 commit——①建包+全部模块落地+删旧文件（中间态不可运行，不拆细）；②如有修正。每 commit 前全量测试绿

## 6. 风险与对策

| 风险 | 对策 |
|------|------|
| 循环 import | 单向依赖规则（§2）；mixin 间只走 self |
| re-export 漏名字导致外部 ImportError | §3.3 逐个核对 import 点 + 冒烟脚本 + 全量测试 |
| 平移中手滑改到逻辑 | 方法体零改动纪律；diff 评审逐 hunk 核对"纯搬家" |
| IDE 报 mixin 未知属性 | 接受（Python mixin 惯例），不补类型协议 |
| Windows 上 `gateway.py` 删除与 `gateway/` 包共存冲突 | 同 commit 内完成删文件+建包，git 自动处理 |

## 7. 验收标准

1. `app/gateway.py` 不存在；`app/gateway/` 包 9 个文件，单文件 ≤ ~280 行
2. `python -m pytest tests/ -q` 335 全绿（测试文件零改动）
3. `from app.gateway import ...` 全部既有名字可用
4. git diff 评审确认无逻辑改动（除 §3.2 声明的两处 path resolve 调用点）
