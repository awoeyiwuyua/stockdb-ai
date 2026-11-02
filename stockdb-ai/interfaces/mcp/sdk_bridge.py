"""sdk_bridge — 上游 stockdb_full_mcp 41 工具契约外壳（0.9.0 M4 / 0.16.1 慢请求隔离版）。

设计见 docs/design/sdk-mcp-bridge.md（已评审合并）。
原则：复用上游 stockdb_* 函数（参数归一化/安全校验在内部），外壳做五件事：
  1. 参数 schema 生成（inspect.signature + docstring）→ 注册进 MCP TOOLS
  2. 调用封装：强制 df=False / panel=False（纯 JSON 契约），只传用户提供的参数
  3. 错误码映射：上游错误字符串 / ValueError → 统一错误码（INVALID_ARGUMENT /
     SOURCE_TIMEOUT / SOURCE_UNAVAILABLE / BUSY / INTERNAL_ERROR / DEPENDENCY_UNAVAILABLE）
  4. 结果契约化：上游 str(repr) 三级解析（json → ast.literal_eval → 原文），
     大结果截断 + truncated 标记
  5. 进程隔离与慢请求保护（0.16.1）：
     - Linux 环境 fork 子进程执行，彻底杜绝 C 扩展持 GIL 冻结主服务事件循环；
     - 熔断降级：检测上游 tcp connect error 自动熔断 30s，快速返回 SOURCE_UNAVAILABLE；
     - 客户端断开检测：客户端超时离线时及时 kill 子进程，防止队列无限堆积；
     - 超时控制：严格遵循 STOCKDB_SDK_TIMEOUT（默认 10s），超时返回 SOURCE_TIMEOUT；
     - 并发限流：有界信号量 STOCKDB_SDK_CONCURRENCY（默认 4），满员快速返回 BUSY。

本模块自身不 import 上游（懒加载），任何环境下均可安全导入。
"""
from __future__ import annotations

import ast
import inspect
import json
import multiprocessing as mp
import os
import threading
import time
from typing import Callable

# ---- 0.16.1：超时、并发与进程隔离配置 ----
STOCKDB_SDK_TIMEOUT: float = float(os.getenv("STOCKDB_SDK_TIMEOUT", "10.0"))
STOCKDB_SDK_CONCURRENCY: int = int(os.getenv("STOCKDB_SDK_CONCURRENCY", "4"))
STOCKDB_SDK_CIRCUIT_COOLDOWN: float = float(os.getenv("STOCKDB_SDK_CIRCUIT_COOLDOWN", "30.0"))


class SdkBridgeError(Exception):
    """SDK Bridge 基础异常，携带 contract 错误码。"""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR", hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class SourceTimeoutError(SdkBridgeError):
    """上游请求超时（>预算时间）。"""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, code="SOURCE_TIMEOUT", hint=hint)


class SourceUnavailableError(SdkBridgeError):
    """上游服务不可用（连接超时、拒绝或熔断冷却）。"""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, code="SOURCE_UNAVAILABLE", hint=hint)


class ServerBusyError(SdkBridgeError):
    """并发达到上限。"""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, code="BUSY", hint=hint)


class ClientDisconnectedError(SdkBridgeError):
    """客户端已主动断开连接。"""

    def __init__(self, message: str = "客户端已断开"):
        super().__init__(message, code="CLIENT_DISCONNECTED")


# ---- 熔断器：上游连接故障快速失败 ----
_CIRCUIT_LOCK = threading.Lock()
_circuit_open_until: float = 0.0
_circuit_last_error: str = ""

_CONNECT_FAILURE_MARKERS = (
    "connect error", "connection timed out", "connection refused",
    "error trying to connect", "os error 110", "os error 111",
    "network is unreachable", "host is unreachable", "tcp connect error"
)


def is_circuit_open() -> tuple[bool, str]:
    """判断上游熔断器是否处于打开状态。"""
    global _circuit_open_until, _circuit_last_error
    now = time.monotonic()
    with _CIRCUIT_LOCK:
        if now < _circuit_open_until:
            rem = max(1, int(_circuit_open_until - now))
            return True, f"上游行情服务连接故障（{_circuit_last_error}），熔断保护中（{rem}s 后重试）"
    return False, ""


def record_circuit_failure(err: str, cooldown: float | None = None) -> None:
    """记录上游故障并触发熔断。"""
    global _circuit_open_until, _circuit_last_error
    if cooldown is None:
        cooldown = STOCKDB_SDK_CIRCUIT_COOLDOWN
    with _CIRCUIT_LOCK:
        _circuit_open_until = time.monotonic() + cooldown
        _circuit_last_error = err[:160]


def record_circuit_success() -> None:
    """上游调用成功，关闭熔断。"""
    global _circuit_open_until, _circuit_last_error
    with _CIRCUIT_LOCK:
        _circuit_open_until = 0.0
        _circuit_last_error = ""


def reset_circuit() -> None:
    """重置熔断器（测试/恢复用）。"""
    record_circuit_success()


# ---- 进程级并发与取消检查 ----
_SDK_SEMAPHORE = threading.Semaphore(STOCKDB_SDK_CONCURRENCY)
_cancel_context = threading.local()


def set_cancel_check(fn: Callable[[], bool] | None) -> None:
    """在当前线程设置客户端存活探针。"""
    _cancel_context.check = fn


def get_cancel_check() -> Callable[[], bool] | None:
    """获取当前线程设置的客户端存活探针。"""
    return getattr(_cancel_context, "check", None)


def _get_isolation_mode() -> str:
    mode = os.getenv("STOCKDB_SDK_ISOLATION", "").strip().lower()
    if mode in ("process", "thread", "direct"):
        return mode
    if "fork" in mp.get_all_start_methods():
        return "process"
    return "direct"


_SDK_MP_CTX = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()


def _sdk_worker_exec(fn_name: str, kwargs: dict, pipe):
    """在隔离子进程中执行上游 SDK 工具（独立 GIL，超时由父进程强杀）。"""
    try:
        mod = _module()
        if mod is None:
            pipe.send(("err_import", f"{fn_name}: SDK 工具不可用（stockdb_full_mcp 加载失败: {import_error()}）"))
            return
        fn = getattr(mod, f"stockdb_{fn_name}", None)
        if fn is None or not callable(fn):
            pipe.send(("err_val", f"{fn_name}: 未知 SDK 工具"))
            return
        text = fn(**kwargs)
        pipe.send(("ok", text))
    except ValueError as exc:
        pipe.send(("err_val", str(exc)))
    except Exception as exc:  # noqa: BLE001
        pipe.send(("err_run", f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            pipe.close()
        except Exception:
            pass


# ---- 懒加载上游（无 pybao / 缺文件 → 工具降级 DEPENDENCY_UNAVAILABLE）----
_full_mcp = None
_IMPORT_ERROR: str | None = None

RESULT_CAP = 2000  # 列表结果硬上限（行），超出截断 + truncated 标记

# 工具分组（0.9.3：MCP Gateway——按业务域分组注册，缓解 53 工具占满 LLM 上下文）。
# 组名与 stockdb_mcp_server._BASE_TOOL_GROUPS 共用同一枚举。
SDK_TOOL_GROUPS: dict[str, str] = {
    # 行情数据（K线/竞价/tick/资金流/日历）
    "get_bars": "market_data", "get_price": "market_data", "get_ticks": "market_data",
    "get_last_tick": "market_data", "get_call_auction": "market_data",
    "get_data": "market_data", "get_all_securities": "market_data",
    "get_security_info": "market_data", "get_trade_days": "market_data",
    "get_money_flow": "market_data", "get_mtss": "market_data", "get_extras": "market_data",
    "get_marginsec_stocks": "market_data", "get_margincash_stocks": "market_data",
    # 基本面（财务/估值/解禁/龙虎榜）
    "get_fundamentals": "fundamental", "get_fundamentals_valuation_legacy": "fundamental",
    "get_fundamentals_income_legacy": "fundamental", "get_fundamentals_generic_legacy": "fundamental",
    "get_fundamentals_cash_flow_legacy": "fundamental",
    "get_fundamentals_indicator_legacy": "fundamental",
    "get_fundamentals_continuously": "fundamental", "get_history_fundamentals": "fundamental",
    "get_valuation": "fundamental", "get_locked_shares": "fundamental",
    "get_billboard_list": "fundamental",
    # 因子/指标（alpha/因子看板/技术指标）
    "get_factor_values": "factor_analysis", "get_factor_values_legacy": "factor_analysis",
    "get_factor_kanban_values": "factor_analysis", "get_all_alpha_101": "factor_analysis",
    "get_all_alpha_191": "factor_analysis", "alpha": "factor_analysis",
    "MACD": "factor_analysis",
    # 市场结构（板块/指数/期货）
    "bk_get": "market_structure", "get_industry": "market_structure",
    "get_index_stocks": "market_structure", "get_index_weights": "market_structure",
    "get_index_style_exposure": "market_structure",
    "get_future_contracts": "market_structure", "get_dominant_future": "market_structure",
    # 系统（表查询）
    "list_query_tables": "system_health", "run_query": "system_health",
}

# 上游 41 个 SDK 工具的静态全名清单（与 stockdb_full_mcp.py 的 @mcp.tool() 一一对应）。
# 即使上游模块未加载（无 pybao / 缺文件）也保持已知，用于 DEPENDENCY_UNAVAILABLE 降级。
KNOWN_SDK_TOOL_NAMES: frozenset[str] = frozenset({
    "get_industry", "get_data", "get_all_securities", "get_security_info",
    "get_trade_days", "get_money_flow", "get_ticks", "get_last_tick",
    "get_bars", "get_price", "get_marginsec_stocks", "get_margincash_stocks",
    "get_mtss", "get_extras", "get_call_auction", "get_billboard_list",
    "bk_get",
    "get_fundamentals_valuation_legacy", "get_fundamentals_income_legacy",
    "get_fundamentals_continuously", "get_fundamentals_generic_legacy",
    "get_fundamentals_cash_flow_legacy", "get_history_fundamentals",
    "get_valuation", "get_fundamentals_indicator_legacy", "get_fundamentals",
    "get_locked_shares",
    "get_future_contracts", "get_dominant_future",
    "get_index_weights", "get_index_stocks",
    "get_all_alpha_101", "get_all_alpha_191", "alpha",
    "get_factor_kanban_values", "MACD", "get_factor_values",
    "get_factor_values_legacy", "get_index_style_exposure",
    "list_query_tables", "run_query",
})


def _module():
    """懒加载上游 stockdb_full_mcp 模块；失败返回 None（_IMPORT_ERROR 记原因）。"""
    global _full_mcp, _IMPORT_ERROR
    if _full_mcp is None and _IMPORT_ERROR is None:
        try:
            import stockdb_full_mcp  # noqa: PLC0415 - 懒加载
            _full_mcp = stockdb_full_mcp
        except Exception as exc:  # noqa: BLE001 - 降级语义：任何导入失败都记录
            _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    return _full_mcp


def import_error() -> str | None:
    """上游模块加载失败原因（None = 可用）；供诊断与降级提示。"""
    _module()
    return _IMPORT_ERROR


# ---- 参数 schema 生成（JSON Schema 子集：type/properties/required/enum/items）----

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}

# 显式参数描述覆盖（重点/易错参数；其余参数自动生成"类型+默认值"描述）
_PARAM_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "get_bars": {
        "security": "标的代码字符串（如 '000001'）或代码列表（如 ['000001','600000']）",
        "unit": "K 线周期：1d/1w/1M 或 5m/15m/30m/60m 等",
        "fields": "返回字段列表，如 ['date','open','high','low','close']",
        "fq_ref_date": "复权参考日期（YYYY-MM-DD），用于指定复权基准",
        "include_now": "是否包含未收盘的当日 bar",
        "skip_paused": "是否跳过停牌/未上市日",
    },
    "get_price": {
        "security": "标的代码字符串或列表",
        "frequency": "周期：daily/1d、minute/1m 或任意 Xd/Xm",
        "fields": "字段列表；可选含 factor/high_limit/low_limit/avg/pre_close/paused/open_interest",
        "fq": "复权：pre(前复权，默认)/post(后复权)/none(不复权)",
        "skip_paused": "是否跳过停牌日（默认 False 用前收填充）",
        "count": "获取的周期条数（与 start_date 二选一）",
        "fill_paused": "停牌数据填充模式（默认 True 前收填充）",
        "round": "复权价格是否四舍五入保留固定小数位",
    },
    "get_call_auction": {
        "security": "标的代码字符串或列表",
        "start_date": "起始日期 YYYY-MM-DD（必填）",
        "end_date": "结束日期 YYYY-MM-DD（必填）",
        "fields": "返回字段列表（可选）",
    },
    "get_ticks": {
        "security": "标的代码字符串",
        "start_dt": "起始时间，YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS",
        "end_dt": "结束时间，同上",
        "count": "返回条数",
        "fields": "字段列表",
        "skip": "跳过停牌时段",
    },
    "get_trade_days": {
        "start_date": "起始日期 YYYY-MM-DD",
        "end_date": "结束日期 YYYY-MM-DD",
        "count": "返回天数",
    },
    "run_query": {
        "table": "查询表名，仅允许白名单（bond/finance/opt 分类下的官方表，或 valuation/income/cash_flow/indicator/balance 根路径）",
        "filters": "过滤条件列表，如 [{'field': 'date', 'op': '>=', 'value': '20260101'}]",
        "fields": "投影字段列表",
        "order_by": "排序列表，如 [{'field': 'date', 'desc': True}]",
        "limit": "返回上限（默认 100）",
        "offset": "偏移量（可选）",
    },
    "get_fundamentals": {
        "security": "标的代码字符串或列表",
        "fields": "财务字段列表（如 income.revenue / balance.total_assets）",
        "start_date": "起始日期 YYYY-MM-DD",
        "end_date": "结束日期 YYYY-MM-DD",
        "count": "返回期数",
    },
    "alpha": {
        "date": "计算日期 YYYY-MM-DD（必填）",
        "index": "指数代码或列表",
        "fq": "复权：pre/post/none",
        "alpha": "alpha 编号列表（如 [1, 2, 101]）",
    },
    "MACD": {
        "security_list": "标的代码列表",
        "check_date": "计算日期 YYYY-MM-DD（必填）",
        "SHORT": "快线周期（默认 12）",
        "LONG": "慢线周期（默认 26）",
        "MID": "信号线周期（默认 9）",
        "unit": "K 线周期（默认 1d）",
        "include_now": "是否包含当日",
    },
}


def _type_to_schema(annotation, default=None) -> dict:
    """Python 注解/默认值 → JSON Schema 类型描述（宽松子集）。"""
    ann = annotation
    if ann is inspect.Parameter.empty:
        ann = type(default) if default is not None and default is not inspect.Parameter.empty else None
    if ann is None or ann is inspect.Signature.empty:
        return {"type": "string"}  # 无注解宽松按 string（日期/代码为主）
    origin = getattr(ann, "__origin__", None)
    args = getattr(ann, "__args__", ())
    if origin is list or ann is list:
        return {"type": "array", "items": {"type": "string"}}
    if origin is dict or ann is dict:
        return {"type": "object"}
    if origin is not None and origin is not list and origin is not dict:
        # Union / Optional
        types = []
        for a in args:
            if a is type(None):  # noqa: E721 - NoneType 比较
                continue
            if a is list:
                types.append({"type": "array", "items": {"type": "string"}})
            elif a in _PY_TO_JSON:
                types.append({"type": _PY_TO_JSON[a]})
            else:
                types.append({"type": "string"})
        if len(types) == 1:
            return types[0]
        return {"anyOf": types} if types else {"type": "string"}
    if ann in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[ann]}
    return {"type": "string"}


def _build_schema(fn) -> dict:
    """函数签名 → MCP inputSchema（properties + required）。"""
    props: dict = {}
    required: list[str] = []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}
    for name, param in sig.parameters.items():
        if name in ("df", "as_df", "panel"):  # 强制 JSON 形态，不暴露
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        schema = _type_to_schema(param.annotation, param.default)
        if param.default is not inspect.Parameter.empty and param.default is not None:
            schema["default"] = param.default if isinstance(param.default, (str, int, float, bool)) else None
            if isinstance(param.default, (list, tuple, dict)):
                schema["default"] = None
        desc = _PARAM_DESCRIPTIONS.get(fn.__name__, {}).get(name, "")
        if not desc:
            if param.default is not inspect.Parameter.empty:
                desc = f"可选参数（默认 {param.default!r}）"
            else:
                desc = f"必填参数（{param.annotation if param.annotation is not inspect.Parameter.empty else 'string'}）"
        schema["description"] = desc
        props[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def _tool_description(fn) -> str:
    """docstring 首段（压缩换行）作为工具描述；无 docstring 用签名兜底。"""
    doc = inspect.getdoc(fn) or ""
    if doc:
        one_line = " ".join(line.strip() for line in doc.splitlines() if line.strip())
        return one_line[:600]
    return f"上游 stock_sdk 工具 {fn.__name__}（参数见 schema）"


def tool_specs() -> list[dict]:
    """41 个 SDK 工具的 MCP TOOLS 规格（无上游模块时返回 []，服务器正常启动）。"""
    mod = _module()
    if mod is None:
        return []
    specs = []
    for name in sorted(dir(mod)):
        if not name.startswith("stockdb_"):
            continue
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        tool_name = name[len("stockdb_"):]  # 去前缀：stockdb_get_bars → get_bars
        specs.append({
            "name": tool_name,
            "description": _tool_description(fn),
            "inputSchema": _build_schema(fn),
            "group": SDK_TOOL_GROUPS.get(tool_name, "market_data"),  # 0.9.3 分组
        })
    return specs


SDK_TOOL_NAMES: frozenset[str] = frozenset()  # 模块加载后由 ensure_registered() 填充


def ensure_registered() -> frozenset[str]:
    """（重新）扫描上游工具名集合；无上游模块 → 空集（降级）。"""
    global SDK_TOOL_NAMES
    mod = _module()
    if mod is None:
        SDK_TOOL_NAMES = frozenset()
        return SDK_TOOL_NAMES
    names = frozenset(
        name[len("stockdb_"):]
        for name in dir(mod)
        if name.startswith("stockdb_") and callable(getattr(mod, name))
    )
    SDK_TOOL_NAMES = names
    return names


# ---- 调用封装 ----

def _parse_result(text: str):
    """上游 str(repr) 三级解析：json → ast.literal_eval → 原文。"""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError, TypeError):
        return s  # 原文兜底（契约标注见下）


def _truncate(data, cap: int = RESULT_CAP):
    """列表结果截断 + truncated 标记（大表保护）。"""
    if isinstance(data, list) and len(data) > cap:
        return {"data": data[:cap], "truncated": True, "total": len(data)}
    if isinstance(data, dict):
        out = dict(data)
        for k, v in list(out.items()):
            if isinstance(v, list) and len(v) > cap:
                out[k] = v[:cap]
                out["truncated"] = True
                out["total"] = len(v)
        return out
    return data


def call_tool(
    name: str,
    args: dict,
    *,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """执行一个 SDK 工具，返回契约化 result dict（供 _apply_contract 套信封）。

    0.16.1 增强：
      - 进程隔离：Linux 环境 fork 子进程执行，彻底杜绝 C 扩展持 GIL 冻结主服务事件循环；
      - 熔断降级：检测上游 tcp connect error 自动熔断 30s，快速返回 SOURCE_UNAVAILABLE；
      - 客户端断开检测：客户端超时离线时及时 kill 子进程，防止队列无限堆积；
      - 超时控制：严格遵循 STOCKDB_SDK_TIMEOUT（默认 10s），超时返回 SOURCE_TIMEOUT；
      - 并发限流：有界信号量 STOCKDB_SDK_CONCURRENCY（默认 4），满员快速返回 BUSY。
    """
    mod = _module()
    if mod is None:
        raise ValueError(f"{name}: SDK 工具不可用（stockdb_full_mcp 加载失败: {import_error()}）")
    fn = getattr(mod, f"stockdb_{name}", None)
    if fn is None or not callable(fn):
        raise ValueError(f"{name}: 未知 SDK 工具")

    kwargs: dict = {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    for key, value in (args or {}).items():
        # 保留参数名（df/as_df/panel）先于 sig 过滤检查：强制 JSON 形态
        if key in ("df", "as_df"):
            if value in (True, "true", "True", 1):
                raise ValueError(f"{name}: df/as_df 必须为 false（MCP 契约强制 JSON 形态）")
            continue
        if key == "panel":
            if value in (True, "true", "True", 1):
                raise ValueError(f"{name}: panel 不受支持（MCP 契约强制 JSON 形态）")
            continue
        if sig is not None and key not in sig.parameters:
            continue  # 忽略未知参数（schema 已校验，防御冗余）
        kwargs[key] = value
    # 强制 JSON 形态（上游默认 df=True 返回 DataFrame 不可序列化）
    if sig is not None and "df" in sig.parameters:
        kwargs["df"] = False
    if sig is not None and "panel" in sig.parameters:
        kwargs["panel"] = False

    # 1. 熔断状态检查
    is_open, open_msg = is_circuit_open()
    if is_open:
        raise SourceUnavailableError(open_msg)

    # 2. 并发限流
    if not _SDK_SEMAPHORE.acquire(blocking=True, timeout=1.0):
        raise ServerBusyError(f"{name}: 上游并发已达上限（{STOCKDB_SDK_CONCURRENCY}），请稍后重试")

    try:
        mode = _get_isolation_mode()
        effective_timeout = timeout if timeout is not None else STOCKDB_SDK_TIMEOUT
        check_fn = cancel_check or get_cancel_check()

        if mode == "process":
            parent_conn, child_conn = _SDK_MP_CTX.Pipe(duplex=False)
            p = _SDK_MP_CTX.Process(target=_sdk_worker_exec, args=(name, kwargs, child_conn))
            p.start()
            child_conn.close()

            deadline = time.monotonic() + effective_timeout
            got_data = False
            while time.monotonic() < deadline:
                rem = min(0.2, max(0.01, deadline - time.monotonic()))
                if parent_conn.poll(rem):
                    got_data = True
                    break
                if check_fn is not None:
                    try:
                        if not check_fn():
                            p.kill()
                            p.join(timeout=0.5)
                            parent_conn.close()
                            raise ClientDisconnectedError(f"{name}: 客户端已断开，终止执行")
                    except ClientDisconnectedError:
                        raise
                    except Exception:
                        pass

            if not got_data:
                p.kill()
                p.join(timeout=0.5)
                parent_conn.close()
                record_circuit_failure(f"调用超时（>{effective_timeout:.1f}s）")
                raise SourceTimeoutError(f"{name}: 上游调用超时（>{effective_timeout:.1f}s）")

            try:
                status, raw_res = parent_conn.recv()
            except EOFError:
                record_circuit_failure("子进程异常退出（无输出）")
                raise RuntimeError(f"{name}: 子进程异常退出（无输出）")
            finally:
                parent_conn.close()
                p.join(timeout=0.5)
        else:
            try:
                status, raw_res = "ok", fn(**kwargs)
            except ValueError as exc:
                status, raw_res = "err_val", str(exc)
            except Exception as exc:  # noqa: BLE001
                status, raw_res = "err_run", f"{type(exc).__name__}: {exc}"

        if status == "err_import":
            raise ValueError(raw_res)
        if status == "err_val":
            raise ValueError(f"{name}: 参数校验失败：{raw_res}")
        if status == "err_run":
            if any(marker in raw_res.lower() for marker in _CONNECT_FAILURE_MARKERS):
                record_circuit_failure(raw_res)
                raise SourceUnavailableError(f"{name}: 上游行情服务连接失败: {raw_res}")
            raise RuntimeError(f"{name}: 调用失败：{raw_res}")
        if status == "ok":
            text = raw_res
            if isinstance(text, str):
                lower_text = text.lower()
                if any(marker in lower_text for marker in _CONNECT_FAILURE_MARKERS):
                    record_circuit_failure(text)
                    raise SourceUnavailableError(f"{name}: 上游行情服务连接失败: {text}")
                marker = "失败:"
                if text.startswith(("调用", "调用失败")) or marker in text[:30]:
                    raise RuntimeError(f"{name}: {text}")
            record_circuit_success()
            return _truncate(_parse_result(text))
    finally:
        _SDK_SEMAPHORE.release()
