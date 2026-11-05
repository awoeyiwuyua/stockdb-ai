"""Synthetic negative regressions against service codebase.
Direct reproduction of:
1. Consumer independent regression tests (1-6) from
   C:/Users/75393/Desktop/mingli/tmp/stockdb_r4_independent_regressions.py
2. Consumer additional regression tests (7-10) from
   C:/Users/75393/Desktop/mingli/tmp/stockdb_r5_additional_regressions.py
3. Consumer lifecycle and thread safety regression tests (11-13) from
   C:/Users/75393/Desktop/mingli/tmp/stockdb_r6_lifecycle_regressions.py
4. Expanded lifecycle, concurrency & provider isolation tests (14-22)
   per R7 review fix contract.
Adapted only for service repository layout. No assertions or expectations modified.
"""
import ast
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

# Locate paths
REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE = REPO_ROOT / "stockdb-ai"
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))
if str(SERVICE / "interfaces/mcp") not in sys.path:
    sys.path.insert(0, str(SERVICE / "interfaces/mcp"))

import config
import interfaces.mcp.board_reliability as br
import interfaces.mcp.market_v3 as v3
import interfaces.mcp.market_v3_research as r

# Path to expected-dates.json (read-only from mingli or fallback)
MINGLI_DATES = Path("C:/Users/75393/Desktop/mingli/tmp/stockdb-followup-20261104/expected-dates.json")
if not MINGLI_DATES.exists():
    MINGLI_DATES = Path("C:/Users/75393/Desktop/mingli/tmp/stockdb-r4-independent-20261104/export/synthetic-expected-dates.json")

OUTPUT_DIR = REPO_ROOT / "stockdb-ai/evidence/consumer_observations"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class ConsumerNegativeRegressions(unittest.TestCase):
    def setUp(self):
        # Reset gates and states between tests
        br.THS_GATE.reset()
        br.EM_GATE.reset()
        dict.__setitem__(r._em_client_state(), "cooldown_until", 0.0)
        dict.__setitem__(r._em_client_state(), "backoff_streak", 0)

    def _full_cache_inspection(self, archive_status=None):
        key = 'CN/board/ths/885809'
        dates = json.loads(MINGLI_DATES.read_text(encoding='utf-8'))
        self.assertEqual(len(dates), 500)
        bars = {day.replace('-', ''): {
            'open': '2504.872', 'high': '2638.434', 'low': '2467.767',
            'close': '2628.766', 'bar_state': 'final', 'retrieved_at': day + 'T15:03:27+08:00'
        } for day in dates}
        if archive_status:
            for row in bars.values():
                row.update(archive_status=archive_status, source_evidence='yearly_archive')
        s = br.load_board_cache(key)
        s['bars'] = bars
        br.save_board_cache(key, s)
        updated = [{'date8': '20261104', 'open': '2504.872', 'high': '2638.434', 'low': '2467.767',
                    'close': '2634.118', 'bar_state': 'final', 'archive_status': 'archived', 'source_evidence': 'yearly_archive'}]
        with patch.object(r, 'board_cache_gap_report', return_value={key: {'gap_count': 0, 'gap_days': []}}), \
             patch.object(br, 'compute_target_trade_date_shanghai', return_value='20261104'), \
             patch.object(br, 'get_shanghai_time', return_value=datetime(2026, 11, 4, 18, tzinfo=ZoneInfo('Asia/Shanghai'))), \
             patch.object(r, 'fetch_ths_board_daily', return_value=updated) as fetch:
            summary = r.board_backfill_cycle(force=False)
        result = {
            'case': 'legacy_final' if archive_status is None else 'archived_final',
            'upstream_calls': fetch.call_count,
            'current_close': br.load_board_cache(key)['bars']['20261104']['close'],
            'summary': summary
        }
        (OUTPUT_DIR / (result['case'] + '-observation.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertGreater(fetch.call_count, 0, 'A due overlap inspection must inspect complete final caches')
        self.assertEqual(result['current_close'], '2634.118')

    def test_consumer_01_legacy_final_cache_is_rechecked(self):
        self._full_cache_inspection()

    def test_consumer_02_archived_final_cache_is_rechecked(self):
        self._full_cache_inspection('archived')

    def test_consumer_03_http_shutdown_stops_health(self):
        import interfaces.mcp.stockdb_mcp_server as server
        fake = MagicMock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(server.http.server, 'ThreadingHTTPServer', return_value=fake), \
             patch.object(v3.HEALTH, 'start') as start, patch.object(v3.HEALTH, 'stop') as stop:
            server.run_http('127.0.0.1', 0)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(stop.call_count, 1, 'HTTP lifecycle must stop its explicitly started health thread')

    def test_consumer_04_force_cannot_bypass_source_cooldown(self):
        gate = br.ProviderGate('synthetic', min_interval_ms=1, base_cooldown_s=60, max_cooldown_s=60)
        gate.record_failure('synthetic WAF', enter_cooldown=True)
        self.assertTrue(gate.get_status()['in_cooldown'])
        with self.assertRaises(br.BoardReliabilityError):
            gate.check_and_throttle(force=True)

    def test_consumer_05_reconciliation_exposes_invalid_rows(self):
        key = 'CN/board/ths/885809'
        rows = [{'date8': '20261104', 'open': '100', 'high': '90', 'low': '110', 'close': '101', 'bar_state': 'final'}]
        with patch.object(br, 'compute_target_trade_date_shanghai', return_value='20261104'):
            try:
                result = br.reconcile_series_cache(key, rows, provider='ths')
            except (br.BoardReliabilityError, ValueError):
                return
        (OUTPUT_DIR / 'invalid-row-observation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertTrue(
            result.get('errors') or result.get('rejected_count', 0) > 0 or result.get('status') in ('failed', 'partial_failure'),
            'Invalid rows must raise or expose rejection; a zero-change result is insufficient'
        )

    def test_consumer_06_worker_force_does_not_fetch_during_cooldown(self):
        key = 'CN/board/ths/885809'
        s = br.load_board_cache(key)
        s['bars']['20261104'] = {
            'open': '100', 'high': '105', 'low': '95', 'close': '101',
            'bar_state': 'final', 'archive_status': 'pending_yearly_archive'
        }
        br.save_board_cache(key, s)
        br.THS_GATE.record_failure('synthetic WAF', enter_cooldown=True)
        with patch.object(r, 'board_cache_gap_report', return_value={key: {'gap_count': 0, 'gap_days': []}}), \
             patch.object(br, 'compute_target_trade_date_shanghai', return_value='20261104'), \
             patch.object(r, 'fetch_ths_board_daily', return_value=[]) as fetch:
            result = r.board_backfill_cycle(force=True)
        (OUTPUT_DIR / 'forced-worker-observation.json').write_text(json.dumps({
            'cooldown': br.THS_GATE.get_status()['in_cooldown'],
            'raw_fetch_calls': fetch.call_count, 'summary': result
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(fetch.call_count, 0, 'Maintenance force must not bypass provider WAF cooldown')

    def test_consumer_07_http_bind_failure_stops_started_health(self):
        import interfaces.mcp.stockdb_mcp_server as server
        with patch.object(v3.HEALTH, 'start') as start, patch.object(v3.HEALTH, 'stop') as stop, patch.object(server.http.server, 'ThreadingHTTPServer', side_effect=OSError('synthetic bind failure')):
            with self.assertRaises(OSError):
                server.run_http('127.0.0.1', 0)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(stop.call_count, 1, 'Startup failure must clean up health already started')

    def test_consumer_08_ths_cooldown_blocks_today_fallback(self):
        br.THS_GATE.record_failure('synthetic WAF', enter_cooldown=True)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'callback({});'
        with patch.object(r, 'datetime') as clock, patch.object(r.urllib.request, 'urlopen', return_value=response) as network:
            clock.now.return_value = datetime(2026, 11, 5, 18)
            try:
                r.fetch_ths_board_daily('885809', '20261105', '20261105')
            except r.ResearchError:
                pass
        (OUTPUT_DIR / 'ths-cooldown-today.json').write_text(json.dumps({'network_calls': network.call_count, 'urls': [c.args[0].full_url for c in network.call_args_list]}, indent=2), encoding='utf-8')
        self.assertEqual(network.call_count, 0, 'today.js must use the same cooldown gate as annual files')

    def test_consumer_09_eastmoney_raw_fetch_preserves_gate_cooldown(self):
        br.EM_GATE.record_failure('synthetic WAF', enter_cooldown=True)
        with patch.object(r, '_em_client_state', return_value={'cooldown_until': 0.0, 'backoff_streak': 0}), patch.object(r, '_http_json', return_value={'data': {'klines': []}}) as network, patch.object(r, '_em_persist_state'), patch.object(br, 'store_raw_provenance'):
            try:
                r.eastmoney_board_daily('90.BK1408', '20261104', '20261104')
            except r.ResearchError:
                pass
        (OUTPUT_DIR / 'eastmoney-gate-reset.json').write_text(json.dumps({'network_calls': network.call_count, 'gate_status': br.EM_GATE.get_status()}, indent=2), encoding='utf-8')
        self.assertEqual(network.call_count, 0, 'A second state store must not clear an active provider cooldown')

    def test_consumer_10_worker_surfaces_rejected_overlap(self):
        key = 'CN/board/ths/885809'
        cache = br.load_board_cache(key)
        cache['bars']['20261104'] = {'open': '100', 'high': '105', 'low': '95', 'close': '101', 'bar_state': 'final'}
        br.save_board_cache(key, cache)
        invalid = [{'date8': '20261104', 'open': '100', 'high': '90', 'low': '110', 'close': '101', 'bar_state': 'final'}]
        with patch.object(r, 'board_cache_gap_report', return_value={key: {'gap_count': 0, 'gap_days': []}}), patch.object(br, 'compute_target_trade_date_shanghai', return_value='20261104'), patch.object(r, 'fetch_ths_board_daily', return_value=invalid):
            worker = br.BoardBackfillWorker()
            summary = worker.run_once()
        state = worker.get_state()
        (OUTPUT_DIR / 'worker-rejected-overlap.json').write_text(json.dumps({'summary': summary, 'worker': state}, indent=2), encoding='utf-8')
        overlap = summary['overlap_inspected'][key]
        self.assertTrue(overlap.get('errors') or overlap.get('rejected_count') or overlap.get('status') in ('failed', 'partial_failure'), 'Worker must propagate structured rejection instead of recording successful empty reconciliation')
        self.assertIsNone(state['last_success_at'])

    def test_consumer_11_web_bind_failure_cleans_health_and_worker(self):
        tree = ast.parse((SERVICE / 'app.py').read_text(encoding='utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        isolated = ast.Module(body=[main], type_ignores=[])
        namespace = {
            'sys': sys, 'signal': types.SimpleNamespace(), 'threading': types.SimpleNamespace(Thread=MagicMock()),
            '_wire_auction_tasks': MagicMock(), '_wire_warehouse_tasks': MagicMock(), '_wire_hk_tasks': MagicMock(),
            'LISTEN_PORT': 0, 'STOCKDB_HOST': '127.0.0.1', 'STOCKDB_PORT': 1, 'DATA_DIR': 'tmp',
            'scheduler_loop': MagicMock(), 'ops_watchdog_loop': MagicMock(), 'auction_scheduler_loop': MagicMock(),
            'WAREHOUSE_ENABLED': False, 'HK_SYNC_ENABLED': False,
            '_BoundedHTTPServer': MagicMock(side_effect=OSError('synthetic Web bind failure')),
        }
        exec(compile(isolated, str(SERVICE / 'app.py'), 'exec'), namespace)
        with patch.dict(sys.modules, {'interfaces.web.handlers': types.SimpleNamespace(Handler=object())}), \
             patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
             patch.object(v3.HEALTH, 'start') as hs, patch.object(v3.HEALTH, 'stop') as he, \
             patch.object(br.WORKER, 'start') as ws, patch.object(br.WORKER, 'stop') as we:
            with self.assertRaises(OSError):
                namespace['main']()
        counts = {'health_start': hs.call_count, 'health_stop': he.call_count, 'worker_start': ws.call_count, 'worker_stop': we.call_count}
        (OUTPUT_DIR / 'web-startup-failure.json').write_text(json.dumps(counts, indent=2), encoding='utf-8')
        self.assertEqual(counts, {'health_start': 1, 'health_stop': 1, 'worker_start': 1, 'worker_stop': 1})

    def test_consumer_12_worker_state_reports_thread_alive_after_stop_timeout(self):
        worker = br.BoardBackfillWorker()
        entered = threading.Event()
        release = threading.Event()

        def blocked_cycle(**kwargs):
            entered.set()
            release.wait(3)
            return {'boards': {}, 'overlap_inspected': {}}

        try:
            with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
                 patch.object(config, 'BOARD_BACKFILL_INITIAL_DELAY_SECONDS', 0), \
                 patch.object(worker, 'run_once', side_effect=blocked_cycle):
                worker.start()
                self.assertTrue(entered.wait(1), 'test fixture did not reach blocked task')
                worker.stop(timeout=0.01)
                observation = {'is_running': worker.is_running(), 'state': worker.get_state()}
                (OUTPUT_DIR / 'worker-stop-timeout.json').write_text(json.dumps(observation, indent=2), encoding='utf-8')
                self.assertTrue(observation['is_running'])
                visible = observation['state']
                self.assertTrue(
                    visible.get('running') or visible.get('thread_alive') or visible.get('lifecycle_state') == 'stopping',
                    'Published worker state must expose a live or stopping thread after a timed-out stop'
                )
        finally:
            release.set()
            worker.stop(timeout=1)

    def test_consumer_13_inflight_success_preserves_newer_em_cooldown(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})

        def response_after_parallel_failure(*args, **kwargs):
            br.EM_GATE.record_failure('synthetic newer WAF from concurrent path', enter_cooldown=True)
            return {'data': {'klines': []}}

        with patch.object(r, '_em_client_state', return_value=state), \
             patch.object(r, '_http_json', side_effect=response_after_parallel_failure), \
             patch.object(r, '_em_persist_state'), \
             patch.object(br, 'store_raw_provenance'):
            r.eastmoney_board_daily('90.BK1408', '20261104', '20261104')
        status = br.EM_GATE.get_status()
        (OUTPUT_DIR / 'em-inflight-success-cooldown.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
        self.assertTrue(status['in_cooldown'], 'An earlier in-flight success cannot clear a newer failure cooldown')

    def test_consumer_14_health_probe_stop_timeout_and_release(self):
        health = v3._EngineHealth()
        entered = threading.Event()
        release = threading.Event()

        def blocked_loop():
            entered.set()
            release.wait(3)

        try:
            with patch.object(health, '_loop', side_effect=blocked_loop):
                health.start()
                self.assertTrue(entered.wait(1), 'Health probe thread did not enter loop')
                health.stop(timeout=0.01)
                st = health.peek()
                self.assertTrue(health.is_running())
                self.assertTrue(st.get('thread_alive') or st.get('stopping') or st.get('lifecycle_state') == 'stopping')
        finally:
            release.set()
            health.stop(timeout=1)
            self.assertFalse(health.is_running())

    def test_consumer_15_worker_and_health_idempotent_start(self):
        worker = br.BoardBackfillWorker()
        health = v3._EngineHealth()
        stop_flag = threading.Event()

        def dummy_loop():
            stop_flag.wait(2)

        try:
            with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
                 patch.object(worker, '_run_loop', side_effect=dummy_loop), \
                 patch.object(health, '_loop', side_effect=dummy_loop):
                worker.start()
                th1 = worker._thread
                worker.start()
                th2 = worker._thread
                self.assertIs(th1, th2, 'Repeated worker start must be idempotent and not create duplicate threads')

                health.start()
                hth1 = health._thread
                health.start()
                hth2 = health._thread
                self.assertIs(hth1, hth2, 'Repeated health start must be idempotent and not create duplicate threads')
        finally:
            stop_flag.set()
            worker.stop(timeout=1)
            health.stop(timeout=1)

    def test_consumer_16_worker_stopping_handles_concurrent_start_safely(self):
        worker = br.BoardBackfillWorker()
        entered = threading.Event()
        release = threading.Event()

        def slow_cycle(**kwargs):
            entered.set()
            release.wait(2)
            return {'boards': {}, 'overlap_inspected': {}}

        try:
            with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
                 patch.object(config, 'BOARD_BACKFILL_INITIAL_DELAY_SECONDS', 0), \
                 patch.object(worker, 'run_once', side_effect=slow_cycle):
                worker.start()
                self.assertTrue(entered.wait(1))
                worker.stop(timeout=0.01)
                self.assertTrue(worker.is_running())
                # Starting while thread is still terminating must either raise or wait cleanly
                with self.assertRaises(RuntimeError):
                    worker.start()
        finally:
            release.set()
            worker.stop(timeout=1)

    def test_consumer_17_em_two_interleaved_requests_preserve_cooldown(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        order = []

        def req1_fetch(*args, **kwargs):
            order.append('req1_fetch')
            # Simulate Request 2 failing with WAF concurrently while Request 1 is in-flight
            br.EM_GATE.record_failure('Req2 WAF drop', enter_cooldown=True)
            return {'data': {'klines': ['2026-11-04,100,105,110,95,1000,10000']}}

        with patch.object(r, '_em_client_state', return_value=state), \
             patch.object(r, '_http_json', side_effect=req1_fetch), \
             patch.object(r, '_em_persist_state'), \
             patch.object(br, 'store_raw_provenance'):
            rows = r.eastmoney_board_daily('90.BK1408', '20261104', '20261104')
            self.assertEqual(len(rows), 1)

        # Gate must still be in cooldown from Req2!
        status = br.EM_GATE.get_status()
        self.assertTrue(status['in_cooldown'], 'Interleaved success must not clear concurrent WAF failure')
        self.assertGreater(status['cooldown_remaining_s'], 0)

    def test_consumer_18_em_zero_writeback_does_not_clear_active_gate_cooldown(self):
        # When an in-flight request finishes, if gate was placed in cooldown concurrently,
        # zero-state writeback must be withheld and gate cooldown preserved.
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        until = 0.0

        def http_with_concurrent_waf(*args, **kwargs):
            nonlocal until
            until = br.EM_GATE.record_failure('simulated concurrent WAF', enter_cooldown=True)
            return {'data': {'klines': ['2026-11-04,100,105,110,95,1000,10000']}}

        with patch.object(r, '_em_client_state', return_value=state), \
             patch.object(r, '_http_json', side_effect=http_with_concurrent_waf), \
             patch.object(r, '_em_persist_state'), \
             patch.object(br, 'store_raw_provenance'):
            rows = r.eastmoney_board_daily('90.BK1408', '20261104', '20261104')
            self.assertEqual(len(rows), 1)

        # Zero state writeback must not have erased gate's active cooldown
        status = br.EM_GATE.get_status()
        self.assertTrue(status['in_cooldown'], 'Zero state writeback must not erase gate active cooldown')
        self.assertEqual(status['cooldown_remaining_s'], int(until - time.time()))

    def test_consumer_19_em_persistence_state_recovery_respects_gate(self):
        br.EM_GATE.record_failure('simulated active WAF', enter_cooldown=True)
        gate_status = br.EM_GATE.get_status()
        self.assertTrue(gate_status['in_cooldown'])

        # Simulating loading empty state dictionary from disk
        recovered = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        # Reading cooldown_until from recovered dict should reflect active gate cooldown
        self.assertGreater(recovered['cooldown_until'], time.time())
        self.assertEqual(recovered['backoff_streak'], gate_status['backoff_streak'])

    def test_consumer_20_provider_isolation_em_failure_does_not_affect_ths(self):
        br.EM_GATE.record_failure('EM WAF drop', enter_cooldown=True)
        self.assertTrue(br.EM_GATE.get_status()['in_cooldown'])
        self.assertFalse(br.THS_GATE.get_status()['in_cooldown'], 'THS gate must remain unaffected by EM WAF')

        # THS requests pass gate check
        br.THS_GATE.check_and_throttle()

    def test_consumer_21_mcp_http_and_stdio_lifecycle_cleanups(self):
        import interfaces.mcp.stockdb_mcp_server as server
        # HTTP bind failure stops health and worker
        with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
             patch.object(v3.HEALTH, 'start') as hs, patch.object(v3.HEALTH, 'stop') as he, \
             patch.object(br.WORKER, 'start') as ws, patch.object(br.WORKER, 'stop') as we, \
             patch.object(server.http.server, 'ThreadingHTTPServer', side_effect=OSError('bind error')):
            with self.assertRaises(OSError):
                server.run_http('127.0.0.1', 0)
            self.assertEqual(hs.call_count, 1)
            self.assertEqual(he.call_count, 1)
            self.assertEqual(ws.call_count, 1)
            self.assertEqual(we.call_count, 1)

        # Stdio normal exit stops health and worker
        with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
             patch.object(v3.HEALTH, 'start') as hs, patch.object(v3.HEALTH, 'stop') as he, \
             patch.object(br.WORKER, 'start') as ws, patch.object(br.WORKER, 'stop') as we, \
             patch.object(sys, 'stdin', ['{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n']):
            with patch.object(server, 'dispatch', return_value=None):
                server.run_stdio()
            self.assertEqual(hs.call_count, 1)
            self.assertEqual(he.call_count, 1)
            self.assertEqual(ws.call_count, 1)
            self.assertEqual(we.call_count, 1)

    def test_consumer_22_web_server_close_failure_still_stops_threads(self):
        tree = ast.parse((SERVICE / 'app.py').read_text(encoding='utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        isolated = ast.Module(body=[main], type_ignores=[])
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt
        mock_server.server_close.side_effect = OSError('server_close failed')
        namespace = {
            'sys': sys, 'signal': types.SimpleNamespace(), 'threading': types.SimpleNamespace(Thread=MagicMock()),
            '_wire_auction_tasks': MagicMock(), '_wire_warehouse_tasks': MagicMock(), '_wire_hk_tasks': MagicMock(),
            'LISTEN_PORT': 0, 'STOCKDB_HOST': '127.0.0.1', 'STOCKDB_PORT': 1, 'DATA_DIR': 'tmp',
            'scheduler_loop': MagicMock(), 'ops_watchdog_loop': MagicMock(), 'auction_scheduler_loop': MagicMock(),
            'WAREHOUSE_ENABLED': False, 'HK_SYNC_ENABLED': False,
            '_BoundedHTTPServer': MagicMock(return_value=mock_server),
        }
        exec(compile(isolated, str(SERVICE / 'app.py'), 'exec'), namespace)
        with patch.dict(sys.modules, {'interfaces.web.handlers': types.SimpleNamespace(Handler=object())}), \
             patch.object(config, 'BOARD_BACKFILL_ENABLED', True), \
             patch.object(v3.HEALTH, 'start') as hs, patch.object(v3.HEALTH, 'stop') as he, \
             patch.object(br.WORKER, 'start') as ws, patch.object(br.WORKER, 'stop') as we:
            try:
                namespace['main']()
            except KeyboardInterrupt:
                pass
        self.assertEqual(hs.call_count, 1)
        self.assertEqual(he.call_count, 1, 'server_close failure must not prevent stopping HEALTH')
        self.assertEqual(ws.call_count, 1)
        self.assertEqual(we.call_count, 1, 'server_close failure must not prevent stopping WORKER')


    def test_consumer_23_generation_check_and_success_commit_are_atomic(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        original = br.EM_GATE.get_generation

        def failure_after_generation_read():
            value = original()
            br.EM_GATE.record_failure('synthetic WAF after generation read', enter_cooldown=True)
            return value

        with patch.object(r, '_em_client_state', return_value=state),              patch.object(r, '_http_json', return_value={'data': {'klines': []}}),              patch.object(r, '_em_persist_state'),              patch.object(br, 'store_raw_provenance'),              patch.object(br.EM_GATE, 'get_generation', side_effect=failure_after_generation_read):
            r.eastmoney_board_daily('90.BK1408', '20261104', '20261104')
        status = br.EM_GATE.get_status()
        (OUTPUT_DIR / 'generation-check-commit-race.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
        self.assertTrue(status['in_cooldown'], 'Generation comparison and state mutation must be one atomic decision')

    def test_consumer_24_simultaneous_start_creates_one_thread(self):
        class ScheduledLock:
            def __init__(self):
                self.inner = threading.Lock()
                self.barrier = threading.Barrier(2, timeout=2)
                self.seen = set()

            def __enter__(self):
                self.inner.acquire()
                return self

            def __exit__(self, *args):
                name = threading.current_thread().name
                wait = name.startswith('consumer-starter-') and name not in self.seen
                if wait:
                    self.seen.add(name)
                self.inner.release()
                if wait:
                    self.barrier.wait()

        for kind in ('worker', 'health'):
            with self.subTest(kind=kind):
                obj = br.BoardBackfillWorker() if kind == 'worker' else v3._EngineHealth()
                obj._lock = ScheduledLock()
                release = threading.Event()
                two_started = threading.Event()
                active = []
                errors = []
                guard = threading.Lock()

                def loop():
                    with guard:
                        active.append(threading.current_thread())
                        if len(active) >= 2:
                            two_started.set()
                    release.wait(3)

                def start():
                    try:
                        obj.start()
                    except Exception as exc:
                        errors.append(str(exc))

                loop_name = '_run_loop' if kind == 'worker' else '_loop'
                starters = [threading.Thread(target=start, name=f'consumer-starter-{i}') for i in range(2)]
                try:
                    with patch.object(config, 'BOARD_BACKFILL_ENABLED', True), patch.object(obj, loop_name, side_effect=loop):
                        for th in starters:
                            th.start()
                        for th in starters:
                            th.join(2.5)
                        two_started.wait(0.1)
                        with guard:
                            count = len(active)
                        (OUTPUT_DIR / f'{kind}-simultaneous-start.json').write_text(
                            json.dumps({'created_threads': count, 'errors': errors, 'starters_alive': [th.is_alive() for th in starters]}, indent=2),
                            encoding='utf-8'
                        )
                        self.assertFalse(any(th.is_alive() for th in starters))
                        self.assertEqual(errors, [])
                        self.assertEqual(count, 1, 'Two simultaneous idempotent start calls must not create duplicate background threads')
                finally:
                    release.set()
                    for th in active:
                        th.join(1)
                    for th in starters:
                        th.join(1)
                    obj.stop(timeout=1)


    def test_consumer_25_older_failure_writeback_preserves_newer_longer_cooldown(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        original = br.EM_GATE.record_failure
        seen = {}

        def interleaved_failure(*args, **kwargs):
            old = original(*args, **kwargs)
            new = original('synthetic later concurrent WAF', enter_cooldown=True)
            seen.update(older_until=old, newer_until=new)
            return old

        with patch.object(r, '_em_client_state', return_value=state), \
             patch.object(br.EM_GATE, 'record_failure', side_effect=interleaved_failure), \
             patch.object(r, '_em_persist_state'):
            result = r._em_enter_cooldown('synthetic earlier WAF', 'waf_drop')

        with br.EM_GATE._lock:
            seen['final_until'] = br.EM_GATE._state.cooldown_until
        seen.update(returned_until=result, gate_status=br.EM_GATE.get_status(), client_until=state['cooldown_until'])
        (OUTPUT_DIR / 'failure-writeback-cooldown.json').write_text(json.dumps(seen, indent=2), encoding='utf-8')
        self.assertGreater(seen['newer_until'], seen['older_until'])
        self.assertGreaterEqual(seen['final_until'], seen['newer_until'],
                                'An older failure writeback must not reduce a newer longer provider cooldown')
        self.assertGreaterEqual(seen['returned_until'], seen['newer_until'],
                                'Returned cooldown until must reflect authoritative gate state')
        self.assertGreaterEqual(seen['client_until'], seen['newer_until'],
                                'Client dictionary cooldown until must reflect authoritative gate state')

    def test_consumer_26_success_and_failure_interleaved_preserves_active_cooldown(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        req_gen = br.EM_GATE.check_and_throttle()

        # Step 1: Interleaved failure 1 sets initial cooldown
        until_1 = br.EM_GATE.record_failure('interleaved WAF 1', enter_cooldown=True)
        self.assertTrue(br.EM_GATE.get_status()['in_cooldown'])

        # Step 2: Previous in-flight request finishes, attempts commit_success
        success_committed = br.EM_GATE.commit_success(req_gen, client_state=state)
        self.assertFalse(success_committed, 'Stale success must not commit')
        self.assertTrue(br.EM_GATE.get_status()['in_cooldown'], 'Active cooldown must survive stale success commit')

        # Step 3: Interleaved failure 2 sets longer cooldown
        until_2 = br.EM_GATE.record_failure('interleaved WAF 2', enter_cooldown=True)
        self.assertGreater(until_2, until_1)

        # Step 4: Stale failure 1 tries to write back until_1 to state
        state['cooldown_until'] = until_1
        with br.EM_GATE._lock:
            final_until = br.EM_GATE._state.cooldown_until
        self.assertGreaterEqual(final_until, until_2, 'Stale failure writeback must not shorten longer cooldown')
        self.assertGreaterEqual(state['cooldown_until'], until_2, 'Client state must retain longer cooldown')
        (OUTPUT_DIR / 'success-failure-interleaved.json').write_text(
            json.dumps({'until_1': until_1, 'until_2': until_2, 'final_until': final_until, 'client_until': state['cooldown_until']}, indent=2),
            encoding='utf-8'
        )


    def test_consumer_27_zero_cooldown_write_cannot_clear_active_gate(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        until = br.EM_GATE.record_failure('synthetic active WAF', enter_cooldown=True)
        state['cooldown_until'] = 0.0
        status = br.EM_GATE.get_status()
        (OUTPUT_DIR / 'ordinary-zero-cooldown.json').write_text(
            json.dumps({'protected_until': until, 'gate_status': status, 'client_until': state['cooldown_until']}, indent=2),
            encoding='utf-8'
        )
        self.assertTrue(status['in_cooldown'], 'Only an authorized generation-validated gate commit may clear active protection')

    def test_consumer_28_zero_streak_write_cannot_reduce_active_backoff(self):
        state = r._EmClientStateDict({'cooldown_until': 0.0, 'backoff_streak': 0})
        br.EM_GATE.record_failure('synthetic WAF 1', enter_cooldown=True)
        br.EM_GATE.record_failure('synthetic WAF 2', enter_cooldown=True)
        state['backoff_streak'] = 0
        status = br.EM_GATE.get_status()
        (OUTPUT_DIR / 'ordinary-zero-streak.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
        self.assertEqual(status['backoff_streak'], 2, 'Ordinary persistence setters must not lower an active failure backoff')


if __name__ == '__main__':
    names = [n for n in dir(ConsumerNegativeRegressions) if n.startswith('test_consumer_')]
    suite = unittest.TestSuite(ConsumerNegativeRegressions(n) for n in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
