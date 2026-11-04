"""Offline capture checks. All HTTP uses MockTransport; clocks advance without waiting."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

import httpx

from polymarket_bot.depth_capture import (
    Budget, CaptureSession, EventRecorder, FOOTER_RESERVE, cli, decode, encode, read_records)
from polymarket_bot.linux_mvp.collector import LiveCollector
from polymarket_bot.market_data.freshness import evaluate
from polymarket_bot.market_data.http import HttpAttempt, JsonRead
from polymarket_bot.market_data.models import MarketSnapshot
from tests.linux_mvp_support import snapshot
from tests.support import FakeClock, NOW, make_book, make_event, TOKEN_UP, TOKEN_DOWN


class Harness:
    def __init__(self, folder, budget=Budget(), handler=None, windows=None, stop=None):
        self.clock = FakeClock()
        self.sends = []
        self.starts, self.completions = [], []
        self.path = Path(folder) / 'events.jsonl.gz'

        def default(request):
            path = request.url.path
            if path == '/time':
                body = str(int(NOW.timestamp()) + int(self.clock.value)).encode()
            elif path.startswith('/events/slug/'):
                body = json.dumps(make_event()).encode()
            elif path == '/book':
                body = json.dumps(make_book(asset_id=request.url.params['token_id'])).encode()
            elif path == '/markets/1001':
                body = json.dumps(make_event()['markets'][0]).encode()
            elif path == '/v2/resolutions':
                body = b'{"data":[]}'
            else:
                return httpx.Response(404, stream=httpx.ByteStream(b'{}'))
            return httpx.Response(200, stream=httpx.ByteStream(body))

        def send(request):
            self.sends.append(self.clock.value)
            return (handler or default)(request)

        self.client = httpx.Client(trust_env=False, transport=httpx.MockTransport(send))
        self.collector = LiveCollector(self.client, monotonic=self.clock.monotonic, should_stop=stop)
        public = self.collector.clob.get
        public.sleep, public.utcnow = self.clock.sleep, self.clock.utcnow
        collect = self.collector.collect

        def measured():
            self.starts.append(self.clock.value)
            try:
                return collect()
            finally:
                self.completions.append(self.clock.value)

        self.collector.collect = measured
        self.recorder = EventRecorder(self.path, size_cap_bytes=budget.size_cap_bytes)
        self.capture = CaptureSession(
            self.collector, self.recorder, budget, monotonic=self.clock.monotonic,
            monotonic_ns=lambda: round(self.clock.value * 1e9), utcnow=self.clock.utcnow,
            sleep=self.clock.sleep, window_table=windows)

    def run(self):
        try:
            summary = self.capture.run()
            return summary, list(read_records(self.path, strict=True))
        finally:
            self.recorder.close()
            self.client.close()


class CodecTests(unittest.TestCase):
    def test_snapshot_full_round_trip_preserves_freshness_and_all_depth(self):
        original = snapshot(slot=2)
        original = replace(original, up=replace(original.up, timing=replace(
            original.up.timing, raw_body=b'\x00\xff raw book', payload={'n': Decimal('0.12300')})))
        restored = decode(json.loads(json.dumps(encode(original), allow_nan=False)))
        self.assertEqual(restored, original)
        self.assertIsInstance(restored, MarketSnapshot)
        self.assertIsInstance(restored.up.book.bids, tuple)
        self.assertIsInstance(restored.up.book.bids[0].price, Decimal)
        self.assertEqual(restored.up.timing.raw_body, b'\x00\xff raw book')
        kwargs = dict(decision_mono_ns=original.validated_mono_ns, max_age_seconds=5, sample_seconds=2)
        self.assertEqual(evaluate(original, **kwargs), evaluate(restored, **kwargs))

    def test_attempt_and_safe_scalars(self):
        attempt = HttpAttempt('CLOB_TIME', 'GET', '/time', (), 2, 1, 2, 3, 4,
                              503, 'failure', 'transport_error', b'bad\xff', False)
        for value in (attempt, Decimal('1.2300'), datetime.now(timezone.utc), b'\x00\xff',
                      (Decimal('2'),), JsonRead({'x': Decimal('.1')}, 1., 2.)):
            self.assertEqual(decode(json.loads(json.dumps(encode(value)))), value)

    def test_unknown_tags_bad_fields_collision_nonfinite_rejected(self):
        for value in ({'@d': 'os.system', '@f': {}}, {'@t': 'pickle', 'v': 'bad'},
                      {'@d': 'MarketSnapshot', '@f': {}}, {'@t': 'bytes', 'v': '$$'},
                      {'@t': 'datetime', 'v': '2026-01-01'}, {'@t': 'Decimal', 'v': 'NaN'}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                decode(value)
        for value in ({'trap': {'@t': 'Decimal', 'v': '1'}}, float('nan'),
                      Decimal('Infinity'), datetime(2026, 1, 1), {1: 'bad'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode(value)


class RecorderTests(unittest.TestCase):
    def test_append_members_typed_records_and_exclusive_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.gz'
            recorder = EventRecorder(path)
            value = snapshot(slot=1)
            recorder.write({'kind': 'session', 'session': 'fixed'})
            prefix = path.read_bytes()
            recorder.write({'kind': 'cycle', 'snapshot': encode(value)})
            self.assertTrue(path.read_bytes().startswith(prefix))
            recorder.close()
            records = list(read_records(path, strict=True))
            self.assertEqual(records[1]['snapshot'], value)
            self.assertEqual(len(gzip.decompress(path.read_bytes()).splitlines()), 2)
            with self.assertRaises(FileExistsError):
                EventRecorder(path)

    def test_size_prechecked_footer_reserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.gz'
            recorder = EventRecorder(path, size_cap_bytes=1200, footer_reserve_bytes=300)
            count = 0
            while recorder.write({'kind': 'cycle', 'cycle': count, 'pad': 'x' * 200}):
                count += 1
            before = path.read_bytes()
            self.assertFalse(recorder.write({'kind': 'cycle', 'cycle': count}))
            self.assertEqual(path.read_bytes(), before)
            self.assertLessEqual(len(before), 900)
            self.assertTrue(recorder.write({'kind': 'summary', 'reason': 'SIZE_LIMIT'}, terminal=True))
            recorder.close()
            self.assertLessEqual(path.stat().st_size, 1200)
            self.assertEqual(len(list(read_records(path, strict=True))), count + 1)

    def test_partial_tail_discarded_even_if_json_decompressed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.gz'
            first = gzip.compress(b'{"kind":"session"}\n', mtime=0)
            last = gzip.compress(b'{"kind":"cycle","cycle":2}\n', mtime=0)
            for cut in (1, 5, 8, len(last) // 2):
                path.write_bytes(first + last[:-cut])
                self.assertEqual(list(read_records(path)), [{'kind': 'session'}])
                with self.assertRaises(ValueError):
                    list(read_records(path, strict=True))


class CaptureTests(unittest.TestCase):
    def test_success_rejected_freshness_recorded_no_double_count_full_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(max_cycles=3))
            summary, records = h.run()
            self.assertEqual(summary['reason'], 'CYCLE_LIMIT')
            cycles = [r for r in records if r['kind'] == 'cycle']
            attempts = [r for r in records if r['kind'] == 'attempt' and r['actual_send']]
            self.assertEqual(len(cycles), 3)
            self.assertEqual(len(attempts), 15)
            self.assertEqual(len(h.sends), summary['attempts'])
            self.assertEqual(summary['attempts'], 15)
            self.assertEqual(records[0]['kind'], 'session')
            self.assertIsInstance(records[0]['session'], str)
            self.assertEqual(records[-1]['kind'], 'summary')
            for record in cycles:
                self.assertIsInstance(record['snapshot'], MarketSnapshot)
                self.assertIsInstance(record['decision_mono_ns'], int)
                self.assertIsInstance(record['decision_wall_us'], int)
                self.assertIn('freshness', record)
            for previous, current in zip(h.completions, h.starts[1:]):
                self.assertGreaterEqual(current - previous, 3 - 1e-9)
            for a, b in zip(h.sends, h.sends[1:]):
                self.assertGreaterEqual(b - a, .34 - 1e-9)

    def test_retry_raw_failure_preserved_then_cycle_recovers(self):
        count = 0

        def respond(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(503, stream=httpx.ByteStream(b'raw failure'))
            if count == 2:
                return httpx.Response(200, stream=httpx.ByteStream(b'not json'))
            # Continued failed cycles must not invoke strict engine's error stop.
            return httpx.Response(404, stream=httpx.ByteStream(b'missing'))

        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(max_cycles=7), handler=respond)
            summary, records = h.run()
            self.assertEqual(summary['reason'], 'CYCLE_LIMIT')
            self.assertEqual(summary['cycles'], 7)
            self.assertEqual(summary['failed_cycles'], 7)
            attempts = [r['attempt'] for r in records if r['kind'] == 'attempt']
            self.assertEqual(attempts[0].raw_body, b'raw failure')
            self.assertEqual(attempts[0].http_status, 503)
            self.assertEqual(attempts[1].attempt_number, 2)
            self.assertEqual(attempts[1].raw_body, b'not json')
            self.assertEqual(summary['attempts'], len(h.sends))
            self.assertTrue(all(r['error'] and r['snapshot'] is None
                                for r in records if r['kind'] == 'cycle'))

    def test_actual_attempt_limit_mid_cycle_no_overshoot(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(max_attempts=4))
            summary, records = h.run()
            self.assertEqual(summary['reason'], 'ATTEMPT_LIMIT')
            self.assertEqual(len(h.sends), 4)
            self.assertEqual(summary['attempts'], 4)
            cycle = next(r for r in records if r['kind'] == 'cycle')
            self.assertIsNone(cycle['snapshot'])
            self.assertTrue(cycle['error'])

    def test_retries_count_toward_actual_attempt_cap(self):
        def respond(request):
            return httpx.Response(503, stream=httpx.ByteStream(b'failed'))
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(max_attempts=2), handler=respond)
            summary, records = h.run()
            self.assertEqual(summary['reason'], 'ATTEMPT_LIMIT')
            self.assertEqual(len(h.sends), 2)
            actual = [r['attempt'] for r in records if r['kind'] == 'attempt' and r['actual_send']]
            self.assertEqual([a.attempt_number for a in actual], [1, 2])

    def test_rolling_rate_enforced_at_send_not_cycle_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(max_cycles=40))
            summary, _ = h.run()
            self.assertEqual(summary['reason'], 'CYCLE_LIMIT')
            for stamp in h.sends:
                self.assertLessEqual(sum(stamp - 60 < other <= stamp for other in h.sends), 180)
            self.assertGreater(len(h.sends), 180)

    def test_hard_monotonic_and_original_stop_safeguard(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(target_seconds=.1, hard_seconds=.5))
            summary, _ = h.run()
            self.assertEqual(summary['reason'], 'HARD_TIMEOUT')
            self.assertLessEqual(len(h.sends), 2)
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, stop=lambda: True)
            summary, _ = h.run()
            self.assertEqual(summary['reason'], 'STOP_REQUESTED')
            self.assertEqual(len(h.sends), 0)

    def test_size_stop_final_summary_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(size_cap_bytes=FOOTER_RESERVE + 2000))
            summary, records = h.run()
            self.assertEqual(summary['reason'], 'SIZE_LIMIT')
            self.assertEqual(records[-1]['kind'], 'summary')
            self.assertLessEqual(h.path.stat().st_size, FOOTER_RESERVE + 2000)

    def test_resolution_receipt_after_response_same_caps_once_per_60s(self):
        condition = '0x' + 'cd' * 32
        window = {'market_id': '1001', 'condition_id': condition, 'event_id': '2001',
                  'start_us': 0, 'end_us': 1}
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(folder, Budget(target_seconds=135), windows={condition: window})
            summary, records = h.run()
            resolutions = [r for r in records if r['kind'] == 'resolution']
            self.assertGreaterEqual(len(resolutions), 2)
            for record in resolutions:
                self.assertEqual(record['window'], window)
                self.assertIn('gamma_raw', record['evidence'])
                self.assertIn('resolution_raw', record['evidence'])
                self.assertIsInstance(record['received_wall_us'], int)
            for a, b in zip(resolutions, resolutions[1:]):
                self.assertGreaterEqual(b['received_mono_ns'] - a['received_mono_ns'], 60_000_000_000)
            attempts = [r['attempt'] for r in records if r['kind'] == 'attempt']
            resolution_attempts = [a for a in attempts if a.operation == 'RESOLUTION']
            for row, attempt in zip(resolutions, resolution_attempts):
                self.assertGreaterEqual(row['received_mono_ns'], attempt.ended_mono_ns)
            self.assertEqual(summary['attempts'], len(h.sends))


class CliTests(unittest.TestCase):
    def test_limits_invalid_no_output_or_network(self):
        for kwargs in (dict(target_seconds=3000), dict(hard_seconds=float('nan')),
                       dict(min_cycle_gap_seconds=2), dict(max_attempts=4501),
                       dict(max_sends_per_window=181), dict(max_cycles=851),
                       dict(size_cap_bytes=262_144_001)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Budget(**kwargs)
        with tempfile.TemporaryDirectory() as folder, redirect_stderr(io.StringIO()):
            self.assertEqual(cli(['--out', folder]), 2)

    def test_cli_offline_single_capture_existing_root_refused(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'new'
            def factory():
                client = httpx.Client(trust_env=False, transport=httpx.MockTransport(
                    lambda request: httpx.Response(404, stream=httpx.ByteStream(b'fail'))))
                collector = LiveCollector(client, monotonic=clock.monotonic)
                collector.clob.get.sleep = clock.sleep
                return collector
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli(['--out', str(output), '--target-seconds', '4'],
                           collector_factory=factory, monotonic=clock.monotonic,
                           monotonic_ns=lambda: round(clock.value * 1e9),
                           utcnow=clock.utcnow, sleep=clock.sleep)
                self.assertEqual(cli(['--out', str(output)]), 2)
            self.assertEqual(code, 0)
            rows = list(read_records(output / 'events.jsonl.gz', strict=True))
            self.assertEqual(rows[-1]['reason'], 'TARGET_REACHED')

    def test_cli_alarm_interrupts_stuck_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'stuck'
            def factory():
                def stuck(request):
                    time.sleep(10)
                    raise AssertionError('alarm did not interrupt')
                return LiveCollector(httpx.Client(trust_env=False, transport=httpx.MockTransport(stuck)))
            before = time.monotonic()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli(['--out', str(output), '--target-seconds', '0.02', '--hard-seconds', '0.1'],
                           collector_factory=factory)
            self.assertEqual(code, 124)
            self.assertLess(time.monotonic() - before, 2)
            rows = list(read_records(output / 'events.jsonl.gz', strict=True))
            self.assertEqual(rows[-1]['reason'], 'HARD_TIMEOUT')
            cycle = next(r for r in rows if r['kind'] == 'cycle')
            self.assertTrue(cycle['error'])


if __name__ == '__main__':
    unittest.main()
