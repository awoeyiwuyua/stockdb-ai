"""test_records — storage.records 日检记录存储单测（0.9.2 引入；0.9.4 日度 Rotate）。

离线：patch config.DATA_DIR 到临时目录；按天文件追加/跨天读取/保留期清理/损坏容错。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import config
from storage import records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_records_")
        p = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def _daily(self, day):
        return Path(self.tmp) / records.RECORDS_DIR / f"{day}.jsonl"

    def test_append_and_recent(self):
        records.append({"date": "20260814", "task": "close", "ok": True, "metrics": {"n_samples": 47}})
        records.append({"date": "20260813", "task": "collect", "ok": False, "reason": "x"})
        recs = records.recent(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["date"], "20260814")  # 日期最新在前
        self.assertEqual(recs[0]["metrics"]["n_samples"], 47)
        # 按天分文件
        self.assertTrue(self._daily("20260814").exists())
        self.assertTrue(self._daily("20260813").exists())

    def test_append_defaults_to_today(self):
        records.append({"task": "close", "ok": True})
        today = datetime.now().strftime("%Y%m%d")
        self.assertTrue(self._daily(today).exists())

    def test_recent_limit_cross_days(self):
        today = datetime.now().strftime("%Y%m%d")
        for i in range(3):
            records.append({"date": today, "task": "close", "ok": True, "i": i})
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        for i in range(2):
            records.append({"date": yesterday, "task": "close", "ok": True, "i": i})
        recs = records.recent(4)
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[0]["i"], 2)      # 今日最新在前
        self.assertEqual(recs[3]["i"], 1)      # 昨日最新（时间序第 4 条）

    def test_corrupt_line_skipped(self):
        today = datetime.now().strftime("%Y%m%d")
        p = self._daily(today)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({"date": today, "ok": True}) + "\n")
        recs = records.recent(10)
        self.assertEqual(len(recs), 1)

    def test_legacy_file_compat(self):
        """0.9.2 单文件（auction_daily.jsonl）仍被 recent 读取（兼容）。"""
        p = Path(self.tmp) / records.LEGACY_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"date": "20260810", "ok": True}) + "\n")
        recs = records.recent(10)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["date"], "20260810")

    def test_cleanup_removes_expired(self):
        """0.10.5 语义：按修改时间保留——文件名日期不再决定清理（历史回填记录
        文件名是旧业务日但 mtime 是现在，必须存活）；mtime 超期才清。"""
        import os
        import time as _t
        old_retention = records.RETENTION_DAYS
        records.RETENTION_DAYS = 2
        try:
            today = datetime.now().strftime("%Y%m%d")
            historical = "20000104"  # 历史回填业务日
            records.append({"date": historical, "ok": True})
            records.append({"date": today, "ok": True})
            self.assertTrue(self._daily(historical).exists())  # 刚写的历史日：存活

            expired = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
            records.append({"date": expired, "ok": True})
            p_expired = self._daily(expired)
            old_ts = _t.time() - 3 * 86400  # mtime 3 天前（超 2 天保留期）
            os.utime(p_expired, (old_ts, old_ts))
            records.append({"date": today, "ok": True})  # 触发 _cleanup
            self.assertFalse(p_expired.exists())  # mtime 超期：清理
            self.assertTrue(self._daily(today).exists())
        finally:
            records.RETENTION_DAYS = old_retention

    def test_missing_dir_empty(self):
        self.assertEqual(records.recent(10), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CleanupMtimeTest(unittest.TestCase):
    """0.10.5：_cleanup 按修改时间（非文件名日期）保留——历史回填记录不再被误删。"""

    def test_old_dated_record_survives_recent_cleanup(self):
        import time as _t
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "DATA_DIR", Path(tmp)):
                # 写一条"2000 年业务日"的记录（模拟历史回填日检）
                records.append({"date": "20000104", "task": "warehouse_sediment",
                                "ok": True, "at": "x"})
                self.assertTrue((Path(tmp) / "records" / "20000104.jsonl").exists())
                # 再写一条今日记录触发 _cleanup —— 20000104（刚写，mtime=now）必须存活
                records.append({"date": "20991231", "task": "t", "ok": True, "at": "x"})
                self.assertTrue((Path(tmp) / "records" / "20000104.jsonl").exists())
                # 真正超期（mtime 90+ 天前）的文件才被清
                old = Path(tmp) / "records" / "20200101.jsonl"
                old.parent.mkdir(exist_ok=True)
                old.write_text("{}" + chr(10), encoding="utf-8")
                old_ts = _t.time() - 100 * 86400
                import os
                os.utime(old, (old_ts, old_ts))
                records.append({"date": "20991231", "task": "t2", "ok": True, "at": "x"})
                self.assertFalse(old.exists())
