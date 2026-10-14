import importlib.util
from pathlib import Path
from typing import cast
import unittest


class CP5SafetyTests(unittest.TestCase):
    def test_research_worker_handoff_never_writes_version_one_event(self):
        from types import SimpleNamespace
        from polymarket_bot.recorder.service import Recorder, ApproximateReferenceWorker
        from polymarket_bot.recorder.free_reference import (
            ResearchFetchResult, ApproximateReferenceReport, ResearchAssessment,
            AcquisitionContext, ResearchWindowEvidence)
        worker = ApproximateReferenceWorker()
        result = ResearchFetchResult((), cast(ApproximateReferenceReport, None), (),
            cast(ResearchAssessment, None), cast(AcquisitionContext, None), 123,
            cast(ResearchWindowEvidence, None), ())
        self.assertTrue(worker.submit(lambda: result))
        worker.stop()
        self.assertIs(worker.take(), result)
        recorder = SimpleNamespace(approximate_worker=worker, run_id='run',
            store=SimpleNamespace(save_approximate_reference=lambda *args: self.fail('v1 write')))
        with self.assertRaisesRegex(ValueError, 'durable'):
            Recorder._approximate_drain(cast(Recorder, recorder), result=result)


    def test_runner_audit_captures_durable_identity_before_send(self):
        import httpx
        module = self.harness()
        events = []
        transport = module.AuditTransport(httpx.MockTransport(lambda request: httpx.Response(200, content=b'1')), events.append)
        transport.capture_identity = lambda request: dict(run_id='run', market_id='market', slot_id=7, leg='PAIRED')
        with httpx.Client(transport=transport) as client:
            client.post('https://clob.polymarket.com/books', json=[{'token_id': '1'}, {'token_id': '2'}])
        self.assertEqual(events[0].get('run_id'), 'run')
        self.assertEqual(events[0].get('slot_id'), 7)

    def test_runner_bounds_audit_history(self):
        import httpx
        from collections import deque
        module = self.harness()
        events = deque(maxlen=4)
        transport = module.AuditTransport(httpx.MockTransport(lambda request: httpx.Response(200, content=b'1')), events.append)
        with httpx.Client(transport=transport) as client:
            for _ in range(8):
                client.get('https://clob.polymarket.com/time')
        self.assertEqual(len(events), 4)
        self.assertEqual([row['attempt'] for row in events], [5, 6, 7, 8])

    def test_submission_keeps_anchor_when_clock_changes(self):
        from types import SimpleNamespace
        from polymarket_bot.recorder.service import Recorder
        operations = []
        session = SimpleNamespace(fetch=lambda **kwargs: ('report', ('coinbase', 'kraken')))
        recorder = SimpleNamespace(
            approximate_worker=SimpleNamespace(submit=lambda operation: operations.append(operation) or True),
            approximate_reference=session, monotonic_ns=lambda: 10,
            clock=SimpleNamespace(usable=lambda: True, anchor=SimpleNamespace(server_us=100, midpoint_ns=0)),
            anchor_id=7, run_id='run', run_deadline_ns=1000)
        self.assertTrue(Recorder._approximate_submit(cast(Recorder, recorder), 1000))
        recorder.anchor_id = 8
        recorder.clock.anchor = SimpleNamespace(server_us=200, midpoint_ns=0)
        self.assertEqual(operations[0](), ('report', ('coinbase', 'kraken'), 7))

    def test_drain_records_ordered_membership(self):
        from types import SimpleNamespace
        from polymarket_bot.recorder.service import Recorder
        events = []
        ids = iter((11, 12))
        store = SimpleNamespace(
            save_approximate_observation=lambda run, observation: next(ids),
            save_approximate_reference=lambda run, report: 13,
            record_event=lambda *args, **kwargs: events.append((args, kwargs)))
        recorder = SimpleNamespace(approximate_worker=True, run_id='run', store=store)
        report = SimpleNamespace(receipt_wall_us=100)
        Recorder._approximate_drain(cast(Recorder, recorder), result=(report, ('coinbase', 'kraken'), 7))
        self.assertEqual(events[0][1]['detail'], {
            'version': 1, 'diagnostic_only': True, 'type': 'APPROXIMATE_FETCH',
            'anchor_id': 7, 'observation_ids': [11, 12], 'reference_id': 13})

    def test_replay_keeps_rejected_future_timestamp_without_contribution(self):
        import json
        observations = [dict(observation_id=i, run_id='run', provider=name,
            pair='BTC/USD', quote_currency='USD', price_text=None if i == 1 else '100',
            bid_price_text=None, ask_price_text=None, source_timestamp_us=101_000_000 if i == 1 else None,
            receipt_wall_us=100_000_000, receipt_server_us=100_000_000,
            receipt_mono_ns=100_000_000_000, raw_payload_hash='hash', raw_payload_size=1,
            transport_type='rest', parser_revision='v1', http_status=200,
            result='error' if i == 1 else 'success', error_code='FUTURE_SOURCE_TIME' if i == 1 else None)
            for i, name in ((1, 'coinbase'), (2, 'kraken'))]
        references = [dict(reference_id=1, run_id='run', reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            window_start_us=40_000_000, window_end_us=100_000_000, observation_count=1,
            source_provider='kraken', was_fallback=1, receipt_wall_us=100_000_000,
            value_text='100', mode='APPROXIMATE', verification_status='UNVERIFIED',
            receipt_server_us=100_000_000, receipt_mono_ns=100_000_000_000,
            window_time_domain='LOCAL_WALL', diagnostics_json=json.dumps([
                'OBSERVATION_GAP', 'FALLBACK_COINBASE_FUTURE_SOURCE_TIME']))]
        events = [dict(run_id='run', detail_json=json.dumps(dict(version=1,
            diagnostic_only=True, type='APPROXIMATE_FETCH', anchor_id=7,
            observation_ids=[1, 2], reference_id=1)))]
        anchors = [dict(run_id='run', anchor_id=7, anchor_server_us=0, anchor_mono_ns=0)]
        self.assertEqual(self.analyzer().replay_rows(observations, references, events, anchors),
            dict(matched=1, observations=2, exact=True))

    def test_replay_requires_complete_membership(self):
        module = self.analyzer()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            module.replay_rows([], [], [], [])

    def test_replay_preserves_order_errors_fallback_and_wall_reversal(self):
        from dataclasses import asdict
        from decimal import Decimal
        from polymarket_bot.recorder.free_reference import ApproximateObservation, ApproximateReferenceSession
        module = self.analyzer()
        observations, references, events = [], [], []
        wall = [100_000_000]
        mono = [100_000_000_000]
        class Provider:
            def __init__(self, name):
                self.name = name
            def fetch_ticker(self, *, deadline):
                failed = self.name == 'coinbase' and len(events) < 2
                return ApproximateObservation(self.name, 'BTC/USD', 'USD',
                    None if failed else Decimal('100'), None, None, None,
                    wall[0], None, 'hash', 4, 'rest', 'v1', 200,
                    'error' if failed else 'success', 'TEST_ERROR' if failed else None, mono[0])
        session = ApproximateReferenceSession(providers={n: Provider(n) for n in ('coinbase', 'kraken')},
            monotonic_ns=lambda: mono[0], wall_us=lambda: wall[0], sleep=lambda _: None)
        for index in range(70):
            wall[0] += 0 if index == 5 else -1000 if index == 6 else 1000
            mono[0] += 1000
            report, batch = session.fetch(run_id='run', anchor_id=7, project=lambda ns: ns // 1000, deadline=1000)
            ids = []
            for obs in batch:
                row = asdict(obs)
                for key in ('price', 'bid_price', 'ask_price'):
                    value = row.pop(key)
                    row[key + '_text'] = str(value) if value is not None else None
                row.update(run_id='run', observation_id=len(observations) + 1)
                ids.append(row['observation_id'])
                observations.append(row)
            row = asdict(report)
            row['value_text'] = str(row.pop('value')) if report.value is not None else None
            row['diagnostics_json'] = __import__('json').dumps(row.pop('diagnostics'))
            row.update(run_id='run', reference_id=index + 1)
            references.append(row)
            events.append({'run_id': 'run', 'detail_json': __import__('json').dumps(dict(
                version=1, diagnostic_only=True, type='APPROXIMATE_FETCH', anchor_id=7,
                observation_ids=ids, reference_id=index + 1))})
        anchors = [{'run_id': 'run', 'anchor_id': 7, 'anchor_server_us': 0, 'anchor_mono_ns': 0}]
        self.assertEqual(module.replay_rows(observations, references, events, anchors)['matched'], 70)
        original = observations[2]['receipt_mono_ns']
        observations[2]['receipt_mono_ns'] = observations[0]['receipt_mono_ns'] - 1000
        observations[2]['receipt_server_us'] = observations[2]['receipt_mono_ns'] // 1000
        with self.assertRaisesRegex(ValueError, 'chronology'):
            module.replay_rows(observations, references, events, anchors)
        observations[2]['receipt_mono_ns'] = original
        observations[2]['receipt_server_us'] = original // 1000
        events.pop()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            module.replay_rows(observations, references, events, anchors)

    def test_transport_rejects_credentials_routes_redirects_and_records_sends(self):
        import httpx
        module = self.harness()
        events, sends = [], []
        def send(request):
            sends.append(request)
            return httpx.Response(302 if request.url.path == '/time' else 200,
                headers={'Location': 'https://evil.example', 'Set-Cookie': 'secret=value'} if request.url.path == '/time' else {},
                content=b'123')
        transport = module.AuditTransport(httpx.MockTransport(send), events.append)
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
            for url, headers in [('https://data-api.polymarket.com/v2/resolutions', {}),
                                 ('https://clob.polymarket.com/order', {}),
                                 ('https://clob.polymarket.com/book', {'Authorization': 'secret'})]:
                with self.assertRaises(ValueError):
                    client.get(url, headers=headers)
            self.assertEqual(len(sends), 0)
            self.assertEqual(client.get('https://api.exchange.coinbase.com/products/BTC-USD/ticker').content, b'123')
            with self.assertRaises(ValueError):
                client.get('https://clob.polymarket.com/time')
        self.assertEqual(len(sends), 2)
        self.assertEqual(events[0]['response_bytes'], 3)
        self.assertTrue(events[1]['redirect'])
        self.assertNotIn('secret', __import__('json').dumps(events))

    def test_public_get_stream_and_cookie_isolation(self):
        import httpx
        import time
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        from polymarket_bot.market_data.http import PublicGet
        from polymarket_bot.market_data.models import MarketDataError
        module = self.harness()
        events = []
        class Stream(httpx.SyncByteStream):
            def __init__(self, oversized=False, slow=False):
                self.oversized, self.slow = oversized, slow
                self.closed = False
            def __iter__(self):
                if self.slow:
                    time.sleep(0.03)
                yield b'1' * 10_485_761 if self.oversized else b'123'
            def close(self):
                self.closed = True
        streams = []
        def send(request):
            self.assertNotIn('cookie', request.headers)
            stream = Stream(request.url.params.get('limit') == '1', request.url.params.get('limit') == '2')
            streams.append(stream)
            return httpx.Response(200, headers={'Set-Cookie': 'secret=value'}, stream=stream)
        with httpx.Client(transport=module.AuditTransport(httpx.MockTransport(send), events.append), trust_env=False) as client:
            public = PublicGet(client, monotonic=time.monotonic, sleep=time.sleep,
                utcnow=lambda: datetime.now(timezone.utc), max_attempts=1)
            self.assertEqual(public.get_json('clob', '/time', deadline=time.monotonic() + 1).payload, 123)
            for selector in ('1', '2'):
                with self.assertRaises(MarketDataError):
                    public.get_json('gamma', '/events', params={'limit': selector},
                        deadline=time.monotonic() + (0.01 if selector == '2' else 1))
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: client.get('https://clob.polymarket.com/time'), range(4)))
            self.assertFalse(client.cookies)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertTrue(all(event['set_cookie_present'] for event in events))
        self.assertNotIn('secret', __import__('json').dumps(events))

    def test_child_closes_client_when_store_open_fails(self):
        import tempfile
        from unittest.mock import patch, Mock
        module = self.harness()
        client = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(module.httpx, 'Client', return_value=client), patch(
                'polymarket_bot.recorder.store.RecorderStore.open_approximate', side_effect=OSError('open failed')):
            with self.assertRaises(OSError):
                module.child(Path(directory) / 'new.sqlite3', 120)
        client.close.assert_called_once()

    def test_analysis_empty_v2_fails_replay_closed(self):
        import tempfile
        from polymarket_bot.recorder.store import RecorderStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new.sqlite3'
            store = RecorderStore.open_approximate(path)
            store.close()
            before = path.read_bytes()
            result = self.analyzer().analyze_db(path)
            self.assertEqual(result['integrity'], ['ok'])
            self.assertEqual(result['user_version'], 2)
            self.assertFalse(result['replay']['exact'])
            self.assertIn('linked_associations', result['isolation'])
            self.assertIn('windows', result)
            self.assertIn('wall_gaps_seconds', result['providers']['coinbase'])
            self.assertIn('fallback_percentage', result['references'])
            self.assertEqual(path.read_bytes(), before)

    def test_cp5_rollover_never_reconciles_resolution(self):
        from unittest.mock import Mock, patch
        from polymarket_bot.recorder.service import Recorder
        module = self.harness()
        public = Mock()
        with patch.object(Recorder, '_reconcile', side_effect=AssertionError('production resolution called')):
            recorder = object.__new__(module.CP5Recorder)
            recorder.public = public
            for deadline in (0, 900_000_000_000, 1200_000_000_000):
                recorder._reconcile(deadline)
        public.get_json.assert_not_called()

    def test_transport_invalid_selectors_never_send(self):
        import httpx
        module = self.harness()
        sends = []
        with httpx.Client(transport=module.AuditTransport(httpx.MockTransport(
                lambda request: sends.append(request) or httpx.Response(200)), lambda event: None)) as client:
            requests = [
                httpx.Request('GET', 'https://clob.polymarket.com/book?token_id=secret'),
                httpx.Request('GET', 'https://clob.polymarket.com/book?token_id=1&token_id=2'),
                httpx.Request('GET', 'https://clob.polymarket.com/time', content=b'secret'),
                httpx.Request('GET', 'https://api.kraken.com/0/public/Ticker?pair=ETHUSD'),
                httpx.Request('POST', 'https://clob.polymarket.com/books', json=[{'token_id': '1'}, {'token_id': '1'}]),
                httpx.Request('POST', 'https://clob.polymarket.com/books', json=[{'token_id': '1', 'secret': 'x'}, {'token_id': '2'}])]
            for request in requests:
                with self.assertRaises(ValueError):
                    client.send(request)
        self.assertEqual(sends, [])

    def test_replay_malformed_json_object_fails_closed(self):
        with self.assertRaises(ValueError):
            self.analyzer().replay_rows([], [], [{'detail_json': '[]'}], [])

    def test_stream_close_failure_emits_evidence(self):
        import httpx
        module = self.harness()
        events = []
        class Broken(httpx.SyncByteStream):
            def close(self):
                raise OSError('close failed')
        stream = module.AuditStream(Broken(), {'error': None}, events.append)
        with self.assertRaises(OSError):
            stream.close()
        self.assertEqual(events[0]['error'], 'OSError')

    def test_window_fixture_rejects_mixed_runs_times_and_tokens(self):
        module = self.analyzer()
        market = dict(market_id='m', window_start_us=0, window_end_us=900_000_000)
        runs = [dict(run_id='a', started_wall_us=0, ended_wall_us=900_000_000)]
        slots = [dict(slot_id=i + 1, market_id='m', run_id='a', slot_index=i,
            scheduled_us=i * 2_000_000) for i in range(450)]
        books = [dict(slot_id=i + 1, outcome=outcome, expected_token_id=token, status='VALID')
            for i in range(450) for outcome, token in [('UP', '1'), ('DOWN', '2')]]
        self.assertTrue(module.window_coverage(market, slots, books, runs)['complete_window'])
        for key, value in [('run_id', 'b'), ('scheduled_us', 1)]:
            original = slots[0][key]
            slots[0][key] = value
            self.assertFalse(module.window_coverage(market, slots, books, runs)['complete_window'])
            slots[0][key] = original
        books[0]['expected_token_id'] = '2'
        self.assertFalse(module.window_coverage(market, slots, books, runs)['complete_window'])

    def test_malformed_event_analysis_fails_closed(self):
        import tempfile
        import sqlite3
        from polymarket_bot.recorder.store import RecorderStore
        from polymarket_bot.recorder.models import RunConfig
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.sqlite3'
            store = RecorderStore.open_approximate(path)
            run = store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'), wall_us=1, app_revision='test')
            store.record_event(run, 'STARTUP', started_us=1)
            store.close()
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE recorder_events SET detail_json='[]'")
            conn.close()
            result = self.analyzer().analyze_db(path)
            self.assertFalse(result['replay']['exact'])
            self.assertFalse(result['smoke_gate'])

    def test_child_client_close_failure_still_closes_store(self):
        import tempfile
        from unittest.mock import Mock, patch
        module = self.harness()
        client, store = Mock(), Mock()
        client.close.side_effect = OSError('client close failed')
        store._closed = True
        with tempfile.TemporaryDirectory() as directory, patch.object(module.httpx, 'Client', return_value=client), patch(
                'polymarket_bot.recorder.store.RecorderStore.open_approximate', return_value=store), patch.object(
                module, 'CP5Recorder', side_effect=RuntimeError('setup failed')):
            with self.assertRaises(OSError):
                module.child(Path(directory) / 'new.sqlite3', 120)
        store.close.assert_called_once()

    def test_child_store_close_failure_follows_client_close(self):
        import tempfile
        from unittest.mock import Mock, patch
        module = self.harness()
        client, store = Mock(), Mock()
        store.close.side_effect = OSError('store close failed')
        store._closed = False
        with tempfile.TemporaryDirectory() as directory, patch.object(module.httpx, 'Client', return_value=client), patch(
                'polymarket_bot.recorder.store.RecorderStore.open_approximate', return_value=store), patch.object(
                module, 'CP5Recorder', side_effect=RuntimeError('setup failed')):
            with self.assertRaises(OSError):
                module.child(Path(directory) / 'new.sqlite3', 120)
        client.close.assert_called_once()

    def test_transport_close_failure_still_attempts_store_close(self):
        import tempfile
        from unittest.mock import Mock, patch
        module = self.harness()
        store = Mock()
        store._closed = True
        with tempfile.TemporaryDirectory() as directory, patch.object(module.AuditTransport, 'close', side_effect=OSError('transport close failed')), patch(
                'polymarket_bot.recorder.store.RecorderStore.open_approximate', return_value=store), patch.object(
                module, 'CP5Recorder', side_effect=RuntimeError('setup failed')):
            with self.assertRaises(OSError):
                module.child(Path(directory) / 'new.sqlite3', 120)
        store.close.assert_called_once()

    def test_alignment_wait_bounded(self):
        module = self.harness()
        waits = []
        self.assertEqual(module.align(now=lambda: 901, sleep=waits.append), 899)
        self.assertEqual(waits, [899])
        self.assertEqual(module.align(now=lambda: 1800, sleep=waits.append), 0)

    def test_wait_retries_early_wake_until_deadline(self):
        from polymarket_bot.recorder.service import Recorder
        recorder = object.__new__(Recorder)
        now = [0]
        recorder.monotonic_ns = lambda: now[0]
        recorder.run_deadline_ns = 100
        def sleep(seconds):
            now[0] += max(1, round(seconds * 1_000_000_000) // 2)
        recorder.sleep = sleep
        recorder._wait(80)
        self.assertEqual(now[0], 80)

    def test_checkpoint_not_admitted_after_reference_consumes_budget(self):
        from unittest.mock import Mock
        from polymarket_bot.recorder.service import Recorder
        recorder = object.__new__(Recorder)
        now = [0]
        recorder.monotonic_ns = lambda: now[0]
        recorder.run_deadline_ns = 100_000_000
        recorder._reference_housekeeping = lambda deadline_ns: now.__setitem__(0, deadline_ns)
        recorder._checkpoint_due = Mock()
        recorder._housekeeping(10_000_000)
        recorder._checkpoint_due.assert_not_called()

    def test_slot_diagnosis_keeps_all_grid_rows_and_unknown_causes(self):
        market = dict(market_id='m', window_start_us=0, window_end_us=900_000_000)
        slots = [dict(market_id='m', slot_id=1, slot_index=1, run_id='r',
            status='MISSED', reason='PROCESSING_OVERRUN', scheduled_us=2_000_000)]
        result = self.analyzer().slot_diagnosis(market, slots, [], [], [])
        self.assertEqual(len(result['rows']), 450)
        self.assertEqual(result['rows'][1]['cause'], 'UNKNOWN')
        self.assertEqual(result['rows'][1]['category'], 'UNSENT')
        self.assertEqual(sum(result['categories'].values()), 450)
        self.assertEqual(result['contiguous_uncollected_slots'], [450])

    def test_cp5_timing_event_records_observed_monotonic_time(self):
        from unittest.mock import Mock
        recorder = object.__new__(self.harness().CP5Recorder)
        recorder.store = Mock()
        recorder.run_id = 'run'
        recorder.monotonic_ns = lambda: 123
        recorder._timing('WAKE', target_ns=100, slot_index=2)
        recorder.store.record_event.assert_not_called()
        detail = recorder._timing_buffer[0][1]
        self.assertEqual(detail['mono_ns'], 123)
        self.assertEqual(detail['phase'], 'WAKE')
        self.assertTrue(detail['diagnostic_only'])

    def test_timing_identity_ends_after_capture_or_changed_target(self):
        recorder = object.__new__(self.harness().CP5Recorder)
        recorder.monotonic_ns = lambda: 123
        for ended in (True, False):
            with self.subTest(finalized=ended):
                recorder._timing('SELECTION', market_id='m', slot_index=2, target_ns=100)
                recorder._timing('HOUSEKEEPING_START', target_ns=100)
                self.assertEqual(recorder._timing_buffer[-1][1]['market_id'], 'm')
                recorder._timing('WAKE', target_ns=100)
                self.assertEqual(recorder._timing_buffer[-1][1]['slot_index'], 2)
                if ended:
                    recorder._timing('FINALIZATION_END', slot_id=7, target_ns=100)
                    self.assertEqual(recorder._timing_buffer[-1][1]['market_id'], 'm')
                    self.assertEqual(recorder._timing_identity, {})
                for phase in ('HOUSEKEEPING_START', 'HOUSEKEEPING_END', 'WAIT_START', 'WAKE'):
                    recorder._timing(phase, target_ns=200)
                    detail = recorder._timing_buffer[-1][1]
                    self.assertNotIn('market_id', detail)
                    self.assertNotIn('slot_index', detail)
                    self.assertEqual(detail['target_ns'], 200)
                self.assertEqual(recorder._timing_identity, {})

    def test_diagnosis_partitions_runs_and_controls(self):
        import json
        market = dict(market_id='m', window_start_us=0, window_end_us=900_000_000)
        slots = [dict(market_id='m', slot_id=1, slot_index=1, run_id='r', status='MISSED', reason='PROCESSING_OVERRUN')]
        attempts = [dict(run_id='other', market_id='other', slot_id=2),
                    dict(run_id='r', market_id=None, slot_id=None)]
        result = self.analyzer().slot_diagnosis(market, slots, attempts, [], [])
        self.assertEqual(result['request_timeline'], [])
        self.assertEqual(len(result['unassigned_controls']), 1)
        self.assertEqual(result['rows'][1]['category'], 'UNSENT')

    def test_diagnosis_attempt_failure_categories(self):
        market = dict(market_id='m', window_start_us=0, window_end_us=900_000_000)
        for error, expected in [('request_timeout', 'ATTEMPTED_TIMEOUT'),
                                ('http_error', 'ATTEMPTED_HTTP'),
                                ('invalid_json', 'ATTEMPTED_MALFORMED')]:
            slots = [dict(market_id='m', slot_id=1, slot_index=1, run_id='r', status='FAILED', reason=None)]
            attempts = [dict(run_id='r', market_id='m', slot_id=1, error_code=error)]
            result = self.analyzer().slot_diagnosis(market, slots, attempts, [], [])
            self.assertEqual(result['rows'][1]['category'], expected)

    def test_diagnosis_separates_run_grids_and_book_errors(self):
        market = dict(market_id='m', window_start_us=0, window_end_us=900_000_000)
        slots = [dict(market_id='m', slot_id=i, slot_index=1, run_id=run,
            status='FAILED', reason=None) for i, run in [(1, 'a'), (2, 'b')]]
        attempts = [dict(run_id=run, market_id='m', slot_id=i, error_code=None)
            for i, run in [(1, 'a'), (2, 'b')]]
        books = [dict(slot_id=1, status='INVALID', error_code='invalid_book'),
                 dict(slot_id=2, status='LATE', error_code=None)]
        result = self.analyzer().slot_diagnosis(market, slots, attempts, [], [], books)
        self.assertEqual(set(result['runs']), {'a', 'b'})
        self.assertEqual(result['runs']['a']['rows'][1]['category'], 'ATTEMPTED_MALFORMED')
        self.assertEqual(result['runs']['b']['rows'][1]['category'], 'ATTEMPTED_LATE')
        self.assertTrue(all(len(run['rows']) == 450 for run in result['runs'].values()))

    def test_timing_flush_uses_one_transaction_and_no_tail(self):
        import tempfile
        from polymarket_bot.recorder.store import RecorderStore
        from polymarket_bot.recorder.models import RunConfig
        recorder = object.__new__(self.harness().CP5Recorder)
        recorder.monotonic_ns = lambda: 123
        with tempfile.TemporaryDirectory() as directory:
            recorder.store = RecorderStore.open_approximate(Path(directory) / 'new.sqlite3')
            recorder.run_id = recorder.store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'), wall_us=1, app_revision='test')
            statements = []
            recorder.store._connection.set_trace_callback(statements.append)
            recorder._timing('WAKE')
            recorder._timing('WAKE')
            recorder._flush_timing()
            recorder.store.close()
            self.assertEqual(sum(sql == 'BEGIN IMMEDIATE' for sql in statements), 1)
            self.assertEqual(recorder._timing_buffer, [])

    def test_shutdown_stops_worker_when_timing_flush_fails(self):
        from unittest.mock import Mock, patch
        from polymarket_bot.recorder.service import Recorder
        recorder = object.__new__(self.harness().CP5Recorder)
        recorder.reference_worker = None
        recorder.approximate_worker = Mock()
        recorder._approximate_drain = Mock()
        recorder.store = Mock()
        recorder.run_id = 'run'
        recorder.wall_us = lambda: 1
        recorder._flush_timing = Mock(side_effect=OSError('timing flush failed'))
        with patch('polymarket_bot.recorder.service.take_checkpoint'):
            with self.assertRaisesRegex(OSError, 'timing flush failed'):
                recorder._shutdown('STOPPED', 'STOP_REQUESTED')
        recorder.approximate_worker.stop.assert_called_once_with()
        recorder._approximate_drain.assert_called_once_with(stopping=True)
        recorder._flush_timing.assert_called_once_with()

    def test_supervisor_kills_overdue_child(self):
        import sys
        module = self.harness()
        result = module.supervise([sys.executable, '-c', 'import time; time.sleep(10)'], 0.1)
        self.assertTrue(result['watchdog_killed'])
        self.assertIsNotNone(result['returncode'])

    @staticmethod
    def harness():
        spec = importlib.util.spec_from_file_location('cp5_run', Path(__file__).parents[1] / 'scripts/cp5_run.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def analyzer():
        spec = importlib.util.spec_from_file_location('cp5_analyze', Path(__file__).parents[1] / 'scripts/cp5_analyze.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_analysis_closes_connection_when_query_fails(self):
        import sqlite3
        import tempfile
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('cp5_analyze', Path(__file__).parents[1] / 'scripts/cp5_analyze.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.sqlite3'
            connection = sqlite3.connect(path)
            connection.close()
            before = path.read_bytes()
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            with patch.object(module.sqlite3, 'connect', return_value=connection):
                with self.assertRaises(sqlite3.OperationalError):
                    module.analyze_db(path)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            path.unlink()

    def test_analysis_missing_database_does_not_create_file(self):
        import tempfile
        spec = importlib.util.spec_from_file_location('cp5_analyze', Path(__file__).parents[1] / 'scripts/cp5_analyze.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(Exception):
                module.analyze_db(path)
            self.assertFalse(path.exists())
