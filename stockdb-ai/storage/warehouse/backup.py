"""storage.warehouse.backup — warehouse.duckdb 在线备份（0.10.8；ROADMAP C5 落地）。

沿 research_store（mydb）的备份模式：
  - 独立连接在线快照，不占 engine 业务锁（沿 0.9.12 教训：主连接内备份会
    锁死全部查询——DuckDB 同进程按路径缓存实例，COPY 期间其他连接被阻塞）
  - 秒级时间戳 + uuid 后缀防同名冲突（沿 0.9.11 教训）
  - 保留最近 BACKUP_KEEP 份；失败静默返回 None（不阻塞沉淀主流程）

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
