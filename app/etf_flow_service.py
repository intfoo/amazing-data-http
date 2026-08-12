"""EtfFlowService：宽基 ETF 净流入统计 Service 层。

职责边界：
- 调用 gateway.get_code_info("EXTRA_ETF") 获取全量 ETF 代码+简称
- 通过名称关键词匹配筛选宽基 ETF（排除行业/主题/策略/增强等）
- 调用 gateway.get_fund_share / get_fund_nav 获取份额和净值时序
- 合并份额+净值，计算每日净流入 = (当日份额 - 前一日份额) × 当日净值
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：HTTP 路由、错误码映射（由 http_app 层完成）

性能：净流入计算在 DataFrame 层向量化完成（merge + diff），避免逐行 dict 操作。

日期语义（沪深差异，核心逻辑）：
ETF 份额变动是 T 日交易收盘后的申赎结果。
- 沪市（.SH）：SDK CHANGE_DATE=T（变动日），ANN_DATE=T+1（公告日），两者差一天，CHANGE_DATE 可信
- 深市（.SZ）：SDK CHANGE_DATE 和 ANN_DATE 相同，均填公告日 T+1，CHANGE_DATE 不可信，
  需用交易日历 snap 到前一交易日还原为真实变动日 T
- 深市 T+1 公告时滞：T 日收盘后的申赎结果 T+1 日才公告。若 T 为周五则 T+1 为周一，
  若 T 为节前最后一天则 T+1 为节后第一天。因此查询最近数据时深市最新交易日可能缺数据
- 拉取区间扩展：前扩 1 交易日（diff() 首日不为 NaN），后扩 10 天（覆盖深市 T+1 公告跨长假）

详见 docs/API.md 的 POST /etf/net_inflow 章节。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.adj_factor_service import _parse_iso
from app.gateway import Gateway
from app.kline_service import to_sdk_date
from app.serializer import serialize_dataframe
from app.subscription_schedule import now_cn

logger = logging.getLogger("amazingdata.etf_flow")


def _to_date_str(s) -> "pd.Series":
    """将日期 Series 统一转为 YYYY-MM-DD 字符串。

    处理 datetime64/int/string 三种类型。int 格式（如 20240102）需用 format="%Y%m%d"
    解析，否则 pd.to_datetime 会将其视为纳秒时间戳 → 1970 年。
    """
    import pandas as pd
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d")
    elif pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        return pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")

# 宽基指数关键词白名单：简称含任一即视为宽基候选
BROAD_BASED_KEYWORDS = [
    "上证50", "沪深300", "中证500", "中证800", "中证1000", "中证2000",
    "创业板50", "创业板指", "创业板", "创业五零",  # "创业板"覆盖 159915/创业板TH/HX 等
    "科创50", "科创100", "科创创业50", "科创板50",  # "科创板50"覆盖 588080
    "双创50", "双创基金",  # "双创基金"覆盖 159783
    "上证180", "深证100", "深证300", "上证380", "中证全指",
    "中证A50", "中证A500", "国证2000",
    "上证综合", "综指ETF",  # 上证综指 ETF（510980/510210）
    # 基金公司+指数名 命名模式（公司简称在前，指数代号在后）
    "华夏300", "天弘300", "景顺A500",
    "科创富国", "科创平安",  # 科创50 的公司前缀变体（588940/589150）
]

# 纯数字/代号简称正则：匹配省略"沪深/中证"前缀的宽基 ETF 简称
# 覆盖：50ETF / 180ETF / 300ETF / 380ETF / 500ETF / 800ETF / 1000ETF / 2000ETF
#       A50 / A100 / A500 / A50ETF / A500ETF / HS300 / ZZ500 / CSI2000 / GZ2000
#       MSCIA50 / SH50ETF / SZ50ETF / AH300ETF / AH500ETF / ZZ100 / ZZ800ETF 等
#       xxx基金（宽基跟踪的基金型 ETF: 300基金/500基金/1000基金/2000基金/180基金/800基金）
#       50科创（科创50 反写）/ D100ETF（深证100 代号）/ 180E / 2000E（E 份额变体）
# 不含：225ETF(日经) / 10年地债 / 30年国债 / 0-4地债(债券) / 580ETF(上证580,非主流宽基) /
#       AI50(主题) / 100ETF(中证100,不在宽基清单,易和深证100混淆) /
#       180指/180指基/180指数(指增类,由 EXCLUDE_SUFFIXES 拦截)
NUMERIC_PATTERNS = [
    r"^50ETF",          # 50ETF / 50ETF基 / SH50ETF / SZ50ETF
    r"^50科创",         # 50科创(科创50 反写)
    r"^180ETF",         # 180ETF / 180ETF指(后缀"指"不在排除,但"指基/指数"在)
    r"^180E$",          # 180E(E 份额变体,单独字母后缀)
    r"^180基金",        # 180基金
    r"^300ETF",         # 300ETF / 300ETF增
    r"^300基金",        # 300基金
    r"^380ETF",         # 380ETF
    r"^500ETF",         # 500ETF / 500ETFEW / 500ETFFG / 500ETF基
    r"^500基金",        # 500基金
    r"^800ETF",         # 800ETF
    r"^800基金",        # 800基金(如有)
    r"^1000ETF",        # 1000ETF
    r"^1000基金",       # 1000基金
    r"^2000ETF",        # 2000ETF
    r"^2000E$",         # 2000E(E 份额变体)
    r"^2000基金",       # 2000基金
    r"^ETF500",         # ETF500
    r"^D100ETF",        # D100ETF(深证100 代号)
    r"^HS300",          # HS300 / HS300E / HS300ETF / HGS300
    r"^HGS300",         # HGS300(港股沪深300)
    r"^ZZ100$",         # ZZ100(中证100 纯代号)
    r"^ZZ500",          # ZZ500ETF
    r"^ZZ800",          # ZZ800ETF
    r"^ZZ1000",         # ZZ1000
    r"^ZZ2000",         # ZZ2000
    r"^ZZA500",         # ZZA500(中证A500 代号)
    r"^CSI2000",        # CSI2000
    r"^GZ2000",         # GZ2000(国证2000 代号)
    r"^AH300ETF",       # AH300ETF
    r"^AH500ETF",       # AH500ETF
    r"^HGS500",         # HGS500 / HGS500E
    r"^MSCIA50",        # MSCIA50
    r"^SH50ETF",        # SH50ETF
    r"^SZ50ETF",        # SZ50ETF
    r"^A50",            # A50 / A50ETF / A50中证 / A50华宝 / A50基金 / A50指数 / A50龙头
    r"^A100",           # A100 / A100ETF / A100基金 / A100指基 / A100指数
    r"^A500",           # A500 / A500ETF / A500E / A500DC / A500万家 / ... 全族
    r"^BOCI500",        # BOCI500
]

# 排除关键词：简称含任一即排除（避免误纳入行业/主题/策略/增强 ETF）
EXCLUDE_KEYWORDS = [
    "增强", "策略", "量价", "行业", "主题", "红利", "低波", "价值", "成长",
    "质量", "动量", "基本面", "ESG", "消费", "医药", "科技", "金融", "能源",
    "新能源", "半导体", "军工", "基建", "材料", "工业", "通信", "环保",
    "食品", "白酒", "房地产", "银行", "券商", "保险", "汽车", "农业", "旅游",
    "创新", "龙头",  # 风格/主题因子
    "创业板增", "创业板综",  # 防止"创业板"关键词误收增强/综合类衍生品
]

# 排除后缀：数字代号简称匹配后,若以这些后缀结尾则排除（指增/增强等衍生产品）
# 注：仅对 NUMERIC_PATTERNS 命中的简称做二次过滤,不影响 BROAD_BASED_KEYWORDS 命中
EXCLUDE_SUFFIXES = [
    "增", "增强", "指增", "增指", "指基", "指数", "现金",  # 增强类/机构定制
]


class EtfFlowService:
    """宽基 ETF 净流入统计 Service。

    缓存语义：
    - 查询结果按 (start_time, end_time) 缓存，TTL 由 cache_ttl_sec 控制（默认 300s）。
      份额数据 T+1 才更新，300s 内无 freshness 风险。缓存上限 64 条，超限整体清空。
    - 宽基 ETF 清单按日缓存（key=当日日期字符串），跨日自动失效重取。
    """

    def __init__(self, gateway: Gateway, cache_ttl_sec: int = 300):
        self._gw = gateway
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_lock = threading.Lock()
        # 宽基 ETF 清单按日缓存：(date_str, codes, name_map)
        self._list_cache: tuple[str, list[str], dict[str, str]] | None = None
        # 查询结果缓存：key=(start_time, end_time) → (cached_at, records)
        self._result_cache: dict[tuple, tuple[float, list[dict]]] = {}

    def query(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """查询宽基 ETF 净流入，返回展平后的记录列表。

        start_time / end_time 可选；为 None 时该侧不过滤。
        空结果返回 []（HTTP 层包装为 {"data": []}）。
        """
        # 1. 解析日期（SDK 8 位整型） + 校验 start <= end
        # start_time/end_time 均缺省时默认近 30 天，避免返回全量历史数据（2012年至今）
        # 导致响应过大且 SDK 查询耗时过长（对标 /minute 的近一年默认策略）
        now_cn_ts = now_cn()  # 一次取用：默认区间与清单缓存日期同源（防跨午夜不一致）
        if start_time is None and end_time is None:
            from datetime import timedelta as _td
            start_time = (now_cn_ts - _td(days=30)).strftime("%Y-%m-%d")
            end_time = now_cn_ts.strftime("%Y-%m-%d")
            logger.debug("etf_flow 默认范围: %s..%s (近 30 天)", start_time, end_time)
        begin_date = to_sdk_date(start_time) if start_time else None
        end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")
        # 保留 datetime 用于后续日期过滤（统一 YYYY-MM-DD 格式）
        start_dt = _parse_iso(start_time) if start_time else None
        end_dt = _parse_iso(end_time) if end_time else None

        # 结果缓存命中检查：key=(start_time, end_time)
        cache_key = (start_time, end_time)
        now_ts = time.monotonic()
        with self._cache_lock:
            hit = self._result_cache.get(cache_key)
            if hit is not None and now_ts - hit[0] <= self._cache_ttl_sec:
                logger.info("etf_flow 结果缓存命中: key=%s records=%d", cache_key, len(hit[1]))
                return list(hit[1])

        # 2. 获取宽基 ETF 清单 + 交易日历
        t0 = time.monotonic()
        # 宽基 ETF 清单按日缓存（key=当日日期字符串），跨日自动失效重取
        today = now_cn_ts.strftime("%Y-%m-%d")
        with self._cache_lock:
            list_hit = self._list_cache if self._list_cache and self._list_cache[0] == today else None
        if list_hit is not None:
            _, codes, name_map = list_hit
        else:
            etf_df = self._gw.get_code_info(security_type="EXTRA_ETF")
            t1 = time.monotonic()
            broad_based = self._filter_broad_based(etf_df)  # → list[(code, name)]
            codes = [c for c, _ in broad_based]
            name_map = {c: n for c, n in broad_based}
            with self._cache_lock:
                self._list_cache = (today, codes, name_map)
            t2 = time.monotonic()
            logger.info(
                "etf_flow get_code_info: gateway=%.3fs filter=%.3fs broad_based=%d/%d",
                t1 - t0, t2 - t1, len(codes), len(etf_df) if etf_df is not None else 0,
            )
        calendar = self._gw.calendar

        if not codes:
            return []

        # 2.5 扩展拉取区间：前扩 1 交易日 + 后扩 10 天
        # 原因：
        #   ① diff() 首日为 NaN，需前一日数据才能算出第一天的份额变动；
        #   ② 深市 share CHANGE_DATE snap 到前一交易日后，begin_date 当天可能 snap 到区间外；
        #   ③ 深市 T+1 公告，T 为节前最后一天时 T+1 为节后第一天（最长 ~10 天），
        #     后扩 10 天确保 SDK 能拉到深市最新公告的数据。
        # 扩展后用 fetch_begin/fetch_end 拉 SDK 数据，计算完 diff() 后再用原始 start_dt/end_dt 过滤。
        fetch_begin, fetch_end = begin_date, end_date
        if calendar:
            import bisect
            cal_sorted = sorted(calendar)
            # 前扩 1 个交易日：找 <= begin_date 的最大交易日的前一个交易日
            if begin_date is not None:
                idx = bisect.bisect_left(cal_sorted, begin_date)
                if idx >= 2:
                    fetch_begin = cal_sorted[idx - 2]
                elif idx == 1:
                    fetch_begin = cal_sorted[0]
            # 后扩 10 天：统一加 10 天（不分情况判断），覆盖深市 T+1 公告跨周末/长假。
            # 多拉的数据量极小（每只每天 1 条），计算完 diff() 后用 end_dt 过滤，用户区间不受影响。
            if end_date is not None:
                from datetime import datetime as _dt, timedelta as _td
                _e = _dt.strptime(str(end_date), "%Y%m%d")
                fetch_end = int((_e + _td(days=10)).strftime("%Y%m%d"))
            logger.debug(
                "etf_flow 扩展拉取区间: %s..%s → %s..%s (前扩1交易日, 后扩10天)",
                begin_date, end_date, fetch_begin, fetch_end,
            )

        # 3. 拉取份额+净值时序（用扩展后的区间，local_path/is_local 由 Gateway 内部从 Config 读取）
        t3 = time.monotonic()
        share_dict = self._gw.get_fund_share(
            codes, is_local=False, begin_date=fetch_begin, end_date=fetch_end
        )
        t4 = time.monotonic()
        nav_dict = self._gw.get_fund_nav(
            codes, is_local=False, begin_date=fetch_begin, end_date=fetch_end
        )
        t5 = time.monotonic()
        logger.info(
            "etf_flow fetch: share=%.3fs nav=%.3fs codes=%d",
            t4 - t3, t5 - t4, len(codes),
        )

        # 4. 计算净流入（传入 start_dt/end_dt 用于日期过滤，calendar 用于深市日期修正）
        records = self._compute_net_inflow(
            codes, name_map, share_dict, nav_dict, start_dt, end_dt, calendar
        )
        t6 = time.monotonic()
        logger.info(
            "etf_flow compute: %.3fs records=%d",
            t6 - t5, len(records),
        )
        # 写结果缓存：key=(start_time, end_time)，64 条上限整体清空
        with self._cache_lock:
            if len(self._result_cache) >= 64:
                self._result_cache.clear()
            self._result_cache[cache_key] = (time.monotonic(), records)
        return records

    @staticmethod
    def _filter_broad_based(df: pd.DataFrame) -> list[tuple[str, str]]:
        """从 ETF DataFrame 筛选宽基 ETF，返回 [(code, name), ...]。

        匹配规则（任一命中即视为宽基候选，再经排除词过滤）：
        1. 简称含任一 BROAD_BASED_KEYWORDS（全称，如"沪深300"）
        2. 简称匹配任一 NUMERIC_PATTERNS（纯数字/代号简称，如"300ETF"/"A500"）

        排除规则（命中任一即排除）：
        - 简称含任一 EXCLUDE_KEYWORDS（行业/主题/风格因子）
        - 简称以任一 EXCLUDE_SUFFIXES 结尾（仅对 NUMERIC_PATTERNS 命中的二次过滤,
          排除"xxx增/xxx指增/xxx指数"等增强类衍生产品）

        简称为空/NaN → 跳过。

        向量化实现：用 str.contains / str.match 正则一次匹配全部，避免 iterrows。
        """
        if df is None or df.empty:
            return []
        import pandas as pd

        names = df.get("symbol")
        if names is None:
            return []
        names = names.astype(str)
        # 跳过空/nan
        valid = (names != "nan") & (names != "") & names.notna()
        names_valid = names[valid]

        # 命中宽基：全称关键词 OR 数字代号模式
        broad_pattern = "|".join(BROAD_BASED_KEYWORDS)
        numeric_pattern = "|".join(f"(?:{p})" for p in NUMERIC_PATTERNS)
        has_broad = names_valid.str.contains(broad_pattern, na=False) | \
                    names_valid.str.match(numeric_pattern, na=False)

        # 排除关键词：行业/主题/风格因子（全称和数字模式统一过滤）
        exclude_pattern = "|".join(EXCLUDE_KEYWORDS)
        has_exclude = names_valid.str.contains(exclude_pattern, na=False)

        # 排除后缀：仅对"数字代号命中但非全称命中"的简称做二次过滤
        # （全称如"中证1000"已明确是宽基,不应被"指数"后缀误伤——但实际全称不含"指数",
        #  此过滤主要拦截"300指数/500指增"等数字简称衍生品）
        only_numeric = has_broad & ~names_valid.str.contains(broad_pattern, na=False)
        suffix_pattern = "|".join(f"(?:{s})$" for s in EXCLUDE_SUFFIXES)
        has_suffix_exclude = only_numeric & names_valid.str.contains(suffix_pattern, na=False)

        selected = names_valid[has_broad & ~has_exclude & ~has_suffix_exclude]

        return [(str(idx), str(name)) for idx, name in selected.items()]

    @staticmethod
    def _compute_net_inflow(
        codes: list[str],
        name_map: dict[str, str],
        share_dict: dict[str, pd.DataFrame],
        nav_dict: dict[str, pd.DataFrame],
        start_dt: datetime | None,
        end_dt: datetime | None,
        calendar: list[int] | None = None,
    ) -> list[dict]:
        """合并份额+净值，计算每日净流入。返回经 serialize_dataframe 处理的 list[dict]。

        start_dt/end_dt: datetime 类型，用于日期过滤；None 时该侧不过滤。
        日期过滤统一用 strftime("%Y-%m-%d") 转字符串比较（对标 adj_factor_service._filter_by_date）。
        calendar: 交易日历 list[int]（8 位整型），用于深市份额 CHANGE_DATE 修正时 snap 到前一个交易日。
        """
        import pandas as pd

        all_frames: list[pd.DataFrame] = []
        for code in codes:
            share_df = share_dict.get(code)
            nav_df = nav_dict.get(code)
            if share_df is None or share_df.empty:
                continue

            # 规范化日期列为 YYYY-MM-DD 字符串，按日期排序
            # 传入 code 用于判断沪深市（深市份额 CHANGE_DATE 实际是公告日，需修正）
            # 传入 calendar 用于深市日期修正时 snap 到前一个交易日（避免落在周末）
            share_df = EtfFlowService._normalize_share_df(share_df, code, calendar)
            nav_df = (
                EtfFlowService._normalize_nav_df(nav_df)
                if nav_df is not None and not nav_df.empty
                else None
            )

            # 合并份额+净值（按交易日对齐 left join，净值缺失时用前值填充）
            if nav_df is not None:
                merged = share_df.merge(nav_df, on="date", how="left")
                merged["nav"] = merged["nav"].ffill()  # 净值前值填充
            else:
                merged = share_df.copy()
                merged["nav"] = None

            # 计算净流入
            merged["net_inflow_share"] = merged["share"].diff()  # 份额变动（万份）
            merged["net_inflow_amount"] = merged["net_inflow_share"] * merged["nav"]

            # 日期过滤（用 datetime.strftime 统一为 YYYY-MM-DD 字符串比较）
            if start_dt is not None:
                merged = merged[merged["date"] >= start_dt.strftime("%Y-%m-%d")]
            if end_dt is not None:
                merged = merged[merged["date"] <= end_dt.strftime("%Y-%m-%d")]

            if merged.empty:
                continue

            # 补充 code/name 列
            merged["code"] = code
            merged["name"] = name_map.get(code, "")
            # 重排列顺序: code, name, date(交易日), share, nav, net_inflow_share, net_inflow_amount
            merged = merged[
                ["code", "name", "date", "share", "nav",
                 "net_inflow_share", "net_inflow_amount"]
            ]
            all_frames.append(merged)

        # 统一序列化：处理 NaN→None、NumPy 标量→Python 原生
        if not all_frames:
            return []
        combined = pd.concat(all_frames, ignore_index=True)
        return serialize_dataframe(combined)

    @staticmethod
    def _normalize_share_df(
        df: pd.DataFrame,
        code: str = "",
        calendar: list[int] | None = None,
    ) -> pd.DataFrame:
        """规范化份额 DataFrame → 含 date/share 两列，按 date 排序。

        SDK 返回的 DataFrame：含 FUND_SHARE/CHANGE_DATE/ANN_DATE。
        - date 列用 CHANGE_DATE（变动日期）：份额变动的实际交易日
        - share 列 = FUND_SHARE

        日期语义说明（沪深差异）：
        ETF 份额变动是 T 日交易收盘后的申赎结果。
        - 沪市：CHANGE_DATE=T（变动日），ANN_DATE=T+1（公告日），两者差一天，CHANGE_DATE 可信
        - 深市：CHANGE_DATE 和 ANN_DATE 相同，均填公告日（T+1），CHANGE_DATE 不可信，
          需还原为真实变动日 T，否则份额日期比净值日期滞后一天导致 merge 错位

        深市修正方法：
        CHANGE_DATE 是公告日 T+1，真实变动日 T = 日历中小于 CHANGE_DATE 的最大交易日。
        例如 CHANGE_DATE=20260720（周一，公告日），前一交易日是 7/17（周五），
        则真实变动日 = 7/17。需传入 calendar（交易日历 list[int]）才能 snap。
        无 calendar 时退化为简单减 1 天（可能落在周末，ffill 会兜底净值对齐）。

        用修正后的 CHANGE_DATE 作为 date，使日期代表"资金实际流入/流出的交易日"，
        与 nav 的 PRICE_DATE（净值计算日）天然对齐。
        """
        import pandas as pd

        # date 列优先用 CHANGE_DATE（变动日期），无则用 ANN_DATE
        if "CHANGE_DATE" in df.columns:
            date_series = df["CHANGE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        # 深市 ETF（.SZ 后缀）：CHANGE_DATE 实际是公告日（T+1），还原为变动日 T
        # 判断依据：深市 CHANGE_DATE == ANN_DATE，沪市 CHANGE_DATE != ANN_DATE（差一天）
        is_sz = code.endswith(".SZ")
        if is_sz and "CHANGE_DATE" in df.columns and "ANN_DATE" in df.columns:
            same_as_ann = (df["CHANGE_DATE"] == df["ANN_DATE"]).all()
            if same_as_ann:
                dt = pd.to_datetime(dates, format="%Y-%m-%d", errors="coerce")
                if calendar:
                    # 用交易日历 snap：找日历中小于 CHANGE_DATE 的最大交易日（即前一交易日 T）
                    cal_set = set(calendar)
                    cal_sorted = sorted(cal_set)
                    import bisect
                    def snap_to_prev_trade(d):
                        if pd.isna(d):
                            return d
                        d_int = int(d.strftime("%Y%m%d"))
                        # bisect_left 找 d_int 在 cal_sorted 的插入位置，前一格就是 < d_int 的最大交易日
                        idx = bisect.bisect_left(cal_sorted, d_int)
                        if idx > 0:
                            prev_int = cal_sorted[idx - 1]
                            return pd.to_datetime(str(prev_int), format="%Y%m%d")
                        return d  # 日历里找不到，保持原值
                    dt = dt.apply(snap_to_prev_trade)
                else:
                    # 无日历时退化为简单减 1 天（可能落在周末，ffill 兜底）
                    dt = dt - pd.Timedelta(days=1)
                dates = dt.dt.strftime("%Y-%m-%d")

        result = pd.DataFrame({
            "date": dates.values if hasattr(dates, "values") else dates,
            "share": df["FUND_SHARE"].values if "FUND_SHARE" in df.columns else None,
        })
        result = result.sort_values("date").reset_index(drop=True)
        return result

    @staticmethod
    def _normalize_nav_df(df: pd.DataFrame) -> pd.DataFrame:
        """规范化净值 DataFrame → 含 date/nav 两列，按 date 排序。

        SDK 返回的 DataFrame：含 UNIT_NAV/PRICE_DATE/ANN_DATE。
        - date 列用 PRICE_DATE（净值交易日）：与 share 的 CHANGE_DATE 对齐
        - nav 列 = UNIT_NAV

        日期语义说明：
        PRICE_DATE 是净值计算日（T 交易日），与 share 的 CHANGE_DATE（同为 T）对齐。
        ANN_DATE 是公告日（T+1），不用作 date。
        """
        import pandas as pd

        # date 列优先用 PRICE_DATE（净值交易日），无则用 ANN_DATE
        if "PRICE_DATE" in df.columns:
            date_series = df["PRICE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        result = pd.DataFrame({
            "date": dates.values if hasattr(dates, "values") else dates,
            "nav": df["UNIT_NAV"].values if "UNIT_NAV" in df.columns else None,
        })
        result = result.sort_values("date").reset_index(drop=True)
        return result
