# ETF 份额/净值原始数据接口设计

日期：2026-08-15
状态：已确认（brainstorming 结论稿）

## 背景与目标

现有 `POST /etf/net_inflow` 接口在服务端一条龙完成：拉全量 ETF 清单 → 名称关键词筛宽基 →
拉份额+净值 → 合并算净流入。本次改造将**宽基识别**和**净流入计算**全部下移给下游，
本项目只对外提供**份额和净值的原始数据接口**，职责收缩为"准确、统一的数据供给"。

设计决策（brainstorming 逐条确认）：

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 接口形态 | 两个独立端点 `/etf/share`、`/etf/nav`，忠实暴露原始时序，服务端不做 join |
| 2 | ETF 清单来源 | 不出独立清单端点；share/nav 端点 `codes` 可留空，留空 = 全量 ETF |
| 3 | 深市份额日期修正 | 主日期字段 `trade_date` 直接给修正后的真实变动日，附原始 `ann_date` 供审计；语义必须在 API 文档和代码注释中明确标注 |
| 4 | 旧 `/etf/net_inflow` | 直接删除，连同 EtfFlowService、宽基关键词表、相关缓存 |
| 5 | 请求护栏 | 日期双缺省默认近 30 天（沿用现 net_inflow 策略）；显式传 `start_time` 可求全历史 |

## 架构与职责边界

```
下游                                本项目
┌─────────────────────┐            ┌──────────────────────────────┐
│ 宽基识别（关键词筛选）│            │ POST /etf/share  份额原始时序 │
│ 净流入计算（diff×nav）│  ◄──HTTP── │ POST /etf/nav    净值原始时序 │
└─────────────────────┘            └──────────┬───────────────────┘
                                              │
                                   FundDataService（新增，轻量）
                                   - codes 缺省 → get_code_info 全量 ETF
                                   - 深市份额日期修正（交易日历 snap）
                                   - 拉取区间后扩 + 日期过滤 + 序列化
                                              │
                                   Gateway.get_fund_share / get_fund_nav（不动）
```

### 删除

- `app/etf_flow_service.py` 整个文件，含：
  - `EtfFlowService`（query / 结果缓存 / 清单缓存）
  - 宽基规则表 `BROAD_BASED_KEYWORDS` / `NUMERIC_PATTERNS` / `EXCLUDE_KEYWORDS` / `EXCLUDE_SUFFIXES`（约 130 行）
  - `_compute_net_inflow`
- `app/http_app.py`：`EtfNetInflowRequest`、`/etf/net_inflow` 路由、`etf_flow_service` 装配
- 配置 `etf_flow_cache_ttl_sec`（改名复用，见下）

### 新增

`app/fund_data_service.py`，对标 `AdjFactorService` 的薄 Service 模式：

- `FundDataService(gateway, cache_ttl_sec)`
- `query_share(codes, start_time, end_time) -> list[dict]`
- `query_nav(codes, start_time, end_time) -> list[dict]`
- 从旧 Service **平移保留**的纯数据准确性逻辑：
  - `_normalize_share_df`（含深市 CHANGE_DATE snap 修正）→ 保留
  - `_normalize_nav_df` → 保留
  - `_to_date_str` → 保留（或移入公共 util，如已有等价物则复用）

### 缓存

- 配置改名：`etf_flow_cache_ttl_sec` → `fund_data_cache_ttl_sec`（env `FUND_DATA_CACHE_TTL_SEC`，默认 300）
- 结果缓存保留：key = (接口, codes 指纹, start_time, end_time)，TTL 300s，上限 64 条整体清空（沿用旧策略）。份额/净值 T+1 更新，300s 无 freshness 风险；全量 ETF 查询重，缓存有意义
- ETF 全量清单（codes 缺省时 `get_code_info("EXTRA_ETF")` 结果）按日缓存，跨日自动失效（沿用旧清单缓存策略，但不再做宽基筛选，缓存原始全量 codes）

## 接口契约

### POST /etf/share

请求体：

```json
{"codes": ["510300.SH", "159915.SZ"], "start_time": "2026-07-15", "end_time": "2026-08-15"}
```

- `codes`：可选。缺省 = 全量 ETF（服务端 `get_code_info("EXTRA_ETF")`）
- `start_time` / `end_time`：可选，`YYYY-MM-DD`。双缺省 = 近 30 天；只传一侧则该侧生效、另一侧不过滤

响应：

```json
{"data": [
  {"code": "159915.SZ", "trade_date": "2026-08-14", "share": 1234567.0, "ann_date": "2026-08-15"}
]}
```

字段语义（**必须写入 docs/API.md 和代码注释**）：

- `trade_date`：份额**实际变动交易日** T。
  - 沪市（.SH）：= SDK `CHANGE_DATE` 原样（沪市 CHANGE_DATE=T 可信）
  - 深市（.SZ）：SDK 的 `CHANGE_DATE` 实际填的是公告日 T+1（与 `ANN_DATE` 相同），
    服务端用交易日历 snap 到前一交易日，还原为真实变动日 T
- `share`：基金份额（SDK `FUND_SHARE`，万份）
- `ann_date`：原始公告日（SDK `ANN_DATE`），供审计/追溯

**内部逻辑（对调用方透明）**：SDK 拉取区间的 `end_date` 后扩 10 天——深市 T+1 公告，
T 为节前最后一天时公告落在节后（最长约 10 天），不后扩会导致区间末尾深市数据缺失。
拉取后按修正后的 `trade_date` 过滤回用户请求区间。
旧实现的"前扩 1 交易日"**删除**——那是为服务端 diff() 首日服务的，diff 已下移给下游。

### POST /etf/nav

请求体同 `/etf/share`。响应：

```json
{"data": [
  {"code": "510300.SH", "trade_date": "2026-08-14", "nav": 4.123}
]}
```

- `trade_date` = SDK `PRICE_DATE`（净值计算日，沪深均准确，**无需修正、无需后扩**）
- `nav` = 单位净值（SDK `UNIT_NAV`）

### 口径统一声明（写入 API 文档）

两个接口的 `trade_date` 语义统一为"真实交易日"，下游按 `(code, trade_date)` 直接 join 后
`share.diff() × nav` 即得净流入。宽基识别、净流入口径由下游全权负责，本项目不再提供
任何筛选/派生计算。

## 错误处理与边界

- `start_time > end_time` 或日期格式非法 → 422（沿用 `to_sdk_date`/`_parse_iso` 模式）
- gateway 未就绪 → 503；SDK 查询失败 → 502（走现有 `_run_sdk_endpoint` 统一映射）
- 空结果（ETF 清单为空、区间内无数据）→ 200 `{"data": []}`
- 深市修正无交易日历可用时：退化为简单减 1 天并记 warning（沿用现有兜底；ffill 场景已不存在，此处仅影响日期落点）
- SDK 返回缺 `FUND_SHARE`/`UNIT_NAV` 列时：对应字段为 None（沿用现有防御）

## 测试

- `tests/test_etf_flow_service.py` → 重写为 `tests/test_fund_data_service.py`：
  - 保留并适配：深市 snap 修正（普通日 / 跨周末 / 跨长假）、沪市原样不修正、
    无日历兜底减 1 天、日期过滤、缺列防御
  - 新增：后扩 10 天拉取 + 按修正后 trade_date 过滤回用户区间、codes 缺省走全量清单、
    双缺省默认近 30 天、结果缓存命中
  - 删除：全部宽基筛选用例、净流入计算用例
- `tests/test_http_etf.py` → 改为两个新端点的路由级测试（200/422/空结果/缺省参数）
- `tests/conftest.py`：FakeGateway 已有 `get_fund_share`/`get_fund_nav`/`get_code_info`，按需微调

## 文档与配置

- `docs/API.md`：`/etf/net_inflow` 章节替换为 `/etf/share`、`/etf/nav` 两节，
  明确写出深市 `trade_date` 修正语义、后扩 10 天说明、职责边界声明
- `.env.example` / `README.md`：`ETF_FLOW_CACHE_TTL_SEC` → `FUND_DATA_CACHE_TTL_SEC`，注释同步
- `app/config.py`：字段改名 + 注释更新

## 影响面

- `scripts/probe_etf_single.py` / `scripts/probe_etf_dynamic.py` 引用了
  `EtfFlowService._compute_net_inflow`。`_compute_net_inflow` 随旧 Service 删除，
  这两个探针脚本同步修改：从 `app.fund_data_service` 导入 `_normalize_share_df` /
  `_normalize_nav_df`，diff×nav 两行计算在脚本内自行完成（探针即"下游"的最小样本）。
- Gateway 层（`query_basedata.py`）零改动。
