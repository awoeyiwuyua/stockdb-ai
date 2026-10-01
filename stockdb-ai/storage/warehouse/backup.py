"""storage.warehouse.backup — warehouse.duckdb 在线备份 + WAL 收口（0.10.8；ROADMAP C5 落地）。

沿 research_store（mydb）的备份模式：
  - 独立连接在线快照，不占 engine 业务锁（沿 0.9.12 教训：主连接内备份会
    锁死全部查询——DuckDB 同进程按路径缓存实例，COPY 期间其他连接被阻塞）
  - 秒级时间戳 + uuid 后缀防同名冲突（沿 0.9.11 教训）
  - 保留最近 BACKUP_KEEP 份；失败静默返回 None（不阻塞沉淀主流程）
  - **调用必须在应用进程内（NAS 实测 2026-09-30）**：`duckdb.connect(路径)` 复用同进程
    缓存实例——换独立进程（docker exec python）拿不到实例锁即失败

DuckDB 等价物：`COPY FROM DATABASE`（1.5 语法）——ATTACH 源/目标后全库镜像
（表/视图/宏/schema/元数据），备份文件独立可打开（沿"备份独立可读"断言模式）。

备份内容 = warehouse.duckdb 全库：meta（watermark/empty 标记）+ codes +
research 用户表 + 视图/宏定义。facts/ 是 Parquet 事实区（可从引擎重拉），
不属本备份范围——恢复时先还原 duckdb，再按需回填 facts。

触发：沉淀任务成功后日级一次（services.warehouse_tasks 经注入点调用，
C3 层纪律：services 不 import storage.warehouse）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import layout

BACKUP_KEEP = 14  # 保留份数（沿 research_store.BACKUP_KEEP）

# 进程内日级守卫：{YYYYMMDD}——每日首次成功沉淀后备份一次，调度/手动不重复
_backup_dates: set[str] = set()


def backup_duckdb(root: Path, force: bool = False) -> Path | None:
    """在线备份 warehouse.duckdb → backups/warehouse-<stamp>-<unique>.db。

    失败静默返回 None（备份是尽力而为：不影响沉淀/查询主流程）。
    force=True 绕过日级守卫（手动备份通道）。
    """
    import duckdb

    root = Path(root)
    src = layout.duckdb_path(root)
    if not src.exists():
        return None
    if not force:
        today = datetime.now().strftime("%Y%m%d")
        if today in _backup_dates:
            return None  # 今日已备份（日级守卫）
    backup_dir = layout.backups_dir(root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique = uuid4().hex[:8]  # 同秒冲突防护
    target = backup_dir / f"warehouse-{stamp}-{unique}.db"
    try:
        # 独立连接：不占 engine 业务锁。源库经 duckdb.connect(路径) 打开——复用同进程
        # 缓存实例（engine 常驻连接已持有该路径；再 ATTACH 同一路径会
        # "Unique file handle conflict"——0.10.8 实测，故不显式 ATTACH 源）。
        con = duckdb.connect(str(src))
        try:
            src_name = con.execute("SELECT current_database()").fetchone()[0]
            con.execute(f"ATTACH '{target.as_posix()}' AS t")
            con.execute(f"COPY FROM DATABASE {src_name} TO t")
            con.execute("DETACH t")
        finally:
            con.close()
        # 保留最近 BACKUP_KEEP 份（清理更旧的）
        backups = sorted(backup_dir.glob("warehouse-*.db"))
        for old in backups[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
        _backup_dates.add(datetime.now().strftime("%Y%m%d"))
        return target
    except Exception:  # noqa: BLE001 - 备份失败静默
        try:
            if target.exists():
                target.unlink()  # 失败不留半成品
        except Exception:  # noqa: BLE001
            pass
        return None


def checkpoint_duckdb(root: Path) -> bool:
    """把 WAL 收口进主库（0.11.1；NAS 2026-09-30 实测缺陷）。

    背景（NAS 2026-09-30 定点实测）：DuckDB 1.5.5 的 `wal_autocheckpoint` 默认
    16 MiB，而仓库日增量仅 ~0.1 MB → 自动 checkpoint 日常从不触发；唯一触发时机
    是引擎干净关闭。于是 `warehouse.duckdb` 的 mtime 停在 09-23，此后 7 天写入
    全留在 `.wal`——**任何"只拷 .duckdb"的裸恢复会静默退到 09-23**。备份路径本身
    无恙（`COPY FROM DATABASE` 是逻辑快照），坏的是裸文件语义。

    与 backup_duckdb 的差异（只有一处）：`duckdb.connect(str(src))` + `CHECKPOINT`
    ——同一缓存实例、同一连接，因此调用纪律同 backup_duckdb：**必须在应用进程内**
    （NAS 实测：独立进程 `docker exec python` 报 "Conflicting lock is held"，0.01s 失败）。

    语义：只 flush 已提交事务，**不改逻辑状态**（NAS 实测：0.38s，主库 mtime 前移、
    .wal 消失，三水位/adjust_events/各视图计数逐项一致）。幂等——重复调用无副作用。

    失败静默返回 False（沿 backup_duckdb：不阻塞沉淀主流程；最坏只是 WAL 多留一天）。
    """
    import duckdb

    root = Path(root)
    src = layout.duckdb_path(root)
    if not src.exists():
        return False
    try:
        con = duckdb.connect(str(src))
        try:
            con.execute("CHECKPOINT")
        finally:
            con.close()
        return True
    except Exception:  # noqa: BLE001 - 收口失败不阻塞沉淀（WAL 仍在，状态不丢）
        return False


def warehouse_db_state(root: Path) -> dict:
    """主库 / WAL 文件新鲜度（只读 stat，不碰 DuckDB；供 /api/diag 观测）。

    0.11.1 起因：WAL 长期不 checkpoint 这件事**完全不可见**——本次巡检就曾把
    "读到旧水位"误判成"水位线停止推进"。故把文件事实透出成一眼可辨的一项。

    返回 {"exists", "mtime", "size", "wal_size", "wal_ratio", "stale"}；
    `stale` = WAL 已达主库 10%（自校准阈值，不依赖业务日期——收口正常时每日
    checkpoint 会让 WAL 归零，故任何显著占比都意味着"多日未收口"）。
    """
    root = Path(root)
    src = layout.duckdb_path(root)
    out = {"exists": False, "mtime": None, "size": 0,
           "wal_size": 0, "wal_ratio": 0.0, "stale": False}
    try:
        if not src.exists():
            return out
        st = src.stat()
        out["exists"] = True
        out["size"] = st.st_size
        out["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        wal = src.with_name(src.name + ".wal")
        if wal.exists():
            out["wal_size"] = wal.stat().st_size
            if st.st_size > 0:
                out["wal_ratio"] = out["wal_size"] / st.st_size
            out["stale"] = out["wal_size"] >= 0.10 * st.st_size
    except Exception:  # noqa: BLE001 - 观测项，任何异常都降级为"未知"
        pass
    return out
