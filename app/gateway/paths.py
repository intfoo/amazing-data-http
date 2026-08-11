"""路径解析模块函数：从实例方法平移，__file__ 偏移修正为 parents[2]。"""

from __future__ import annotations

from pathlib import Path

from app.gateway.base import logger


def resolve_adj_factor_local_path(configured: str) -> str:
    """解析 adj_factor 本地缓存基目录：配置非空用配置，否则用项目根 data 兜底 + 警告。

    启动时（实例化）调用，目录不存在则自动创建。SDK 要求绝对路径，且**末尾必须带分隔符**。

    SDK 字符串拼接坑（详见 base_data.pyc get_adj_factor 反汇编 + LocalDataFolder enum）：
    SDK 内部构造缓存路径用 `local_path + 'basedata/adj_factor/'`（字符串 +，非 os.path.join）：
        folder_name = LocalDataFolder.BASEDATA.value + '/' + LocalDataFolder.ADJ_FACTOR.value
        path = local_path + folder_name + '/'
    - local_path 末尾无分隔符（如 'D:/.../data/adj_factor'）→ 拼成 'D:/.../data/adj_factorbasedata/adj_factor/'
      （'adj_factor' 与 'basedata' 粘在一起，目录名错乱但能工作）
    - local_path 末尾带分隔符（如 'D:/.../data/'）→ 拼成 'D:/.../data/basedata/adj_factor/'（正确）
    手册 3.5.2.6 注(1) 示例 'D://AmazingData_local_data//' 末尾双斜杠正是为此设计。
    本方法强制末尾带分隔符，避免依赖调用方记忆此坑。

    兜底路径用 `项目根/data`（不带 `adj_factor` 后缀）：SDK 会自动在下面建
    `basedata/adj_factor/` 子目录，不需要在 local_path 里预设。若预设 `adj_factor` 后缀
    且末尾带分隔符，SDK 会拼出 `data/adj_factor/basedata/adj_factor/`（多一层 adj_factor）。
    """
    if configured:
        local_path = str(Path(configured).resolve())
    else:
        # 默认项目根/data（paths.py 在 app/gateway/，parents[2] = 项目根）
        local_path = str(Path(__file__).resolve().parents[2] / "data")
        logger.warning(
            "ADJ_FACTOR_LOCAL_PATH 未配置，使用默认路径: %s"
            "（建议设为持久化绝对路径以供 SDK 缓存）",
            local_path,
        )
    # SDK 字符串拼接要求末尾带分隔符，否则 'data/adj_factor' + 'basedata' → 'adj_factorbasedata'
    if not local_path.endswith(('/', '\\')):
        local_path = local_path + '/'
    Path(local_path).mkdir(parents=True, exist_ok=True)
    return local_path


def resolve_fund_local_path(configured: str) -> str:
    """解析 fund 本地缓存基目录：配置非空用配置，否则用项目根 data 兜底 + 警告。

    逻辑完全对标 resolve_adj_factor_local_path()，读 fund_local_path 配置。
    SDK get_fund_share/get_fund_nav 的 local_path 同样要求绝对路径且末尾带分隔符。
    """
    if configured:
        local_path = str(Path(configured).resolve())
    else:
        # 默认项目根/data（paths.py 在 app/gateway/，parents[2] = 项目根）
        local_path = str(Path(__file__).resolve().parents[2] / "data")
        logger.warning(
            "FUND_LOCAL_PATH 未配置，使用默认路径: %s"
            "（建议设为持久化绝对路径以供 SDK 缓存）",
            local_path,
        )
    # SDK 字符串拼接要求末尾带分隔符
    if not local_path.endswith(('/', '\\')):
        local_path = local_path + '/'
    Path(local_path).mkdir(parents=True, exist_ok=True)
    return local_path
