"""storage.warehouse.queries — 仓库查询门面（0.10.0 W3）。

服务层/接口层统一入口：availability 检查 + 单例引擎委托。
异常约定（上层映射契约错误码）：
  - WarehouseUnavailable → DEPENDENCY_UNAVAILABLE
  - GuardrailError / duckdb 参数类异常 → INVALID_ARGUMENT
  - TimeoutError → INTERNAL_ERROR（附超时说明）
"""
from __future__ import annotations

from storage import warehouse as _wh
from . import catalog, layout
from .engine import GuardrailError, WarehouseEngine, WarehouseUnavailable, get_engine, reset_engine

__all__ = [
    "GuardrailError", "WarehouseUnavailable", "WarehouseEngine",
    "get_engine", "reset_engine", "run_sql", "status", "list_objects", "known_at",
    "read_fullmarket_daily",
]


def _require_engine() -> WarehouseEngine:
    ok, note = _wh.availability()
    if not ok:
        raise WarehouseUnavailable(note)
    return get_engine()


def run_sql(sql: str) -> dict:
    """执行单条 SQL（读写全开，护栏见 engine.run_sql）。"""
    return _require_engine().run_sql(sql)


def status() -> dict:
    """仓库状态（watermark/分区数/快照/代码数/duckdb 版本）。"""
    info = _require_engine().status()
    info["available"] = True
    return info


def list_objects() -> dict:
    """仓库对象清单（表/视图 + 指标宏）。"""
    return _require_engine().list_objects()


def known_at() -> str | None:
    """仓库可信时点（daily watermark；空仓返回 None）。"""
    return catalog.get_watermark(layout.root_dir(), "daily")


# 单批取行数：全市场 × (预热+区间) 可达数十万行，一次性 fetchall 会把 Python
# 元组全部堆在内存里（NAS 实测 RSS 峰值风险），故流式分批。
_DAILY_BATCH = 50000


def read_fullmarket_daily(start: str, end: str) -> dict:
    """全市场日K原始行（**只读 facts 分区**）——打板开盘溢价慢路径的仓库取数源。

    0.11.2 起因（NAS 实测）：打板开盘溢价慢路径原先逐码走引擎 HTTP（5200 码 ×
    8 并发 × 每请求 50ms 节流 → `query_fullmarket_daily_snapshot` 实测 **68.9s**），
    而同一份日K 在仓库里以 Parquet 落盘，等价取数只要 **0.05s 计数 / 1.36s 取
    538572 行**——瓶颈是取数源而非算法（`compute_board_open_effect_details` 仅 2.32s）。

    返回（**行键形刻意与引擎返回对齐**，使调用侧的装配/ST 判定逻辑零改动）：
      - code_rows: [(code, [row, ...]), ...]，code 升序、行内 date 升序；
        row = {date(YYYYMMDD), open, high, low, close, **pre_close**, volume,
               amount, is_st, adj_factor, name}
        · `pre_close` 即仓库的 `prev_close` 列（引擎侧键名，语义同为"上一实际
          成交日收盘/除权日法定参考价"）；`is_st` 给 "1"/"0" 字符串（引擎字面量
          形态）——两者都由本函数完成键名/类型归一，调用侧不分叉。
      - codes_present: 窗口内出现过行的代码集合（**判定"空码/停牌"用**：无行即
        窗口内无成交）
      - watermark / row_count

    边界：无分区、非 8 位日期、duckdb 缺失 → `code_rows` 为空（调用方回退引擎路径）。
    不写任何状态；`facts/` 只读（沿 engine.run_sql 同款护栏语义）。
    """
    out: dict = {"code_rows": [], "codes_present": set(), "watermark": None, "row_count": 0}
    try:
        import duckdb
    except Exception:  # noqa: BLE001 - 无 duckdb：调用方回退引擎路径
        return out
    try:
        layout_module = layout
        for value in (start, end):
            layout_module.normalize_date(value)
    except Exception:  # noqa: BLE001 - 非法日期：不猜，交调用方处理
        return out

    root = layout.root_dir()
    glob = (layout.facts_dir(root) / "daily" / "*" / "*" / "date=*.parquet").as_posix()
    sql = (
        "SELECT code, strftime(date, '%Y%m%d') AS d, open, high, low, close, "
        "prev_close, volume, amount, is_st, adj_factor, name "
        "FROM read_parquet(?, hive_partitioning=true) "
        "WHERE date BETWEEN ?::DATE AND ?::DATE "
        "ORDER BY code, date"
    )
    try:
        con = duckdb.connect()  # 独立只读连接：不占 engine 业务锁
        try:
            cur = con.execute(sql, [glob, layout.iso_date(start), layout.iso_date(end)])
            by_code: dict[str, list[dict]] = {}
            total = 0
            while True:
                batch = cur.fetchmany(_DAILY_BATCH)
                if not batch:
                    break
                for (code, d, o, h, lo, c, prev, vol, amt, is_st, adj, name) in batch:
                    by_code.setdefault(str(code), []).append({
                        "date": d,
                        "open": o,
                        "high": h,
                        "low": lo,
                        "close": c,
                        "pre_close": prev,          # 引擎键名对齐（仓库列名 prev_close）
                        "volume": vol,
                        "amount": amt,
                        "is_st": "1" if is_st else "0",  # 引擎字面量形态
                        "adj_factor": adj,
                        "name": name,
                    })
                    total += 1
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 分区缺失/损坏：降级为空（调用方回退引擎）
        return out
    out["code_rows"] = sorted(by_code.items(), key=lambda item: item[0])
    out["codes_present"] = set(by_code)
    out["row_count"] = total
    try:
        out["watermark"] = catalog.get_watermark(root, "daily")
    except Exception:  # noqa: BLE001 - 水位线读取失败不影响数据本身
        out["watermark"] = None
    return out
