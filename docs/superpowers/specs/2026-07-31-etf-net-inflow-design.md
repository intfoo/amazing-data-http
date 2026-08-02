# 宽基 ETF 净流入统计 — 设计文档

## 1. 背景与目标

基于 AmazingData SDK 3.5.11 ETF 数据接口,实现"宽基 ETF 净流入"统计功能,通过 HTTP 端点对外提供。

**业务定义**:
- **宽基 ETF**: 跟踪宽基指数(非行业/主题/策略)的 ETF,如上证50、沪深300、中证500等
- **净流入**: 一级市场申赎导致的资金净流入 = (当日份额 − 前一日份额) × 当日单位净值

**目标**: 新增 `POST /etf/net_inflow` 端点,返回指定日期区间内各宽基 ETF 的每日净流入数据。

## 2. SDK 接口可行性分析

### 2.1 使用的 SDK 接口

| SDK 接口 | 所属对象 | 用途 | 输入 | 输出关键字段 |
|---------|---------|------|------|-------------|
| `get_code_info` | `BaseData` | 获取全量 ETF 代码+简称 | `security_type='EXTRA_ETF'` | `symbol`(证券简称) |
| `get_fund_share` | `InfoData` | 获取 ETF 份额历史时序 | `code_list`, `local_path`, `is_local`, `begin_date`, `end_date` | `FUND_SHARE`(万份), `CHANGE_DATE` |
| `get_fund_nav` | `InfoData` | 获取 ETF 净值历史时序 | 同上 | `UNIT_NAV`(单位净值), `PRICE_DATE` |

### 2.2 SDK 无"宽基"识别接口

经全文检索,SDK 没有基金类型/跟踪指数/基金分类等元数据接口:
- `get_code_info` 只返回简称、涨跌停价、昨收
- `get_stock_basic` 只返回名称、上市日期、板块
- `get_etf_pcf` 有 `underlying_security_id`(拟合指数)但**仅深圳有效**,沪市拿不到

**结论**: 宽基识别只能靠 ETF 简称关键词匹配。

### 2.3 净流入口径选择

选择 **A 口径(份额变动×净值)**,理由:
- 份额变动是 ETF 一级市场申赎的直接体现(申购增份额,赎回减份额)
- `get_fund_share` + `get_fund_nav` 提供完整时序数据
- 业内主流计算方法(东方财富、Wind 等均用此口径)

不选其他口径:
- B(成交额): 只反映活跃度,无方向性
- C(资产净值变动): 净值涨跌混入,无法区分资金流入 vs 持仓升值
- D(组合): 二级市场成交额无方向,加了也是噪音

## 3. 宽基 ETF 动态识别方案

### 3.1 方案: 名称关键词匹配(方案 A)

**流程**:
1. `get_code_info('EXTRA_ETF')` 获取全量 ETF 代码 + `symbol`(简称)
2. 对简称做关键词匹配,筛出宽基 ETF

**关键词白名单**(宽基指数名):
```
上证50, 沪深300, 中证500, 中证800, 中证1000, 中证2000,
创业板50, 创业板指, 科创50, 科创100, 科创创业50,
上证180, 深证100, 深证300, 上证380, 中证全指
```

**排除关键词**(避免误纳入行业/主题/策略/增强 ETF):
```
增强, 策略, 量价, 行业, 主题, 红利, 低波, 价值, 成长,
质量, 动量, 基本面, ESG, 消费, 医药, 科技, 金融, 能源,
新能源, 半导体, 军工, 基建, 材料, 工业, 通信, 环保,
食品, 酒, 房地产, 银行, 券商, 保险, 汽车, 农业, 旅游
```

**匹配规则**: 简称含任一白名单关键词 **且** 不含任一排除关键词。

**优点**:
- 全自动动态维护,新发宽基 ETF 名称含关键词自动纳入,零维护
- 实现成本低,无需依赖 `get_etf_pcf` 的部分字段
- 每次请求实时计算,`get_code_info` 是"每日最新"接口,成本极低

**缺点**:
- 名称不含指数关键词的宽基(如"MSCI中国A50")会漏 — 通过补充关键词白名单解决
- 非宽基但名称含数字的(如"300ETF增强")会误纳入 — 通过排除关键词解决

### 3.2 边缘情况处理
- 简称为空/NaN: 跳过该 ETF
- 简称含多个宽基关键词(如"沪深300中证500ETF"): 纳入(不重复)
- 关键词白名单首版用常量列表,不做配置化(见 §9)

## 4. 架构设计

遵循项目既有 4 层模式:Gateway Protocol → Service → HTTP 路由 → 错误处理。

```
┌─ HTTP 层 (http_app.py) ──────────────────────────────────┐
│  POST /etf/net_inflow  ← 新端点                           │
│    请求体: { start_time?, end_time? }                    │
│    响应: { "data": [{ code, name, date, share, nav,      │
│             net_inflow_share, net_inflow_amount }, ...] } │
└──────────────────────────────────────────────────────────┘
          │ 委托
          ▼
┌─ Service 层 (etf_flow_service.py) ← 新建 ────────────────┐
│  EtfFlowService                                          │
│    1. _get_broad_based_etfs() → 宽基 ETF 代码+简称        │
│         (gateway.get_code_info('EXTRA_ETF') + 名称匹配)   │
│    2. gateway.get_fund_share(codes, is_local, begin, end) → 份额时序│
│    3. gateway.get_fund_nav(codes, is_local, begin, end) → 净值时序│
│    4. _compute_net_inflow() → 计算净流入                   │
│         net_inflow_share = 当日份额 - 前一日份额           │
│         net_inflow_amount = net_inflow_share × 当日净值   │
│    5. serialize_dataframe → 展平为 list[dict]            │
└──────────────────────────────────────────────────────────┘
          │ 委托
          ▼
┌─ Gateway 层 (gateway.py) ← 新增 3 方法 ──────────────────┐
│  Gateway Protocol 新增:                                   │
│    get_code_info(security_type) → DataFrame              │
│    get_fund_share(codes, is_local, begin_date, end_date)  │
│                   → dict[code, DataFrame]                │
│    get_fund_nav(codes, is_local, begin_date, end_date)    │
│                 → dict[code, DataFrame]                  │
│  AmazingDataGateway 实现:                                │
│    login 时保存 self._info_data = ad.InfoData()           │
│      (在 self._ready = True 之前,与 BaseData/MarketData  │
│       同处 try 块,失败时纳入回滚逻辑)                     │
│    logout 时清理 self._info_data = None                   │
│      (在 self._base_data = None 之后,逆序清理)            │
│    get_fund_share/get_fund_nav 复用 _is_connection_error │
│      + 惰性重连模式(同 get_adj_factor)                    │
│    local_path/is_local 由 Gateway 内部从 Config 读取      │
│      (Service 层不传 local_path,同 AdjFactorService 模式) │
└──────────────────────────────────────────────────────────┘
```

### 4.1 Gateway 层改动 (`app/gateway.py`)

#### 4.1.1 `Gateway` Protocol 新增 3 方法

```python
# 在现有 Protocol 类中追加
def get_code_info(self, security_type: str = "EXTRA_STOCK_A") -> "pd.DataFrame": ...
def get_fund_share(
    self, codes: list[str],
    is_local: bool = False,
    begin_date: int | None = None, end_date: int | None = None,
) -> dict[str, "pd.DataFrame"]: ...
def get_fund_nav(
    self, codes: list[str],
    is_local: bool = False,
    begin_date: int | None = None, end_date: int | None = None,
) -> dict[str, "pd.DataFrame"]: ...
```

**注意**: Protocol 签名不包含 `local_path` 参数。`local_path` 和 `is_local` 的实际值由 `AmazingDataGateway` 实现内部从 `Config` 读取(对标 `get_adj_factor` 模式,`gateway.py:596-616`)。`is_local` 在 Protocol 中保留默认值 `False` 仅为接口契约明确性,实际实现从 `self._config.fund_is_local` 读取。

#### 4.1.2 `AmazingDataGateway` 实现

- `__init__`: 新增 `self._info_data = None` + `self._fund_local_path = self._resolve_fund_local_path()`(在 `__init__` 中预解析,同 `_adj_factor_local_path` 模式,启动时创建目录)
- `_do_login`: 在 `self._base_data = base`(行 198)之后、`self._ready = True`(行 202)之前追加 `self._info_data = ad.InfoData()`,与 `BaseData`/`MarketData` 同处 try 块内,失败时纳入 `sdk_logged_in` 回滚逻辑
- `_safe_logout`: 在 `self._base_data = None`(行 344)之后追加 `self._info_data = None`(逆序清理,InfoData 依赖 BaseData 的 SDK 登录状态)
- 新增 `get_code_info`: 委托 `self._base_data.get_code_info(security_type)`,加 `_lock` 串行化,模式同 `get_code_list`
- 新增 `get_fund_share`: 委托 `self._info_data.get_fund_share(codes, local_path=self._fund_local_path, is_local=self._config.fund_is_local, begin_date=begin_date, end_date=end_date)`,加 `_lock` 串行化,**复用 `_is_connection_error` + 惰性重连模式**(同 `get_adj_factor`,检测连接错误后持锁 relogin + 重试一次)
- 新增 `get_fund_nav`: 委托 `self._info_data.get_fund_nav(codes, local_path=self._fund_local_path, is_local=self._config.fund_is_local, begin_date=begin_date, end_date=end_date)`,加 `_lock` 串行化,复用重连模式

**local_path 处理**: 新增 `_resolve_fund_local_path()` 方法,逻辑完全复用 `_resolve_adj_factor_local_path()`(`gateway.py:126-161`):配置非空用配置,否则用项目根 `data/` 兜底 + 警告,强制末尾带分隔符(SDK 字符串拼接坑)。Config 新增 `fund_local_path` 和 `fund_is_local` 字段。

### 4.2 Service 层 (`app/etf_flow_service.py` 新建)

```python
class EtfFlowService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """查询宽基 ETF 净流入,返回展平后的记录列表。"""
        # 1. 解析日期(SDK 8 位整型) + 校验 start <= end
        begin_date = to_sdk_date(start_time) if start_time else None
        end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")
        # 保留 ISO 字符串用于后续日期过滤(统一 YYYY-MM-DD 格式)
        start_dt = _parse_iso(start_time) if start_time else None
        end_dt = _parse_iso(end_time) if end_time else None

        # 2. 获取宽基 ETF 清单
        etf_df = self._gw.get_code_info(security_type="EXTRA_ETF")
        broad_based = self._filter_broad_based(etf_df)  # → list[(code, name)]

        # 3. 拉取份额+净值时序(local_path/is_local 由 Gateway 内部从 Config 读取)
        codes = [c for c, _ in broad_based]
        name_map = {c: n for c, n in broad_based}
        share_dict = self._gw.get_fund_share(
            codes, is_local=False, begin_date=begin_date, end_date=end_date
        )
        nav_dict = self._gw.get_fund_nav(
            codes, is_local=False, begin_date=begin_date, end_date=end_date
        )

        # 4. 计算净流入(传入 start_dt/end_dt 用于日期过滤)
        records = self._compute_net_inflow(
            codes, name_map, share_dict, nav_dict, start_dt, end_dt
        )
        return records
```

**复用说明**: `to_sdk_date` 从 `app.kline_service` 导入(`from app.kline_service import to_sdk_date`),`_parse_iso` 从 `app.adj_factor_service` 导入或复制(两者逻辑相同,`adj_factor_service.py:32-42`)。

#### 4.2.1 宽基识别 `_filter_broad_based`

```python
BROAD_BASED_KEYWORDS = [
    "上证50", "沪深300", "中证500", "中证800", "中证1000", "中证2000",
    "创业板50", "创业板指", "科创50", "科创100", "科创创业50", "双创50",
    "上证180", "深证100", "深证300", "上证380", "中证全指",
    "中证A50", "中证A500", "国证2000",
]

EXCLUDE_KEYWORDS = [
    "增强", "策略", "量价", "行业", "主题", "红利", "低波", "价值", "成长",
    "质量", "动量", "基本面", "ESG", "消费", "医药", "科技", "金融", "能源",
    "新能源", "半导体", "军工", "基建", "材料", "工业", "通信", "环保",
    "食品", "白酒", "房地产", "银行", "券商", "保险", "汽车", "农业", "旅游",
]

def _filter_broad_based(self, df: pd.DataFrame) -> list[tuple[str, str]]:
    """从 ETF DataFrame 筛选宽基 ETF,返回 [(code, name), ...]。"""
    result = []
    for code, row in df.iterrows():  # index=ETF代码
        name = str(row.get("symbol", ""))
        if not name or name == "nan":
            continue
        has_broad = any(kw in name for kw in BROAD_BASED_KEYWORDS)
        has_exclude = any(kw in name for kw in EXCLUDE_KEYWORDS)
        if has_broad and not has_exclude:
            result.append((code, name))
    return result
```

#### 4.2.2 净流入计算 `_compute_net_inflow`

```python
def _compute_net_inflow(
    self, codes, name_map, share_dict, nav_dict, start_dt, end_dt,
) -> list[dict]:
    """合并份额+净值,计算每日净流入。返回经 serialize_dataframe 处理的 list[dict]。

    start_dt/end_dt: datetime 类型,用于日期过滤;None 时该侧不过滤。
    日期过滤统一用 strftime("%Y-%m-%d") 转字符串比较(对标 adj_factor_service._filter_by_date)。
    """
    import pandas as pd
    all_frames: list[pd.DataFrame] = []  # 收集各 code 的 DataFrame,最后统一 serialize
    for code in codes:
        share_df = share_dict.get(code)  # index=日期, 含 FUND_SHARE, CHANGE_DATE
        nav_df = nav_dict.get(code)      # index=日期, 含 UNIT_NAV, PRICE_DATE
        if share_df is None or share_df.empty:
            continue

        # 规范化日期列为 YYYY-MM-DD 字符串,按日期排序
        share_df = self._normalize_share_df(share_df)
        nav_df = self._normalize_nav_df(nav_df) if nav_df is not None and not nav_df.empty else None

        # 合并份额+净值(按日期 left join,净值缺失时用前值填充)
        if nav_df is not None:
            merged = share_df.merge(nav_df, on="date", how="left")
            merged["nav"] = merged["nav"].ffill()  # 净值前值填充
        else:
            merged = share_df.copy()
            merged["nav"] = None

        # 计算净流入
        merged["net_inflow_share"] = merged["share"].diff()  # 份额变动(万份)
        merged["net_inflow_amount"] = merged["net_inflow_share"] * merged["nav"]

        # 日期过滤(用 datetime.strftime 统一为 YYYY-MM-DD 字符串比较)
        if start_dt is not None:
            merged = merged[merged["date"] >= start_dt.strftime("%Y-%m-%d")]
        if end_dt is not None:
            merged = merged[merged["date"] <= end_dt.strftime("%Y-%m-%d")]

        # 补充 code/name 列,收集到统一列表(不逐行构建 dict)
        merged["code"] = code
        merged["name"] = name_map.get(code, "")
        # 重排列顺序: code, name, date, share, nav, net_inflow_share, net_inflow_amount
        merged = merged[["code", "name", "date", "share", "nav",
                         "net_inflow_share", "net_inflow_amount"]]
        all_frames.append(merged)

    # 统一序列化: 处理 NaN→None、NumPy 标量→Python 原生(同 KlineService._flatten 模式)
    if not all_frames:
        return []
    combined = pd.concat(all_frames, ignore_index=True)
    return serialize_dataframe(combined)
```

**关键处理**:
- `diff()` 计算份额变动:首日为 NaN(无前一日数据),经 `serialize_dataframe` 转为 `null`
- 净值缺失时用 `ffill()`(前值填充):ETF 净值日更,但份额变动公告日可能非交易日,用最近净值
- 日期规范化:SDK 返回的 `CHANGE_DATE`/`PRICE_DATE` 可能是字符串或 int,统一转 `YYYY-MM-DD`
- **序列化**: 统一用 `serialize_dataframe`(`app.serializer`)处理 NaN/NumPy 标量,与 `KlineService._flatten`、`AdjFactorService._process` 一致。**不逐行构建 dict**(避免 NaN 未经序列化导致 JSON 500)
- **日期过滤**: 用 `start_dt.strftime("%Y-%m-%d")` 统一为字符串比较,对标 `adj_factor_service._filter_by_date`(`adj_factor_service.py:255-265`)

### 4.3 HTTP 层改动 (`app/http_app.py`)

#### 4.3.1 新增请求体模型

```python
class EtfNetInflowRequest(BaseModel):
    """POST /etf/net_inflow 请求体。start_time/end_time 可选。"""
    start_time: str | None = None
    end_time: str | None = None
```

#### 4.3.2 新增路由

```python
@app.post("/etf/net_inflow")
async def etf_net_inflow(req: EtfNetInflowRequest, request: Request):
    """宽基 ETF 净流入统计。返回 {"data": [...]}。"""
    logger.info("request_id=%s /etf/net_inflow %s..%s",
                get_request_id(request),
                req.start_time or "(default)", req.end_time or "(default)")
    if not app.state.sdk_gate.try_acquire():
        raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
    try:
        data = await asyncio.to_thread(
            app.state.etf_flow_service.query, req.start_time, req.end_time
        )
        return {"data": data}
    except AppError:
        raise
    except ValueError as e:
        raise AppError(INVALID_REQUEST, str(e), 422)
    except GatewayNotReadyError as e:
        raise AppError(SDK_NOT_READY, str(e), 503)
    except GatewayQueryError as e:
        raise AppError(SDK_QUERY_FAILED, str(e), 502)
    except (TypeError, OverflowError) as e:
        if "serialize" in str(e).lower() or "json" in str(e).lower():
            raise AppError(SERIALIZATION_FAILED, str(e), 502)
        raise AppError(INTERNAL_ERROR, str(e), 500)
    except Exception as e:
        logger.error("未处理异常: %s: %s", type(e).__name__, e)
        raise AppError(INTERNAL_ERROR, str(e), 500)
    finally:
        app.state.sdk_gate.release()
```

#### 4.3.3 `create_app` 注册 service

```python
etf_flow_service = EtfFlowService(gateway)
# ...
app.state.etf_flow_service = etf_flow_service
```

### 4.4 Config 改动 (`app/config.py`)

新增 `fund_local_path` 和 `fund_is_local` 字段(对标 `adj_factor_local_path`/`adj_factor_is_local`):
```python
fund_local_path: str = ""    # SDK get_fund_share/get_fund_nav 的 local_path,必须为绝对路径
fund_is_local: bool = False  # SDK is_local:False=每次远程取最新,True=本地优先无则远程
```
`from_env` 中:
```python
fund_local_path=os.environ.get("FUND_LOCAL_PATH", "") or "",
fund_is_local=os.environ.get("FUND_IS_LOCAL", "false").lower() in ("1", "true", "yes", "on"),
```

### 4.5 FakeGateway 扩展 (`tests/conftest.py`)

`FakeGateway` 新增 3 方法(签名与 Protocol 一致,**不含 `local_path` 参数**;调用记录在 `__init__` 中预声明的列表,对标 `query_calls`/`adj_factor_query_calls` 模式):
```python
def get_code_info(self, security_type="EXTRA_STOCK_A"):
    if not self._ready:
        raise GatewayNotReadyError("fake not ready")
    return self._code_info_result  # 可注入的 DataFrame

def get_fund_share(self, codes, is_local=False, begin_date=None, end_date=None):
    if not self._ready:
        raise GatewayNotReadyError("fake not ready")
    self.fund_share_calls.append({
        "codes": codes, "is_local": is_local,
        "begin_date": begin_date, "end_date": end_date,
    })
    return self._fund_share_result  # 可注入的 dict[code, DataFrame]

def get_fund_nav(self, codes, is_local=False, begin_date=None, end_date=None):
    if not self._ready:
        raise GatewayNotReadyError("fake not ready")
    self.fund_nav_calls.append({
        "codes": codes, "is_local": is_local,
        "begin_date": begin_date, "end_date": end_date,
    })
    return self._fund_nav_result
```

`__init__` 新增可注入字段 + 预声明调用记录列表(对标 `conftest.py:22-23` 的 `query_calls`/`adj_factor_query_calls`):
```python
# __init__ 新增参数
code_info_result: pd.DataFrame | None = None,
fund_share_result: dict[str, pd.DataFrame] | None = None,
fund_nav_result: dict[str, pd.DataFrame] | None = None,
# __init__ 内赋值
self._code_info_result = code_info_result
self._fund_share_result = fund_share_result or {}
self._fund_nav_result = fund_nav_result or {}
self.fund_share_calls: list[dict] = []  # 预声明,不用 getattr
self.fund_nav_calls: list[dict] = []
```

## 5. 数据流

```
HTTP POST /etf/net_inflow { start_time, end_time }
    │
    ▼ asyncio.to_thread
EtfFlowService.query(start_time, end_time)
    │
    ├─ 1. gateway.get_code_info("EXTRA_ETF")
    │      → DataFrame[index=code, columns=[symbol, ...]]
    │      → _filter_broad_based() → [(code, name), ...] (~50-200 只)
    │
    ├─ 2. gateway.get_fund_share(codes, local_path, is_local=False, begin, end)
    │      → dict[code, DataFrame[index=日期, columns=[FUND_SHARE, CHANGE_DATE, ...]]
    │
    ├─ 3. gateway.get_fund_nav(codes, is_local=False, begin, end)
    │      → dict[code, DataFrame[index=日期, columns=[UNIT_NAV, PRICE_DATE, ...]]
    │      (注意: SDK 文档明确 get_fund_nav 的 index 为日期,非序号)
    │
    ├─ 4. _compute_net_inflow()
    │      per code:
    │        - 规范化日期列 → "YYYY-MM-DD"
    │        - merge share + nav (left join on date, ffill nav)
    │        - diff() 计算份额变动
    │        - net_inflow_amount = share_diff × nav
    │        - 日期过滤
    │
    └─ 5. 返回 list[dict]: [{code, name, date, share, nav, net_inflow_share, net_inflow_amount}, ...]
    │
    ▼
HTTP 200 {"data": [...]}
```

## 6. 错误处理

复用项目现有错误码和异常处理模式:

| 场景 | 异常 | HTTP 状态码 | 错误码 |
|------|------|-----------|--------|
| SDK 未登录 | `GatewayNotReadyError` | 503 | `SDK_NOT_READY` |
| SDK 查询失败 | `GatewayQueryError` | 502 | `SDK_QUERY_FAILED` |
| 日期格式无效 | `ValueError` | 422 | `INVALID_REQUEST` |
| start > end | `ValueError` | 422 | `INVALID_REQUEST` |
| 并发超限 | `AppError(SERVICE_BUSY)` | 503 | `SERVICE_BUSY` |
| 序列化失败 | `TypeError/OverflowError` | 502 | `SERIALIZATION_FAILED` |
| 未分类异常 | `Exception` | 500 | `INTERNAL_ERROR` |

## 7. 测试策略

### 7.1 单元测试 (`tests/test_etf_flow_service.py` 新建)

- `test_filter_broad_based`: 验证关键词匹配
  - 含"沪深300" → 纳入
  - 含"沪深300增强" → 排除
  - 含"医药ETF" → 排除
  - 简称为空 → 跳过
- `test_compute_net_inflow`: 验证净流入计算
  - 份额不变 → net_inflow_share=0, net_inflow_amount=0
  - 份额增加 → net_inflow_share>0, net_inflow_amount>0
  - 净值缺失 → ffill 用前值
  - 首日 → net_inflow_share=None
- `test_query_full_flow`: FakeGateway 注入 mock 数据,验证端到端
- `test_date_filter`: 验证日期范围过滤
- `test_empty_result`: 宽基列表为空/份额数据为空 → 返回 []

### 7.2 FakeGateway 扩展测试

- `test_fake_gateway_get_code_info`: 验证调用记录和返回
- `test_fake_gateway_get_fund_share`: 验证调用记录和返回
- `test_fake_gateway_get_fund_nav`: 验证调用记录和返回

### 7.3 HTTP 端点测试 (`tests/test_http_etf.py` 新建)

- `test_etf_net_inflow_200`: 正常请求返回数据
- `test_etf_net_inflow_empty`: 宽基为空返回 `{"data": []}`
- `test_etf_net_inflow_invalid_date`: 日期格式错误 → 422
- `test_etf_net_inflow_start_after_end`: start > end → 422
- `test_etf_net_inflow_sdk_not_ready`: SDK 未就绪 → 503

## 8. 性能考量

- `get_code_info('EXTRA_ETF')`: 一次调用,返回约 800-1000 只 ETF,内存极小
- `get_fund_share`/`get_fund_nav`: 传入选出的宽基 codes(~50-200 只),SDK 内部批量查询
- `is_local=False`: 首次从远程拉取,SDK 自动缓存到 `local_path`;后续请求可改 `is_local=True` 走本地缓存
- 净流入计算: pandas 向量化操作,200 只 ETF × N 天数据,内存可控
- 单次请求预估: SDK 调用 3 次(get_code_info ~1s + get_fund_share ~10-30s + get_fund_nav ~10-30s),总耗时 **20-60s**(取决于网络+数据量)。客户端应设置 ≥60s 超时。
- `SdkGate` 并发闸门: 复用现有 `sdk_max_concurrent=5`,单个 ETF 请求占 1 个并发槽位但持锁时间长(20-60s),高并发时其他请求可能 503。
- 如 SDK 对单次 codes 数量有上限,需分批查询再合并(首版假设 200 只以内无上限)。

## 9. 不做的事 (YAGNI)

- **不缓存宽基 ETF 清单**: 每次请求实时计算,`get_code_info` 成本极低,避免缓存一致性问题
- **不实现 `get_fund_iopv`**: IOPV 是盘中实时估值,净流入计算用收盘净值(`UNIT_NAV`)足够
- **不实现 `get_etf_pcf`**: PCF 是当日申赎清单,与历史净流入计算无关
- **不拆分多个端点**: 单一 `/etf/net_inflow` 端点,参数控制日期范围
- **不自动切换 `is_local`**: 首版固定 `is_local=False`(远程取最新),后续可配置
- **不做关键词配置化**: 首版用常量列表,未来有需求再改配置文件
- **不返回汇总统计**: 只返回逐 ETF 逐日明细,汇总(合计/排名)由消费方完成

## 10. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `app/gateway.py` | 修改 | Protocol 新增 3 方法 + AmazingDataGateway 实现 + `_info_data` 生命周期 + `_resolve_fund_local_path` |
| `app/config.py` | 修改 | 新增 `fund_local_path` 字段 |
| `app/etf_flow_service.py` | 新建 | EtfFlowService + 宽基识别 + 净流入计算 |
| `app/http_app.py` | 修改 | 新增 `EtfNetInflowRequest` + `/etf/net_inflow` 路由 + service 注册 |
| `tests/conftest.py` | 修改 | FakeGateway 新增 3 方法 + 可注入字段 |
| `tests/test_etf_flow_service.py` | 新建 | Service 层单测 |
| `tests/test_http_etf.py` | 新建 | HTTP 端点测试 |
| `.env.example` 或 `.env` | 修改 | 新增 `FUND_LOCAL_PATH` 示例(可选) |
