"""storage.providers.mydb_store — mydb 私有存储读写（0.9.2 批次 3 从 app.py 搬迁）。

上游 stockdb 内置私有存储 ./mydb：HTTP 层只读，写入须用 pybao 客户端
（stockdb.abi3.so + stock_sdk.py，随发行包分发，PYTHONPATH 注入）。
本机开发若未装 pybao，相关接口优雅降级（A 股功能不受影响）。
行为与 app.py 搬迁前完全一致（0.8.10 起：锁 + 自愈重连 + 值归一化）。

职责（0.9.5 研究成果迁自持 SQLite 后收窄）：hk日k 港股日K、用户自定义表
（/api/data/write 开放命名空间）、RESEARCH_STORE=mydb 回滚写回——
打板指标/序列/清单/竞价快照主线已迁 storage/research_store.py。
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time  # 0.9.14：mydb_tables 30s 缓存

import config  # 模块引用（config.STOCKDB_HOST/PORT 动态读取，测试 patch 生效）

# 保留表前缀：禁止覆盖上游同步数据，防止与 A 股行情冲突
_RESERVED_TABLES = ("日k", "分钟k", "复权", "股票代码", "周k", "月k", "板块", "行业", "概念")


def _mydb_import():
    """惰性导入 pybao 客户端。未安装/加载失败时抛 ImportError（调用方降级）。

    候选路径：容器内 /opt/stockdb/pybao，本地开发 /tmp/pybao_mac 等；
    本机开发经 PYTHONPATH 注入原生 pybao 目录（见 docs/development-guide.md）。
    """
    candidates = ["/opt/stockdb/pybao", "/tmp/pybao_mac"]
    for p in candidates:
        try:
            sys.path.insert(0, p)
            return importlib.import_module("stockdb")
        except ImportError:
            continue
    raise ImportError("pybao 写库不可用（PYTHONPATH 未注入或平台不兼容）")


_rd_lock = threading.RLock()  # pybao rd 单连接非线程安全：全部 rd 读写持锁串行化（0.8.10）
# 事故背景 2026-08-16：/api/data/* 与回填线程并发用同一 socket，协议帧交错 →
# C 扩展阻塞持 GIL → 全进程冻结。锁保证同一时刻只有一条 rd 请求在线上。
# 0.9.11：改为 RLock 并让 _mydb_rd 初始化自身持锁——连接创建也串行化，且
# interfaces/mcp/pybao_tools 复用本锁（同进程容器与 MCP 共享同一底层连接）。


def _mydb_rd_reset():
    """丢弃缓存的 rd 连接：调用失败后置空，下次调用重新 init（0.8.10 自愈楔死连接）。"""
    with _rd_lock:
        _mydb_rd._rd = None


def _mydb_rd():
    """获取连接 stockdb 的 pybao 客户端（惰性、缓存，函数对象属性持连接）。

    0.9.11：初始化自身持 _rd_lock（RLock 可重入，调用方再持锁不冲突）——
    首次并发访问不再产生双连接（socket 泄漏）；调用方（mydb_read/write/tables
    及 pybao_tools.rd_get/rd_keys）仍应持锁执行请求。
    """
    with _rd_lock:
        if getattr(_mydb_rd, "_rd", None) is None:
            mod = _mydb_import()
            _mydb_rd._rd = mod.init(config.STOCKDB_HOST, int(config.STOCKDB_PORT),
                                    socket_timeout=5)
        return _mydb_rd._rd


def _rd_to_py(v):
    """pybao 返回值归一化：QueryResult → 原生 list/dict；原生数据与 JSON 串原样透出。

    0.8.10：旧实现转换失败原样返回 QueryResult，json.dumps 直接崩
    （"Object of type QueryResult is not JSON serializable"，/api/data/read 实证）。
    0.10.41（NAS 实机定位，两处同源缺陷）：
      ① **必须先试 `.do()`**：QueryResult 是惰性响应对象，`dict(QueryResult)` 会走
         `.keys()`，而 `.keys()` 在 vals/keys 结果上返回错误文案字符串
         （实测 `'Missing required parameters'`）→ `{"M": {}, "i": {}, ...}` 逐字符垃圾。
         后果：`mydb_tables()` 返回 `[" ", "M", "a", …]`、`mydb_read(table, "")` 同款垃圾、
         `hk_klines()` 恒空（**港股写入进得去、读不出来**）。MCP 侧 `pybao_tools._to_py`
         一直有这层 `.do()`，本函数缺失 → 两处行为分叉。
      ② **原生数据必须原样透出**：键列表是 `list[str]`（如 `['t:20260814', …]`），
         草案一度对字符串做 `json.loads` → 解析失败返回 None，把合法键全抹成 `[None, None]`。
         故顺序＝QueryResult → JSON 串 → 其它原样返回。
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return list(v)          # 键列表等原生序列：原样透出（绝不逐项 JSON 解析）
    if isinstance(v, str):
        try:                    # 单值 JSON 串 → 对象；非 JSON → 无效值（旧契约）
            return json.loads(v)
        except ValueError:
            return None
    if isinstance(v, (dict, int, float, bool)):
        return v
    if hasattr(v, "do") and callable(v.do):
        try:  # QueryResult：do() 才真正取回原生数据（dict 或 list）
            return _rd_to_py(v.do())
        except Exception:  # noqa: BLE001 - 转换失败按缺失处理
            return None
    if hasattr(v, "keys") and hasattr(v, "all"):
        try:  # 兼容旧形态替身（带 keys/all 的 mapping-like）
            return dict(v)
        except Exception:  # noqa: BLE001
            return None
    return None


def validate_custom_table(table: str) -> str:
    """校验自定义表名：禁止覆盖上游保留表，禁止危险字符。返回规范化表名。"""
    t = str(table or "").strip().strip(":")
    if not t:
        raise ValueError("表名不能为空")
    if not all(ch.isalnum() or ch in "_:-" for ch in t):
        raise ValueError("表名只能含字母数字与 _:-")
    for r in _RESERVED_TABLES:
        if t == r or t.startswith(r + ":"):
            raise ValueError(f"表名 {t!r} 与上游保留表 {r!r} 冲突，请用自定义命名空间（如 hk日k: / 自定义:）")
    return t


def _has_nan_inf(value) -> bool:
    """递归检查 value 中是否含 NaN/Inf 浮点（0.9.4 写前护栏）。

    pybao 存原生 dict 时 NaN/Inf 会导致序列化失败/脏数据——写入前拦截。
    """
    import math
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, list):
        return any(_has_nan_inf(v) for v in value)
    if isinstance(value, dict):
        return any(_has_nan_inf(v) for v in value.values())
    return False


def mydb_write(table: str, items: list[tuple], batch: bool = False) -> dict:
    """写入 mydb 私有存储。items=[(key, value), ...]。

    注意：pybao 的 rd.set 返回 QueryResult，必须调用 .do() 才真正发送写入
    （否则只是客户端排队，读不到）。batch 参数保留兼容，统一逐条 .do()。
    0.8.10：全程持 _rd_lock；任何 rd 异常 → 丢弃缓存连接（下次调用重连自愈）。
    0.9.4：写前护栏——含 NaN/Inf 的条目剔除并计数（skipped_invalid），不落盘
    （拦截脏数据污染研究资产）；全部被拦截 → ValueError（调用方告警）。
    """
    table = validate_custom_table(table)
    clean: list[tuple] = []
    skipped = 0
    for key, value in items:
        if _has_nan_inf(value):
            skipped += 1
            continue
        clean.append((key, value))
    if not clean:
        raise ValueError(f"没有可写入的数据（{skipped} 条被 NaN/Inf 护栏拦截）")
    with _rd_lock:
        try:
            rd = _mydb_rd()
            result = []
            for key, value in clean:
                result.append(rd.set(table, key, value).do())
            # 回读校验（QueryResult 转原生）
            readback = []
            for key, _ in clean:
                try:
                    readback.append(_rd_to_py(rd.get(table, key)))
                except Exception:  # noqa: BLE001 - 单键回读失败按缺失
                    readback.append(None)
            return {"table": table, "written": len(clean), "skipped_invalid": skipped,
                    "readback": readback, "result": result}
        except Exception:
            _mydb_rd_reset()
            raise


def _rd_get_any(table: str, seg: str, key: str):
    """按候选键形态逐个读，返回首个非空值；全部落空 → None，全异常 → 上抛首个异常。

    0.10.41（NAS 实机取证，同一台 0.3.5 引擎）：
      - **三段键**（`hk日k` 的 `00700` + `20240828`）：`get(table, code, date)` 命中；
      - **两段键**（`打板指标` / `自定义` 的 `metrics` / `daily_notes`）：`get(table, key)` 命中；
      - `get(table, "00700:20240828")` **两段拼接形态恒返回 `[]`**（实测），带表名的整串同样恒空。
    修复前 `mydb_read(table, "")` 的 658 个键全空、MCP `query_mydb` 全 null——
    正是"港股写入进得去、读不出来"。
    候选顺序：含冒号的键先按「段1 + 段2」走三段，再退整串两段；单段键直接两段。
    异常语义：某候选抛错（rd 楔死等）时继续试其余候选；**全部候选都失败时上抛首个
    异常**，调用方据此重置连接自愈（0.8.10 契约，用例断言 RuntimeError）。
    """
    with _rd_lock:
        rd = _mydb_rd()
        key = str(key)
        candidates: list[tuple] = []
        if ":" in key:                                  # 复合键：三段优先（实测唯一命中形态）
            seg0, _, rest = key.partition(":")
            if seg0 and rest:
                candidates.append((table, seg0, rest))
            candidates.append((table, key))             # 两段兜底（保留旧契约）
        else:
            candidates.append((table, key))             # 单段键：表名之后就是键
            if seg and seg != table:
                candidates.append((table, f"{seg}:{key}"))
        first_exc: Exception | None = None
        for cand in candidates:
            try:
                val = _rd_to_py(rd.get(*cand))
            except Exception as exc:  # noqa: BLE001 - 形态不符 → 换下一候选
                if first_exc is None:
                    first_exc = exc
                continue
            if val:
                return val
        if first_exc is not None:
            raise first_exc
        return None


def rd_get_strip_table(table: str, full_key: str):
    """读一条"带表名前缀"的完整键（`rd.keys` 输出的形态），自动剥前缀并多形态兜底。

    `rd.keys(table, "*")` / `rd.keys(prefix, "*")` 返回的键**含表名段**，形如
    `hk日k:00700:20240828`、`打板指标:20260105:metrics`；而 `rd.get` 要的是
    **表名之后**的部分。把整串喂给 `rd.get(table, full)` 在 0.3.5 引擎上恒返回空
    （0.10.41 NAS 实机取证：`mydb_read("hk日k", "")` 658 键全空、MCP `query_mydb` 同款）。
    先试剥前缀形态，落空再回退整串，兼容两种键契约。
    """
    full = str(full_key)
    sub = full.split(":", 1)[1] if ":" in full else full
    if sub != full:
        val = _rd_get_any(table, "", sub)
        if val:
            return val
    return _rd_get_any(table, "", full)


def mydb_read(table: str, key: str = "") -> dict:
    """读取 mydb 自定义表。key 为空时列出表内全部键值。
    0.8.10：持 _rd_lock；值统一 _rd_to_py 归一化；rd 异常 → 丢弃连接自愈。
    0.9.12：全表列取改细粒度持锁——keys 枚举一次持锁，逐键 get 每次独立持锁。
    此前 500 键连续持锁（引擎慢时 25s+），MCP 快速通道/打板任务等全部 rd 访问
    排队 → 点击多时 webui 假死。面板展示对中间态不敏感，可接受。"""
    table = validate_custom_table(table)
    if key:
        # 0.10.41：单键读也必须走两段形态（此前 `rd.get(table, key)`，key 为
        # "00700:20240828" 时被当成单参 → 引擎恒返回空；NAS 实机取证）。
        # 0.10.41：异常仍须丢弃缓存连接（自愈契约，_rd_lock 可重入）
        with _rd_lock:
            try:
                val = _rd_get_any(table, "", key)
            except Exception:
                _mydb_rd_reset()
                raise
        return {"table": table, "key": key, "value": val}
    with _rd_lock:
        try:
            rd = _mydb_rd()
            # 0.10.41：经 _rd_to_py（内部 .do()）取键列表——直接迭代 QueryResult 会得到
            # 错误文案逐字符（实机 `mydb_read(table, "")` 返回 {"M": {}, "i": {}, ...}）
            keys = _rd_to_py(rd.keys(table, "*"))
        except Exception:
            _mydb_rd_reset()
            raise
    if not isinstance(keys, list):
        keys = []
    values = {}
    for k in keys:
        # 0.9.11：复合键解析（split(":", 1) 保留代码段）——键形如
        # "hk日k:00700:20250425"，此前 split(":")[-1] 只取日期段，
        # 同一日期多只股票时读出错误记录/读不到（pybao_tools.query_mydb
        # 已修同款，app 侧同步）
        #
        # 0.10.41：键来自 rd.keys，含表名前缀，必须剥段后多形态读
        # （此前 split(":", 1)[-1] 得到整串 → 引擎侧无此键，658 键全空，实机取证）
        full = str(k)
        try:  # 0.9.12：逐键独立持锁（细粒度，见函数注释）
            with _rd_lock:
                values[full] = rd_get_strip_table(table, full)
        except Exception:  # noqa: BLE001 - 单键失败按缺失，但连接须丢弃自愈
            values[full] = None
            _mydb_rd_reset()
    return {"table": table, "keys": keys, "values": values}


# 业务命名空间前缀（0.9.14：mydb_tables 弃用 keys("*") 全库扫描——引擎全库枚举
# 数十万键、持 _rd_lock 数十秒，面板点击即卡死全站 rd；research_store 已明令
# 禁止 keys("*")。改逐前缀枚举，覆盖实际使用的表空间）
_MYDB_TABLE_PREFIXES = ("hk日k", "打板指标", "竞价快照", "打板序列", "清单", "自定义")

_tables_cache: dict = {"at": 0.0, "val": None}  # 30s 缓存：面板 4s 轮询不重复扫描


def mydb_tables() -> list[str]:
    """列出自定义表名（业务命名空间前缀枚举）。

    0.9.14：弃用 rd.keys("*") 全库扫描（引擎串行处理全库枚举极慢且持 _rd_lock
    阻塞全部 rd 访问——面板 mydb 页点击即全站卡顿）；改对已知业务前缀逐前缀
    keys(prefix, "*")（单前缀枚举毫秒级），30s 缓存防 4s 轮询重复扫描。
    命名空间之外的 AI 自定义表不在列表（引擎无廉价的全库枚举手段）。
    """
    now = time.time()
    if _tables_cache["val"] is not None and now - _tables_cache["at"] < 30:
        return _tables_cache["val"]
    tables: set[str] = set()
    for prefix in _MYDB_TABLE_PREFIXES:
        with _rd_lock:
            try:
                rd = _mydb_rd()
                # 0.10.41：必须经 _rd_to_py（内部走 .do()）——直接迭代 QueryResult 会得到
                # 错误文案的逐字符（NAS 实测 `mydb_tables()` 返回 [' ', 'M', 'a', ...]）
                keys = _rd_to_py(rd.keys(prefix, "*"))
            except Exception:  # noqa: BLE001 - 单前缀失败不影响其余（不重置连接：前缀不存在可能报错）
                continue
        if not isinstance(keys, list):
            continue
        for k in keys:
            table = str(k).split(":")[0] if ":" in str(k) else str(k)
            if table and not any(table.startswith(r) for r in _RESERVED_TABLES):
                tables.add(table)
    result = sorted(tables)
    _tables_cache.update(at=now, val=result)
    return result


# ---- 打板序列薄封装（0.9.2 批次 3 迁入；读/写链路由 services 层注入） ----

def auction_series_read(key: str):
    """打板序列读取封装：read_fn(key) -> 原始存储值|None（JSON 解析交给 load_series）。

    mydb 物理布局：表=打板序列:<metric>、子键="series"；mydb_read(key, "") 列出
    {子键: 值}，取任一非 None 值返回。读取异常 → None（冷启动空序列，不阻塞采集）。
    """
    try:
        res = mydb_read(key, "")
        for v in (res.get("values") or {}).values():
            if v is not None:
                return v
    except Exception:  # noqa: BLE001 - 序列缺失/损坏 → 调用方按空序列处理
        return None
    return None


def auction_series_write(key: str, value) -> None:
    """打板序列写入封装：write_fn(key, value)；子键固定 "series"，覆盖写幂等。"""
    mydb_write(key, [("series", value)])  # 0.8.6：pybao 存原生 dict
