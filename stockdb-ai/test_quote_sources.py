#!/usr/bin/env python3
"""test_quote_sources — 数据层行情源采集器单测（0.7.0 起 test_auction_collect，D11 随迁）"""
import json
import unittest
from unittest import mock

from storage.providers import quote_sources as AC


def _tencent_line(code: str, name: str, open_: str, prev: str, vol: str = "100",
                  amt: str = "0", current: str = "0") -> str:
    """按真实腾讯行布局构造（0.10.35 实测校准，split("~") 后 0-based）：

    0=市场 1=名称 2=代码 3=当前价 4=昨收 5=今开 6=量 … 37=额。
    current 默认为 "0"（与今开区分）：回归用「当前价 ≠ 今开」验证取值位正确。
    """
    fields = ["51", name, code, current, prev, open_, vol]
    fields += ["0"] * 30          # 7..36 占位
    fields += [amt, "0"]          # 37=成交额, 38=占位
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return f'v_{prefix}{code}="{"~".join(fields)}";'


def _east_payload(open_: float | None, prev: float, vol: float = 100, amt: float = 0) -> dict:
    return {"data": {"f46": open_, "f60": prev, "f47": vol, "f48": amt}}


class _Resp:
    def __init__(self, text: str):
        self._t = text.encode("utf-8")

    def read(self):
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TencentParseTests(unittest.TestCase):
    def test_normal_line(self):
        p = AC._parse_tencent_line(_tencent_line("600000", "浦发银行", "10.50", "10.00"))
        self.assertEqual(p["code"], "600000")
        self.assertAlmostEqual(p["open_price"], 10.50)
        self.assertAlmostEqual(p["prev_close"], 10.00)

    def test_open_is_field5_not_current_field3(self):
        """0.10.35 回归：今开取第 5 位，而非第 3 位（当前价）——2026-09-10 实证。

        旧实现取第 3 位（当前价），仅 09:26 前后 current==open 才恰好正确；
        过了连续竞价时段再采会拿盘中/收盘价冒充开盘价，对账全红。此处构造
        「当前价 11.85 ≠ 今开 11.68」的收盘后形态，断言取到的是今开。
        """
        line = _tencent_line("000001", "平安银行", "11.68", "11.70", current="11.85")
        p = AC._parse_tencent_line(line)
        self.assertAlmostEqual(p["open_price"], 11.68)   # 今开（第 5 位）
        self.assertAlmostEqual(p["prev_close"], 11.70)
        self.assertNotEqual(p["open_price"], 11.85)      # 不得取当前价

    def test_suspended_open_empty(self):
        p = AC._parse_tencent_line(_tencent_line("600000", "X", "", "10.00"))
        self.assertIsNone(p["open_price"])
        self.assertAlmostEqual(p["prev_close"], 10.00)

    def test_garbage_line(self):
        self.assertIsNone(AC._parse_tencent_line("junk without quote"))
        self.assertIsNone(AC._parse_tencent_line('v_sh600000="broken~only~two";'))


class FetchQuotesTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(AC.time, "sleep")
        self.m_sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_batch_split_and_rate_limit(self):
        codes = [f"{600000 + i}" for i in range(120)]
        urlopen = mock.Mock(return_value=_Resp(""))
        with mock.patch.object(AC.urllib.request, "urlopen", urlopen):
            AC.fetch_quotes(codes)
        # 120/50 → 3 批，批间 2 次 sleep
        self.assertEqual(self.m_sleep.call_count, 2)

    def test_primary_success(self):
        lines = [_tencent_line("600000", "A", "10.5", "10.0"),
                 _tencent_line("000001", "B", "9.5", "10.0")]
        urlopen = mock.Mock(return_value=_Resp("\n".join(lines)))
        with mock.patch.object(AC.urllib.request, "urlopen", urlopen):
            r = AC.fetch_quotes(["600000", "000001"])
        self.assertEqual(len(r["ok"]), 2)
        self.assertEqual(r["source_usage"]["tencent"], 2)
        self.assertEqual(len(r["errors"]), 0)
        self.assertTrue(all(s["source"] == AC.PRIMARY_SOURCE for s in r["ok"]))

    def test_fallback_after_retry(self):
        # 主源两次尝试都失败 → 该批逐只走备源
        urlopen = mock.Mock(side_effect=[
            OSError("tencent down"),                          # 主源第 1 次
            OSError("tencent down"),                          # 主源重试
            _Resp(json.dumps(_east_payload(10.5, 10.0))),      # 备源成功
        ])
        with mock.patch.object(AC.urllib.request, "urlopen", urlopen):
            r = AC.fetch_quotes(["600000"])
        self.assertEqual(len(r["ok"]), 1)
        self.assertEqual(r["ok"][0]["source"], AC.FALLBACK_SOURCE)
        self.assertEqual(r["source_usage"]["eastmoney"], 1)

    def test_all_failed_goes_to_errors(self):
        urlopen = mock.Mock(side_effect=OSError("down"))
        with mock.patch.object(AC.urllib.request, "urlopen", urlopen):
            r = AC.fetch_quotes(["600000"])
        self.assertEqual(len(r["ok"]), 0)
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["errors"][0]["code"], "NO_DATA")
        self.assertEqual(r["contract"], AC.QUOTE_CONTRACT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
