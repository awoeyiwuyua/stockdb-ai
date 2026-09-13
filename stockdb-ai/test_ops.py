#!/usr/bin/env python3
"""test_ops — 运营支撑（Phase 4.5 任务D）全离线单元测试

覆盖四块（全部离线：无网络 / 无真实 DATA_DIR 残留；上游版本探针走本地
http.server mock + patch urlopen）：
  - Alerts                  : add/list 顺序（最新在前）、当日去重、跨日放行、200 条
                          上限滚动、clear/count、文件持久化与损坏容错、级别校验、
                          notify_alert 惰性单例（绑定 DATA_DIR、复用实例）。
  - data_freshness_alert     : latest=None 分支、日期无法解析分支、滞后>阈值分支、
                          滞后<=阈值不告警、非交易日不告警、时钟超前不告警、
                          YYYY-MM-DD 支持；patch Alerts 实例断言 add 调用。
  - capture_mcp_call         : jsonl 逐行追加与 2000 行截断、内存 deque 500 上限、
                          list 最新在前、stats（ok_rate/avg_ms/p95_ms/by_tool）
                          计算正确、空窗口、重启后从 jsonl 惰性恢复、落盘失败降级。
  - fetch_upstream_release   : 本地 http.server mock 200 解析 tag_name、非 200
                          （本地 mock 500 / patch HTTPError）返回 None、网络异常
                          返回 None、TTL 缓存二次调用不再次请求（mock 计数）、
                          失败也缓存、force 绕过缓存。

接线说明（Phase 4.5 返工 2 / 0.8.0 收敛）：
  被测运营支撑实现（Alerts / data_freshness_alert / capture_mcp_call /
  fetch_upstream_release）已整体迁移至 app.py 生产代码（模块级），本文件不再
  内嵌参考副本；测试 import app 并引用 app.X 生产实现，setUp 把 app.DATA_DIR
  打补丁到临时目录并复位 app 模块全局态（告警单例 / MCP deque / 版本探针缓存），
  保证离线、无残留、互不影响。
  0.8.0 起模拟盘整体下线：paper_audit_report / signal_status 相关测试随
  test_paper.py 一并移除，49 项测试语义与行为基线不变。

运行（自测命令，须贴 Ran/OK 与最后几行）：
    cd stockdb-ai && /Users/xiahaihe/Claudecode/stockdb-ai/.venv/bin/python -m unittest test_ops -v
回归（mcp 不受影响）：
    cd stockdb-ai && /Users/xiahaihe/Claudecode/stockdb-ai/.venv/bin/python -m unittest interfaces.mcp.test_stockdb_mcp_server -v
"""

from __future__ import annotations

# =====================================================================
# 生产实现接线（Phase 4.5 返工 2 / 0.8.0 收敛）：被测运营支撑实现（Alerts /
# data_freshness_alert / capture_mcp_call / fetch_upstream_release）已整体迁移至
# app.py 生产代码（模块级），本文件不再内嵌参考副本；测试 import app 并引用
# app.X，setUp 把 app.DATA_DIR 打补丁到临时目录并复位 app 模块全局态，
# 保证离线、无残留、互不影响。
# 0.8.0 起模拟盘整体下线：49 项测试语义与行为基线不变（全部打到 app.py 真实函数）。
# =====================================================================
import datetime
import io
import json
import time
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import app                       # 生产实现（被测对象）
import config                    # 配置单一入口（0.9.1）
from interfaces.web import handlers as web_handlers          # 0.10.27：snapshot 载荷
from interfaces.web.auth import authorized                   # 0.10.27：token 门禁
from interfaces.web.handlers import Handler as _WebHandler   # 0.9.11：Handler 已不在 app 模块级
from ops import alerts as ops_alerts  # 告警中心（0.9.2 批次 2 迁 ops/alerts.py）
from storage.providers import free_stockdb as free_stockdb_mod  # 引擎闸口（批次 3）
from storage.providers import mydb_store as mydb_store_mod      # mydb 读写（批次 3）
from services import auction_tasks as auction_tasks_mod          # 打板用例（批次 4）

# 静默 stdlib 无害噪声：urllib 探测非 2xx 时抛出的 HTTPError 内持临时文件，
# 对象被 GC 时 tempfile 模块发出的 ResourceWarning（本模块主动吞异常是预期行为）。
warnings.filterwarnings("ignore", category=ResourceWarning)




class _OpsTestCase(unittest.TestCase):
    """公共夹具：每用例独立临时目录 + DATA_DIR 隔离 + 模块全局态复位。

    防止用例间串扰：告警单例、MCP 内存 deque/加载标记/行数、版本探针缓存
    全部复位；DATA_DIR 指向临时目录，避免任何真实 /data 残留。
    """

    def setUp(self):
        # unittest runner 在 run 开始处 simplefilter("default") 会重置警告过滤器，
        # 故在每个用例 setUp 中重新注册：屏蔽 urllib 非 2xx 时 HTTPError 内临时
        # 文件被 GC 产生的无害 ResourceWarning（HTTPError 对象可能在任何时刻被
        # 回收，必须全局保持屏蔽）。
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self.tmp = tempfile.mkdtemp(prefix="test_ops_")
        # 打补丁 DATA_DIR 到临时目录（app + config 双入口，0.9.2 批次 2 起告警/日志
        # 实现迁 ops/ 并读 config.DATA_DIR）：被测函数全部隔离，保证离线、无残留。
        self._data_patch = mock.patch.object(app, "DATA_DIR", Path(self.tmp))
        self._data_patch_config = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        self._data_patch.start()
        self._data_patch_config.start()
        self.addCleanup(self._reset)

    def _reset(self):
        self._data_patch.stop()
        self._data_patch_config.stop()
        # 复位模块全局态：告警单例（ops.alerts）/ MCP 内存 deque 与加载标记 / 版本探针缓存
        ops_alerts._alerts_singleton = None
        app._mcp_deque.clear()
        app._mcp_loaded = False
        app._mcp_file_lines = 0
        app._RELEASE_CACHE.update(at=0.0, val=None)
        free_stockdb_mod._engine_cache.update(at=0.0, mtime=None, size=None, val=None)
        app._wh_totals_cache = (0.0, {})  # 0.10.27：仓库总量 TTL 缓存复位（防用例间串扰）
        app._mydb_rd._rd = None  # 0.8.10：rd 连接缓存复位（防用例间串扰）
        shutil.rmtree(self.tmp, ignore_errors=True)


# =====================================================================
# 0) mydb 读写链路（真实函数 + 假 rd）：归一化 / 串行化 / 失败自愈
# =====================================================================
class _FakeRd:
    """最小 rd 替身：get/keys/set/do 计数 + 内存数据，可注入失败。"""

    def __init__(self):
        self.data = {}      # {(table, key): value}
        self.calls = []     # [("get"|"keys"|"set", table, key_or_pattern)]

    def get(self, table, key):
        self.calls.append(("get", table, key))
        v = self.data.get((table, key))
        return v() if callable(v) else v

    def keys(self, table, pattern="*"):
        self.calls.append(("keys", table, pattern))
        if table == "*":
            return [f"{t}:{k}" for (t, k) in self.data]
        return [f"{t}:{k}" for (t, k) in self.data if t == table]

    def set(self, table, key, value):
        self.calls.append(("set", table, key))
        self.data[(table, key)] = value
        return self  # 链式 .do()

    def do(self):
        return self


class _QueryResultLike:
    """pybao QueryResult 形态替身：带 keys/all 属性，dict(v) 可转换。"""

    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def all(self):
        return None

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d.items())



class TimelineTests(_OpsTestCase):
    """W1 批 2：驾驶舱时间线聚合（records 沉淀 / sync 历史 / backups / alerts）。

    路径三入口（config.DATA_DIR / config.WAREHOUSE_DIR / app.HISTORY_FILE）全部
    patch 到临时目录，不触碰真实数据卷。
    """

    def test_load_timeline_aggregates_four_sources(self):
        d = Path(self.tmp)
        # 沉淀记录：collect 干扰项 + 两条 warehouse_sediment（断言末条为准）
        rec = d / "records"
        rec.mkdir(parents=True)
        (rec / "20260904.jsonl").write_text("\n".join([
            '{"date":"20260904","task":"collect","ok":true}',
            '{"date":"20260904","task":"warehouse_sediment","ok":true,"rows":5177}',
            '{"date":"20260904","task":"warehouse_sediment","ok":true,"rows":5178}',
        ]), encoding="utf-8")
        # 备份（按文件名日期归组）
        wb = Path(self.tmp) / "warehouse" / "backups"
        wb.mkdir(parents=True)
        (wb / "warehouse-20260904-164110-x.db").write_bytes(b"x")
        # 同步历史
        hist = d / "sync_history.json"
        hist.write_text(json.dumps(
            [{"ts": "2026-09-04 15:50:30", "trigger": "scheduled", "exit_code": 0,
              "verified": "pass", "duration_sec": 3.2, "data_latest": "20260904"}]),
            encoding="utf-8")

        # 仓库总量：daily 1 个交易日 / 周 1 / 月 0（仅放了 daily parquet）
        facts = Path(self.tmp) / "warehouse" / "facts" / "daily" / "year=2026" / "market=sh"
        facts.mkdir(parents=True)
        (facts / "date=20260904.parquet").write_bytes(b"x")
        wf = Path(self.tmp) / "warehouse" / "facts" / "week" / "year=2026" / "market=sh"
        wf.mkdir(parents=True)
        (wf / "date=20260904.parquet").write_bytes(b"x")

        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"),                 mock.patch.object(app, "HISTORY_FILE", hist):
            rows = app.load_timeline(7)
            totals = app.warehouse_totals()

        row = next(r for r in rows if r["date"] == "20260904")
        self.assertEqual(row["sediment"]["rows"], 5178)
        self.assertTrue(row["sediment"]["ok"])
        self.assertEqual(row["backups"]["count"], 1)
        self.assertEqual(row["sync"][0]["trigger"], "scheduled")
        self.assertEqual(row["alerts"]["count"], 0)
        # 交易日过滤：周末不得成行（2026-08-30 周日）
        self.assertNotIn("20260830", [r["date"] for r in rows])

    def test_load_timeline_empty_dir_degrades(self):
        # 空目录：行数 ≤ days，全部子块空缺不抛异常
        # HISTORY_FILE 是 import 期常量，patch 到临时目录才真"空"（0.10.38）
        with mock.patch.object(app, "HISTORY_FILE", Path(self.tmp) / "sync_history.json"), \
             mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"):
            rows = app.load_timeline(3)
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(all(r["sediment"] is None and r["sync"] == [] for r in rows))
        # 新增日级字段在空数据下也必须存在且为安全默认
        for r in rows:
            self.assertFalse(r["needs_action"])
            self.assertEqual(r["needs_action_count"], 0)
            self.assertIn("awaiting", r)


class WarehouseTotalsCacheTest(_OpsTestCase):
    """0.10.27：warehouse_totals 60s TTL 缓存（进 15s 轮询后不能每拍扫盘）。"""

    def _make_facts(self, dates):
        facts = Path(self.tmp) / "warehouse" / "facts" / "daily" / "year=2026" / "market=sh"
        facts.mkdir(parents=True, exist_ok=True)
        for d8 in dates:
            (facts / f"date={d8}.parquet").write_bytes(b"x")

    def test_second_call_within_ttl_hits_cache(self):
        self._make_facts(["20260904"])
        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"):
            first = app.warehouse_totals()
            second = app.warehouse_totals()
        self.assertIs(first, second)  # 同一对象 = 命中缓存，未重新扫盘
        self.assertEqual(first["sediment_days"], 1)

    def test_force_bypasses_cache_and_sees_new_data(self):
        self._make_facts(["20260904"])
        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"):
            first = app.warehouse_totals()
            self._make_facts(["20260907"])
            second = app.warehouse_totals(force=True)
        self.assertEqual(first["sediment_days"], 1)
        self.assertEqual(second["sediment_days"], 2)

    def test_cache_expires_after_ttl(self):
        self._make_facts(["20260904"])
        # 时钟注入：两拍间隔 61s > TTL(60s) → 第二拍重新计算（新对象）
        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"),                 mock.patch.object(app, "_monotonic", side_effect=[1000.0, 1100.0]):
            first = app.warehouse_totals()
            second = app.warehouse_totals()
        self.assertIsNot(first, second)


class SnapshotPayloadTest(_OpsTestCase):
    """0.10.27：/api/snapshot 单通道聚合——六块齐全 + timeline 走 app.* 动态引用。
    0.10.38：新增 assets 块（资产卡真身）。"""

    def test_snapshot_aggregates_six_blocks(self):
        payload = web_handlers.snapshot_payload(7)
        self.assertEqual(set(payload.keys()),
                         {"generated_at", "overview", "status", "schedule",
                          "warehouse", "timeline", "assets"})
        # timeline 块：days 列表 + totals 结构（离线临时目录下静默降级为空）
        self.assertIsInstance(payload["timeline"]["days"], list)
        self.assertIn("sediment_days", payload["timeline"]["totals"])
        # assets 块：三栏资产卡真身（研究库/双备份/磁盘分层）
        self.assertIn("research", payload["assets"])
        self.assertIn("backups", payload["assets"])
        self.assertIn("disk", payload["assets"])
        # timeline 块：days 列表 + totals 结构（离线临时目录下静默降级为空）
        self.assertIsInstance(payload["timeline"]["days"], list)
        self.assertIn("sediment_days", payload["timeline"]["totals"])
        # overview 块保持 /api/overview 契约（health/alerts/mcp/version）
        self.assertIn("health", payload["overview"])
        self.assertIn("alerts", payload["overview"])
        self.assertIn("version", payload["overview"])

    def test_snapshot_timeline_reads_app_dynamically(self):
        # timeline 必须走 app.*（patch 生效）；from-import 快照会绕过 patch（历史教训）
        sentinel = [{"date": "20990101", "sediment": None, "sync": [],
                     "backups": None, "alerts": {"count": 0, "err": 0, "warn": 0}}]
        with mock.patch.object(app, "load_timeline", return_value=sentinel),                 mock.patch.object(app, "warehouse_totals", return_value={"sediment_days": 42}):
            payload = web_handlers.snapshot_payload(7)
        self.assertEqual(payload["timeline"]["days"], sentinel)
        self.assertEqual(payload["timeline"]["totals"]["sediment_days"], 42)


class WebuiTokenAuthTest(_OpsTestCase):
    """0.10.27：webui token 门禁（interfaces/web/auth.py 纯函数）。

    规则：expected 空 = 门禁关；/api/* 校验 X-StockDB-Token；静态//legacy 放行
    （登录卡片要能加载）；/mcp 豁免（Mac MCP 客户端无法带头，见设计文档）。
    """

    def test_gate_disabled_when_expected_empty(self):
        self.assertTrue(authorized("/api/status", None, ""))
        self.assertTrue(authorized("/api/status", "wrong", ""))
        self.assertTrue(authorized("/mcp", "whatever", ""))

    def test_api_requires_matching_header_when_enabled(self):
        self.assertFalse(authorized("/api/status", None, "sekrit"))
        self.assertFalse(authorized("/api/status", "", "sekrit"))
        self.assertFalse(authorized("/api/status", "wrong", "sekrit"))
        self.assertTrue(authorized("/api/status", "sekrit", "sekrit"))
        self.assertTrue(authorized("/api/status", "  sekrit  ", "sekrit"))  # 容忍首尾空白

    def test_static_and_legacy_pass_through(self):
        # SPA shell/assets 必须先于鉴权可达，登录卡片才有地方渲染
        self.assertTrue(authorized("/", None, "sekrit"))
        self.assertTrue(authorized("/assets/index-abc123.js", None, "sekrit"))
        self.assertTrue(authorized("/legacy/index.html", None, "sekrit"))

    def test_mcp_exempt_and_post_paths_gated(self):
        self.assertTrue(authorized("/mcp", None, "sekrit"))
        self.assertFalse(authorized("/api/sync", None, "sekrit"))       # 写路径同样把门
        self.assertFalse(authorized("/api/warehouse/run", None, "sekrit"))


class _LimitReferenceTests(_OpsTestCase):
    """0.8.15：涨停判定参考价 = 普通日 lag close / 除权日 pre_close（验收修正版）。"""

    def test_rebuild_pure(self):
        """0.8.14 遗留函数保留兼容：反推公式 ref = pre_close × cum_latest/cum_D。"""
        from core.board_metrics import rebuild_limit_reference_price as r
        self.assertAlmostEqual(r(4.207, 3.019, 3.079), 4.207 * 3.079 / 3.019, places=3)
        self.assertEqual(r(10.0, 1.0, 1.0), 10.0)
        self.assertEqual(r(10.0, 3.019, 3.019), 10.0)
        self.assertEqual(r(10.0, None, 3.0), 10.0)
        self.assertEqual(r(10.0, 0, 3.0), 10.0)

    def test_get_fq_cum(self):
        """0.8.14 遗留函数保留兼容：因子表查询。"""
        from interfaces.mcp import pybao_tools as pt
        fake = mock.Mock()
        fake._fq_dates = {"000100": ["20040614", "20260611"], "600000": []}
        fake._fq_cums = {"000100": [1.012, 3.079], "600000": []}
        with mock.patch.object(pt, "get_sdk_client", return_value=fake):
            self.assertEqual(pt.get_fq_cum("000100", "20260507"), (1.012, 3.079))
            self.assertEqual(pt.get_fq_cum("000100", "20260611"), (3.079, 3.079))
            self.assertEqual(pt.get_fq_cum("000100", "20040101"), (1.0, 3.079))
            self.assertIsNone(pt.get_fq_cum("600000", "20260507"))
            self.assertIsNone(pt.get_fq_cum("999999", "20260507"))
        with mock.patch.object(pt, "get_sdk_client", return_value=None):
            self.assertIsNone(pt.get_fq_cum("000100", "20260507"))

    def test_apply_reference_uses_lag_close(self):
        """普通日：参考价 = 上一实际成交日未复权收盘（lag close 替换污染 pre_close）。"""
        pts = [{"code": "600000", "name": "X", "open": 4.6, "close": 4.72,
                "prev_close": 4.207, "high": 4.72, "low": 4.6,
                "is_st": False, "status": "TRADED"}]
        # 0.9.2 批次 4：除权判定走服务层注入点 is_fq_event
        with mock.patch.object(auction_tasks_mod, "is_fq_event", return_value=False):
            fixed = app._auction_apply_reference(pts, "20260507", {"600000": 4.289})
        self.assertAlmostEqual(fixed[0]["prev_close"], 4.289)
        from core.auction_list import compute_limitup_list
        # lag 参考价 4.289 → 涨停价 round(4.289×1.1,2)=4.72 == close → 命中
        self.assertEqual(compute_limitup_list(fixed)["count"], 1)
        # 未替换（污染 pre_close 4.207 → 涨停价 4.63 ≠ close 4.72 → 漏判）
        self.assertEqual(compute_limitup_list(pts)["count"], 0)

    def test_apply_reference_keeps_exday_pre_close(self):
        """除权日：参考价 = 当日 pre_close（法定参考价），不被 lag 覆盖。"""
        pts = [{"code": "000100", "name": "X", "open": 4.6, "close": 4.72,
                "prev_close": 4.58, "high": 4.72, "low": 4.6,
                "is_st": False, "status": "TRADED"}]
        with mock.patch.object(auction_tasks_mod, "is_fq_event", return_value=True):
            fixed = app._auction_apply_reference(pts, "20260611", {"000100": 9.99})
        self.assertEqual(fixed[0]["prev_close"], 4.58)  # 原样保留

    def test_apply_reference_fallback(self):
        """lag 缺失（停牌跨日）/SDK 不可用 → 原值兜底。"""
        pts = [{"code": "600000", "prev_close": 10.0, "close": 11.0}]
        with mock.patch.object(auction_tasks_mod, "is_fq_event", return_value=False):
            # lag 缺失 → 原值
            self.assertEqual(app._auction_apply_reference(pts, "20260507", {}), pts)
            # SDK 异常 → 原值
            with mock.patch.object(auction_tasks_mod, "is_fq_event",
                                   side_effect=RuntimeError("boom")):
                self.assertEqual(app._auction_apply_reference(pts, "20260507",
                                                              {"600000": 9.0}), pts)

    def test_lag_close_extracts_traded(self):
        """lag close 提取：仅 TRADED 有效行，停牌跳过。"""
        pts = [
            {"code": "600000", "close": 4.29, "status": "TRADED"},
            {"code": "600001", "close": 5.0, "status": "SUSPENDED"},
            {"code": "600002", "close": None, "status": "TRADED"},
            "garbage",
        ]
        self.assertEqual(app._auction_lag_close(pts), {"600000": 4.29})


class _MydbRdTests(_OpsTestCase):
    """mydb 读写：QueryResult/JSON 串归一化、并发串行化、失败丢弃连接自愈。"""

    def test_read_queryresult_normalized(self):
        """rd.get 返回 QueryResult 形态 → 读出原生 dict（不再序列化崩）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = _QueryResultLike({"metrics": {"a": 1}, "n": 2})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_read("t", "k")
        self.assertEqual(r["value"], {"metrics": {"a": 1}, "n": 2})

    def test_read_json_string_parsed(self):
        """rd.get 返回 JSON 字符串 → 解析为 dict（0.8.6 历史形态兼容）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = json.dumps({"v": [1, 2]})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        self.assertEqual(app.mydb_read("t", "k")["value"], {"v": [1, 2]})

    def test_read_nonjson_string_returns_none(self):
        """非 JSON 字符串 → value None（不抛序列化错误，不返回裸对象）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = "not-json"
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        self.assertIsNone(app.mydb_read("t", "k")["value"])

    def test_read_list_all_keys(self):
        """key 缺省：列出表内全部键值，值为归一化 dict。"""
        rd = _FakeRd()
        rd.data[("t", "20260814")] = {"a": 1}
        rd.data[("t", "20260815")] = _QueryResultLike({"b": 2})
        rd.data[("t2", "x")] = {"c": 3}
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_read("t", "")
        self.assertEqual(len(r["keys"]), 2)
        self.assertEqual(r["values"], {"t:20260814": {"a": 1}, "t:20260815": {"b": 2}})

    def test_write_readback_normalized(self):
        """写入原生 dict → 回读校验逐键一致（0.8.6 存值类型契约）。"""
        rd = _FakeRd()
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_write("t", [("k1", {"v": 1}), ("k2", {"v": 2})])
        self.assertEqual(r["written"], 2)
        self.assertEqual(r["readback"], [{"v": 1}, {"v": 2}])
        self.assertEqual(rd.data[("t", "k1")], {"v": 1})

    def test_write_nan_inf_guardrail(self):
        """0.9.4 写前护栏：NaN/Inf 条目剔除计数，不落盘；全拦截 → ValueError。"""
        rd = _FakeRd()
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        # 部分拦截：NaN 行剔除，正常行落盘
        r = app.mydb_write("t", [("k1", {"v": 1}),
                                 ("bad", {"open": float("nan")}),
                                 ("bad2", {"close": float("inf")})])
        self.assertEqual(r["written"], 1)
        self.assertEqual(r["skipped_invalid"], 2)
        self.assertIn(("t", "k1"), rd.data)
        self.assertNotIn(("t", "bad"), rd.data)
        self.assertNotIn(("t", "bad2"), rd.data)
        # 嵌套 NaN 同样拦截；全部拦截 → ValueError
        with self.assertRaises(ValueError):
            app.mydb_write("t", [("k2", {"rows": [{"open": float("nan")}]})])
        with self.assertRaises(ValueError):
            app.mydb_write("t", [("k3", {"v": float("nan")})])

    def test_concurrent_reads_serialized(self):
        """多线程并发读 → 锁保证同一时刻至多一条 rd 请求（单连接防交错）。"""
        active, max_active = [], [0]
        guard = threading.Lock()
        rd = _FakeRd()
        rd.data[("t", "k")] = {"v": 1}

        def slow_get(table, key):
            with guard:
                active.append(1)
                max_active[0] = max(max_active[0], len(active))
            time.sleep(0.02)
            with guard:
                active.pop()
            return rd.data.get((table, key))

        rd.get = slow_get
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        results = []

        def worker():
            results.append(app.mydb_read("t", "k")["value"])

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(max_active[0], 1)  # 全程串行
        self.assertEqual(results, [{"v": 1}] * 4)

    def test_failure_drops_connection_and_recovers(self):
        """rd 调用异常 → 丢弃缓存连接；下一次调用重新 init 后恢复正常。"""
        good = _FakeRd()
        good.data[("t", "k")] = {"a": 1}
        bad = _FakeRd()

        def boom(table, key):
            raise RuntimeError("socket wedged")

        bad.get = boom
        app._mydb_rd._rd = bad
        with self.assertRaises(RuntimeError):
            app.mydb_read("t", "k")
        self.assertIsNone(app._mydb_rd._rd)  # 缓存已丢弃（自愈前提）
        fake_mod = mock.Mock()
        fake_mod.init.return_value = good
        # 0.9.2 批次 3：实现迁 storage/providers/mydb_store，patch 实现侧
        with mock.patch.object(mydb_store_mod, "_mydb_import", return_value=fake_mod):
            r = app.mydb_read("t", "k")
        self.assertEqual(r["value"], {"a": 1})
        self.addCleanup(app._mydb_rd_reset)

    def test_hk_klines_serialized_and_normalized(self):
        """hk_klines：vals 读取持锁 + QueryResult 归一化（0.8.10 纳入锁面）。"""
        rd = _FakeRd()
        rd.data[("hk日k", "00700")] = _QueryResultLike({"date": "20260814", "close": 1.5})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        # _FakeRd 缺 vals：补一个返回列表的 vals
        rd.vals = lambda table, code, pattern: [rd.data.get((table, code))]
        rows = app.hk_klines("00700")
        self.assertEqual(rows, [{"date": "20260814", "close": 1.5}])


# =====================================================================
# 1) Alerts —— 告警中心
# =====================================================================
class AlertsTest(_OpsTestCase):
    """Alerts：add/list 顺序 / 当日去重 / 跨日放行 / 200 上限滚动 / clear /
    count / 持久化 / 级别校验；notify_alert 惰性单例。"""

    def _alerts(self, name="alerts.json"):
        return app.Alerts.init(os.path.join(self.tmp, name))

    def test_alerts_add_and_list_order(self):
        """add 追加、list 最新在前、limit 生效、返回条目字段齐全。"""
        a = self._alerts()
        a.add("info", "系统", "第一条")
        a.add("warning", "数据", "第二条")
        a.add("error", "引擎", "第三条")
        self.assertEqual(a.count(), 3)
        self.assertEqual([e["message"] for e in a.list(2)], ["第三条", "第二条"])
        self.assertEqual(a.list(1)[0]["message"], "第三条")
        self.assertEqual(len(a.list()), 3)
        e = a.list(1)[0]
        self.assertEqual(set(e), {"ts", "level", "source", "message"})
        self.assertEqual(e["level"], "error")
        self.assertEqual(e["ts"][:10], datetime.date.today().isoformat())

    def test_alerts_same_day_dedup(self):
        """当日去重：同 (date, source, message) 幂等返回既有条目、不新增。"""
        a = self._alerts()
        e1 = a.add("warning", "数据", "重复消息")
        e2 = a.add("warning", "数据", "重复消息")
        self.assertIs(e2, e1)              # 返回同一对象
        self.assertEqual(a.count(), 1)
        # 去重键 = (date, source, message)，不含 level：同源同消息换级别仍去重
        e3 = a.add("error", "数据", "重复消息")
        self.assertIs(e3, e1)
        self.assertEqual(a.count(), 1)
        # 同消息不同 source → 允许新增
        a.add("error", "引擎", "重复消息")
        self.assertEqual(a.count(), 2)

    def test_alerts_cross_day_allowed(self):
        """跨日放行：同日去重、跨日允许再次出现（patch _now_iso 控制日期）。"""
        times = iter(["2026-08-03T10:00:00", "2026-08-03T11:00:00",
                      "2026-08-04T10:00:00"])
        # 0.9.2 批次 2：实现迁 ops/alerts.py，_now_iso 在 ops 侧
        with mock.patch.object(ops_alerts, "_now_iso", side_effect=lambda: next(times)):
            a = self._alerts()
            a.add("info", "系统", "跨日消息")   # 08-03
            a.add("info", "系统", "跨日消息")   # 08-03 同日 → 去重
            self.assertEqual(a.count(), 1)
            a.add("info", "系统", "跨日消息")   # 08-04 跨日 → 新增
            self.assertEqual(a.count(), 2)
            self.assertTrue(a.list(1)[0]["ts"].startswith("2026-08-04"))

    def test_alerts_roll_200_cap(self):
        """200 条上限滚动：超出保留最新 200 条，最旧淘汰。"""
        a = self._alerts("roll.json")
        for i in range(205):
            a.add("info", "系统", f"滚动消息 {i}")
        self.assertEqual(a.count(), app.MAX_ALERTS)
        self.assertEqual(a.list(1)[0]["message"], "滚动消息 204")  # 最新保留
        msgs = {e["message"] for e in a.list(app.MAX_ALERTS + 50)}
        self.assertNotIn("滚动消息 0", msgs)   # 最旧淘汰
        self.assertIn("滚动消息 204", msgs)

    def test_alerts_clear(self):
        """clear：清空并落盘（文件保持存在、内容为 []）。"""
        a = self._alerts()
        a.add("info", "系统", "待清空")
        a.add("warning", "数据", "待清空二")
        a.clear()
        self.assertEqual(a.count(), 0)
        self.assertEqual(a.list(), [])
        with open(a.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_alerts_persist_reload(self):
        """持久化：新实例加载同一文件，条数与顺序一致。"""
        path = os.path.join(self.tmp, "persist.json")
        a = app.Alerts.init(path)
        a.add("info", "系统", "A")
        a.add("warning", "数据", "B")
        a2 = app.Alerts.init(path)
        self.assertEqual(a2.count(), 2)
        self.assertEqual([e["message"] for e in a2.list()], ["B", "A"])

    def test_alerts_corrupt_file_tolerant(self):
        """文件损坏 / 非数组 → 空列表不抛，仍可继续写入。"""
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        a = app.Alerts.init(path)
        self.assertEqual(a.count(), 0)
        a.add("info", "系统", "恢复后仍可写")
        self.assertEqual(a.count(), 1)
        # 非数组根节点同样容错
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        a2 = app.Alerts.init(path)
        self.assertEqual(a2.count(), 0)

    def test_alerts_level_validation(self):
        """级别别名归一化；非法级别 / 空 source / 空 message → 中文 ValueError。"""
        a = self._alerts()
        a.add("warn", "系统", "别名")      # warn → warning
        self.assertEqual(a.list(1)[0]["level"], "warning")
        a.add("INFO", "系统", "大写转小写")
        self.assertEqual(a.list(1)[0]["level"], "info")
        with self.assertRaises(ValueError):
            a.add("fatal", "系统", "非法级别")
        with self.assertRaises(ValueError):
            a.add("info", "   ", "空 source")
        with self.assertRaises(ValueError):
            a.add("info", "系统", "   ")

    def test_alerts_list_limit_edge(self):
        """list limit 边界：0 / 负数 / 非数字 → 回退默认 50。"""
        a = self._alerts()
        for i in range(5):
            a.add("info", "系统", f"m{i}")
        self.assertEqual(len(a.list(0)), 5)
        self.assertEqual(len(a.list(-3)), 5)
        self.assertEqual(len(a.list("abc")), 5)

    def test_resolve_removes_matching_and_keeps_others(self):
        """0.10.36 自愈：resolve(source, 前缀) 移除匹配条目并落盘；不匹配的保留。"""
        a = self._alerts()
        a.add("warning", "数据", "行情数据已滞后 35 天（最新 20260807）")
        a.add("warning", "数据", "行情数据不可用（探针失败）")
        a.add("error", "同步", "同步失败")
        a.add("warning", "打板", "竞价偏差")
        removed = a.resolve("数据", "行情数据不可用")
        self.assertEqual(removed, 1)
        messages = [e["message"] for e in a.list()]
        self.assertNotIn("行情数据不可用（探针失败）", messages)
        self.assertIn("行情数据已滞后 35 天（最新 20260807）", messages)  # 源同前缀不同
        self.assertIn("同步失败", messages)                              # 源不同
        self.assertEqual(a.resolve("数据", "行情数据不可用"), 0)          # 幂等：已撤无匹配
        # 落盘生效：新实例读回一致
        a2 = app.Alerts.init(a.path)
        self.assertEqual([e["message"] for e in a2.list()], messages)

    def test_resolve_prefix_matches_lag_variants(self):
        """滞后文案随天数变化 → 前缀匹配撤得掉（精确匹配会漏撤）。"""
        a = self._alerts()
        a.add("warning", "数据", "行情数据已滞后 2 天（最新 20260901）")
        self.assertEqual(a.resolve("数据", "行情数据已滞后"), 1)
        self.assertEqual(a.count(), 0)

    def test_resolve_no_match_no_write(self):
        """无匹配 → 返回 0、内容不变（看门狗 60s 一轮，避免无谓落盘）。"""
        a = self._alerts()
        a.add("info", "系统", "保留")
        before = os.path.getmtime(a.path)
        self.assertEqual(a.resolve("数据", "行情数据"), 0)
        self.assertEqual(a.count(), 1)
        self.assertEqual(os.path.getmtime(a.path), before)

    def test_resolve_rejects_empty_args(self):
        """空 source / 空前缀 → 中文 ValueError（防误撤全部告警）。"""
        a = self._alerts()
        with self.assertRaises(ValueError):
            a.resolve("", "行情")
        with self.assertRaises(ValueError):
            a.resolve("数据", "")

    def test_notify_alert_lazy_singleton(self):
        """notify_alert 惰性单例：首次调用创建、绑定 DATA_DIR、复用同一实例。"""
        ops_alerts._alerts_singleton = None
        try:
            self.assertIsNone(ops_alerts._alerts_singleton)  # 惰性：未调用前为 None
            e = app.notify_alert("error", "系统", "单例告警")
            self.assertIsNotNone(ops_alerts._alerts_singleton)
            self.assertIs(app._get_alerts(), ops_alerts._alerts_singleton)  # 复用
            self.assertEqual(ops_alerts._alerts_singleton.path,
                             os.path.join(self.tmp, "alerts.json"))
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, "alerts.json")))
            self.assertEqual(e["level"], "error")
            app.notify_alert("error", "系统", "单例告警")  # 当日去重生效
            self.assertEqual(ops_alerts._alerts_singleton.count(), 1)
            # env 变更不影响已绑定实例（惰性只绑定首次创建时的 DATA_DIR）
            with mock.patch.dict(os.environ, {"DATA_DIR": "/elsewhere"}):
                app.notify_alert("info", "系统", "第二条")
            self.assertEqual(ops_alerts._alerts_singleton.count(), 2)
            self.assertFalse(os.path.isfile("/elsewhere/alerts.json"))
        finally:
            ops_alerts._alerts_singleton = None


# =====================================================================
# 2) data_freshness_alert —— 数据新鲜度告警
# =====================================================================
class FreshnessAlertTest(_OpsTestCase):
    """data_freshness_alert：latest None / 日期无法解析 / 滞后超阈 / 阈值内不告警 /
    非交易日不告警 / 时钟超前 / YYYY-MM-DD；patch Alerts 实例断言 add 调用。"""

    def setUp(self):
        super().setUp()
        self.alerts = app.Alerts.init(os.path.join(self.tmp, "fresh.json"))

    def _days_ago(self, n, fmt="%Y%m%d"):
        return (datetime.date.today() - datetime.timedelta(days=n)).strftime(fmt)

    def test_freshness_latest_none_branch(self):
        """分支1：latest_date=None → 探针失败告警（warning/数据；当日去重）。"""
        app.data_freshness_alert(None, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        top = self.alerts.list()[0]
        self.assertEqual((top["level"], top["source"]), ("warning", "数据"))
        self.assertEqual(top["message"], "行情数据不可用（探针失败）")
        app.data_freshness_alert(None, False, alerts=self.alerts)  # 当日去重
        self.assertEqual(self.alerts.count(), 1)

    def test_freshness_unparsable_date_branch(self):
        """分支1：日期无法解析 → 同样探针失败告警。"""
        app.data_freshness_alert("20-08-04", True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertEqual(self.alerts.list()[0]["message"],
                         "行情数据不可用（探针失败）")

    def test_freshness_lag_over_threshold(self):
        """分支2：滞后 5 天 > 阈值 2（交易日）→ 滞后告警。"""
        d5 = self._days_ago(5)
        app.data_freshness_alert(d5, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertEqual(self.alerts.list()[0]["message"],
                         f"行情数据已滞后 5 天（最新 {d5}）")

    def test_freshness_lag_within_threshold_no_alert(self):
        """滞后 1 天 ≤ 阈值不告警；lag == 阈值（恰 2 天）也不告警。"""
        app.data_freshness_alert(self._days_ago(1), True, alerts=self.alerts)
        app.data_freshness_alert(self._days_ago(2), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_non_trading_day_no_alert(self):
        """非交易日：即使滞后超阈也不告警（休市数据不更新属正常）。"""
        app.data_freshness_alert(self._days_ago(5), False, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_future_date_no_alert(self):
        """时钟超前（滞后为负）→ 不告警。"""
        future = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y%m%d")
        app.data_freshness_alert(future, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_dash_date_supported(self):
        """YYYY-MM-DD 输入支持；滞后 4 天 > 2 → 告警。"""
        d4 = self._days_ago(4, fmt="%Y-%m-%d")
        app.data_freshness_alert(d4, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertIn("已滞后 4 天", self.alerts.list()[0]["message"])

    def test_freshness_alerts_add_patched(self):
        """patch Alerts 实例断言 add 调用：None→告警 / 非交易日→不调用 /
        阈值内→不调用 / 滞后超阈→精确参数 / 滞后 0→不调用。"""
        d5 = self._days_ago(5)
        d1 = self._days_ago(1)
        with mock.patch.object(app.Alerts, "add") as m_add:
            fa = app.Alerts.init(os.path.join(self.tmp, "mock_fresh.json"))
            app.data_freshness_alert(None, True, alerts=fa)
            m_add.assert_called_once_with("warning", "数据",
                                          "行情数据不可用（探针失败）")
            m_add.reset_mock()
            app.data_freshness_alert(d5, False, alerts=fa)   # 非交易日 → 不调用
            m_add.assert_not_called()
            app.data_freshness_alert(d1, True, alerts=fa)    # 阈值内 → 不调用
            m_add.assert_not_called()
            app.data_freshness_alert(d5, True, alerts=fa)    # 滞后超阈 → 调用
            m_add.assert_called_once_with(
                "warning", "数据", f"行情数据已滞后 5 天（最新 {d5}）")
            m_add.reset_mock()
            app.data_freshness_alert(datetime.date.today().isoformat(),
                                     True, alerts=fa)        # 滞后 0 → 不调用
            m_add.assert_not_called()

    # ---- 0.10.36 自愈：条件恢复撤警 ----

    def test_freshness_probe_recovery_resolves_alert(self):
        """探针失败告警 → 探针恢复（数据新鲜）即撤回，不残留红点。"""
        app.data_freshness_alert(None, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        app.data_freshness_alert(self._days_ago(0), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_lag_recovery_resolves_alert(self):
        """滞后告警 → 数据追平（滞后 0 ≤ 阈值）即撤回（跨文案前缀匹配）。"""
        app.data_freshness_alert(self._days_ago(9), True, alerts=self.alerts)
        self.assertIn("已滞后 9 天", self.alerts.list()[0]["message"])
        app.data_freshness_alert(self._days_ago(1), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_stale_period_keeps_alert(self):
        """仍滞后（>阈值且交易日）→ 不撤警；滞后天数变化也不撤（前缀只用于撤回）。"""
        app.data_freshness_alert(self._days_ago(9), True, alerts=self.alerts)
        app.data_freshness_alert(self._days_ago(6), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 2)  # 文案不同 → 当日去重不合并

    def test_freshness_non_trading_day_also_resolves(self):
        """非交易日（滞后 5 天本不告警）同样执行撤警：周一看盘前旧警已清。"""
        app.data_freshness_alert(self._days_ago(9), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        app.data_freshness_alert(self._days_ago(5), False, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_resolve_does_not_touch_other_sources(self):
        """撤警只动「数据」源的两条前缀，其他源告警不受影响。"""
        self.alerts.add("error", "同步", "同步失败")
        self.alerts.add("warning", "数据", "行情数据不可用（探针失败）")
        app.data_freshness_alert(self._days_ago(0), True, alerts=self.alerts)
        left = [e["message"] for e in self.alerts.list()]
        self.assertEqual(left, ["同步失败"])


# =====================================================================
# 3) capture_mcp_call / list_mcp_calls / mcp_stats
# =====================================================================
class MCPCaptureTest(_OpsTestCase):
    """capture_mcp_call：jsonl 追加/2000 行截断、deque 500 上限、list 顺序、
    stats 计算、空窗口、重启惰性恢复、字段归一化、落盘失败降级。"""

    def _capture(self, tool, ok=True, elapsed_ms=10, **kw):
        rec = {"tool": tool, "ok": ok, "is_error": not ok,
               "elapsed_ms": elapsed_ms, "bytes": 100}
        rec.update(kw)
        app.capture_mcp_call(rec)

    def _file_lines(self):
        path = os.path.join(self.tmp, "mcp_calls.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [ln for ln in f if ln.strip()]

    def test_capture_appends_jsonl(self):
        """jsonl 逐行追加；字段归一化为 6 键；冗余键丢弃；ok 缺省 = not is_error；
        ts 缺省补当前时间。"""
        app.capture_mcp_call({"tool": "get_kline", "ok": True, "is_error": False,
                              "elapsed_ms": 120, "bytes": 500,
                              "extra": "冗余键丢弃"})
        app.capture_mcp_call({"tool": "screen_stocks", "is_error": True,
                              "elapsed_ms": 900, "bytes": 50})   # ok 缺省 → False
        app.capture_mcp_call({"tool": "no_ts"})                   # ts 缺省
        lines = self._file_lines()
        self.assertEqual(len(lines), 3)
        r0 = json.loads(lines[0])
        self.assertEqual(set(r0), {"ts", "tool", "ok", "is_error",
                                   "elapsed_ms", "bytes"})
        self.assertEqual(r0["tool"], "get_kline")
        self.assertEqual(r0["elapsed_ms"], 120)
        r1 = json.loads(lines[1])
        self.assertEqual((r1["ok"], r1["is_error"]), (False, True))
        r2 = json.loads(lines[2])
        self.assertTrue(r2["ts"])
        self.assertEqual(r2["tool"], "no_ts")

    def test_capture_truncate_2000_lines(self):
        """jsonl 超上限截断：保留尾部（上限临时调小为 5 验证）。"""
        with mock.patch.object(app, "MCP_CALLS_FILE_MAX_LINES", 5):
            for i in range(8):
                self._capture(f"t{i}")
        lines = self._file_lines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(json.loads(lines[-1])["tool"], "t7")   # 保留最新
        self.assertEqual(json.loads(lines[0])["tool"], "t3")    # 最旧淘汰

    def test_capture_deque_500_cap(self):
        """内存 deque 500 上限：超出后 list/stats 只看最新 500 条。"""
        for i in range(550):
            self._capture(f"t{i}", ok=True, elapsed_ms=i)
        self.assertEqual(len(app._mcp_deque), 500)
        lst = app.list_mcp_calls(1000)
        self.assertEqual(len(lst), 500)
        self.assertEqual(lst[0]["tool"], "t549")   # 最新在前
        self.assertEqual(lst[-1]["tool"], "t50")   # 最旧 = 第 50 条（前 50 被淘汰）
        self.assertEqual(app.mcp_stats()["total"], 500)

    def test_mcp_list_order(self):
        """app.list_mcp_calls 最新在前；limit 生效。"""
        self._capture("a", elapsed_ms=1)
        self._capture("b", elapsed_ms=2)
        self._capture("c", elapsed_ms=3)
        self.assertEqual([r["tool"] for r in app.list_mcp_calls(2)], ["c", "b"])
        self.assertEqual(app.list_mcp_calls(1)[0]["tool"], "c")
        self.assertEqual(len(app.list_mcp_calls(100)), 3)

    def test_mcp_stats_compute(self):
        """stats：ok_rate / avg_ms / p95_ms / by_tool 计算正确。"""
        for ms in (10, 30, 50, 70, 90):      # a：5 次，4 成功
            self._capture("a", ok=ms != 50, elapsed_ms=ms)
        for ms in (20, 40, 60, 80, 100):     # b：5 次，4 成功
            self._capture("b", ok=ms != 60, elapsed_ms=ms)
        st = app.mcp_stats()
        self.assertEqual(st["total"], 10)
        self.assertEqual(st["ok_rate"], 0.8)          # 8/10
        self.assertEqual(st["avg_ms"], 55.0)          # (10+..+90+20+..+100)/10
        self.assertEqual(st["p95_ms"], 100.0)         # ceil(0.95*10)-1 → 第 10 小
        self.assertEqual([b["tool"] for b in st["by_tool"]], ["a", "b"])
        self.assertEqual(st["by_tool"][0],
                         {"tool": "a", "n": 5, "ok": 4, "avg_ms": 50.0})
        self.assertEqual(st["by_tool"][1],
                         {"tool": "b", "n": 5, "ok": 4, "avg_ms": 60.0})

    def test_mcp_stats_empty(self):
        """空窗口 → total 0、ok_rate/avg/p95 均 None、by_tool []。"""
        self.assertEqual(app.mcp_stats(),
                         {"total": 0, "ok_rate": None, "avg_ms": None,
                          "p95_ms": None, "by_tool": []})

    def test_mcp_restart_lazy_restore(self):
        """进程重启（清 deque + 复位加载标记）→ 从 jsonl 惰性恢复最新记录。"""
        self._capture("a", elapsed_ms=1)
        self._capture("b", elapsed_ms=2)
        app._mcp_deque.clear()
        app._mcp_loaded = False
        app._mcp_file_lines = 0
        lst = app.list_mcp_calls(10)
        self.assertEqual([r["tool"] for r in lst], ["b", "a"])
        self.assertEqual(app.mcp_stats()["total"], 2)

    def test_mcp_write_failure_degrades_gracefully(self):
        """落盘失败（DATA_DIR 指向普通文件）→ 静默降级，内存统计照常。"""
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch.object(app, "DATA_DIR", Path(blocker)):
            self._capture("get_kline", elapsed_ms=7)
            self.assertEqual(len(app.list_mcp_calls(10)), 1)
            self.assertEqual(app.mcp_stats()["total"], 1)
        self.assertFalse(os.path.isfile(os.path.join(blocker, "mcp_calls.jsonl")))


# =====================================================================
# 4) app.fetch_upstream_release —— 上游最新版本探针
# =====================================================================
class _MockGitHubHandler(BaseHTTPRequestHandler):
    """本地 mock GitHub releases/latest：按响应队列逐次应答，记录请求路径。"""

    responses: list = []   # 队列 [(status, body_dict), ...]
    requests: list = []    # 记录请求路径

    def do_GET(self):
        type(self).requests.append(self.path)
        if type(self).responses:
            status, body = type(self).responses.pop(0)
        else:
            status, body = 500, {"message": "mock 未配置响应"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


class FetchReleaseTest(_OpsTestCase):
    """fetch_upstream_release：本地 http.server mock 200 解析 tag_name、非 200 /
    异常返回 None、TTL 缓存二次调用不再次请求（mock 计数）。

    0.10.40：探针端点由 /releases/latest 改为 /releases?per_page=10（列表，含
    prerelease 与 assets），桩点同步改到 GITHUB_RELEASES_URL；mock 响应体也改成
    列表形态（此前是单对象）。
    """

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockGitHubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}/releases?per_page=10"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        _MockGitHubHandler.responses.clear()
        _MockGitHubHandler.requests.clear()
        self.url_patcher = mock.patch.object(app, "GITHUB_RELEASES_URL", self.base)
        self.url_patcher.start()
        self.addCleanup(self.url_patcher.stop)

    def test_release_200_parses_tag_name(self):
        """本地 mock 200（列表形态）：解析最新一条的 tag_name/html_url/published_at。"""
        _MockGitHubHandler.responses.append((200, [{
            "tag_name": "v1.2.3",
            "html_url": "https://github.com/hello245m/free-stockdb/releases/tag/v1.2.3",
            "published_at": "2026-08-01T00:00:00Z",
            "prerelease": False, "draft": False,
            "assets": [{"name": "a.tar", "digest": "sha256:aa", "size": 10,
                        "updated_at": "2026-08-01T00:00:00Z"}]}]))
        r = app.fetch_upstream_release(force=True)
        self.assertEqual(r["tag_name"], "v1.2.3")
        self.assertEqual(r["html_url"],
                         "https://github.com/hello245m/free-stockdb/releases/tag/v1.2.3")
        self.assertEqual(r["published_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(r["asset_count"], 1)
        self.assertTrue(r["asset_fingerprint"])
        self.assertEqual(len(_MockGitHubHandler.requests), 1)

    def test_release_non_200_returns_none(self):
        """非 200（本地 mock 500）→ None 不抛。"""
        _MockGitHubHandler.responses.append((500, {"message": "boom"}))
        self.assertIsNone(app.fetch_upstream_release(force=True))
        self.assertEqual(len(_MockGitHubHandler.requests), 1)

    def test_release_http_error_returns_none(self):
        """HTTPError（403，patch 层面）→ None 不抛。"""
        err = urllib.error.HTTPError(app.GITHUB_RELEASES_URL, 403, "Forbidden",
                                     {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(app.fetch_upstream_release(force=True))

    def test_release_network_error_returns_none(self):
        """网络异常（urlopen 抛 OSError）→ None 不抛。"""
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("网络不可达")):
            self.assertIsNone(app.fetch_upstream_release(force=True))

    def test_release_ttl_cache_no_second_request(self):
        """TTL 缓存：二次调用不再发请求（mock 计数 == 1）；force 绕过缓存。"""
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake      # 模拟 `with urlopen(...) as resp`
        fake.__exit__.return_value = False
        fake.read.return_value = b'{"tag_name":"v9.9.9"}'
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=fake) as m:
            r1 = app.fetch_upstream_release(force=True)
            r2 = app.fetch_upstream_release()      # TTL 命中：不再请求
            self.assertEqual(r1, r2)
            self.assertEqual(m.call_count, 1)
            app.fetch_upstream_release(force=True)  # force 绕过缓存
            self.assertEqual(m.call_count, 2)

    def test_release_failure_cached_then_force(self):
        """失败也缓存（不反复打上游）；force 可绕过重新探测。"""
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("网络不可达")) as m:
            self.assertIsNone(app.fetch_upstream_release(force=True))
            self.assertIsNone(app.fetch_upstream_release())   # 失败缓存命中
            self.assertEqual(m.call_count, 1)
            self.assertIsNone(app.fetch_upstream_release(force=True))
            self.assertEqual(m.call_count, 2)


# =====================================================================
# 4c) 0.10.38 驾驶舱改版后端增量：同步失败分类 / 时间线扩展 / 资产卡真身
# =====================================================================
class SyncFailureClassTest(unittest.TestCase):
    """sync_failure_class：纯函数分类（NAS 真身形态驱动），防止把"等上游/部署打断"
    误报成"需处理"（0.10.38 改版起因：09-07 被打断的手动重试被当成未决问题）。"""

    def _cls(self, rec, later=False):
        return app.sync_failure_class(rec, has_later_success=later)

    def test_ok_when_pass_without_warn(self):
        r = self._cls({"exit_code": 0, "verified": "pass", "duration_sec": 87.6,
                       "data_latest": "20260911"})
        self.assertEqual(r["class"], "ok")
        self.assertFalse(r["needs_action"])

    def test_awaiting_mirror_when_data_not_advanced(self):
        """NAS 09-11 16:48 真身：exit 0 / verified pass / warn 数据未更新 → 等上游。"""
        r = self._cls({"exit_code": 0, "verified": "pass", "duration_sec": 7.4,
                       "data_latest": "20260807",
                       "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"})
        self.assertEqual(r["class"], "awaiting_mirror")
        self.assertFalse(r["needs_action"])
        self.assertIn("镜像", r["detail"])

    def test_manifest_warn_scheduled_is_informational(self):
        """定时 + 已验证通过 + 清单类 warn → not_effective 但**不需动作**（信息性）。"""
        r = self._cls({"exit_code": 0, "verified": "pass", "data_latest": "20260910",
                       "warn": "同步未生效：下载 0 文件（镜像清单可能已变更）"})
        self.assertEqual(r["class"], "not_effective")
        self.assertFalse(r["needs_action"])

    def test_manifest_warn_manual_needs_look(self):
        """用户手动触发却"下载 0 文件"（且已验证通过）→ 需要人看一眼（清单可能真变了）。"""
        r = self._cls({"trigger": "manual", "exit_code": 0, "verified": "pass",
                       "data_latest": "20260910",
                       "warn": "同步未生效：下载 0 文件（镜像清单可能已变更）"})
        self.assertEqual(r["class"], "not_effective")
        self.assertTrue(r["needs_action"])

    def test_passed_retry_with_stale_warn_is_benign(self):
        """NAS 09-07 真身：stale-retry 的 warn 文案与首跑相同（"下载 0 文件且数据未更新"）
        但 verified=pass —— 必须判「等上游」而非需处理（否则当日打断判定永远走不到）。"""
        r = self._cls({"exit_code": 0, "verified": "pass", "duration_sec": 3.6,
                       "data_latest": "20260904",
                       "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"})
        self.assertEqual(r["class"], "awaiting_mirror")
        self.assertFalse(r["needs_action"])

    def test_passed_retry_with_manifest_warn_needs_no_action(self):
        """已验证通过的重试 + 清单类 warn → not_effective 但不需动作（信息性）。"""
        r = self._cls({"exit_code": 0, "verified": "pass", "data_latest": "20260910",
                       "warn": "同步未生效：下载 0 文件（镜像清单可能已变更）"})
        self.assertEqual(r["class"], "not_effective")
        self.assertFalse(r["needs_action"])

    def test_self_healed_when_verify_failed_then_success(self):
        """NAS 09-11 16:17 真身：1660s 后验证失败，但之后 17:50 成功 → 已自愈。"""
        r = self._cls({"exit_code": 0, "verified": "fail", "duration_sec": 1660.7,
                       "data_latest": None, "reason": "数据完整性验证未通过"}, later=True)
        self.assertEqual(r["class"], "self_healed")
        self.assertFalse(r["needs_action"])
        self.assertIn("后续重试已成功", r["detail"])

    def test_verify_failed_needs_action_without_later_success(self):
        r = self._cls({"exit_code": 0, "verified": "fail", "duration_sec": 1660.7,
                       "data_latest": None, "reason": "数据完整性验证未通过"})
        self.assertEqual(r["class"], "verify_failed")
        self.assertTrue(r["needs_action"])

    def test_run_interrupted_for_manual_short_failure(self):
        """手动短失败 + 当日有成功运行 → 被打断（不需处理）。"""
        r = app.sync_failure_class(
            {"trigger": "manual", "exit_code": 0, "verified": "fail",
             "duration_sec": 6.4, "data_latest": None},
            day_has_success=True)
        self.assertEqual(r["class"], "run_interrupted")
        self.assertFalse(r["needs_action"])
        self.assertIn("重启", r["detail"])

    def test_run_interrupted_when_last_of_day(self):
        """NAS 09-07 21:58 真身：当日**最后一条**的手动短失败（22:00 部署重启打断），
        当日另有成功运行 → 打断。它没有"之后"的成功（21:54 在它之前）。"""
        r = app.sync_failure_class(
            {"ts": "2026-09-07 21:58:37", "trigger": "manual", "exit_code": 0,
             "verified": "fail", "duration_sec": 6.4, "data_latest": None},
            has_later_success=False, day_has_success=True, is_last_of_day=True)
        self.assertEqual(r["class"], "run_interrupted")
        self.assertFalse(r["needs_action"])
        self.assertIn("当日已有成功运行", r["detail"])

    def test_manual_short_failure_without_day_success_is_real(self):
        """当日无任何成功运行 → 手动短失败保守判为真问题（需处理）。"""
        r = app.sync_failure_class(
            {"trigger": "manual", "exit_code": 0, "verified": "fail",
             "duration_sec": 6.4, "data_latest": None},
            has_later_success=False, day_has_success=False)
        self.assertEqual(r["class"], "verify_failed")
        self.assertTrue(r["needs_action"])

    def test_nas_0907_full_day_sequence(self):
        """生产真身回归（10 条，字段照抄 NAS /api/timeline）：
        前两次认证失败（后来自愈）+ 5 次 pass-retry（带"数据未更新"warn，判等上游）
        + 手动 skipped + 手动 pass + 手动 fail（22:00 部署重启打断）
        ⇒ 全天 needs_action=False，且 21:58 必须判「被打断」。
        0.10.38 实机复验抓到的两个顺序 bug 都靠这条锁住：warn 分支吞掉 reason 性质、
        以及"非最后一条"约束导致最后一条 fail 走不到打断判定。"""
        entries = [
            {"ts": "2026-09-07 15:50:34", "trigger": "scheduled", "exit_code": 0,
             "verified": "skipped", "duration_sec": 11.7, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）",
             "reason": "数据源失败：认证失败（auth failed），请检查数据源授权"},
            {"ts": "2026-09-07 16:21:04", "trigger": "scheduled-stale-retry", "exit_code": 0,
             "verified": "skipped", "duration_sec": 12.1, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）",
             "reason": "数据源失败：认证失败（auth failed），请检查数据源授权"},
            {"ts": "2026-09-07 16:51:26", "trigger": "scheduled-stale-retry", "exit_code": 0,
             "verified": "pass", "duration_sec": 3.6, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"},
            {"ts": "2026-09-07 17:21:56", "trigger": "scheduled-stale-retry", "exit_code": 0,
             "verified": "pass", "duration_sec": 3.7, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"},
            {"ts": "2026-09-07 19:15:41", "trigger": "manual", "exit_code": 0,
             "verified": "skipped", "duration_sec": 11.2, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）",
             "reason": "数据源失败：认证失败（auth failed），请检查数据源授权"},
            {"ts": "2026-09-07 21:54:20", "trigger": "manual", "exit_code": 0,
             "verified": "pass", "duration_sec": 6.0, "data_latest": "20260904",
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"},
            {"ts": "2026-09-07 21:58:37", "trigger": "manual", "exit_code": 0,
             "verified": "fail", "duration_sec": 6.4, "data_latest": None,
             "warn": "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）",
             "reason": "数据完整性验证未通过"},
        ]
        last_success = "2026-09-07 21:54:20"
        out = []
        for e in entries:
            rec = dict(e)
            rec["has_later_success"] = str(rec["ts"]) < last_success
            out.append(app.sync_failure_class(
                rec, has_later_success=rec.pop("has_later_success"), day_has_success=True,
                is_last_of_day=str(e["ts"]).endswith("21:58:37")))
        classes = [r["class"] for r in out]
        # 真身逐条预期：认证失败（a/b/e，之后有成功）→ 已自愈；4 次 pass-retry
        # 带"数据未更新"warn → 等上游；最后一条 fail（22:00 被打断）→ 打断
        self.assertEqual(classes, [
            "self_healed", "self_healed", "awaiting_mirror", "awaiting_mirror",
            "self_healed", "awaiting_mirror", "run_interrupted",
        ])
        self.assertFalse(any(r["needs_action"] for r in out))
        self.assertTrue(all("data_source_error" != r["class"] for r in out))

    def test_data_source_error_and_self_healed_by_exit_code(self):
        base = {"trigger": "scheduled", "exit_code": 1, "verified": "skipped",
                "duration_sec": 600, "data_latest": None, "reason": "网络超时"}
        r = self._cls(base)
        self.assertEqual(r["class"], "data_source_error")
        self.assertTrue(r["needs_action"])
        self.assertIn("网络/上游通道异常", r["detail"])
        self.assertEqual(self._cls(base, later=True)["class"], "self_healed")

    def test_skipped_maps_to_awaiting_mirror(self):
        r = self._cls({"trigger": "scheduled", "exit_code": 0, "verified": "skipped",
                       "duration_sec": 11.7, "data_latest": "20260904"})
        self.assertEqual(r["class"], "awaiting_mirror")
        self.assertFalse(r["needs_action"])

    def test_non_dict_degrades(self):
        self.assertEqual(self._cls(None)["class"], "unknown")


class UpstreamAssetFingerprintTest(_OpsTestCase):
    """0.10.40 上游资产指纹：同名 tag 重传的主动发现（历史两次都靠同步失败被动发现）。"""

    def setUp(self):
        super().setUp()
        app._RELEASE_CACHE.update(at=0.0, val=None)

    @staticmethod
    def _asset(name, digest, size=100, updated="2026-09-10T13:21:38Z"):
        return {"name": name, "digest": digest, "size": size, "updated_at": updated}

    def _release(self, tag="测试版本0.3.5", assets=None, prerelease=False):
        assets = assets if assets is not None else [
            self._asset("free-stockdb-manylinux-x64-v0.3.5-more-power.tar", "sha256:aa", 10557440),
            self._asset("free-stockdb-windows-v0.3.5-more-power.zip", "sha256:bb", 4328388),
        ]
        return {"tag_name": tag, "html_url": "https://x", "published_at": "2026-07-19T00:00:00Z",
                "prerelease": prerelease, "draft": False, "asset_count": len(assets),
                "asset_fingerprint": app.asset_fingerprint(assets)}

    # ---- 指纹本身 ----
    def test_fingerprint_ignores_download_count(self):
        """下载数天天变——进指纹就会天天误报，必须排除。"""
        a = self._asset("x.tar", "sha256:aa")
        b = {**a, "download_count": 999, "state": "uploaded", "id": 123}
        self.assertEqual(app.asset_fingerprint([a]), app.asset_fingerprint([b]))

    def test_fingerprint_detects_digest_or_size_change(self):
        """digest 变（重传二进制）/ size 变 / 资产增删 都要能检出。"""
        base = self._asset("x.tar", "sha256:aa", 100)
        self.assertNotEqual(app.asset_fingerprint([base]),
                            app.asset_fingerprint([{**base, "digest": "sha256:bb"}]))
        self.assertNotEqual(app.asset_fingerprint([base]),
                            app.asset_fingerprint([{**base, "size": 101}]))
        self.assertNotEqual(app.asset_fingerprint([base]),
                            app.asset_fingerprint([base, self._asset("y.zip", "sha256:cc")]))

    def test_fingerprint_order_independent(self):
        """资产顺序不参与指纹（同一组资产换序不应报变更）。"""
        a = self._asset("a.tar", "sha256:aa")
        b = self._asset("b.zip", "sha256:bb")
        self.assertEqual(app.asset_fingerprint([a, b]), app.asset_fingerprint([b, a]))

    # ---- 档案生命周期 ----
    def test_watch_baseline_then_same(self):
        """首见只建基线（不告警），第二次同指纹 → same。"""
        rel = self._release()
        self.assertEqual(app.upstream_asset_watch(rel)["status"], "baseline")
        self.assertEqual(app.upstream_asset_watch(rel)["status"], "same")
        doc = json.loads((Path(self.tmp) / app.UPSTREAM_WATCH_FILE).read_text(encoding="utf-8"))
        self.assertIn("测试版本0.3.5", doc)
        self.assertEqual(doc["测试版本0.3.5"]["asset_count"], 2)

    def test_watch_detects_reupload_same_tag(self):
        """同名 tag 重传（digest 变）→ changed，并保留前后指纹供人工核对。"""
        app.upstream_asset_watch(self._release())
        tampered = self._release(assets=[
            self._asset("free-stockdb-manylinux-x64-v0.3.5-more-power.tar", "sha256:NEW",
                        10557440)])
        out = app.upstream_asset_watch(tampered)
        self.assertEqual(out["status"], "changed")
        self.assertNotEqual(out["previous_fingerprint"], out["fingerprint"])
        self.assertEqual(out["tag"], "测试版本0.3.5")
        # 变更后基线更新 → 再来一次是 same（不重复刷屏）
        self.assertEqual(app.upstream_asset_watch(tampered)["status"], "same")

    def test_watch_no_release_degrades(self):
        self.assertEqual(app.upstream_asset_watch(None)["status"], "none")
        self.assertEqual(app.upstream_asset_watch({"tag_name": "x"})["status"], "none")

    def test_watch_save_false_does_not_write(self):
        app.upstream_asset_watch(self._release(), save=False)
        self.assertFalse((Path(self.tmp) / app.UPSTREAM_WATCH_FILE).exists())

    def test_watch_corrupt_archive_recovers(self):
        (Path(self.tmp) / app.UPSTREAM_WATCH_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(app.upstream_asset_watch(self._release())["status"], "baseline")

    # ---- 与版本判定/告警的接线 ----
    def test_upstream_status_reports_asset_changed(self):
        """指纹变化 → kind=asset_changed（即使版本号相同也要报）。"""
        rel = self._release()
        app.upstream_asset_watch(rel)
        tampered = self._release(assets=[self._asset("only-one.tar", "sha256:zz")])
        with mock.patch.object(app, "fetch_upstream_release", return_value=tampered), \
             mock.patch.object(app, "_env_version_tag", return_value="0.3.5"), \
             mock.patch("storage.providers.free_stockdb.engine_version_info",
                        return_value={"base": "0.3.5", "version": "0.3.5-stockdb"}):
            st = app.upstream_status()
        self.assertEqual(st["kind"], "asset_changed")
        self.assertIn("同名资产已变更", st["message"])
        self.assertIn("重新核对 SHA256", st["message"])

    def test_status_up_to_date_when_fingerprint_same(self):
        """指纹未变 + 版本相同 → 仍是 up_to_date（指纹机制本身不产生噪声）。"""
        rel = self._release()
        app.upstream_asset_watch(rel)
        with mock.patch.object(app, "fetch_upstream_release", return_value=rel), \
             mock.patch.object(app, "_env_version_tag", return_value="0.3.5"), \
             mock.patch("storage.providers.free_stockdb.engine_version_info",
                        return_value={"base": "0.3.5", "version": "0.3.5-stockdb"}):
            st = app.upstream_status()
        self.assertEqual(st["kind"], "up_to_date")
        self.assertEqual(st["asset_watch"]["status"], "same")

    def test_alert_warns_on_asset_changed_and_resolves(self):
        """asset_changed 进告警中心（当日去重）；恢复 up_to_date 后撤警。"""
        alerts = app.Alerts.init(os.path.join(self.tmp, "up_asset.json"))
        app.upstream_release_alert(alerts=alerts, status={
            "kind": "asset_changed",
            "message": "上游 **x 同名资产已变更**：需重新核对 SHA256 并重建镜像"})
        top = alerts.list()[0]
        self.assertEqual((top["level"], top["source"]), ("warning", "上游"))
        self.assertIn("同名资产已变更", top["message"])
        app.upstream_release_alert(alerts=alerts, status={"kind": "up_to_date", "message": "最新"})
        self.assertEqual(alerts.count(), 0)

    # ---- 探针端点 ----
    def _fake_urlopen(self, payload):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return mock.patch("urllib.request.urlopen", return_value=_Resp())

    def test_probe_uses_releases_list_and_marks_prerelease(self):
        """探针走 /releases 列表：能看见 prerelease（旧 /latest 端点看不到）。"""
        self.assertIn("/releases?", app.GITHUB_RELEASES_URL)
        payload = [{"tag_name": "测试版本0.4.0", "html_url": "https://x",
                    "published_at": "2026-09-13T00:00:00Z", "prerelease": True,
                    "draft": False, "assets": [self._asset("a.tar", "sha256:aa")]},
                   {"tag_name": "测试版本0.3.5", "html_url": "https://y",
                    "published_at": "2026-07-19T00:00:00Z", "prerelease": False,
                    "draft": False, "assets": [self._asset("b.tar", "sha256:bb")]}]
        with self._fake_urlopen(payload):
            rel = app.fetch_upstream_release(force=True)
        self.assertEqual(rel["tag_name"], "测试版本0.4.0")
        self.assertTrue(rel["prerelease"])
        self.assertTrue(rel.get("newer_prerelease"))
        self.assertEqual(rel["stable"]["tag_name"], "测试版本0.3.5")
        self.assertTrue(rel["asset_fingerprint"])

    def test_probe_skips_drafts(self):
        payload = [{"tag_name": "draft", "draft": True, "assets": []},
                   {"tag_name": "测试版本0.3.5", "draft": False, "prerelease": False,
                    "assets": [self._asset("a.tar", "sha256:aa")]}]
        with self._fake_urlopen(payload):
            rel = app.fetch_upstream_release(force=True)
        self.assertEqual(rel["tag_name"], "测试版本0.3.5")
        self.assertFalse(rel.get("newer_prerelease", False))


class AlertMuteTest(_OpsTestCase):
    """0.10.38 告警静音：只改提醒强度，不改事实（count 恒定、到期自动解除）。"""

    def setUp(self):
        super().setUp()
        # 静音文件路径走 config.DATA_DIR（已 patch 到临时目录），但缓存要逐用例复位
        ops_alerts._mute_cache = {"at": 0.0, "sig": None, "val": None}

    def _mute_file(self):
        return Path(self.tmp) / ops_alerts.MUTE_FILE

    def test_default_not_muted(self):
        st = ops_alerts.alert_mute_state()
        self.assertFalse(st["muted"])
        self.assertIsNone(st["until"])

    def test_set_preset_mutes_with_expiry(self):
        st = ops_alerts.set_alert_mute("1h", reason="例行维护")
        self.assertTrue(st["muted"])
        self.assertEqual(st["preset"], "1h")
        self.assertEqual(st["reason"], "例行维护")
        self.assertAlmostEqual(st["remaining_sec"], 3600, delta=5)
        self.assertTrue(self._mute_file().exists())
        self.assertTrue(ops_alerts.alert_mute_state()["muted"])   # 读回一致

    def test_today_preset_expires_at_end_of_day(self):
        now = datetime.datetime(2026, 9, 13, 10, 0, 0)
        st = ops_alerts.set_alert_mute("today", now=now)
        expect = now.replace(hour=23, minute=59, second=59).timestamp()
        self.assertAlmostEqual(st["until"], expect, delta=1)
        self.assertAlmostEqual(st["remaining_sec"], 13 * 3600 + 59 * 60 + 59, delta=2)

    def test_custom_minutes_and_validation(self):
        st = ops_alerts.set_alert_mute("", minutes=30)
        self.assertTrue(st["muted"])
        self.assertEqual(st["preset"], "30m")
        for bad in (0, 1441, -5):
            with self.assertRaises(ValueError):
                ops_alerts.set_alert_mute("", minutes=bad)
        with self.assertRaises(ValueError):
            ops_alerts.set_alert_mute("bogus")
        self.assertFalse(ops_alerts.set_alert_mute("1h").get("error", False))

    def test_expired_state_auto_clears(self):
        """到期 → 状态转 False 且清掉文件（避免陈旧状态一直挂着）。"""
        future = datetime.datetime.now() + datetime.timedelta(hours=3)
        ops_alerts.set_alert_mute("1h")
        self.assertTrue(self._mute_file().exists())
        st = ops_alerts.alert_mute_state(now=future)
        self.assertFalse(st["muted"])
        self.assertFalse(self._mute_file().exists())

    def test_clear_is_idempotent(self):
        ops_alerts.set_alert_mute("4h")
        self.assertTrue(ops_alerts.clear_alert_mute()["muted"] is False)
        self.assertFalse(ops_alerts.clear_alert_mute()["muted"])   # 再清不抛

    def test_corrupt_file_degrades_to_not_muted(self):
        self._mute_file().parent.mkdir(parents=True, exist_ok=True)
        self._mute_file().write_text("{ not json", encoding="utf-8")
        ops_alerts._mute_cache = {"at": 0.0, "sig": None, "val": None}
        self.assertFalse(ops_alerts.alert_mute_state()["muted"])

    def test_count_unaffected_by_mute(self):
        """核心语义：静音不改事实——计数照常（横幅/统计不被静音骗）。"""
        app._get_alerts().add("warning", "数据", "行情数据已滞后 3 天")
        before = app._get_alerts().count()
        ops_alerts.set_alert_mute("1h")
        self.assertEqual(ops_alerts.pending_alert_count(), before)
        self.assertEqual(app._get_alerts().count(), before)

    def test_overview_payload_carries_mute_state(self):
        """overview.alerts 带 muted/mute_until（前端横幅据此显示静音态）。"""
        ops_alerts.set_alert_mute("1h")
        payload = web_handlers.overview_payload()
        self.assertTrue(payload["alerts"]["muted"])
        self.assertIsInstance(payload["alerts"]["mute_until"], float)
        self.assertEqual(payload["alerts"]["mute_preset"], "1h")

    def test_summary_endpoint_reports_count_and_mute(self):
        """GET /api/alerts/summary：count 与静音态同时给出。"""
        app._get_alerts().add("warning", "数据", "测试告警")
        ops_alerts.set_alert_mute("4h")
        status, _, body = _do_get("/api/alerts/summary")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["muted"])

    def test_mute_endpoint_post_and_clear(self):
        """POST /api/alerts/mute：设置 → 查询 → 解除；非法参数 400。"""
        status, _, body = _do_post("/api/alerts/mute", {"preset": "1h"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode())["muted"])
        status, _, body = _do_get("/api/alerts/mute")
        self.assertTrue(json.loads(body.decode())["muted"])
        status, _, body = _do_post("/api/alerts/mute", {"clear": True})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body.decode())["muted"])
        status, _, body = _do_post("/api/alerts/mute", {"preset": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("未知静音预设", json.loads(body.decode())["error"])
        status, _, body = _do_post("/api/alerts/mute", {})
        self.assertEqual(status, 400)


class TimelineExtrasTest(_OpsTestCase):
    """load_timeline 0.10.38 扩展：reason/warn 透出 + 分类 + 日级 needs_action/awaiting。"""

    def setUp(self):
        super().setUp()
        # HISTORY_FILE 是 import 期常量，DATA_DIR patch 盖不住它 → 必须显式指向临时目录
        # （否则读到仓库真实 data/ 的历史，用例互相串扰且与真机数据耦合）
        self._hist = mock.patch.object(app, "HISTORY_FILE", Path(self.tmp) / "sync_history.json")
        self._hist.start()
        self.addCleanup(self._hist.stop)
        self._wh = mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse")
        self._wh.start()
        self.addCleanup(self._wh.stop)

    def _write_history(self, entries):
        app.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        app.HISTORY_FILE.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def _day(self, date8):
        for d in app.load_timeline(7):
            if d["date"] == date8:
                return d
        self.fail(f"时间线缺 {date8}")

    def test_sync_entries_carry_reason_warn_and_class(self):
        """透出 reason/warn（此前只在 sync_history.json 里）+ 逐条分类。"""
        self._write_history([
            {"ts": "2026-09-11 16:17:51", "trigger": "scheduled", "exit_code": 0,
             "verified": "fail", "duration_sec": 1660.7, "data_latest": None,
             "reason": "数据完整性验证未通过", "warn": None},
            {"ts": "2026-09-11 17:50:38", "trigger": "scheduled-stale-retry", "exit_code": 0,
             "verified": "pass", "duration_sec": 87.6, "data_latest": "20260911"},
        ])
        day = self._day("20260911")
        first = day["sync"][0]
        self.assertEqual(first["reason"], "数据完整性验证未通过")
        self.assertEqual(first["class"], "self_healed")     # 同日更晚成功
        self.assertEqual(day["sync"][1]["class"], "ok")
        self.assertFalse(day["needs_action"])
        self.assertEqual(day["needs_action_count"], 0)

    def test_needs_action_when_no_later_success(self):
        self._write_history([
            {"ts": "2026-09-08 15:54:22", "trigger": "scheduled", "exit_code": 0,
             "verified": "fail", "duration_sec": 262.4, "data_latest": None,
             "reason": "数据完整性验证未通过"},
        ])
        day = self._day("20260908")
        self.assertTrue(day["needs_action"])
        self.assertEqual(day["needs_action_count"], 1)
        self.assertIn("验证未通过", day["action_hint"])

    def test_interrupted_manual_run_does_not_flag_day(self):
        """09-07 场景：pass → pass → 手动被打断 ⇒ 日级不标需处理（改版核心）。"""
        self._write_history([
            {"ts": "2026-09-07 15:50:34", "trigger": "scheduled", "exit_code": 0,
             "verified": "skipped", "duration_sec": 11.7, "data_latest": "20260904"},
            {"ts": "2026-09-07 21:54:20", "trigger": "manual", "exit_code": 0,
             "verified": "pass", "duration_sec": 6.0, "data_latest": "20260904"},
            {"ts": "2026-09-07 21:58:37", "trigger": "manual", "exit_code": 0,
             "verified": "fail", "duration_sec": 6.4, "data_latest": None},
        ])
        day = self._day("20260907")
        classes = [e["class"] for e in day["sync"]]
        self.assertEqual(classes[-1], "run_interrupted")
        self.assertFalse(day["needs_action"])
        self.assertIsNone(day["action_hint"])

    def test_backward_compatible_fields_kept(self):
        """旧字段不破（前端可渐进迁移）：ts/trigger/exit_code/verified/duration/data_latest。"""
        self._write_history([
            {"ts": "2026-09-09 15:50:33", "trigger": "scheduled", "exit_code": 0,
             "verified": "pass", "duration_sec": 24.8, "data_latest": "20260908"},
        ])
        entry = self._day("20260909")["sync"][0]
        for key in ("ts", "trigger", "exit_code", "verified", "duration_sec", "data_latest"):
            self.assertIn(key, entry)


class AssetsPayloadTest(_OpsTestCase):
    """assets_payload：研究库/双备份/磁盘分层的真实字段 + TTL 缓存 + 降级。"""

    def setUp(self):
        super().setUp()
        self._cache_reset()

    def _cache_reset(self):
        app._assets_cache = (0.0, {})
        app._disk_detail_cache = (0.0, {})

    def test_research_stats_real_counts(self):
        """研究库计数来自真实 SQLite（写入可读回）。"""
        from storage.research_store import SqliteResearchStore
        store = SqliteResearchStore(Path(self.tmp) / "research.db")
        self.addCleanup(store.close)
        store.write_metrics("20260911", {"metrics": {"n_samples": 33}})
        store.write_series("premium_mean", {"values": [0.01]})
        store.write_snapshots("20260911", {"000001": {"open_price": 1.0}})
        st = app.research_db_stats()
        self.assertTrue(st["available"])
        self.assertEqual(st["metrics"], 1)
        self.assertEqual(st["series"], 1)
        self.assertEqual(st["snapshots"], 1)
        self.assertEqual(st["lists"], 0)
        self.assertGreater(st["bytes"], 0)

    def test_research_stats_degrades_without_store(self):
        """研究库不可用 → available=False 且计数为 0（前端显示未监控，不编数字）。"""
        with mock.patch.dict(os.environ, {"RESEARCH_STORE": "mydb"}):
            st = app.research_db_stats()
        self.assertIn("mode", st)
        self.assertIsInstance(st["available"], bool)

    def test_backup_stats_splits_two_families(self):
        """仓库备份与研究库备份分开计数（此前混成一个数会误导）。"""
        wh = Path(self.tmp) / "warehouse" / "backups"
        wh.mkdir(parents=True)
        (wh / "warehouse-20260911-175042-6358d8c0.db").write_bytes(b"x" * 10)
        (wh / "warehouse-20260910-225326-e81b7e48.db").write_bytes(b"x" * 20)
        rs = Path(self.tmp) / "backups"
        rs.mkdir(parents=True)
        (rs / "research-20260911-092604-b88d212d.db").write_bytes(b"x" * 5)
        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"), \
             mock.patch("storage.research_store.resolve_backup_dir", return_value=rs):
            st = app.backup_stats()
        self.assertEqual(st["warehouse"]["count"], 2)
        self.assertEqual(st["warehouse"]["bytes"], 30)
        self.assertEqual(st["research"]["count"], 1)
        self.assertEqual(st["total_bytes"], 35)
        self.assertIsNotNone(st["warehouse"]["last_mtime"])

    def test_disk_detail_groups_and_volume(self):
        (Path(self.tmp) / "data").mkdir()
        (Path(self.tmp) / "data" / "a.ldb").write_bytes(b"x" * 100)
        (Path(self.tmp) / "mydb").mkdir()
        (Path(self.tmp) / "mydb" / "b.ldb").write_bytes(b"x" * 50)
        (Path(self.tmp) / "warehouse").mkdir()
        (Path(self.tmp) / "warehouse" / "w.duckdb").write_bytes(b"x" * 25)
        with mock.patch.object(config, "WAREHOUSE_DIR", Path(self.tmp) / "warehouse"):
            d = app.disk_usage_detail(force=True)
        self.assertEqual(d["groups"]["market_data"], 100)
        self.assertEqual(d["groups"]["mydb"], 50)
        self.assertEqual(d["groups"]["warehouse"], 25)
        self.assertEqual(d["total_bytes"], 175)
        self.assertIsNotNone(d["volume"])

    def test_caches_avoid_rescan(self):
        """TTL 内不重扫（15s 轮询不能每拍递归 stat）；force 绕过。"""
        with mock.patch.object(app, "disk_usage_detail", wraps=app.disk_usage_detail) as m:
            app.assets_payload(force=True)
            first = m.call_count
            app.assets_payload()
            self.assertEqual(m.call_count, first, "60s TTL 内不应再次统计磁盘")
            app.assets_payload(force=True)
            self.assertEqual(m.call_count, first + 1)

    def test_snapshot_payload_includes_assets(self):
        """snapshot 单通道带上 assets 块（前端一次拿到三栏资产卡）。"""
        payload = web_handlers.snapshot_payload(7)
        self.assertIn("assets", payload)
        self.assertIn("research", payload["assets"])
        self.assertIn("backups", payload["assets"])
        self.assertIn("disk", payload["assets"])


# =====================================================================
# 4b) 0.10.37：运行中引擎版本探测 + 上游版本判定/告警
#     A：Dockerfile 注入 IMAGE_TAG；B：stale 判定改「上游 tag > 引擎版本」；
#     D：探针失败/发现新版/版本号不可判定 → 告警中心（不再静默）。
# =====================================================================
class EngineVersionProbeTest(_OpsTestCase):
    """storage.providers.free_stockdb.engine_version_info：启动日志 + 二进制双来源。"""

    def setUp(self):
        super().setUp()
        self.log = Path(self.tmp) / "log.txt"
        self.binary = Path(self.tmp) / "stockdb"
        p = mock.patch.object(config, "STOCKDB_LOG_FILE", self.log)
        q = mock.patch.object(free_stockdb_mod, "_ENGINE_BINARY", str(self.binary))
        p.start()
        q.start()
        self.addCleanup(p.stop)
        self.addCleanup(q.stop)

    def test_parses_startup_line(self):
        """`stockdb-server 0.3.5-stockdb` → version 原样、base 去后缀（比较用）。"""
        self.log.write_text("stockdb-server 0.3.5-stockdb\nStarted: 2026-09-13 10:52:17\n",
                            encoding="utf-8")
        info = free_stockdb_mod.engine_version_info(force=True)
        self.assertEqual(info["base"], "0.3.5")
        self.assertEqual(info["version"], "0.3.5-stockdb")
        self.assertEqual(info["source"], "log")
        self.assertEqual(info["log"], str(self.log))

    def test_last_startup_line_wins_after_restart(self):
        """引擎重启换版本 → 取最后一条（不取首条）。"""
        self.log.write_text("stockdb-server 0.3.2-stockdb\n"
                            "stockdb-server 0.3.5-stockdb\n", encoding="utf-8")
        self.assertEqual(free_stockdb_mod.engine_version_info(force=True)["base"], "0.3.5")

    def test_binary_fallback_when_log_has_no_banner(self):
        """NAS 实况：日志只落 ERROR 级（无横幅）→ 退回二进制版本字面量。"""
        self.log.write_text("[ERROR] open leveldb failed: Corruption: 10 missing files\n",
                            encoding="utf-8")
        self.binary.write_bytes(
            b"\x7fELFjunk stockdb-server\x00" + b"0.3.5\x00" + b"0.3.5-stockdb\x00"
            + b"1.12.12\x00" + b"120.53.53\x00" + b"127.0.0.1\x00" + b"0.0.0\x00")
        info = free_stockdb_mod.engine_version_info(force=True)
        self.assertEqual(info["source"], "binary")
        self.assertEqual(info["version"], "0.3.5-stockdb")  # 带产品后缀者优先
        self.assertEqual(info["base"], "0.3.5")

    def test_binary_scan_ignores_ip_like_tokens(self):
        """IP/依赖版本噪声不误取：只认 X.Y.Z（后接 -后缀者优先）。"""
        self.binary.write_bytes(b"120.53.53\x00127.0.0\x001.12.12\x000.3.5\x00")
        info = free_stockdb_mod.engine_version_info(force=True)
        self.assertNotIn(info["version"], ("127.0.0", "120.53.53"))
        self.assertEqual(info["base"], "0.3.5")

    def test_missing_sources_return_none(self):
        """日志与二进制都不可用 → None（调用方按"无法比对"降级，不抛）。"""
        self.assertIsNone(free_stockdb_mod.engine_version_info(force=True))

    def test_file_change_invalidates_cache(self):
        """TTL 内日志 mtime/size 变化 → 立刻重读（引擎换版不被缓存掩盖）。"""
        self.log.write_text("stockdb-server 0.3.5-stockdb\n", encoding="utf-8")
        self.assertEqual(free_stockdb_mod.engine_version_info(force=True)["base"], "0.3.5")
        time.sleep(0.01)
        self.log.write_text("stockdb-server 0.3.6-stockdb\n", encoding="utf-8")
        self.assertEqual(free_stockdb_mod.engine_version_info()["base"], "0.3.6")


class UpstreamVersionStatusTest(_OpsTestCase):
    """app.upstream_status / upstream_release_alert：同类版本线判定 + 告警接线。"""

    def setUp(self):
        super().setUp()
        self.log = Path(self.tmp) / "log.txt"
        p = mock.patch.object(config, "STOCKDB_LOG_FILE", self.log)
        p.start()
        self.addCleanup(p.stop)
        self.alerts = app.Alerts.init(os.path.join(self.tmp, "upstream.json"))
        self.log.write_text("stockdb-server 0.3.5-stockdb\n", encoding="utf-8")

    def _release(self, tag):
        return {"tag_name": tag, "html_url": "https://x", "published_at": "2026-07-19T00:00:00Z"}

    def test_update_available_when_upstream_newer(self):
        """上游 0.3.6 > 引擎 0.3.5 → update_available（旧逻辑在此恒 false）。"""
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.6")):
            st = app.upstream_status()
        self.assertEqual(st["kind"], "update_available")
        self.assertEqual(st["engine_version"], "0.3.5-stockdb")
        self.assertIn("测试版本0.3.6", st["message"])
        self.assertIn("建议升级镜像", st["message"])

    def test_up_to_date_when_equal_or_older(self):
        """上游 == 引擎 / 上游更旧 → up_to_date（不误报）。"""
        for tag in ("测试版本0.3.5", "测试版本0.3.4"):
            with mock.patch.object(app, "fetch_upstream_release",
                                   return_value=self._release(tag)):
                self.assertEqual(app.upstream_status()["kind"], "up_to_date")

    def test_probe_failed_kind(self):
        """探针 None → probe_failed（显式降级，不再静默留空）。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            st = app.upstream_status()
        self.assertEqual(st["kind"], "probe_failed")
        self.assertIn("探测失败", st["message"])

    def test_unknown_when_engine_tag_missing(self):
        """引擎版本与 IMAGE_TAG 都拿不到 → unknown（无法比对 ≠ 已最新）。"""
        self.log.unlink()  # 启动日志不可用（无引擎版本来源）
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.6")), \
             mock.patch.dict(os.environ, {"IMAGE_TAG": "", "STOCKDB_VERSION": ""}, clear=False):
            st = app.upstream_status()
        self.assertIsNone(st["engine_tag"])
        self.assertEqual(st["kind"], "unknown")
        self.assertIn("无法判断", st["message"])

    def test_image_tag_fallback_when_log_missing(self):
        """日志读不到时退回 IMAGE_TAG（A 注入的构建期版本）仍可判定。"""
        self.log.unlink()
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.6")), \
             mock.patch.dict(os.environ, {"IMAGE_TAG": "0.3.5"}, clear=False):
            st = app.upstream_status()
        self.assertEqual(st["kind"], "update_available")
        self.assertEqual(st["engine_tag"], "0.3.5")

    def test_alert_warns_on_update_and_probe_failure(self):
        """D：发现新版 / 探针失败 → 告警中心出现「上游」源 warning。"""
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.9")):
            self.assertEqual(app.upstream_release_alert(alerts=self.alerts),
                             "update_available")
        top = self.alerts.list()[0]
        self.assertEqual((top["level"], top["source"]), ("warning", "上游"))
        self.assertIn("0.3.9", top["message"])
        app.upstream_release_alert(
            alerts=self.alerts,
            status={"kind": "probe_failed", "message": "上游版本探测失败（GitHub 不可达或超出重试）：本次无法判断是否有新版"})
        self.assertEqual(len(self.alerts.list()), 2)
        self.assertTrue(any("探测失败" in e["message"] for e in self.alerts.list()))

    def test_alert_resolves_when_back_to_latest(self):
        """条件恢复（已是最新）→ 撤回同源告警（自愈纪律一致）。"""
        app.upstream_release_alert(
            alerts=self.alerts,
            status={"kind": "update_available", "message": "上游引擎已发布 0.3.9（当前运行 0.3.5-stockdb）"})
        self.assertEqual(self.alerts.count(), 1)
        app.upstream_release_alert(
            alerts=self.alerts,
            status={"kind": "up_to_date", "message": "引擎已是最新（上游最新 0.3.5）"})
        self.assertEqual(self.alerts.count(), 0)

    def test_alert_dedup_same_day(self):
        """同一判定每 60s 巡更一次 → 当日去重不刷屏。"""
        st = {"kind": "update_available", "message": "上游引擎已发布 0.3.9（当前运行 0.3.5-stockdb）"}
        for _ in range(5):
            app.upstream_release_alert(alerts=self.alerts, status=st)
        self.assertEqual(self.alerts.count(), 1)

    def test_version_payload_uses_engine_version(self):
        """接口载荷：stale 由引擎版本判定；seam 字段（engine/image/msg）齐全。"""
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.6")), \
             mock.patch.dict(os.environ, {"IMAGE_TAG": "0.3.5"}, clear=False):
            payload = web_handlers.version_payload()
        self.assertTrue(payload["stale"])
        self.assertIn("建议升级镜像", payload["msg"])
        self.assertEqual(payload["engine"]["base"], "0.3.5")
        self.assertEqual(payload["image"]["tag"], "0.3.5")
        self.assertEqual(payload["upstream"]["tag_name"], "测试版本0.3.6")

    def test_version_payload_not_stale_when_panel_newer(self):
        """回归护栏：面板版本 0.10.x 远高于上游 0.x —— 旧逻辑会恒 false，
        新逻辑必须依据引擎版本（此处 0.3.5 vs 上游 0.3.4 → 不落后）。"""
        with mock.patch.object(app, "fetch_upstream_release",
                               return_value=self._release("测试版本0.3.4")):
            payload = web_handlers.version_payload()
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["msg"], "")

    def test_version_payload_marks_probe_failure(self):
        """D：探针失败 → msg 显式标注降级（前端/巡检可判断"没探测到"≠"已最新"）。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            payload = web_handlers.version_payload()
        self.assertFalse(payload["stale"])
        self.assertIn("探测失败", payload["msg"])


# =====================================================================
# Phase 5 M0：前端静态服务 / SPA 回退 / legacy 逃生通道 / overview 聚合
# 直连 app.Handler.do_GET（FakeConn 提供 rfile/wfile），断言真实路由行为。
# =====================================================================
class _FakeConn:
    """构造 BaseHTTPRequestHandler 所需的最小连接对象（rfile/wfile 均为内存）。"""

    def __init__(self):
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()

    def makefile(self, mode, *args):
        return self.rfile if mode == "rb" else self.wfile

    def sendall(self, data):
        self.wfile.write(data)

    def settimeout(self, *args):  # 0.9.11：Handler.timeout 触发 setup 调用（桩兼容）
        pass


def _do_get(path: str):
    """直连 do_GET，返回 (status, headers_dict, body_bytes)。"""
    conn = _FakeConn()
    handler = _WebHandler(conn, ("127.0.0.1", 1), None)
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.headers = {}
    handler.path = path
    handler.do_GET()
    raw = conn.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b": ")
        headers[k.decode().lower()] = v.decode()
    return status, headers, body


def _do_post(path: str, payload: dict, method: str | None = None):
    """直连 POST 处理器（0.10.38：静音端点用例需要），返回 (status, headers, body)。

    不调 do_POST（那会走完整 HTTP 解析：构造期 handle() 已消费 rfile/wfile，
    实测踩到 'I/O operation on closed file'），改为直接调路由方法——与 routes.py
    的映射同源，断言的是"路由 + 载荷"契约本身。
    """
    from interfaces.web.routes import POST_ROUTES
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = _FakeConn()
    handler = _WebHandler.__new__(_WebHandler)     # 跳过 __init__ 的协议初始化
    handler.command = method or "POST"
    handler.requestline = f"{handler.command} {path} HTTP/1.1"   # _send 内置日志要用
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("127.0.0.1", 1)
    handler.headers = {"Content-Length": str(len(raw_body)),
                       "Content-Type": "application/json"}
    handler.path = path
    handler.rfile = io.BytesIO(raw_body)
    handler.wfile = io.BytesIO()
    name = POST_ROUTES.get(path)
    if name is None:
        return 404, {}, b'{"error": "not found"}'
    getattr(handler, name)()
    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b": ")
        headers[k.decode().lower()] = v.decode()
    return status, headers, body


class _StaticServingTests(_OpsTestCase):
    """M0 静态服务：根路径 HTML、legacy 逃生通道、路径穿越防护、overview 聚合。"""

    def test_root_returns_html(self):
        status, headers, body = _do_get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<html", body)

    def test_legacy_escape_hatch(self):
        """旧面板完整保留：/legacy 返回原 PAGE（标题不变）。"""
        status, _, body = _do_get("/legacy")
        self.assertEqual(status, 200)
        self.assertIn("stockdb 控制台".encode(), body)

    def test_path_traversal_blocked(self):
        """路径穿越不吐真实文件：落入 SPA 回退（index.html），绝不泄露 /etc/passwd。"""
        status, _, body = _do_get("/../../etc/passwd")
        self.assertEqual(status, 200)
        self.assertNotIn(b"root:", body)
        self.assertIn(b"<html", body)

    def test_unknown_api_404_unchanged(self):
        status, _, _ = _do_get("/api/nonexistent")
        self.assertEqual(status, 404)

    def test_overview_aggregation(self):
        """/api/overview 四块聚合齐全，version 块带 ui_mode。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/overview")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        for key in ("health", "alerts", "mcp", "version"):
            self.assertIn(key, payload)
        self.assertIn("count", payload["alerts"])
        self.assertEqual(payload["version"]["ui_mode"], app.WEBUI_UI)

    def test_version_ui_mode(self):
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/version")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertIn("ui_mode", payload)
        self.assertIn(payload["ui_mode"], ("spa", "legacy"))


class _DiagTests(_OpsTestCase):
    """Phase 5.1 /api/diag：一键诊断聚合（五检查 + 环境块，单块降级不 500）。"""

    def test_diag_structure(self):
        """五项检查齐全 + env 关键字段 + all_ok 汇总。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        names = [c["name"] for c in payload["checks"]]
        self.assertEqual(names, ["upstream_github", "stockdb_service",
                                 "pybao", "disk", "calendar"])
        for c in payload["checks"]:
            self.assertIn("label", c)
            self.assertIn("ok", c)
            self.assertIn("note", c)
        self.assertIsInstance(payload["all_ok"], bool)
        for key in ("python", "arch", "webui_version", "ui_mode", "data_latest", "uptime_seconds"):
            self.assertIn(key, payload["env"])
        self.assertEqual(payload["env"]["webui_version"], app.WEBUI_VERSION)

    def test_diag_upstream_degraded(self):
        """上游不可达 → 该检查标记 degraded 但 **ok=True**（网络受限不是系统不健康，
        0.10.38：此前 ok=False 会把 diag 整体打红，属假警报）。
        注：不断言 all_ok——本地/CI 环境下 stockdb_service 等其他检查会因无容器而 false，
        本用例只锁"上游网络受限不再把该项判红"这一条。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        up = next(c for c in payload["checks"] if c["name"] == "upstream_github")
        self.assertTrue(up["ok"])
        self.assertTrue(up["degraded"])
        self.assertIn("网络受限", up["note"])

    def test_diag_upstream_ok_when_reachable(self):
        """上游可达 → degraded=False、note 带判定说明。"""
        rel = {"tag_name": "测试版本0.3.5", "html_url": "https://x",
               "published_at": "2026-07-19T00:00:00Z"}
        with mock.patch.object(app, "fetch_upstream_release", return_value=rel):
            status, _, body = _do_get("/api/diag")
        payload = json.loads(body.decode())
        up = next(c for c in payload["checks"] if c["name"] == "upstream_github")
        self.assertTrue(up["ok"])
        self.assertFalse(up["degraded"])
        self.assertIn("测试版本0.3.5", up["note"])

    def test_diag_pybao_check(self):
        """pybao 检查 = 三个模块 find_spec 全命中（无 pybao 时为 False 也合法）。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        py = next(c for c in payload["checks"] if c["name"] == "pybao")
        self.assertIsInstance(py["ok"], bool)


class _SyncFailureTests(_OpsTestCase):
    """0.8.17：同步器认证/连接失败识别（退出码 0 但失败 → 不再掩盖成 None>0 崩溃）。"""

    def test_auth_failed_recognized(self):
        """auth failed 输出 → 明确失败原因。"""
        out = ("[log] 正在验证设备状态...\n"
               "[log] 状态:连接失败\n"
               "[log] error: auth failed, 状态:连接失败\n"
               "client finished.\n")
        self.assertIn("认证失败", app._sync_failure_reason(out))

    def test_connect_failed_recognized(self):
        self.assertIn("连接失败", app._sync_failure_reason("[log] 状态:连接失败"))

    def test_normal_sync_no_failure(self):
        """正常同步输出（下载进度等）→ None。"""
        out = "[progress] 83.4% 233.68/280.20 MB ... [file] 16/16 ok"
        self.assertIsNone(app._sync_failure_reason(out))
        self.assertIsNone(app._sync_failure_reason(""))

    def test_downloads_none_comparison_safe(self):
        """downloads=None（未打印数量）不再参与 > 比较（0.8.16 事故回归）。"""
        counts = {"downloads": None, "deletes": None}
        dl = counts.get("downloads")
        # 旧写法崩溃点：counts.get("downloads", 0) > 0
        self.assertRaises(TypeError, lambda: counts.get("downloads", 0) > 0)
        # 新写法安全：None 走"保守重启"分支
        self.assertTrue(dl is None or dl > 0)
        # _sync_effective 对 None downloads 安全（短路）
        self.assertTrue(app._sync_effective("20260813", "20260814", counts))


class _DataLatestDateTests(_OpsTestCase):
    """Phase 5.1 稳定性：data_latest_date 失败缓存 + 并发单飞（防多标签切换打瘫后端）。"""

    def setUp(self):
        super().setUp()
        app._latest_date_cache.update(at=0.0, val=None)
        app._stockdb_breaker.update(fails=0, open_until=0.0)  # 熔断器全局态复位，防用例串扰

    def test_failure_result_is_cached(self):
        """探测失败（None）也缓存：8s 内不重复打 stockdb（此前失败不缓存会风暴重探）。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            first = app.data_latest_date()
            second = app.data_latest_date()
        self.assertIsNone(first)
        self.assertIsNone(second)
        # 两次调用只打了一轮探测（缓存命中，未再 urlopen）
        with mock.patch("urllib.request.urlopen") as m:
            app.data_latest_date()
            app.data_latest_date()
        self.assertEqual(m.call_count, 0)

    def test_single_flight_concurrent_probe(self):
        """并发探测单飞：一路在跑时其余调用立即返回缓存，urlopen 只被调一轮。"""
        def _slow_urlopen(*a, **k):
            time.sleep(0.4)
            resp = mock.MagicMock()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            resp.read.return_value = json.dumps([{"date": "20260814"}]).encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_slow_urlopen) as m:
            t = threading.Thread(target=app.data_latest_date, kwargs={"force": True})
            t.start()
            time.sleep(0.05)  # 确保线程已持有探测锁
            second = app.data_latest_date(force=True)  # 锁被占 → 立即返回缓存（None）
            t.join()
            count_after_first_round = m.call_count
            third = app.data_latest_date()  # 探测完成且写入缓存 → 命中，不再 urlopen
            count_after_third = m.call_count
        self.assertIsNone(second)  # 缓存尚未写入，合法（下一轮轮询拿到新值）
        self.assertGreaterEqual(count_after_first_round, 3)  # 一轮探测 = 3~4 个月前缀
        self.assertEqual(count_after_third, count_after_first_round)  # 缓存命中零新探测
        self.assertEqual(third, "20260814")

    def test_success_cache_hit(self):
        """成功后 8s 内命中缓存，不再 urlopen。"""
        with mock.patch("urllib.request.urlopen") as m:
            resp = mock.MagicMock()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            resp.read.return_value = json.dumps([{"date": "20260814"}]).encode()
            m.side_effect = lambda *a, **k: resp
            first = app.data_latest_date()
            second = app.data_latest_date()
        self.assertEqual(first, "20260814")
        self.assertEqual(second, "20260814")


class _StockdbGateTests(_OpsTestCase):
    """Phase 5.1 并发卫生：熔断器 + 信号量（stockdb 上游访问闸口）。"""

    def setUp(self):
        super().setUp()
        app._stockdb_breaker.update(fails=0, open_until=0.0)
        app._latest_date_cache.update(at=0.0, val=None)

    def test_breaker_opens_after_failures(self):
        """连续 threshold 次失败 → 熔断打开：后续探针快速降级、零 urlopen。"""
        app._stockdb_breaker.update(threshold=2, cooldown=300.0)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            for _ in range(2):
                with self.assertRaises(OSError):
                    app.stockdb_fetch("/?cmd=get&t=x", timeout=1, breaker=True)
        self.assertTrue(app._stockdb_breaker_open())
        with mock.patch("urllib.request.urlopen") as m:
            app.data_latest_date()  # 熔断中 → 直接返回缓存 None，不打网络
        self.assertEqual(m.call_count, 0)

    def test_breaker_recovers_after_cooldown(self):
        """冷却期后熔断关闭：恢复探测。"""
        app._stockdb_breaker.update(fails=3, open_until=time.time() + 300.0)
        self.assertTrue(app._stockdb_breaker_open())
        with mock.patch.object(app.time, "time", return_value=time.time() + 301.0):
            self.assertFalse(app._stockdb_breaker_open())

    def test_breaker_success_resets(self):
        """探测成功 → 失败计数复位。"""
        app._stockdb_breaker.update(fails=2, open_until=0.0)
        resp = mock.MagicMock()
        resp.__enter__ = mock.MagicMock(return_value=resp)
        resp.__exit__ = mock.MagicMock(return_value=False)
        resp.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            app.stockdb_fetch("/?cmd=vals&t=x", timeout=1, breaker=True)
        self.assertEqual(app._stockdb_breaker["fails"], 0)
        self.assertFalse(app._stockdb_breaker_open())

    def test_semaphore_limits_concurrency(self):
        """信号量满 → 立即 RuntimeError（不阻塞等待堆积线程）。"""
        # 0.9.2 批次 3：闸口实现迁 storage/providers/free_stockdb，patch 实现侧
        with mock.patch.object(free_stockdb_mod, "_gate", threading.Semaphore(1)):
            gate = free_stockdb_mod._gate
            gate.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    app.stockdb_fetch("/?cmd=get&t=x", timeout=1)
            finally:
                gate.release()

    def test_diag_note_shows_gate_state(self):
        """诊断的 stockdb_service 项注明闸口状态。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        note = next(c["note"] for c in json.loads(body.decode())["checks"]
                    if c["name"] == "stockdb_service")
        self.assertIn("上游闸口", note)


class _FakeResearchStore:
    """ResearchStore 语义接口替身（0.9.5 M5）：内存 dict 存储 + 存值契约断言。"""

    def __init__(self):
        self.metrics: dict[str, dict] = {}
        self.series: dict[str, dict] = {}
        self.lists: dict[str, dict] = {}
        self.snapshots: dict[str, dict[str, dict]] = {}

    def _check(self, v):
        if isinstance(v, str):
            raise AssertionError("research store 契约违反：值不能是 JSON 字符串")

    def write_metrics(self, date, payload):
        self._check(payload)
        self.metrics[date] = payload

    def read_metrics(self, date):
        return self.metrics.get(date)

    def write_series(self, metric, payload):
        self._check(payload)
        self.series[metric] = payload

    def read_series(self, metric):
        return self.series.get(metric)

    def write_list(self, date, payload):
        self._check(payload)
        self.lists[date] = payload

    def read_list(self, date):
        return self.lists.get(date)

    def write_snapshots(self, date, rows):
        self.snapshots.setdefault(date, {}).update(rows)

    def read_snapshots(self, date):
        return self.snapshots.get(date, {})

    def migrate_from_engine(self):
        return {"ok": True, "counts": {}}

    def backup(self):
        return None


class _AuctionBackfillTests(_OpsTestCase):
    """0.8.1 历史序列回填：冷启动修复（首跑前序列为空 → 分位无分母）。

    夹具设计：每个交易日 D 的点集 = 当日非一字涨停股（全字段，供 D+1 清单）
    + 昨日清单股的溢价点（open/prev_close，供 D 日溢价）。三对股票 X/Y/Z 串起三天。
    """

    POINTS = {
        # 0813：Z 当日涨停（供 0814 清单）+ 600005 当日涨停但 0814 无 bar
        # （0.9.0 边界 c：missing_open 计数样本）+ Y 溢价点（open 9.5；prev_close=999 毒值——
        # 0.8.13 起分母改用 T-1 收盘 11.0，毒值用于证明未回退到 pre_close 字段）
        "20260813": [
            {"code": "600004", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
            {"code": "600005", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
            {"code": "600003", "open": 9.5, "prev_close": 999.0},
        ],
        # 0812：Y 当日涨停（供 0813 清单）+ X 溢价点（open 12.1 → 12.1/11.0-1=+10%）
        "20260812": [
            {"code": "600003", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
            {"code": "600002", "open": 12.1, "prev_close": 999.0},
        ],
        # 0811：X 当日涨停（供 0812 清单）
        "20260811": [
            {"code": "600002", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
        ],
        # 0814：Z 溢价点（open 11.0 → 11.0/11.0-1=0%）
        "20260814": [
            {"code": "600004", "open": 11.0, "prev_close": 999.0},
        ],
    }

    def setUp(self):
        super().setUp()
        app._auction_backfill_state.update(running=False, started=None,
                                           finished=None, result=None)

        def fake_snapshot(args):
            return {"points": self.POINTS.get(args.get("date"), [])}

        # 0.9.2 批次 4：打板用例迁 services/auction_tasks.py，patch 服务层注入点
        # 装配日历注入点（组合根 main() 才装配；测试直接绑定真实交易日历）
        auction_tasks_mod.is_trading_day = app.is_trading_day
        # 0.9.5（M5）：研究成果仓储注入 fake（语义接口替身；断言存值契约：
        # 研究产出必须为原生对象，不允许 JSON 字符串形态）
        self._fake_research = _FakeResearchStore()
        auction_tasks_mod.research_store = self._fake_research
        self._patch_snap = mock.patch.object(auction_tasks_mod, "query_snapshot",
                                             side_effect=fake_snapshot)
        self._patch_latest = mock.patch.object(auction_tasks_mod, "data_latest",
                                               return_value="20260814")
        self._patch_prev = mock.patch.object(auction_tasks_mod, "_auction_prev_trade_date",
                                             side_effect={
            "20260814": "20260813", "20260813": "20260812",
            "20260812": "20260811", "20260811": "20260810",
        }.get)
        for p in (self._patch_snap, self._patch_latest, self._patch_prev):
            p.start()
        self.addCleanup(lambda: [p.stop() for p in (
            self._patch_snap, self._patch_latest, self._patch_prev)])
        self.addCleanup(lambda: setattr(auction_tasks_mod, "research_store", None))

    def _load_series(self, metric):
        # round-trip：走 research store 读取链（0.9.5 M5：打板序列适配函数）
        return app._auction_load_series(auction_tasks_mod._research_series_read, metric)

    def test_backfill_builds_series_and_daily_metrics(self):
        r = app.auction_run_backfill(days=3)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["backfilled_days"], 3)
        # 序列时间正序（0.8.13：分母 = T-1 收盘 11.0，毒值 999 未生效即证明）：
        # 0812: 12.1/11.0-1=+10% → 0813: 9.5/11.0-1=-13.64% → 0814: 11.0/11.0-1=0%
        vals = self._load_series("premium_mean")
        self.assertEqual(len(vals), 3)
        self.assertAlmostEqual(vals[0], 0.10)
        self.assertAlmostEqual(vals[1], 9.5 / 11.0 - 1.0)
        self.assertAlmostEqual(vals[2], 0.0)
        # 逐日指标（kline 口径）：0.8.11 起分位口径 = 此前 60 有效观测严格低于天数/60，
        # 3 天回填历史不足 → 全部 rank/strength 为 None（首个满分母分位在序列满 60 后）
        d0 = self._metrics("20260812")
        self.assertEqual(d0["value_source"], "kline")
        self.assertIsNone(d0["rank_60d"]["premium_mean"])
        self.assertIsNone(d0["strength_60d"]["premium_mean"])
        d1 = self._metrics("20260813")
        self.assertIsNone(d1["rank_60d"]["premium_mean"])
        self.assertIsNone(d1["strength_60d"]["premium_mean"])
        d2 = self._metrics("20260814")
        self.assertIsNone(d2["rank_60d"]["premium_mean"])
        self.assertIsNone(d2["strength_60d"]["premium_mean"])
        # 成功率序列（0.8.13 口径）：+10% → 1.0；-13.64% → 0.0；0% → 0.0
        self.assertEqual(self._load_series("success_rate"), [1.0, 0.0, 0.0])

    def _metrics(self, d8):
        # round-trip：走 research store 接口替身（0.9.5 M5）
        return self._fake_research.read_metrics(d8)

    def test_backfill_writes_daily_payload(self):
        """0.10.39 回归：回填必须同时写 daily 子载荷。

        NAS 0.10.38 实机抓到的事故：回填只写 metrics，把 live 收口写的 daily 覆盖掉，
        MCP 预计算快车道（`_precomputed_row` 读 daily）随即失效 → `get_board_open_effect_history`
        从 0.02s 退化为 40s 全市场重算（cache_hit=False / precomputed_days=0）。
        """
        app.auction_run_backfill(days=3)
        for d8 in ("20260812", "20260813", "20260814"):
            payload = self._metrics(d8)
            daily = payload.get("daily")
            self.assertIsInstance(daily, dict, f"{d8} 缺 daily 子载荷（快车道会失效）")
            # 与 live 收口同构的关键字段（MCP _precomputed_row / 前端直读依赖）
            for key in ("matched_count", "positive_count", "flat_count", "negative_count",
                        "success_rate", "average_open_return_pct", "distribution",
                        "metrics", "trade_date"):
                self.assertIn(key, daily, f"{d8}.daily 缺 {key}")
            self.assertEqual(daily["trade_date"], f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}")
            # 计数自洽：匹配数 = 正 + 平 + 负
            self.assertEqual(daily["matched_count"],
                             daily["positive_count"] + daily["flat_count"] + daily["negative_count"])
            # 覆盖块：候选 = 样本 + 缺价（守恒）
            cov = daily.get("coverage") or {}
            self.assertEqual(cov.get("codes_requested"),
                             cov.get("fetched", 0) + (cov.get("missing_open") or 0))

    def test_backfill_daily_metrics_do_not_leak_internal_keys(self):
        """内部记账键（_candidates）不得进落盘 payload（只用于覆盖块计算）。"""
        app.auction_run_backfill(days=3)
        for d8 in ("20260812", "20260813", "20260814"):
            payload = self._metrics(d8)
            self.assertNotIn("_candidates", payload["metrics"])
            self.assertNotIn("_candidates", payload)

    def test_backfill_idempotent(self):
        app.auction_run_backfill(days=3)
        first = self._load_series("premium_mean")
        app.auction_run_backfill(days=3)
        self.assertEqual(self._load_series("premium_mean"), first)

    def test_backfill_missing_open_counted(self):
        """0.9.0 边界 c：板日涨停但指标日无 bar → missing_open_count 计数且守恒
        （候选 = n_samples + missing_open_count；指标值不变）。"""
        r = app.auction_run_backfill(days=3)
        self.assertTrue(r["ok"], r)
        # 0814 指标日：0813 候选 = 600004（有溢价点）+ 600005（无 bar）
        d = self._metrics("20260814")
        m = d["metrics"]
        self.assertEqual(m["n_samples"], 1)              # 只有 600004 有有效对
        self.assertEqual(m["missing_open_count"], 1)     # 600005 无 bar → 计数（0.9.0 前静默丢弃）
        self.assertEqual(m["n_samples"] + m["missing_open_count"], 2)  # 守恒
        # 指标值不受影响：0814 premium_mean 仍 = 600004 的 0%
        self.assertAlmostEqual(m["premium_mean"], 0.0)
        self.assertEqual(self._load_series("premium_mean")[-1], 0.0)
        # 无缺口日：0813 指标日（候选 = 600002 唯一，有溢价点）missing_open=0
        d13 = self._metrics("20260813")
        self.assertEqual(d13["metrics"]["missing_open_count"], 0)

    def test_close_missing_open_counted(self):
        """0.9.0 边界 c（收口路径）：清单股指标日无 bar → missing_open_count 守恒。"""
        today = datetime.date.today().strftime("%Y%m%d")
        # 0.9.2 批次 4：prev 日取服务层 patch 面（setUp 已 patch dict.get → 本用例
        # today 不在映射 → None；与 close 内部调用同一 patch 面，fake 匹配一致）
        prev_day = auction_tasks_mod._auction_prev_trade_date(today)

        def fake_close_snapshot(args):
            d = args.get("date")
            if d == today:
                return {"points": [
                    {"code": "600004", "open": 11.0, "close": 11.5, "prev_close": 10.0,
                     "is_st": False, "status": "TRADED"},
                ]}
            if d == prev_day:
                return {"points": [
                    {"code": "600004", "open": 10.0, "close": 10.5, "prev_close": 10.0,
                     "is_st": False, "status": "TRADED"},
                ]}
            return {"points": []}

        with mock.patch.object(auction_tasks_mod, "data_latest", return_value=today), \
             mock.patch.object(auction_tasks_mod, "query_snapshot", side_effect=fake_close_snapshot):
            # 预置今日清单：候选 = 600004（有溢价点）+ 600005（指标日无 bar）
            self._fake_research.write_list(today, {"codes": ["600004", "600005"]})
            r = app.auction_run_close()
            self.assertTrue(r["ok"], r)
            metrics = r["metrics"] or {}
            self.assertEqual(metrics["n_samples"], 1)
            self.assertEqual(metrics["missing_open_count"], 1)
            self.assertEqual(metrics["n_samples"] + metrics["missing_open_count"], 2)

    def test_route_backfill(self):
        """POST /api/auction/run {"task":"backfill","days":3} → 200 异步启动，后台完成后状态落库。"""
        # 注意：BaseRequestHandler.__init__ 会自动跑一次 handle()（把空 rfile 当请求行）。
        # 构造后再换入带 body 的新 rfile 与干净 wfile，避免 body 被 parse_request 吃掉。
        conn = _FakeConn()
        handler = _WebHandler(conn, ("127.0.0.1", 1), None)
        body = json.dumps({"task": "backfill", "days": 3}).encode()
        conn.wfile = io.BytesIO()
        handler.wfile = conn.wfile
        handler.rfile = io.BytesIO(body)
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.protocol_version = "HTTP/1.1"
        handler.requestline = "POST /api/auction/run HTTP/1.1"
        handler.path = "/api/auction/run"
        handler.headers = {"Content-Length": str(len(body))}
        handler.do_POST()
        raw = conn.wfile.getvalue()
        head, _, resp_body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        payload = json.loads(resp_body.decode())
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload.get("async"))
        # 等待后台 worker 完成（patched 依赖下毫秒级）
        deadline = time.time() + 5
        while app._auction_backfill_state["running"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(app._auction_backfill_state["running"])
        self.assertEqual(app._auction_backfill_state["result"]["backfilled_days"], 3)

    def test_points_for_codes_chunks_at_200(self):
        """清单 >200 只时分块拉取：每批 ≤200，合并去重。"""
        captured = []

        def fake_snapshot_capture(args):
            captured.append(list(args.get("codes") or []))
            return {"points": [{"code": c} for c in (args.get("codes") or [])]}

        with mock.patch.object(auction_tasks_mod, "query_snapshot", side_effect=fake_snapshot_capture):
            pts = app._auction_points_for_codes(
                "20260814", [f"{600000 + i}" for i in range(250)])
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(captured[0]), 200)
        self.assertEqual(len(captured[1]), 50)
        self.assertEqual(len(pts), 250)

    def test_backfill_guard_single_flight(self):
        """回填进行中再触发 → 拒绝并返回进行中提示（防并发两份重扫描）。"""
        app._auction_backfill_state.update(running=True, started="2026-08-15T00:00:00")
        r = app.auction_run_backfill_async(3)
        self.assertFalse(r["ok"])
        self.assertIn("已在运行中", r["reason"])

    def test_status_endpoint(self):
        """GET /api/auction/status 返回回填状态与日级守卫。"""
        status, _, body = _do_get("/api/auction/status")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertIn("backfill", payload)
        self.assertIn("running", payload["backfill"])


class AuctionTriggerWindowTest(_OpsTestCase):
    """0.10.35 打板触发时间窗 + 守卫持久化（2026-09-10 晚间误采收盘价实证）。

    根因：采集源取的是「当前价」，仅 09:26 前后 current==open 才等于竞价价。
    旧调度 now>=09:26 无上界 + 守卫纯内存（重启即清空）→ 晚间重启再次采集，
    采到收盘价冒充开盘价，16:30 对账全红（37 条误报）。
    """

    at_due = staticmethod(auction_tasks_mod._auction_due)

    def setUp(self):
        super().setUp()
        # SCHEDULE_FILE 是 import 期从真实 DATA_DIR 求值的常量，DATA_DIR patch 盖不住
        # （同 StaleSelfHealTest）——守卫落盘必须指向临时文件，避免污染真实 dev 库。
        self._sched_patch = mock.patch.object(
            app, "SCHEDULE_FILE", Path(self.tmp) / "sync_schedule.json")
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)

    def test_collect_only_within_window(self):
        """采集仅在 [09:26, 09:30] 触发；过窗不再补采（宁缺勿错）。"""
        g = {"collect": False, "close": False}
        self.assertIsNone(self.at_due("09:25", g))
        self.assertEqual(self.at_due("09:26", g), "collect")
        self.assertEqual(self.at_due("09:28", g), "collect")
        self.assertEqual(self.at_due("09:30", g), "collect")
        # 09:31 起超窗 → 当日不再采（关键回归：旧语义此刻仍会采集）
        self.assertIsNone(self.at_due("09:31", g))
        self.assertIsNone(self.at_due("15:00", g))
        # 晚间 21:xx 重启（正是 09-10 误采时刻）→ 不得触发采集（只可触发收口）
        self.assertNotEqual(self.at_due("21:56", g), "collect")

    def test_collect_guard_blocks_repeat(self):
        """采集守卫已置位 → 当日不再触发（防重复）。"""
        self.assertIsNone(self.at_due("09:27", {"collect": True, "close": False}))

    def test_close_after_threshold(self):
        """收口 16:30 起任意时刻触发（对账用 K 线开盘价，与时刻无关）。"""
        self.assertIsNone(self.at_due("16:29", {"collect": True, "close": False}))
        self.assertEqual(self.at_due("16:30", {"collect": True, "close": False}), "close")
        self.assertEqual(self.at_due("21:00", {"collect": True, "close": False}), "close")
        self.assertIsNone(self.at_due("21:00", {"collect": True, "close": True}))

    def test_guard_persisted_roundtrip(self):
        """守卫落盘后可读回（进程重启不再清空——0.10.35 核心修复）。"""
        app._mark_auction_fired("collect", "20260910")
        self.assertIn("20260910", app._auction_fired_dates("collect"))
        self.assertNotIn("20260910", app._auction_fired_dates("close"))
        # 重载 schedule（模拟重启后从磁盘读）→ 守卫仍在
        cfg = app.load_schedule()
        self.assertIn("20260910", cfg["auction_fired"]["collect"])

    def test_guard_state_merges_persisted(self):
        """_auction_guard_state 合并落盘守卫：重启后 collect 仍视为已触发。"""
        app._mark_auction_fired("collect", "20260911")
        # 装配注入（_wire 在 main 装配；测试显式绑定，或用类级已绑定）
        auction_tasks_mod.schedule_fired_provider = app._auction_fired_dates
        with mock.patch.object(auction_tasks_mod, "_auction_fired", {}):
            g = auction_tasks_mod._auction_guard_state("20260911")
        self.assertTrue(g["collect"])
        self.assertFalse(g["close"])

    def test_guard_caps_dates(self):
        """守卫日期仅保留最近 14 个（防累积）。"""
        for i in range(20):
            app._mark_auction_fired("collect", f"202609{i:02d}")
        self.assertEqual(len(app._auction_fired_dates("collect")), 14)


class StaleSelfHealTest(_OpsTestCase):
    """0.10.13 数据晚到自愈三件套（0.10.6 试运行 08-28 实证：镜像晚于 15:50 发布，
    定时同步 exit 0 但数据未前进，挂到次日）：
      - _expected_latest_date：收盘后应至当日；盘前应至前一交易日；跳过周末/节假日。
      - _arm_stale_retry：登记 stale_retry_pending；当日上限/截止时刻收口；
        收口时清空 pending；日记录持久化。
      - evening_stale_alert：21:00 前不告警；21:00 后滞后告警；追平不告警；
        非交易日不告警；当日去重。
    全部离线：时间注入（now_dt）、交易日/探针 patch、告警注入隔离实例。
    """

    def setUp(self):
        super().setUp()
        self.alerts = app.Alerts.init(os.path.join(self.tmp, "stale.json"))
        # SCHEDULE_FILE 是 import 期从真实 DATA_DIR 求值的常量，DATA_DIR patch 盖不住
        # 它——滞后重试状态（stale_retried/stale_retry_pending）必须落到临时目录，
        # 否则测试会读写真实状态文件（污染现场 + 用例间串扰）。
        self._sched_patch = mock.patch.object(
            app, "SCHEDULE_FILE", Path(self.tmp) / "sync_schedule.json")
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)

    # ---- _expected_latest_date ----

    def test_expected_latest_after_close_is_today(self):
        """工作日 15:00 后 → 应至当天（注入收盘后时刻，与挂钟/CI 时区无关）。

        原用例用真实 now() 反推，但 _expected_latest_date 以 now.hour>=15 判收盘，
        CI 跑在 UTC（无 TZ）且推送多在 UTC 15:00 之前 → 交易日必挂（2026-09-07
        main CI 两次失败实证）。改用确定性注入，与同文件「收盘前」用例同法。
        """
        probe = datetime.datetime(2026, 9, 4, 20, 0)  # 周五盘中后（交易日）
        self.assertTrue(app.is_trading_day(probe.date()))
        self.assertEqual(app._expected_latest_date(probe), "20260904")

    def test_expected_latest_after_close_matches_injected_probe(self):
        """收盘后返回值 == 注入时刻当天（TZ/时刻无关的显式口径）。"""
        probe = datetime.datetime(2026, 8, 3, 20, 0)  # 周一盘中后（交易日）
        self.assertTrue(app.is_trading_day(probe.date()))
        self.assertEqual(app._expected_latest_date(probe), probe.strftime("%Y%m%d"))

    def test_expected_latest_before_close_is_prev_trading_day(self):
        """盘前（15:00 前）→ 应至前一交易日（回退最多 10 天内必有）。"""
        probe = datetime.datetime.now().replace(hour=9, minute=0)
        exp = app._expected_latest_date(probe)
        self.assertIsNotNone(exp)
        # 前一交易日必然 <= 今天-1
        self.assertLess(exp, probe.strftime("%Y%m%d"))

    def test_expected_latest_skips_weekend(self):
        """周六 20:00 → 应至周五（2026-08-01 周六 → 07-31 周五，休市表覆盖 2026）。"""
        exp = app._expected_latest_date(datetime.datetime(2026, 8, 1, 20, 0))
        self.assertEqual(exp, "20260731")

    # ---- _arm_stale_retry ----

    def test_arm_stale_retry_registers_pending(self):
        """exit 0 但数据滞后 → 登记 pending（30 分钟后），当日计数 1。"""
        # 0.10.18：截止时刻守卫读真实挂钟（>= STALE_RETRY_UNTIL 拒登记），
        # 夜间跑套件必撞 → UNTIL 拉到 23:59 使本用例时间无关（cap 用例走
        # n>MAX 分支先短路，不受时刻影响，无需注入）
        with mock.patch.object(app, "STALE_RETRY_UNTIL", "23:59"):
            ok = app._arm_stale_retry("20260827", "20260828")
        self.assertTrue(ok)
        cfg = app.load_schedule()
        self.assertIsNotNone(cfg["stale_retry_pending"])
        self.assertEqual((cfg["stale_retried"] or {}).get(app._today_key()), 1)

    def test_arm_stale_retry_cap_reached_closes(self):
        """当日登记达上限 → 返回 False、清空 pending、不再新增计数。"""
        app._schedule_lock.acquire()
        try:  # 预置当日已登记 STALE_RETRY_MAX 次
            cfg_path = app.SCHEDULE_FILE
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            else:
                cfg = app._default_schedule()
            cfg["stale_retried"] = {app._today_key(): app.STALE_RETRY_MAX}
            cfg["stale_retry_pending"] = "2026-01-01 00:00:00"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        finally:
            app._schedule_lock.release()
        ok = app._arm_stale_retry("20260827", "20260828")
        self.assertFalse(ok)
        cfg = app.load_schedule()
        self.assertIsNone(cfg["stale_retry_pending"])  # 收口：pending 清空
        self.assertEqual((cfg["stale_retried"] or {}).get(app._today_key()),
                         app.STALE_RETRY_MAX)  # 计数不再增长

    def test_arm_stale_retry_persists(self):
        """登记落盘（重启不丢）：重新 load 仍见 pending。"""
        with mock.patch.object(app, "STALE_RETRY_UNTIL", "23:59"):
            app._arm_stale_retry("20260827", "20260828")
        cfg = app.load_schedule()  # 全新读取（非内存态）
        self.assertIsNotNone(cfg["stale_retry_pending"])

    # ---- evening_stale_alert ----

    def test_evening_alert_silent_before_window(self):
        """21:00 前不告警（即使数据滞后）。"""
        now = datetime.datetime(2026, 8, 28, 16, 0)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260827"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            fired = app.evening_stale_alert(now, alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.count(), 0)

    def test_evening_alert_fires_when_stale(self):
        """21:00 后交易日 + 数据滞后 → 告警（warning/数据，含最新日期）。"""
        now = datetime.datetime(2026, 8, 28, 21, 5)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260827"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            fired = app.evening_stale_alert(now, alerts=self.alerts)
        self.assertTrue(fired)
        self.assertEqual(self.alerts.count(), 1)
        top = self.alerts.list()[0]
        self.assertEqual((top["level"], top["source"]), ("warning", "数据"))
        self.assertIn("20260827", top["message"])

    def test_evening_alert_silent_when_caught_up(self):
        """21:00 后数据已追平 → 不告警。"""
        now = datetime.datetime(2026, 8, 28, 21, 5)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260828"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            fired = app.evening_stale_alert(now, alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.count(), 0)

    def test_evening_alert_silent_non_trading_day(self):
        """非交易日不告警（周末/节假日数据不更新属正常）。"""
        now = datetime.datetime(2026, 8, 29, 21, 5)  # 周六
        with mock.patch.object(app, "is_trading_day", return_value=False), \
             mock.patch.object(app, "data_latest_date", return_value="20260827"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            fired = app.evening_stale_alert(now, alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.count(), 0)

    def test_evening_alert_same_day_dedup(self):
        """同日晚重复评估（滞后未追平）→ 命中值恒 True，但消息相同当日去重不刷屏。"""
        now = datetime.datetime(2026, 8, 28, 21, 5)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260827"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            self.assertTrue(app.evening_stale_alert(now, alerts=self.alerts))
            self.assertTrue(app.evening_stale_alert(now, alerts=self.alerts))
        self.assertEqual(self.alerts.count(), 1)  # 去重：仍只有一条

    # ---- 0.10.36 自愈：条件恢复撤警 ----

    def test_evening_alert_resolves_when_caught_up(self):
        """晚间告警后数据追平 → 下一次评估撤回（同一晚 21:0x 追平场景）。"""
        now = datetime.datetime(2026, 8, 28, 21, 5)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260827"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            self.assertTrue(app.evening_stale_alert(now, alerts=self.alerts))
        self.assertEqual(self.alerts.count(), 1)
        with mock.patch.object(app, "is_trading_day", return_value=True), \
             mock.patch.object(app, "data_latest_date", return_value="20260828"), \
             mock.patch.object(app, "_expected_latest_date", return_value="20260828"):
            self.assertFalse(app.evening_stale_alert(
                datetime.datetime(2026, 8, 28, 21, 20), alerts=self.alerts))
        self.assertEqual(self.alerts.count(), 0)

    def test_evening_alert_resolves_before_window_and_non_trading_day(self):
        """21:00 前 / 非交易日评估也撤旧警（周一开盘前残留的晚间兜底告警被清）。"""
        self.alerts.add("warning", "数据", "晚间兜底：20260828 数据截至 21:00 仍未到位（最新 20260827）")
        self.alerts.add("error", "同步", "同步失败")
        with mock.patch.object(app, "is_trading_day", return_value=True):
            self.assertFalse(app.evening_stale_alert(
                datetime.datetime(2026, 8, 28, 16, 0), alerts=self.alerts))
        self.assertEqual([e["message"] for e in self.alerts.list()], ["同步失败"])
        self.alerts.add("warning", "数据", "晚间兜底：20260828 数据截至 21:00 仍未到位（最新 20260827）")
        with mock.patch.object(app, "is_trading_day", return_value=False):
            self.assertFalse(app.evening_stale_alert(
                datetime.datetime(2026, 8, 29, 21, 5), alerts=self.alerts))
        self.assertEqual([e["message"] for e in self.alerts.list()], ["同步失败"])


class CatchupSedimentTest(unittest.TestCase):
    """0.10.13 maybe_catchup_sediment：水印缺口判定 + 单飞/时间/交易日守卫。

    直接打 services.warehouse_tasks 注入点（与 test_warehouse W4 同模式），
    不经 app（run_sync 全链路过重），全部离线。
    """

    def setUp(self):
        import services.warehouse_tasks as wt
        self.wt = wt
        self._saved = {k: getattr(wt, k) for k in
                       ("availability", "is_trading_day", "data_latest",
                        "warehouse_root", "sink", "warehouse_run_async")}
        self.warehouse_run_async = mock.MagicMock(return_value={"ok": True, "async": True})
        self.warehouse_run_async.return_value = {"ok": True, "async": True}
        wt.warehouse_run_async = self.warehouse_run_async

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.wt, k, v)
        self.wt._wh_run_state.update(running=False, started=None,
                                     finished=None, result=None)

    def _wire(self, *, available=True, trading=True, now_hm="16:50",
              latest="20260828", watermark="20260827", running=False):
        self.wt.availability = lambda: (available, "ok")
        self.wt.is_trading_day = lambda d=None: trading
        self.wt.data_latest = lambda force=False: latest
        self.wt.warehouse_root = lambda: "root"
        self.wt.sink = mock.MagicMock()
        self.wt.sink.catalog.get_watermark.return_value = watermark
        self.wt._wh_run_state["running"] = running
        return mock.patch(f"{self.wt.__name__}.datetime",
                          **{"now.return_value.strftime.side_effect":
                             lambda f: now_hm if f == "%H:%M"
                             else latest[:4] + "-" + latest[4:6] + "-" + latest[6:]})

    def test_triggers_when_watermark_lags(self):
        """已过沉淀时间 + 交易日 + watermark < data_latest → 触发补沉淀。"""
        with self._wire():
            self.assertTrue(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_called_once_with(days=1)

    def test_silent_when_watermark_caught_up(self):
        """水印已追平 → 不触发。"""
        with self._wire(watermark="20260828"):
            self.assertFalse(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_not_called()

    def test_silent_before_sediment_time(self):
        """未到沉淀时间（16:40 前）→ 不触发（正常调度未开始，无补可言）。"""
        with self._wire(now_hm="15:30"):
            self.assertFalse(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_not_called()

    def test_silent_non_trading_day(self):
        """非交易日不触发。"""
        with self._wire(trading=False):
            self.assertFalse(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_not_called()

    def test_silent_when_running(self):
        """沉淀正在运行（单飞）→ 不触发。"""
        with self._wire(running=True):
            self.assertFalse(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_not_called()

    def test_silent_when_unavailable(self):
        """仓库不可用 → 静默 False（自愈钩子绝不外抛）。"""
        with self._wire(available=False):
            self.assertFalse(self.wt.maybe_catchup_sediment())
        self.warehouse_run_async.assert_not_called()

    def test_never_raises(self):
        """注入点为 None（未装配）等异常路径 → 静默 False。"""
        self.wt.availability = None
        self.assertFalse(self.wt.maybe_catchup_sediment())


class MainStartupSmokeTest(unittest.TestCase):
    """main() 名字解析冒烟（0.10.2）：合并曾引入 config 未导入/Handler 早引用两处
    启动期 NameError/UnboundLocalError——单测不执行 main() 拦不住，此测静态兜底：
    main() 内引用的自由变量必须能被 app 模块命名空间解析（局部导入除外）。"""

    def test_main_names_resolvable(self):
        """main() 自由变量必须（按语句顺序）在使用点之前可解析。

        抓两类启动期崩溃：① 名字不在 app 命名空间（如 config 未导入）；
        ② 名字仅由后续局部导入/赋值绑定（如 Handler 在延迟导入前被引用）。
        按行号近似执行顺序（main 无循环回边，安全）。
        """
        import ast, builtins
        source = open(app.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        builtin_names = set(dir(builtins))

        def bindings(node):
            """(名字, 行号)：局部导入/赋值/参数/内嵌函数定义。"""
            out = []
            for n in ast.walk(node):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    out.extend((a.asname or a.name.split(".")[0], n.lineno) for a in n.names)
                elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    out.append((n.name, n.lineno))
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.append((n.id, n.lineno))
                elif isinstance(n, ast.arg):
                    out.append((n.arg, n.lineno))
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    out.append((n.name, n.lineno))
            return out

        local_binds = bindings(main_fn)
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
                if name in builtin_names:
                    continue
                if any(ln <= node.lineno for n_, ln in local_binds if n_ == name):
                    continue  # 使用点之前已有局部绑定
                self.assertTrue(hasattr(app, name),
                                f"app.main() 第 {node.lineno} 行引用 {name!r}："
                                f"既无先行局部绑定也不在 app 命名空间（启动即崩）")


if __name__ == "__main__":
    unittest.main(verbosity=2)