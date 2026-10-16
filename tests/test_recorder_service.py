"""Offline coordinator behavior through actual adapters and the owned database."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from typing import Any, cast

import httpx

from polymarket_bot.recorder.models import RunConfig
from tests.recorder_support import RecorderFixture, NOW_US
from polymarket_bot.recorder.store import decode_evidence
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError


class RecorderServiceTests(unittest.TestCase):
    def test_failed_preparation_has_durable_phase_trace(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.metadata import acquire_market
        from polymarket_bot.market_data.models import MarketDataError
        fixture = self.fixture(offset_us=570_000_000)
        recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
        recorder.config = RunConfig()
        recorder._calibrate(fixture.monotonic()+10)
        recorder.revision = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
            deadline=fixture.monotonic()+10, use_fast_path=False)
        with patch('polymarket_bot.recorder.service.acquire_next_market', side_effect=MarketDataError('no_next_market','none')):
            recorder._prepare_next(recorder.run_deadline_ns)
        trace = [json.loads(row['detail_json']) for row in fixture.store._rows('SELECT detail_json FROM recorder_events')]
        failed = [row for row in trace if row.get('type') == 'PREPARATION_PHASE' and row.get('reason') == 'no_next_market']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get('failure_reason'), 'NO_VALID_SUCCESSOR')
        self.assertIn('deadline_ns', failed[0])
        self.assertIn('active_target_ns', failed[0])
        from json import JSONDecodeError
        failures = [('invalid_metadata', 'schema', 'EVENT_SCHEMA_INVALID'),
            ('invalid_metadata', 'Successor event evidence exceeds bound.', 'EVIDENCE_BOUND_EXCEEDED'),
            ('ambiguous_market', 'none', 'AMBIGUOUS_SUCCESSOR'),
            ('transport_error', 'Response exceeds 10 MiB.', 'RESPONSE_TOO_LARGE'),
            ('invalid_numeric', 'numeric', 'NUMERIC_PARSE_ERROR'),
            ('invalid_metadata', 'json', 'JSON_DECODE_ERROR')]
        for code, message, expected in failures:
            with self.subTest(expected=expected):
                recorder._preparation_phase = None
                error = MarketDataError(code, message)
                if message == 'json':
                    error.__cause__ = JSONDecodeError('invalid', '', 0)
                with patch('polymarket_bot.recorder.service.acquire_next_market', side_effect=error):
                    recorder._prepare_next(recorder.run_deadline_ns)
                saved = json.loads(fixture.store._rows(
                    'SELECT detail_json FROM recorder_events ORDER BY event_id DESC LIMIT 1')[0]['detail_json'])
                self.assertEqual(saved['reason'], code)
                self.assertEqual(saved['failure_reason'], expected)
                self.assertNotIn('message', saved)
        recorder._preparation_phase = None
        def late(*args, **kwargs):
            fixture.advance(10)
            return recorder.revision
        with patch('polymarket_bot.recorder.service.acquire_next_market', side_effect=late):
            recorder._prepare_next(recorder.run_deadline_ns)
        trace = [json.loads(row['detail_json']) for row in fixture.store._rows('SELECT detail_json FROM recorder_events')]
        reasons = [row['reason'] for row in trace if row.get('type') == 'PREPARATION_PHASE']
        self.assertIn('LATE_COMPLETION', reasons)
        self.assertNotIn('COMPLETE', reasons)

    def test_calibrated_endpoint_never_extends_fixed_deadline(self):
        from polymarket_bot.recorder.service import Recorder
        self.assertTrue(callable(getattr(Recorder, '_cap_intended_end', None)))
        from types import SimpleNamespace
        recorder = cast(Any, object.__new__(Recorder))
        events = []
        recorder.store = SimpleNamespace(record_event=lambda *args, **kwargs: events.append(kwargs['detail']))
        recorder.run_id = 'r'
        recorder.wall_us = lambda: 0
        recorder.monotonic_ns = lambda: 0
        recorder.intended_end_us = 910_000_000
        recorder.run_deadline_ns = 75_000_000_000
        recorder.clock = SimpleNamespace(usable=lambda: True, deadline_ns=lambda us: (us-855_000_000)*1000)
        recorder._cap_intended_end()
        self.assertEqual(recorder.run_deadline_ns, 55_000_000_000)
        recorder.clock.deadline_ns = lambda us: 80_000_000_000
        recorder._cap_intended_end()
        self.assertEqual(recorder.run_deadline_ns, 55_000_000_000)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['previous_deadline_ns'], 75_000_000_000)
        self.assertEqual(events[1]['calibrated_endpoint_ns'], 80_000_000_000)
        self.assertEqual(events[1]['effective_deadline_ns'], 55_000_000_000)

    def test_predecessor_revision_change_discards_preparation(self):
        from dataclasses import replace
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.metadata import acquire_market
        fixture = self.fixture()
        recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder._calibrate(fixture.monotonic()+10)
        previous = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
            deadline=fixture.monotonic()+10, use_fast_path=False)
        recorder._save_revision(previous)
        recorder._prepared = {'owner':'old'}
        recorder._preparation_phase = ('boundary', 'PREPARED')
        recorder._save_revision(replace(previous, capture_ready=False))
        self.assertIsNone(recorder._prepared)
        self.assertEqual(recorder._preparation_phase, ('boundary', 'FAILED'))

    def test_prepared_activation_rejects_late_stale_invalidated_and_storage_failure(self):
        from dataclasses import replace
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.metadata import acquire_market
        for fault in ('late', 'owner', 'invalidated', 'clock', 'storage'):
            with self.subTest(fault=fault):
                fixture = self.fixture(offset_us=570_000_000)
                recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                    wall_us=fixture.wall_us, sleep=fixture.sleep))
                recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
                recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
                recorder.invalidated = set()
                recorder.config = RunConfig()
                recorder.discovery_gap_id = None
                recorder._calibrate(fixture.monotonic()+10)
                prior = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
                    deadline=fixture.monotonic()+10, use_fast_path=False)
                recorder._save_revision(prior)
                boundary = int(prior.market.window_end.timestamp()*1e6)
                future = replace(prior, market=replace(prior.market, market_id='1002',
                    condition_id='0x'+'cd'*32, window_start=prior.market.window_end,
                    window_end=prior.market.window_end+__import__('datetime').timedelta(seconds=900),
                    up=replace(prior.market.up, token_id='3'), down=replace(prior.market.down, token_id='4')),
                    gamma_raw=b'{"id":"1002"}')
                from polymarket_bot.recorder.metadata import _identity
                target = recorder._nominal_target_ns(boundary)
                recorder._prepared = dict(revision=future, owner=(recorder.run_id, prior.market.event_id, _identity(prior)),
                    boundary=boundary, prepared_ns=target-30_000_000_000, validated_ns=target-2_000_000_000,
                    armed_ns=target-2_000_000_000, target_ns=target)
                fixture.advance((target-fixture.monotonic_ns())/1e9)
                if fault == 'late': fixture.advance(2)
                if fault == 'owner': recorder.run_id = 'other'
                if fault == 'invalidated': recorder.invalidated.add('1002')
                if fault == 'clock': recorder.clock.anchor = None
                if fault == 'storage':
                    with patch.object(fixture.store, 'record_event', side_effect=OSError('write failed')):
                        with self.assertRaises(OSError): recorder._activate_prepared()
                    self.assertEqual(cast(Any, fixture.store._one('SELECT count(*) AS n FROM markets'))['n'], 1)
                    self.assertEqual(cast(Any, fixture.store._one("SELECT lifecycle FROM markets WHERE market_id='1001'"))['lifecycle'], 'CURRENT')
                else:
                    self.assertFalse(recorder._activate_prepared())
                self.assertIsNone(recorder._prepared)
                self.assertIs(recorder.revision, prior)

    def test_final_arm_skips_metadata_and_calibration_housekeeping(self):
        from polymarket_bot.recorder.service import Recorder
        recorder = cast(Any, object.__new__(Recorder))
        recorder._prepared = {'armed_ns': 1}
        recorder.monotonic_ns = lambda: 0
        recorder.run_deadline_ns = 100_000_000
        recorder._reference_housekeeping = lambda _: self.fail('Control IO after final arm')
        recorder._housekeeping(50_000_000)

    def test_intended_opening_unavailable_is_blocked_not_retargeted(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.market_data.models import MarketDataError
        self.assertTrue(callable(getattr(Recorder, '_check_opening_deadline', None)))
        recorder = cast(Any, object.__new__(Recorder))
        from types import SimpleNamespace
        recorder.intended_boundary_us = 900_000_000
        recorder._now = lambda: 899_999_000
        recorder.clock = SimpleNamespace(usable=lambda: True)
        recorder.revision = None
        with self.assertRaises(MarketDataError):
            recorder._check_opening_deadline()

    def test_prepared_arm_clears_on_clock_discontinuity(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.market_data.http import JsonRead
        fixture = self.fixture()
        recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
        recorder._calibrate(fixture.monotonic()+10)
        recorder._prepared = {'sentinel':True}
        recorder._armed_opening = ('old', 1)
        cast(Any, fixture).server_shift_us = 20_000_000
        recorder._calibrate(fixture.monotonic()+10)
        self.assertIsNone(recorder._prepared)
        self.assertIsNone(recorder._armed_opening)

    def test_preparation_incomplete_budget_never_starts_network(self):
        from polymarket_bot.recorder.service import Recorder
        from types import SimpleNamespace
        from datetime import datetime, timezone
        recorder = cast(Any, object.__new__(Recorder))
        market = SimpleNamespace(window_end=datetime.fromtimestamp(900, timezone.utc),
            window_start=datetime.fromtimestamp(0, timezone.utc), event_id='e', market_id='m',
            condition_id='c', up=SimpleNamespace(token_id='1'), down=SimpleNamespace(token_id='2'))
        recorder.revision = SimpleNamespace(market=market, up_index=0, down_index=1)
        recorder.clock = SimpleNamespace(usable=lambda: True, deadline_ns=lambda us: us*1000)
        recorder.run_id = 'r'
        recorder.monotonic_ns = lambda: 875_000_000_000
        recorder._now = lambda: 875_000_000
        recorder._phase_trace = lambda *args: None
        recorder._control = lambda operation: self.fail('Incomplete phase budget admitted')
        recorder._prepare_next(880_000_000_000)
        self.assertIsNone(recorder._prepared)

    def test_preparation_rejects_intended_boundary_mismatch(self):
        from polymarket_bot.recorder.service import Recorder
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from polymarket_bot.market_data.models import MarketDataError
        from typing import Any, cast
        recorder = cast(Any, object.__new__(Recorder))
        recorder.revision = SimpleNamespace(market=SimpleNamespace(window_end=datetime.fromtimestamp(900, timezone.utc)))
        recorder.clock = SimpleNamespace(usable=lambda: True)
        recorder.intended_boundary_us = 1800_000_000
        recorder.run_id = 'r'
        recorder.revision.market.event_id = 'e'
        recorder.revision.market.market_id = 'm'
        recorder.revision.market.condition_id = 'c'
        recorder.revision.market.window_start = datetime.fromtimestamp(0, timezone.utc)
        recorder.revision.market.up = SimpleNamespace(token_id='1')
        recorder.revision.market.down = SimpleNamespace(token_id='2')
        recorder.revision.up_index, recorder.revision.down_index = 0, 1
        recorder._now = lambda: 0
        with self.assertRaises(MarketDataError):
            recorder._prepare_next(10)

    def test_prepared_state_cleared_on_shutdown_and_clock_loss(self):
        from polymarket_bot.recorder.service import Recorder
        from types import SimpleNamespace
        from typing import Any, cast
        recorder = cast(Any, object.__new__(Recorder))
        recorder._prepared = {'sentinel': True}
        recorder._armed_opening = ('old', 1)
        recorder.reference_worker = recorder.approximate_worker = None
        recorder.store = SimpleNamespace(recover_interrupted=lambda **kw: None,
            record_event=lambda *a, **kw: None, finish_run=lambda *a, **kw: None)
        recorder.run_id = 'r'
        recorder.wall_us = lambda: 0
        with patch('polymarket_bot.recorder.service.take_checkpoint'):
            recorder._shutdown('STOPPED', 'test')
        self.assertIsNone(recorder._prepared)
        self.assertIsNone(recorder._armed_opening)

    def test_preparation_control_wakes_without_remaining_slot(self):
        from polymarket_bot.recorder.service import Recorder
        self.assertTrue(callable(getattr(Recorder, '_preparation_wake_ns', None)))
        from types import SimpleNamespace
        from typing import Any, cast
        recorder = cast(Any, object.__new__(Recorder))
        recorder.run_deadline_ns = 100_000_000_000
        recorder.monotonic_ns = lambda: 0
        recorder.clock = SimpleNamespace(deadline_ns=lambda us: us*1000)
        recorder._preparation_phase = None
        self.assertEqual(recorder._preparation_wake_ns(90_000_000), 60_000_000_000)
        recorder._preparation_phase = ('owner', 'PREPARED')
        self.assertEqual(recorder._preparation_wake_ns(90_000_000), 78_000_000_000)
        recorder._preparation_phase = ('owner', 'ARMED')
        self.assertEqual(recorder._preparation_wake_ns(90_000_000), 90_000_000_000)

    def test_prepared_rollover_captures_actual_five_opening_targets(self):
        from datetime import datetime, timezone
        from polymarket_bot.recorder.service import Recorder
        from tests.support import make_event, make_market
        fixture = self.fixture(offset_us=540_000_000)
        boundary = ((fixture.wall_us()//900_000_000)+1)*900_000_000
        future = make_market(id='1002', conditionId='0x'+'cd'*32, clobTokenIds='["3","4"]',
            eventStartTime=datetime.fromtimestamp(boundary/1e6, timezone.utc).isoformat(),
            endDate=datetime.fromtimestamp(boundary/1e6+900, timezone.utc).isoformat())
        def hook(request, payload, owner):
            if request.url.path == '/events':
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps([make_event(markets=[owner.market_row()]),
                    make_event(id='2002', markets=[future])]).encode()))
            if request.url.path == '/markets/1002':
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(future).encode()))
            if request.url.path == '/clob-markets/'+'0x'+'cd'*32:
                return httpx.Response(200, stream=httpx.ByteStream(b'{"t":[{"t":"3"},{"t":"4"}]}'))
        cast(Any, fixture).hook = hook
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        result = recorder.run(RunConfig(), should_stop=lambda: fixture.wall_us() >= boundary+10_000_000,
            deadline_ns=fixture.monotonic_ns()+70_000_000_000)
        self.assertEqual(result.exit_code, 0)
        slots = fixture.rows("SELECT slot_index,start_mono_ns AS started_mono_ns,scheduled_us FROM sample_slots WHERE market_id='1002' AND status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([row['slot_index'] for row in slots], list(range(5)))
        for row in slots:
            self.assertGreaterEqual(row['started_mono_ns'], fixture.epoch_ns+(row['scheduled_us']-fixture.initial_wall_us)*1000)
        self.assertTrue(fixture.rows("SELECT event_id FROM recorder_events WHERE detail_json LIKE '%PREPARED_ACTIVATION%'"))

    def test_prepared_approximate_rollover_delayed_submission_keeps_actual_bounds(self):
        from datetime import datetime, timezone
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.free_reference import ApproximateReferenceSession, CoinbaseRestProvider, KrakenRestProvider
        from tests.support import make_event, make_market
        fixture = self.fixture(offset_us=540_000_000)
        boundary = ((fixture.wall_us()//900_000_000)+1)*900_000_000
        future = make_market(id='1002', conditionId='0x'+'cd'*32, clobTokenIds='["3","4"]',
            eventStartTime=datetime.fromtimestamp(boundary/1e6, timezone.utc).isoformat(),
            endDate=datetime.fromtimestamp(boundary/1e6+900, timezone.utc).isoformat())
        sends = []
        def hook(request, payload, owner):
            if request.url.path == '/events':
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps([make_event(markets=[owner.market_row()]),
                    make_event(id='2002', markets=[future])]).encode()))
            if request.url.path == '/markets/1002':
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(future).encode()))
            if request.url.path == '/clob-markets/'+'0x'+'cd'*32:
                return httpx.Response(200, stream=httpx.ByteStream(b'{"t":[{"t":"3"},{"t":"4"}]}'))
            if request.url.path in ('/book', '/books'):
                sends.append((fixture.wall_us(), fixture.monotonic_ns()))
        cast(Any, fixture).hook = hook
        fixture.store.close()
        fixture.db.path = fixture.db.path.parent / 'research.sqlite3'
        fixture.store = RecorderStore.open_approximate(fixture.db.path)
        provider_sends = []
        with httpx.Client(transport=httpx.MockTransport(lambda request: provider_sends.append(fixture.monotonic_ns())
                or httpx.Response(200, json={}))) as client:
            providers = {name: cls(client, monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us,
                sleep=lambda seconds: None) for name, cls in (('coinbase', CoinbaseRestProvider), ('kraken', KrakenRestProvider))}
            session = ApproximateReferenceSession(providers=providers, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=lambda seconds: None)
            recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, approximate_reference=session))
            original = recorder._approximate_submit
            def delayed(deadline_ns, **identity):
                if identity.get('market_id') == '1002':
                    fixture.advance(2)
                return original(deadline_ns, **identity)
            recorder._approximate_submit = delayed
            result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                should_stop=lambda: fixture.wall_us() >= boundary+10_000_000,
                deadline_ns=fixture.monotonic_ns()+70_000_000_000)
        self.assertEqual(result.exit_code, 0, result)
        self.assertTrue(fixture.rows("SELECT event_id FROM recorder_events WHERE detail_json LIKE '%PREPARED_ACTIVATION%'"),
            fixture.rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARATION_PHASE%'"))
        self.assertFalse([wall for wall, mono in sends if boundary <= wall < boundary+10_000_000])
        self.assertTrue(fixture.rows("SELECT slot_id FROM sample_slots WHERE market_id='1002'"))

    def test_prepared_activation_atomic_single_use_and_admission(self):
        from dataclasses import replace
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.metadata import acquire_market
        self.assertTrue(callable(getattr(Recorder, '_prepare_next', None)))
        fixture = self.fixture(offset_us=570_000_000)
        recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
        recorder.invalidated = set()
        recorder.discovery_gap_id = None
        recorder.discovery_gap_start = None
        recorder.next_clock_ns = 2**63-1
        recorder.config = RunConfig()
        recorder._calibrate(fixture.monotonic()+10)
        previous = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
            deadline=fixture.monotonic()+10, use_fast_path=False)
        recorder._save_revision(previous)
        boundary = int(previous.market.window_end.timestamp()*1_000_000)
        future = replace(previous, market=replace(previous.market, market_id='1002', event_id='2002',
            condition_id='0x'+'cd'*32, window_start=previous.market.window_end,
            window_end=previous.market.window_end+__import__('datetime').timedelta(seconds=900),
            up=replace(previous.market.up, token_id='3'), down=replace(previous.market.down, token_id='4')))
        provenance = [dict(sha256='a'*64, bytes=1689540, complete=True, replay='selected_event_only')]
        future = replace(future, gamma_raw=b'{"id":"1002"}', market=replace(future.market,
            metadata=dict(future.market.metadata, prepared_list_evidence=provenance)))
        with patch('polymarket_bot.recorder.service.acquire_next_market', return_value=future), patch(
                'polymarket_bot.recorder.service.revalidate_prepared', return_value=future):
            recorder._prepare_next(recorder.run_deadline_ns)
            self.assertIs(recorder.revision, previous)
            self.assertEqual(cast(Any, fixture.store._one('SELECT count(*) AS n FROM markets'))['n'], 1)
            fixture.advance((recorder.clock.deadline_ns(boundary-12_000_000)-fixture.monotonic_ns())/1e9)
            recorder._prepare_next(recorder.run_deadline_ns)
            self.assertIsNotNone(recorder._prepared)
            self.assertTrue(fixture.store._rows("SELECT event_id FROM recorder_events WHERE detail_json LIKE '%PREPARED_ARM%'"))
            arm = json.loads(fixture.store._rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARED_ARM%'")[0]['detail_json'])
            arm_bytes = len(json.dumps(arm, sort_keys=True, separators=(',', ':')).encode())
            self.assertLessEqual(arm_bytes, 1_048_576)
            self.assertEqual(arm.get('list_evidence'), provenance)
            self.assertFalse(recorder._activate_prepared())
            fixture.advance((max(recorder._nominal_target_ns(boundary), recorder.clock.deadline_ns(boundary))-
                fixture.monotonic_ns())/1e9)
            recorder.reference_worker = object()
            self.assertTrue(recorder._activate_prepared())
            self.assertEqual({row['role'] for row in fixture.store._rows(
                "SELECT role FROM reference_associations WHERE market_id='1002'")}, {'OPENING', 'CLOSING'})
            recorder.reference_worker = None
            self.assertEqual(recorder.revision.market.market_id, '1002')
            self.assertFalse(recorder._activate_prepared())
            self.assertEqual(cast(Any, fixture.store._one("SELECT lifecycle FROM markets WHERE market_id='1001'"))['lifecycle'], 'EXPIRED')

    def test_arm_activation_durable_boundary(self):
        from dataclasses import replace
        from datetime import timedelta as dt
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.metadata import acquire_market
        from polymarket_bot.recorder.models import RunConfig
        import json
        fixture = self.fixture(offset_us=570_000_000)
        store = fixture.store
        recorder = cast(Any, Recorder(store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        try:
            recorder.run_id = store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
            recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
            recorder.invalidated = set()
            recorder.discovery_gap_id = None
            recorder.discovery_gap_start = None
            recorder.next_clock_ns = 2**63-1
            recorder.config = RunConfig()
            recorder._calibrate(fixture.monotonic()+10)
            previous = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
                deadline=fixture.monotonic()+10, use_fast_path=False)
            recorder._save_revision(previous)
            boundary = int(previous.market.window_end.timestamp()*1_000_000)
            recorder.intended_boundary_us = boundary
            future_base = replace(previous, market=replace(previous.market, market_id='1002', event_id='2002',
                condition_id='0x'+'cd'*32, window_start=previous.market.window_end,
                window_end=previous.market.window_end+dt(seconds=900),
                up=replace(previous.market.up, token_id='3'), down=replace(previous.market.down, token_id='4')))
            future_base = replace(future_base, gamma_raw=b'{"id":"1002"}')
            future_base = replace(future_base, market=replace(future_base.market, metadata=dict(
                future_base.market.metadata, prepared_list_evidence=[])))
            with patch('polymarket_bot.recorder.service.acquire_next_market', return_value=future_base), patch(
                    'polymarket_bot.recorder.service.revalidate_prepared', return_value=future_base):
                recorder._prepare_next(recorder.run_deadline_ns)
                fixture.advance((recorder.clock.deadline_ns(boundary-12_000_000)-fixture.monotonic_ns())/1e9)
                recorder._prepare_next(recorder.run_deadline_ns)
            base_arm = store._rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARED_ARM%' ORDER BY event_id DESC")[0]
            base_bytes = len(base_arm['detail_json'].encode())
            saved_prepared = dict(recorder._prepared)
            budget = 1_048_576 - base_bytes
            overhead = len(json.dumps([{'fill': ''}], separators=(',', ':'))) - 2
            for delta in (-1, 0, 1):
                with self.subTest(delta=delta):
                    recorder._prepared = dict(saved_prepared)
                    recorder._preparation_phase = (saved_prepared['owner'], 'PREPARED')
                    if budget + delta > overhead:
                        fill_size = max(0, budget + delta - overhead)
                        future = replace(future_base, market=replace(future_base.market, metadata=dict(
                            future_base.market.metadata, prepared_list_evidence=[{'fill': 'x' * fill_size}])))
                    else:
                        future = future_base
                    with patch('polymarket_bot.recorder.service.acquire_next_market', return_value=future), patch(
                            'polymarket_bot.recorder.service.revalidate_prepared', return_value=future):
                        fix = (recorder.clock.deadline_ns(boundary-12_000_000)-fixture.monotonic_ns())/1e9
                        fixture.advance(fix if fix > 0 else 0)
                        if delta > 0:
                            from polymarket_bot.market_data.models import MarketDataError
                            with self.assertRaises(MarketDataError) as caught:
                                recorder._prepare_next(recorder.run_deadline_ns)
                            self.assertEqual(caught.exception.reason, 'EVIDENCE_BOUND_EXCEEDED')
                            self.assertIsNone(recorder._prepared)
                        else:
                            recorder._prepare_next(recorder.run_deadline_ns)
                            arm_row = store._rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARED_ARM%' ORDER BY event_id DESC")[0]
                            actual = len(arm_row['detail_json'].encode())
                            self.assertLessEqual(actual, 1_048_576)
                            if delta <= 0:
                                self.assertEqual(actual, 1_048_576+delta)
        finally:
            store.close()
            fixture.close()

    def test_approximate_session_lifecycle_owned_by_recorder(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.free_reference import ApproximateReferenceSession
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                store = RecorderStore.open_approximate(Path(directory) / 'new.db')
                session = ApproximateReferenceSession(providers={}, monotonic_ns=lambda: 123,
                    wall_us=lambda: 0, sleep=lambda _: None)
                public = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
                from polymarket_bot.market_data.http import PublicGet
                adapter = PublicGet(public, monotonic=lambda: 0, sleep=lambda _: None,
                    utcnow=lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc))
                recorder = Recorder(store, adapter, monotonic_ns=lambda: 123,
                    wall_us=lambda: 0, sleep=lambda _: None, approximate_reference=session)
                def record(owner, config, **kwargs):
                    self.assertTrue(session._started)
                    self.assertEqual(session.sampling_origin_ns, 123)
                    owner.run_id, owner.finalized = '', 0
                    if fail:
                        raise OSError('fixture failure')
                    from polymarket_bot.recorder.models import RunResult
                    return RunResult('', 'STOPPED', 0, 0, 'fixture')
                try:
                    with patch.object(Recorder, '_record', record):
                        result = recorder._execute_run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: True)
                        self.assertEqual(result.exit_code, 1 if fail else 0)
                    self.assertFalse(session._started)
                    assert recorder.approximate_worker is not None
                    self.assertIsNone(recorder.approximate_worker._thread)
                finally:
                    public.close()
                    store.close()

    def test_armed_target_survives_housekeeping_at_target(self):
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture()
        original = Recorder._housekeeping
        armed = []
        def housekeeping(recorder, target_ns, **kwargs):
            original(recorder, target_ns, **kwargs)
            if not armed:
                armed.append(target_ns)
                fixture.advance((target_ns - fixture.monotonic_ns()) / 1_000_000_000)
        with patch.object(Recorder, '_housekeeping', housekeeping):
            result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        slots = fixture.rows("SELECT slot_index FROM sample_slots WHERE status='COMPLETE'")
        self.assertEqual([row['slot_index'] for row in slots], [151])

    def test_nominal_cadence_survives_anchor_shift_with_active_utc_guards(self):
        from dataclasses import replace
        from polymarket_bot.recorder.service import Recorder
        for shift in (-632_500, 632_500):
            with self.subTest(shift=shift):
                fixture = self.fixture()
                fixture.store.close()
                fixture.db.path = fixture.db.path.with_name('approximate.sqlite3')
                fixture.store = RecorderStore.open_approximate(fixture.db.path)
                original = Recorder._housekeeping
                planned = []
                changed = []
                def housekeeping(recorder, target_ns, **kwargs):
                    planned.append(target_ns)
                    original(recorder, target_ns, **kwargs)
                    if not changed and recorder.clock.anchor is not None:
                        changed.append(True)
                        recorder.clock.anchor = replace(recorder.clock.anchor,
                            server_us=recorder.clock.anchor.server_us + shift)
                from types import SimpleNamespace
                recorder = Recorder(fixture.store, fixture.public,
                    monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us,
                    sleep=fixture.sleep, approximate_reference=SimpleNamespace(fetch=lambda **kwargs: None))
                with patch.object(Recorder, '_housekeeping', housekeeping):
                    result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                        should_stop=lambda: fixture.book_count >= 3)
                self.assertEqual(result.exit_code, 0)
                sends = [row[2] for row in fixture.requests if row[1] == '/books']
                self.assertEqual(len(sends), 3)
                first = planned[0]
                self.assertGreaterEqual(sends[0], first)
                self.assertGreaterEqual(sends[1], first + 2_000_000_000)
                self.assertGreaterEqual(sends[2], first + 4_000_000_000)

    def test_real_research_nominal_grid_transport_guards_and_rollover(self, stop_case=None, fault=None):
        from dataclasses import replace
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.free_reference import ApproximateReferenceSession, CoinbaseRestProvider, KrakenRestProvider
        for shift in ((632_500,) if fault is not None else (-2_500_000, -632_500, 632_500, 2_500_000)):
            with self.subTest(shift=shift):
                fixture = self.fixture(offset_us=590_000_000)
                fixture.store.close()
                fixture.db.path = fixture.db.path.with_name('research.sqlite3')
                fixture.store = RecorderStore.open_approximate(fixture.db.path)
                sends, planned, calls = [], [], {}
                from threading import Event, Timer
                entered, release = Event(), Event()
                def respond(request):
                    if request.url.host not in ('api.coinbase.com', 'api.kraken.com'):
                        return fixture.respond(request)
                    if stop_case is not None and not entered.is_set():
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError('provider barrier timed out')
                    opportunity, identity, _ = recorder._research_pending
                    anchor = recorder.clock.anchor
                    assert anchor is not None
                    now = fixture.monotonic_ns()
                    opening = anchor.midpoint_ns + (identity['scheduled_us'] - anchor.server_us) * 1000
                    end = anchor.midpoint_ns + (identity['scheduled_us'] + 2_000_000 - anchor.server_us) * 1000
                    sends.append((now, identity['target_ns'], opening, end, opportunity))
                    calls[request.url.host] = calls.get(request.url.host, 0) + 1
                    if calls[request.url.host] == 1:
                        return httpx.Response(429, json={})
                    if request.url.host == 'api.kraken.com':
                        return httpx.Response(200, json={'error': [], 'result': {'BTC/USD': {'c': ['100'], 'b': ['99'], 'a': ['101']}}})
                    return httpx.Response(200, json={'price': '100', 'bid': '99', 'ask': '101',
                        'time': fixture.utcnow().isoformat()})
                client = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(client.close)
                providers = {name: cls(client, monotonic_ns=fixture.monotonic_ns,
                    wall_us=fixture.wall_us, sleep=lambda _: None) for name, cls in
                    (('coinbase', CoinbaseRestProvider), ('kraken', KrakenRestProvider))}
                session = ApproximateReferenceSession(providers=providers,
                    monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us, sleep=lambda _: None)
                recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                    wall_us=fixture.wall_us, sleep=fixture.sleep, approximate_reference=session)
                original_housekeeping = Recorder._housekeeping
                original_submit = Recorder._approximate_submit
                if fault in ('wait', 'reserve', 'drain'):
                    method = '_wait' if fault == 'wait' else '_approximate_drain'
                    target_object = fixture.store if fault == 'reserve' else recorder
                    method = 'reserve_slot' if fault == 'reserve' else method
                    operation = getattr(target_object, method)
                    injected = []
                    def expire(*args, **kwargs):
                        value = operation(*args, **kwargs)
                        if not injected and recorder.clock.anchor is not None:
                            injected.append(True)
                            fixture.advance(121)
                        return value
                    patcher = patch.object(target_object, method, side_effect=expire)
                    patcher.start()
                    self.addCleanup(patcher.stop)
                changed = []
                def housekeeping(owner, target_ns, **kwargs):
                    planned.append(target_ns)
                    original_housekeeping(owner, target_ns, **kwargs)
                    if fault is not None and not changed and fault not in ('wait', 'reserve', 'drain'):
                        changed.append(True)
                        if fault == 'metadata':
                            owner.revision = replace(owner.revision, capture_ready=False)
                        elif fault == 'identity':
                            owner.revision = None
                        elif fault == 'changed_market':
                            owner.revision = replace(owner.revision, market=replace(owner.revision.market, market_id='changed-market'))
                        elif fault == 'expiry':
                            fixture.advance(121)
                        elif fault == 'discontinuity':
                            owner.clock.anchor = replace(owner.clock.anchor, status='DISCONTINUITY')
                        return
                    if not changed and owner.clock.anchor is not None:
                        changed.append(True)
                        owner.clock.anchor = replace(owner.clock.anchor,
                            midpoint_ns=owner.clock.anchor.midpoint_ns - shift * 1000,
                            request_start_mono_ns=owner.clock.anchor.request_start_mono_ns - shift * 1000,
                            request_end_mono_ns=owner.clock.anchor.request_end_mono_ns - shift * 1000)
                        owner.anchor_id = owner.store.save_anchor(owner.run_id, owner.clock.anchor)
                        if owner.clock.anchor.request_end_mono_ns > fixture.monotonic_ns():
                            fixture.advance((owner.clock.anchor.request_end_mono_ns - fixture.monotonic_ns()) / 1e9)
                def submit(owner, deadline_ns, **identity):
                    accepted = original_submit(owner, deadline_ns, **identity)
                    if accepted:
                        if stop_case is not None:
                            self.assertTrue(entered.wait(2))
                            guard = fixture.public.before_attempt
                            def stop_guard():
                                timer = Timer(0.01, release.set)
                                timer.start()
                                self.addCleanup(timer.join)
                                if stop_case == 'interrupt':
                                    raise KeyboardInterrupt
                                from polymarket_bot.recorder.service import _StopRequested
                                raise _StopRequested
                            fixture.public.before_attempt = stop_guard
                        else:
                            owner.approximate_worker._thread.join(2)
                            self.assertFalse(owner.approximate_worker._thread.is_alive())
                    return accepted
                book_sends = []
                def book(request, payload, f):
                    if request.url.path == '/books':
                        identity = recorder._book_identity
                        anchor = recorder.clock.anchor
                        assert anchor is not None
                        book_sends.append((fixture.monotonic_ns(), identity['target_ns'],
                            anchor.midpoint_ns + (recorder._research_pending[1]['scheduled_us'] - anchor.server_us) * 1000,
                            anchor.midpoint_ns + (recorder._research_pending[1]['scheduled_us'] + 2_000_000 - anchor.server_us) * 1000))
                setattr(fixture, 'hook', book)
                no_target_patcher = None
                if fault == 'no_target':
                    from polymarket_bot.recorder import service
                    select = service.select_future_slot
                    def no_target(*args, **kwargs):
                        return replace(select(*args, **kwargs), target=None)
                    no_target_patcher = patch.object(service, 'select_future_slot', side_effect=no_target)
                    no_target_patcher.start()
                    self.addCleanup(no_target_patcher.stop)
                deadline = fixture.monotonic_ns() + 20_000_000_000
                with patch.object(Recorder, '_housekeeping', housekeeping), patch.object(Recorder, '_approximate_submit', submit):
                    result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                        should_stop=lambda: fixture.book_count >= 5 or (fault == 'changed_market' and bool(changed)), deadline_ns=deadline)
                if no_target_patcher is not None:
                    no_target_patcher.stop()
                self.assertEqual(result.exit_code, 2 if fault in ('reserve', 'drain') else 130 if stop_case == 'interrupt' else 0, result.reason)
                if fault in ('reserve', 'drain'):
                    self.assertEqual(result.reason, 'time_unavailable')
                if fault is not None:
                    events = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
                    submitted = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY' and row.get('disposition') == 'submitted']
                    terminal = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
                    self.assertEqual({row['opportunity_id'] for row in terminal},
                        {row['opportunity_id'] for row in events if row.get('type') == 'APPROXIMATE_FETCH'})
                    self.assertEqual(len(submitted), len(terminal))
                    if fault == 'metadata':
                        self.assertTrue(any(row.get('disposition') == 'unsubmitted' and row.get('reason') == 'METADATA_NOT_READY' for row in events))
                    if fault == 'changed_market':
                        self.assertFalse(any(row.get('market_id') == '1001' and row.get('disposition') == 'submitted' for row in events))
                    if fault == 'no_target':
                        self.assertFalse(submitted)
                        self.assertFalse(sends)
                        self.assertFalse(book_sends)
                    continue
                self.assertTrue(sends)
                if stop_case is not None:
                    self.assertTrue(fixture.store._closed)
                    assert recorder.approximate_worker is not None
                    self.assertIsNone(recorder.approximate_worker._thread)
                    events = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
                    terminal = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]['disposition'], 'completed')
                    client.close()
                    self.assertTrue(client.is_closed)
                    continue
                for now, nominal, opening, end, *_ in sends + book_sends:
                    self.assertGreaterEqual(now, nominal)
                    self.assertGreaterEqual(now, opening)
                    self.assertLess(now, end)
                    self.assertLess(now, deadline)
                events = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
                opportunities = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY']
                targets = [row['target_ns'] for row in opportunities]
                self.assertTrue(all(b - a == 2_000_000_000 for a, b in zip(targets, targets[1:])))
                self.assertEqual(len(targets), len(set(targets)))
                self.assertEqual(len({row['market_id'] for row in opportunities}), 2)
                self.assertEqual(len([row for row in events if row.get('type') == 'APPROXIMATE_SESSION']), 1)
                self.assertTrue(any(row['disposition'] == 'skipped' for row in opportunities))

    def test_actual_book_guard_stops_with_provider_transport_pending(self):
        for signal in ('stop', 'interrupt'):
            with self.subTest(signal=signal):
                self.test_real_research_nominal_grid_transport_guards_and_rollover(stop_case=signal)

    def test_research_housekeeping_faults_have_no_unowned_fetches(self):
        for fault in ('metadata', 'identity', 'changed_market', 'no_target', 'expiry', 'discontinuity', 'wait', 'reserve', 'drain'):
            with self.subTest(fault=fault):
                self.test_real_research_nominal_grid_transport_guards_and_rollover(fault=fault)

    def test_checkpoint_baseline_precedes_capture_and_shutdown_precedes_close(self):
        fixture = self.fixture()
        def before_capture(request,payload,f):
            if request.url.path=="/books":
                self.assertEqual(f.store._connection.execute("SELECT COUNT(*) FROM storage_checkpoints").fetchone()[0],1)
        fixture.hook = before_capture
        result = self.run_recorder(fixture,pairs=1)
        self.assertEqual(result.exit_code,0)
        checkpoints = fixture.rows("SELECT * FROM storage_checkpoints ORDER BY checkpoint_id")
        self.assertEqual(len(checkpoints),2)
        self.assertEqual([row["committed_slot_count"] for row in checkpoints],[0,152])
        self.assertEqual(checkpoints[-1]["db_file_bytes"],fixture.db.path.stat().st_size)

    def test_hourly_checkpoint_overhead_cannot_burst_or_change_cadence(self):
        fixture = self.fixture()
        def long_gap(request,payload,f):
            if request.url.path=="/books" and f.book_count==1:
                f.advance(3601)
        fixture.hook = long_gap
        from polymarket_bot.recorder import service
        self.assertTrue(hasattr(service,"take_checkpoint"),"Coordinator checkpoint integration is missing")
        real_checkpoint = service.take_checkpoint
        observed = []
        def slow_checkpoint(store,*,run_id,wall_us):
            self.assertFalse(store._connection.in_transaction)
            real_checkpoint(store,run_id=run_id,wall_us=wall_us)
            observed.append(fixture.monotonic_ns())
            if len(observed)==2:
                fixture.advance(3)
        with patch.object(service,"take_checkpoint",side_effect=slow_checkpoint):
            result = self.run_recorder(fixture,pairs=3)
        self.assertEqual(result.exit_code,0)
        checkpoints = fixture.rows("SELECT * FROM storage_checkpoints ORDER BY checkpoint_id")
        self.assertEqual(len(checkpoints),3)
        self.assertGreaterEqual(checkpoints[1]["observed_wall_us"]-checkpoints[0]["observed_wall_us"],3_600_000_000)
        books = [row[2] for row in fixture.requests if row[1]=="/books"]
        self.assertTrue(all(b-a>=2_000_000_000 for a,b in zip(books,books[1:])))
        config = json.loads(fixture.rows("SELECT config_json FROM recording_runs")[0]["config_json"])
        self.assertEqual(config["cadence_us"],2_000_000)

    def test_checkpoint_deferred_when_armed_target_has_no_budget(self):
        fixture = self.fixture()
        from polymarket_bot.recorder.service import Recorder
        recorder = Recorder(fixture.store,fixture.public,monotonic_ns=fixture.monotonic_ns,
                            wall_us=fixture.wall_us,sleep=fixture.sleep)
        self.assertTrue(hasattr(recorder,"_checkpoint_due"),"Budget-aware checkpoint scheduling is missing")
        recorder.run_id = fixture.store.start_run(RunConfig(),wall_us=fixture.wall_us(),app_revision="fixture")
        recorder.next_checkpoint_ns = fixture.monotonic_ns()
        recorder._checkpoint_due(fixture.monotonic_ns()+500_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM storage_checkpoints")),0)
        recorder._checkpoint_due(fixture.monotonic_ns()+2_000_000_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM storage_checkpoints")),1)

    def test_restart_keeps_each_run_startup_baseline(self):
        fixture = self.fixture()
        self.run_recorder(fixture,pairs=1)
        fixture.store = RecorderStore.open(fixture.db.path)
        fixture.advance(4)
        result = self.run_recorder(fixture,pairs=2)
        self.assertEqual(result.exit_code,0)
        rows = fixture.rows("SELECT run_id,COUNT(*) n,MIN(observed_wall_us) first FROM storage_checkpoints GROUP BY run_id")
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row["n"]==2 for row in rows))

    def fixture(self, **kwargs):
        fixture = RecorderFixture(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def run_recorder(self, fixture, *, pairs=2, should_stop=None, config=None):
        from polymarket_bot.recorder.service import Recorder
        return Recorder(fixture.store, fixture.public,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us,
            sleep=fixture.sleep).run(config or RunConfig(),
                should_stop=should_stop or (lambda: fixture.book_count >= pairs))

    def test_reserve_batch_atomic_finalize_and_future_grid(self):
        fixture = self.fixture()
        def inspect(request, payload, f):
            if request.url.path == "/books":
                row = f.store._connection.execute(
                    "SELECT status FROM sample_slots ORDER BY slot_index DESC LIMIT 1").fetchone()
                self.assertEqual(row[0], "IN_PROGRESS")
        fixture.hook = inspect
        result = self.run_recorder(fixture)
        self.assertEqual((result.state, result.exit_code, result.slots_finalized), ("STOPPED", 0, 2))
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([row["slot_index"] for row in slots], [151, 152])
        self.assertEqual([row["scheduled_us"] for row in slots], [NOW_US + 2_000_000, NOW_US + 4_000_000])
        self.assertEqual(len(fixture.rows("SELECT * FROM book_observations")), 4)
        attempts = fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH'")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({row["method"] for row in attempts}, {"POST"})
        self.assertEqual({row["path"] for row in attempts}, {"/books"})
        self.assertEqual([row["start_mono_ns"] for row in attempts],
                         [fixture.epoch_ns + 2_005_000_000, fixture.epoch_ns + 4_005_000_000])
        anchor = fixture.rows("SELECT * FROM clock_anchors")[0]
        self.assertEqual(anchor["request_start_mono_ns"], fixture.epoch_ns)
        self.assertEqual(anchor["anchor_mono_ns"], fixture.epoch_ns + 5_000_000)
        self.assertEqual(anchor["diagnostic_json"], None)
        self.assertTrue(all(row["status"] == "DISABLED" for row in fixture.rows("SELECT * FROM reference_associations")))

    def test_old_and_new_market_never_share_a_pair(self):
        fixture = self.fixture(offset_us=597_000_000)
        result = self.run_recorder(fixture)
        self.assertEqual(result.slots_finalized, 2)
        slots = fixture.rows("SELECT market_id,slot_index FROM sample_slots WHERE status='COMPLETE' ORDER BY scheduled_us")
        self.assertEqual([(row["market_id"], row["slot_index"]) for row in slots], [("1001", 449), ("1002", 1)])
        books = fixture.rows("""SELECT m.condition_id,b.expected_condition_id FROM book_observations b
            JOIN sample_slots s USING(slot_id) JOIN markets m USING(market_id)""")
        self.assertTrue(all(row["condition_id"] == row["expected_condition_id"] for row in books))
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "EXPIRED")
        for method, path, ns, timeout, body in fixture.requests:
            if path == "/books" and "9000000000000000000001" in body.decode():
                self.assertLess(fixture.initial_wall_us + (ns-fixture.epoch_ns)//1000, NOW_US+600_000_000)

    def test_rollover_gap_includes_successful_acquisition_and_retry_wait(self):
        from polymarket_bot.recorder.metrics import measure
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                fixture = self.fixture(offset_us=597_000_000)
                failures = []
                def outage(request, payload, f):
                    if (fail_first and f.book_count == 1 and request.url.path.startswith("/events/slug/")
                            and not failures):
                        failures.append(True)
                        f.advance(10.2)
                        return httpx.Response(503, stream=httpx.ByteStream(b"outage"))
                fixture.hook = outage
                result = self.run_recorder(fixture, pairs=2)
                self.assertEqual(result.exit_code, 0)
                old, new = fixture.rows("SELECT * FROM markets ORDER BY window_start_us")
                expected_us = new["first_seen_us"]-old["window_end_us"]
                self.assertEqual(expected_us, 12_230_000 if fail_first else 20_000)
                reader = RecorderStore.read_only(fixture.db.path)
                try:
                    report = measure(reader, start_us=fixture.initial_wall_us,
                                     end_us=fixture.wall_us()+1_000_000)
                finally:
                    reader.close()
                self.assertIsNone(report.diagnostics["quality_time_unavailable_reason"])
                self.assertEqual(report.rollover_gaps["count"], 1)
                self.assertAlmostEqual(report.rollover_gaps["duration_seconds"], expected_us/1_000_000)
                actual = [row for row in fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
                          if not json.loads(row["detail_json"]).get("diagnostic_only")]
                self.assertEqual(len(actual), 1)
                self.assertEqual((actual[0]["started_us"], actual[0]["ended_us"]),
                                 (old["window_end_us"], new["first_seen_us"]))

    def test_bad_numeric_payload_does_not_end_run(self):
        fixture = self.fixture()
        def malformed(request, payload, f):
            if request.url.path == "/books" and f.book_count == 1:
                for row in payload:
                    row["bids"][0]["size"] = "1e99999999999999999999999"
        fixture.hook = malformed
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 2))
        slots = fixture.rows("SELECT slot_index,status FROM sample_slots WHERE status!='MISSED' ORDER BY slot_index")
        self.assertEqual([(row["slot_index"], row["status"]) for row in slots], [(151, "FAILED"), (152, "COMPLETE")])
        self.assertEqual([row["status"] for row in fixture.rows("SELECT status FROM book_observations WHERE slot_id=(SELECT slot_id FROM sample_slots WHERE slot_index=151)")], ["INVALID", "INVALID"])

    def test_extreme_aware_gamma_timestamps_are_discovery_failures(self):
        for timestamp in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
            with self.subTest(timestamp=timestamp):
                fixture = self.fixture()
                def invalid(request, payload, f):
                    if request.url.path.startswith("/events/slug/"):
                        payload["markets"][0]["eventStartTime"] = timestamp
                    elif request.url.path == "/events":
                        payload[0]["markets"][0]["eventStartTime"] = timestamp
                fixture.hook = invalid
                result = self.run_recorder(fixture, should_stop=lambda: fixture.elapsed_ns > 1_000_000_000)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(fixture.book_count, 0)
                events = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
                self.assertTrue(events)
                self.assertIn("invalid_metadata", events[0]["detail_json"])

    def test_fractional_fallback_window_keeps_exact_slot_identity(self):
        fixture = self.fixture()
        from datetime import datetime, timedelta
        def fractional(request, payload, f):
            if request.url.path == "/events":
                payload[0]["slug"] = "fractional-event"
                rows = payload[0]["markets"]
            elif request.url.path.startswith("/markets/"):
                rows = [payload]
            else:
                return
            for row in rows:
                row["slug"] = "fractional-market"
                for field in ("eventStartTime", "endDate"):
                    row[field] = (datetime.fromisoformat(row[field]) + timedelta(microseconds=500_000)).isoformat()
        fixture.hook = fractional
        result = self.run_recorder(fixture, pairs=2, config=RunConfig(use_fast_path=False))
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 2))
        slots = fixture.rows("SELECT scheduled_us FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([row["scheduled_us"] for row in slots], [NOW_US+500_000, NOW_US+2_500_000])
        self.assertEqual(fixture.rows("SELECT window_start_us FROM markets")[0]["window_start_us"], NOW_US-299_500_000)

    def test_partial_outcome_survives_independently(self):
        fixture = self.fixture()
        def partial(request, payload, f):
            if request.url.path == "/books":
                payload[0]["bids"][0]["price"] = "NaN"
        fixture.hook = partial
        self.run_recorder(fixture, pairs=1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "PARTIAL")
        books = fixture.rows("SELECT status,best_bid_text FROM book_observations ORDER BY outcome")
        self.assertEqual([(row["status"], row["best_bid_text"]) for row in books], [("INVALID", None), ("VALID", "0.60")])

    def test_late_original_bytes_survive_expiry_and_new_condition(self):
        fixture = self.fixture(offset_us=597_000_000)
        raw = b"old condition response across expiry"
        class LateBody(httpx.SyncByteStream):
            def __iter__(self):
                yield raw
                fixture.advance(2.5)
        def late(request, payload, f):
            if request.url.path == "/books" and f.book_count == 1:
                return httpx.Response(200, stream=LateBody())
        fixture.hook = late
        self.run_recorder(fixture)
        old = fixture.rows("SELECT status FROM sample_slots WHERE market_id='1001' AND slot_index=449")[0]
        self.assertEqual(old["status"], "FAILED")
        self.assertEqual({row["status"] for row in fixture.rows("SELECT status FROM book_observations WHERE expected_condition_id=?", ("0x"+"ab"*32,))}, {"LATE"})
        attempt = fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH' ORDER BY request_id")[0]
        self.assertEqual(decode_evidence(attempt["response_raw_z"], attempt["raw_size_bytes"], attempt["codec_version"]), raw)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE market_id='1002' AND slot_index=1")[0]["status"], "COMPLETE")

    def test_discovery_gap_is_durable_and_retry_pauses_two_seconds(self):
        fixture = self.fixture()
        failures = []
        def gap(request, payload, f):
            if request.url.path.startswith("/events/slug/") and not failures:
                failures.append(f.monotonic_ns())
                f.advance(10.2)
                return httpx.Response(503, stream=httpx.ByteStream(b"outage"))
        fixture.hook = gap
        self.run_recorder(fixture, pairs=1)
        events = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertEqual(len(events), 1)
        self.assertIn("transport_error", events[0]["detail_json"])
        discovery = [row for row in fixture.requests if row[1].startswith("/events/slug/")]
        self.assertGreaterEqual((discovery[1][2]-discovery[0][2])/1e9, 12.2)
        self.assertTrue(all(value <= 10 for value in discovery[0][3].values()))
        self.assertEqual(fixture.rows("SELECT slot_index FROM sample_slots WHERE status='COMPLETE'")[0]["slot_index"], 157)

    def test_ambiguous_discovery_never_starts_books(self):
        fixture = self.fixture()
        def ambiguity(request, payload, f):
            if request.url.path.startswith("/events/slug/"):
                payload["markets"].append(dict(payload["markets"][0], id="1999"))
            elif request.url.path == "/events":
                payload[0]["markets"].append(dict(payload[0]["markets"][0], id="1999"))
        fixture.hook = ambiguity
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+5_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.book_count, 0)
        self.assertTrue(all("ambiguous_market" in row["detail_json"] for row in fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")))

    def test_clock_refresh_and_metadata_revision_used_by_later_slot(self):
        fixture = self.fixture()
        def changed(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["description"] = "rules observed after startup"
        fixture.hook = changed
        self.run_recorder(fixture, pairs=34)
        anchors = fixture.rows("SELECT * FROM clock_anchors")
        self.assertEqual(len(anchors), 2)
        self.assertGreaterEqual(anchors[1]["request_start_mono_ns"]-anchors[0]["anchor_mono_ns"], 60_000_000_000)
        revisions = fixture.rows("SELECT * FROM market_revisions ORDER BY revision_id")
        self.assertEqual(len(revisions), 2)
        last = fixture.rows("SELECT metadata_revision_id FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index DESC LIMIT 1")[0]
        self.assertEqual(last["metadata_revision_id"], revisions[-1]["revision_id"])

    def test_failed_metadata_refresh_throttles_and_retains_age(self):
        fixture = self.fixture()
        def failed(request, payload, f):
            if request.url.path == "/markets/1001":
                return httpx.Response(503, stream=httpx.ByteStream(b"metadata unavailable"))
        fixture.hook = failed
        self.run_recorder(fixture, pairs=36)
        refresh = [row for row in fixture.requests if row[1] == "/markets/1001"]
        self.assertEqual(len(refresh), 2)  # One logical refresh, one bounded retry.
        self.assertEqual(len(fixture.rows("SELECT * FROM market_revisions")), 1)
        events = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any("metadata_refresh_failed" in row["detail_json"] and "age_us" in row["detail_json"] for row in events))

    def test_not_ready_metadata_pauses_capture(self):
        fixture = self.fixture()
        def not_ready(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["enableOrderBook"] = False
        fixture.hook = not_ready
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+70_000_000)
        self.assertLess(fixture.book_count, 34)
        slots = fixture.rows("SELECT reason FROM sample_slots WHERE scheduled_us>=?", (NOW_US+64_000_000,))
        self.assertTrue(slots)
        self.assertTrue(all(row["reason"] == "METADATA_NOT_READY" for row in slots))

    def test_expired_anchor_stops_books_until_calibration_succeeds(self):
        fixture = self.fixture()
        def failed_clock(request, payload, f):
            if request.url.path == "/time" and f.elapsed_ns > 0:
                return httpx.Response(503, stream=httpx.ByteStream(b"time unavailable"))
        fixture.hook = failed_clock
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+130_000_000)
        books = [row for row in fixture.requests if row[1] == "/books"]
        self.assertTrue(books)
        self.assertTrue(all(row[2]-fixture.epoch_ns <= 120_005_000_000 for row in books))
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))

    def test_discontinuity_requires_pause_fresh_clock_and_market_validation(self):
        fixture = self.fixture()
        calls = []
        def shift(request, payload, f):
            if request.url.path == "/time":
                calls.append(f.monotonic_ns())
                if len(calls) >= 2:
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
        fixture.hook = shift
        self.run_recorder(fixture, pairs=34)
        self.assertEqual([row["status"] for row in fixture.rows("SELECT status FROM clock_anchors")], ["VALID", "DISCONTINUITY", "VALID"])
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))
        self.assertEqual(len([row for row in fixture.requests if row[1].startswith("/events/slug/")]), 2)
        complete = fixture.rows("SELECT slot_index FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual(len({row["slot_index"] for row in complete}), 34)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='CLOCK_PAUSE'"))

    def reopen(self, fixture, *, advance_us, epoch_ns=1007):
        fixture.store.close()
        fixture.initial_wall_us = fixture.wall_us() + advance_us
        fixture.elapsed_ns = 0
        fixture.epoch_ns = epoch_ns
        fixture.store = RecorderStore.open(fixture.db.path)
        fixture.book_count = 0
        fixture.requests.clear()
        fixture.hook = None
        fixture.sleep_hook = None

    def test_restart_preserves_original_quality(self):
        fixture = self.fixture()
        self.run_recorder(fixture)
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='COMPLETE'")
        books = fixture.rows("SELECT * FROM book_observations ORDER BY slot_id,outcome")
        references = fixture.rows("SELECT * FROM reference_associations")
        old_run = fixture.rows("SELECT * FROM recording_runs")[0]
        self.reopen(fixture, advance_us=20_000_000)
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.rows("SELECT * FROM sample_slots WHERE run_id=? AND status='COMPLETE'", (old_run["run_id"],)), slots)
        self.assertEqual(fixture.rows("SELECT * FROM book_observations WHERE slot_id IN (?,?) ORDER BY slot_id,outcome", tuple(row["slot_id"] for row in slots)), books)
        self.assertEqual(fixture.rows("SELECT * FROM reference_associations WHERE slot_id IN (?,?)", tuple(row["slot_id"] for row in slots)), references)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='DOWNTIME'"))
        new_anchor = fixture.rows("SELECT * FROM clock_anchors WHERE run_id=?", (result.run_id,))[0]
        self.assertEqual(new_anchor["request_start_mono_ns"], 1007)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='RESTART'"))

    def test_ctrl_c_after_reservation_persists_interrupted_and_closes(self):
        fixture = self.fixture()
        reserve = fixture.store.reserve_slot
        def interrupted(*args, **kwargs):
            reserve(*args, **kwargs)
            raise KeyboardInterrupt
        fixture.store.reserve_slot = interrupted
        try:
            result = self.run_recorder(fixture, pairs=1)
        except KeyboardInterrupt:
            self.fail("Coordinator leaked Ctrl+C instead of persisting interruption and closing.")
        self.assertEqual((result.exit_code, result.state), (130, "INTERRUPTED"))
        self.assertEqual(fixture.book_count, 0)
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='INTERRUPTED'")
        self.assertEqual(len(slots), 1)
        self.assertEqual({row["status"] for row in fixture.rows("SELECT * FROM book_observations")}, {"INTERRUPTED"})
        self.assertEqual(fixture.store._lock_file, None)

    def test_write_failure_stops_capture(self):
        fixture = self.fixture()
        fixture.store._connection.execute("""CREATE TRIGGER reject_book BEFORE INSERT ON book_observations
            BEGIN SELECT RAISE(FAIL, 'forced write failure'); END""")
        result = self.run_recorder(fixture, pairs=2)
        self.assertEqual((result.state, result.exit_code, result.slots_finalized), ("FAILED", 1, 0))
        self.assertEqual(result.reason, "storage_failure")
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.rows("SELECT * FROM book_observations"), [])
        self.assertEqual(fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH'"), [])
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "IN_PROGRESS")
        self.assertEqual(fixture.store._lock_file, None)

    def test_restart_recovers_reserved_slot_before_new_run(self):
        fixture = self.fixture()
        reserve = fixture.store.reserve_slot
        def abrupt(*args, **kwargs):
            reserve(*args, **kwargs)
            raise RuntimeError("abrupt simulated process loss")
        fixture.store.reserve_slot = abrupt
        with self.assertRaisesRegex(RuntimeError, "abrupt simulated process loss"):
            self.run_recorder(fixture)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertIsNone(fixture.public.on_attempt)
        self.assertIsNone(fixture.public.before_attempt)
        self.assertEqual(fixture.store._lock_file, None)
        self.reopen(fixture, advance_us=5_000_000)
        def inspect(request, payload, f):
            if request.url.path == "/time":
                self.assertEqual(f.store._connection.execute("SELECT status FROM sample_slots WHERE slot_index=151").fetchone()[0], "INTERRUPTED")
        fixture.hook = inspect
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        slot = fixture.rows("SELECT * FROM sample_slots WHERE slot_index=151")[0]
        self.assertIsNone(slot["finished_server_us"])
        self.assertIsNone(slot["end_mono_ns"])
        old_run = fixture.rows("SELECT * FROM recording_runs WHERE run_id=?", (slot["run_id"],))[0]
        self.assertEqual(old_run["state"], "INTERRUPTED")

    def test_known_expired_downtime_and_unknown_windows_are_disclosed(self):
        fixture = self.fixture()
        self.run_recorder(fixture, pairs=1)
        self.reopen(fixture, advance_us=1_800_000_000)
        self.run_recorder(fixture, pairs=1)
        known = fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001'")
        self.assertEqual(len(known), 450)
        self.assertEqual(known[-1]["reason"], "DOWNTIME")
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "EXPIRED")
        self.assertEqual({row["market_id"] for row in fixture.rows("SELECT market_id FROM markets")}, {"1001", "1003"})
        gaps = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any(json.loads(row["detail_json"]).get("unknown_windows") for row in gaps))

    def test_exhausted_housekeeping_allowance_preserves_due_queue_without_http(self):
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        original_due = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
        self.reopen(fixture, advance_us=31_000_000)
        original = Recorder._housekeeping
        stopped = []
        snapshots = []
        def spent(recorder, target_ns, **kwargs):
            fixture.advance(3)  # The armed future target is less than two seconds away.
            before_requests = len(fixture.requests)
            before = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
            original(recorder, target_ns, **kwargs)
            after = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
            snapshots.append((before_requests, len(fixture.requests), before, after))
            stopped.append(True)
        with patch.object(Recorder, "_housekeeping", spent):
            result = self.run_recorder(fixture, should_stop=lambda: bool(stopped))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][0], snapshots[0][1])
        self.assertEqual(snapshots[0][2], snapshots[0][3])
        self.assertEqual(snapshots[0][3], original_due)
        self.assertEqual(fixture.book_count, 0)

    def test_fatal_invariant_stops_before_further_http(self):
        fixture = self.fixture()
        def invalid(*args, **kwargs):
            raise RecorderStoreError("fatal_invariant", "injected writer invariant")
        fixture.store.finalize_slot = invalid
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.reason), (1, "fatal_invariant"))
        self.assertEqual(fixture.book_count, 1)

    def test_reference_worker_never_blocks_book_owner_or_writes_sqlite(self):
        import threading
        from polymarket_bot.recorder import service
        self.assertTrue(hasattr(service, 'ReferenceWorker'))
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return 'evidence'
        worker = service.ReferenceWorker()
        self.addCleanup(release.set)
        self.assertTrue(worker.submit(slow))
        self.assertTrue(entered.wait(1))
        self.assertIsNone(worker.take())
        self.assertFalse(worker.submit(slow))
        release.set()
        worker.stop(timeout=2)
        self.assertEqual(worker.take(), 'evidence')
        self.assertFalse(worker.submit(slow))

    def test_offline_reference_capture_keeps_books_and_unverified_evidence(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder import reference
        fixture = self.fixture()
        self.assertTrue(hasattr(reference, 'OfflineReferenceSession'))
        session = reference.OfflineReferenceSession(price_scale=8,
            decoder_revision='structural-v2', discovery_payload={
                'feedId': reference.CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=reference.ActivationEvidence(True, True, True, True, True, True),
            latest=lambda deadline: ({'feedId': reference.CHAINLINK_FEED_ID,
                'fullReport': str(fixture.wall_us()//1_000_000).encode().hex(), 'validFromTimestamp': fixture.wall_us()//1_000_000-60,
                'observationsTimestamp': fixture.wall_us()//1_000_000,
                'expiresAt': fixture.wall_us()//1_000_000+60, 'benchmarkPrice': '6000000000000'}),
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.slots_finalized, 2)
        self.assertEqual(fixture.rows("SELECT DISTINCT verification_status FROM reference_reports"),
                         [{'verification_status': 'UNVERIFIED'}])
        rows = fixture.rows("SELECT status,available_live FROM reference_associations WHERE role='SLOT_CURRENT'")
        self.assertTrue(rows)
        self.assertTrue(all(r['status']=='UNAVAILABLE' and r['available_live']==0 for r in rows))
        self.assertEqual(fixture.rows('PRAGMA user_version')[0]['user_version'], 1)
        feed = fixture.rows('SELECT price_scale,decoder_revision,verification_evidence_json FROM reference_feeds')[0]
        self.assertEqual(feed['price_scale'], 8)
        self.assertEqual(feed['decoder_revision'], 'structural-v2')
        self.assertIn('SYNTHETIC_OFFLINE_ONLY', feed['verification_evidence_json'])

    def test_slow_offline_fallback_does_not_drop_valid_book_capture(self):
        import threading
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture()
        entered, release = threading.Event(), threading.Event()
        def latest(deadline):
            entered.set()
            release.wait(2)
            return None
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=latest,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        def books(request, payload, f):
            if request.url.path == '/books':
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                release.set()
        fixture.hook = books
        self.addCleanup(release.set)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.slots_finalized, 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]['status'], 'COMPLETE')

    def test_offline_worker_joined_when_book_writer_fails(self):
        import threading
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        fixture.store.finalize_slot = lambda *a, **k: (_ for _ in ()).throw(RecorderStoreError('storage_failure', 'injected'))
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(recorder.reference_worker._stopped)
        self.assertFalse(recorder.reference_worker._thread and recorder.reference_worker._thread.is_alive())

    def test_malformed_offline_reference_and_missing_policy_preserve_books(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True),
            latest=lambda deadline: {'feedId': CHAINLINK_FEED_ID, 'fullReport': '616263',
                'validFromTimestamp': 1, 'observationsTimestamp': True, 'expiresAt': 3,
                'benchmarkPrice': 'NaN'}, boundary=lambda target, deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        with patch('os.getenv', side_effect=AssertionError('credential lookup')), \
             patch('hmac.new', side_effect=AssertionError('signing')), \
             patch('threading.Thread', wraps=__import__('threading').Thread):
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
            result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
        self.assertEqual(result.slots_finalized, 2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.rows('SELECT DISTINCT decode_status FROM reference_reports'), [{'decode_status': 'INVALID'}])
        self.assertTrue(fixture.rows("SELECT * FROM reference_associations WHERE role IN ('OPENING','CLOSING') AND status='PENDING'"))
        self.assertFalse(fixture.rows("SELECT * FROM reference_associations WHERE role IN ('OPENING','CLOSING') AND status='AVAILABLE'"))
        self.assertFalse(fixture.rows('SELECT * FROM reference_associations WHERE available_live=1'))

    def test_book_only_ignores_supplied_reference_session_without_threads(self):
        fixture = self.fixture()
        from polymarket_bot.recorder.service import Recorder
        with patch('threading.Thread', side_effect=AssertionError('reference thread')), \
             patch('os.getenv', side_effect=AssertionError('credential lookup')):
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=object())
            result = recorder.run(RunConfig(), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(recorder.reference_worker)

    def test_offline_session_rechecks_mutated_gate_before_startup(self):
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from polymarket_bot.recorder.service import Recorder
        from dataclasses import replace
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        session.evidence = replace(session.evidence, boundary_verified=False)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.rows('SELECT * FROM recording_runs'), [])

    def test_offline_gap_provider_persists_once_without_schema_change(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        session.gaps = lambda: [(fixture.epoch_ns/1e9, fixture.epoch_ns/1e9+0.1)]
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=3)
        gaps = [r for r in fixture.rows('SELECT detail_json FROM recorder_events') if 'REFERENCE_CONNECTION_GAP' in r['detail_json']]
        self.assertEqual(len(gaps), 1)

    def review_session(self, fixture, **providers):
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
        return OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': CHAINLINK_FEED_NAME,
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True),
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us, **providers)

    def test_boundary_contention_still_samples_current_buffer(self):
        import threading
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        entered, release = threading.Event(), threading.Event()
        def boundary(target, deadline):
            entered.set()
            release.wait(2)
            return None
        def live(age, now):
            return (str(int(now)).encode(), {'feedId': CHAINLINK_FEED_ID,
                'validFromTimestamp': int(now)-60, 'observationsTimestamp': int(now),
                'expiresAt': int(now)+60, 'benchmarkPrice': '100'})
        session = self.review_session(fixture, latest=lambda d: None, boundary=boundary, live=live)
        def book(request, payload, f):
            if request.url.path == '/books':
                self.assertTrue(entered.wait(1))
                release.set()
        fixture.hook = book
        self.addCleanup(release.set)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(fixture.rows("SELECT * FROM reference_fetches WHERE purpose='SLOT_CURRENT'"))

    def test_expected_provider_failures_are_durable_and_books_continue(self):
        from polymarket_bot.recorder.service import Recorder
        for error in (ConnectionError, TimeoutError, OSError):
            fixture = self.fixture()
            def fail(deadline):
                raise error('secret sentinel')
            session = self.review_session(fixture, latest=fail)
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
            try:
                result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
            except (ConnectionError, TimeoutError, OSError):
                self.fail('Expected provider failure escaped coordinator')
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(fixture.rows("SELECT * FROM reference_fetches WHERE error_code='reference_transport_failed'"))
            self.assertFalse(fixture.rows("SELECT * FROM reference_fetches WHERE error_code LIKE '%sentinel%'"))

    def test_shutdown_join_failure_returns_failed_result_and_retains_worker(self):
        from polymarket_bot.recorder.service import Recorder, ReferenceWorker
        from polymarket_bot.market_data.models import MarketDataError
        fixture = self.fixture()
        session = self.review_session(fixture, latest=lambda d: None)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        with patch.object(ReferenceWorker, 'stop', side_effect=MarketDataError('reference_shutdown_failed', 'join failed')):
            try:
                result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
            except MarketDataError:
                self.fail('Shutdown error escaped RunResult')
        self.assertEqual((result.state, result.reason), ('FAILED', 'reference_shutdown_failed'))
        self.assertIsNotNone(recorder.reference_worker)
        recorder.reference_worker.stop()

    def test_boundary_preparation_bounded_and_deadline_aware(self):
        from polymarket_bot.recorder.service import Recorder, ReferenceWorker
        from tests.recorder_support import seed_writer_market
        fixture = self.fixture()
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        recorder.reference_worker = ReferenceWorker()
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        seed_writer_market(fixture.store, recorder.run_id)
        recorder.offline_reference = self.review_session(fixture, latest=lambda d: None)
        recorder.clock.usable = lambda: True
        recorder._now = fixture.wall_us
        fixture.store.restart_context = lambda: self.fail('Entire market scan forbidden')
        recorder._reference_submit = lambda *a: False
        recorder._reference_housekeeping(fixture.monotonic_ns()+1_000_000_000)
        self.assertLessEqual(len(fixture.rows('SELECT * FROM reference_associations')), 2)
        before = len(fixture.rows('SELECT * FROM reference_associations'))
        recorder._reference_housekeeping(fixture.monotonic_ns())
        self.assertEqual(len(fixture.rows('SELECT * FROM reference_associations')), before)

    def test_real_ws_frame_arrival_preserved_at_later_sample(self):
        import json
        from polymarket_bot.recorder.chainlink_ws import ChainlinkLiveConsumer
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, ReferenceAssociationDraft
        from tests.test_schema_v2_decoder import build_schema_v2_report_payload
        fixture = self.fixture()
        now = fixture.wall_us()//1_000_000
        payload, signed = build_schema_v2_report_payload(valid_from=now-60, observations=now, expires=now+60)
        frame = json.dumps(payload, indent=2).encode()
        consumer = ChainlinkLiveConsumer('wss://ws.dataengine.chain.link', CHAINLINK_FEED_ID,
            'synthetic-id', 'synthetic-secret', clock_seconds=lambda: fixture.wall_us()/1_000_000,
            monotonic=lambda: fixture.monotonic_ns()/1_000_000_000)
        arrival_ns, arrival_wall = fixture.monotonic_ns(), fixture.wall_us()
        consumer._handle_websocket_message(frame)
        fixture.advance(1)
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)), discovery=lambda d: {},
            credentials=lambda: ('id','secret'), monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, decoder_revision='abi', authorization=__import__('tests.test_schema_v2_decoder', fromlist=['managed_authorization']).managed_authorization('abi'))
        session.consumer, session.price_scale, session.live = consumer, 8, consumer.get_freshest_report
        row = ReferenceAssociationDraft('test', 'SLOT_CURRENT', fixture.wall_us(), 'UNAVAILABLE',
            'NOT_FETCHED', False, fixture.wall_us(), fixture.wall_us())
        result = session.fetch(row, fixture.monotonic_ns()/1_000_000_000+2, run_id='test', anchor_id=None,
            project=lambda ns: arrival_wall+(ns-arrival_ns)//1000, buffer_only=True)
        self.assertEqual(result[1].raw_response, frame)
        self.assertEqual(result[1].attempt.ended_mono_ns, arrival_ns)
        self.assertEqual(result[2][0].first_received_wall_us, arrival_wall)
        self.assertEqual(result[2][0].first_received_server_us, arrival_wall)

    def test_slow_reference_writer_never_claims_live(self):
        from polymarket_bot.recorder.service import Recorder
        from tests.test_schema_v2_decoder import build_schema_v2_report_payload
        fixture = self.fixture()
        def live(age, now):
            payload, signed = build_schema_v2_report_payload(valid_from=int(now)-60,
                observations=int(now), expires=int(now)+60)
            return signed, payload['report']
        session = self.review_session(fixture, latest=lambda d: None, live=live)
        original = fixture.store.save_reference_evidence
        def slow(*args):
            result = original(*args)
            fixture.advance(3)
            return result
        fixture.store.save_reference_evidence = slow
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertFalse(fixture.rows('SELECT * FROM reference_associations WHERE available_live=1'))

    def test_startup_transport_failure_closes_store_and_sanitizes(self):
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture()
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda d: (_ for _ in ()).throw(ConnectionError('secret-startup')),
            credentials=lambda: self.fail('credentials'), monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, decoder_revision='abi', authorization=__import__('tests.test_schema_v2_decoder', fromlist=['managed_authorization']).managed_authorization('abi'))
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: False)
        self.assertEqual(result.reason, 'reference_transport_failed')
        reopened = RecorderStore.open(fixture.db.path)
        reopened.close()

    def test_reference_worker_error_returns_failed_and_closes(self):
        from polymarket_bot.recorder.service import Recorder, ReferenceWorker
        fixture = self.fixture()
        session = self.review_session(fixture, latest=lambda d: None)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        with patch.object(ReferenceWorker, 'take', side_effect=RuntimeError('synthetic worker failure')):
            result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.reason, 'reference_worker_failed')
        self.assertTrue(recorder.reference_worker._stopped)

    def test_managed_real_rest_fake_ws_capture_and_cleanup(self):
        import httpx
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.chainlink_rest import ChainlinkRestClient
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
        from polymarket_bot.recorder.service import Recorder
        from tests.test_schema_v2_decoder import build_schema_v2_report_payload
        fixture = self.fixture()
        calls = []
        class Live:
            def start(self): calls.append('start')
            def stop(self): calls.append('stop')
            def get_connection_gaps(self): return ()
            def get_freshest_report(self, age, now):
                payload, signed = build_schema_v2_report_payload(valid_from=int(now)-60,
                    observations=int(now), expires=int(now)+60, price_int=100)
                return signed, payload['report']
        def rest_factory(identity, secret):
            def handler(request):
                target = int(request.url.params.get('timestamp', str(fixture.wall_us()//1_000_000)))
                payload, signed = build_schema_v2_report_payload(valid_from=target,
                    observations=target+1, expires=target+60, price_int=100)
                return httpx.Response(200, json=payload)
            return ChainlinkRestClient('https://api.dataengine.chain.link', identity, secret,
                httpx.MockTransport(handler), monotonic=lambda: fixture.monotonic_ns()/1_000_000_000,
                clock_seconds=lambda: fixture.wall_us()/1_000_000)
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda d: {'feedId': CHAINLINK_FEED_ID, 'name': CHAINLINK_FEED_NAME,
                'status': 'active', 'schemaVersion': 2, 'decimals': 8},
            credentials=lambda: ('synthetic-id', 'synthetic-secret'), rest_factory=rest_factory,
            live_factory=lambda *a: Live(), monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, decoder_revision='abi-v2', authorization=__import__('tests.test_schema_v2_decoder', fromlist=['managed_authorization']).managed_authorization('abi-v2'))
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(calls, ['start', 'stop'])
        self.assertTrue(fixture.rows("SELECT * FROM reference_associations WHERE available_live=1"))
        self.assertEqual(fixture.rows('SELECT DISTINCT verification_status FROM reference_reports'),
            [{'verification_status': 'UNVERIFIED'}])
        self.assertIsNone(session.rest)
        self.assertIsNone(session.consumer)
        self.assertIsNotNone(fixture.rows('SELECT verified_us FROM reference_feeds')[0]['verified_us'])
        proofs = [__import__('json').loads(row['detail_json']) for row in fixture.rows(
            "SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%REFERENCE_DURABLE%'")]
        self.assertTrue(proofs)
        for proof in proofs:
            row = fixture.rows('SELECT target_us FROM reference_associations WHERE market_id=? AND role=? AND slot_id IS ? AND learned_wall_us=?',
                (proof['market_id'], proof['role'], proof['slot_id'], proof['learned_wall_us']))[0]
            self.assertEqual(proof.get('target_us'), row['target_us'])

    def test_stale_and_late_buffer_evidence_never_eligible(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        for offset, delay, reason in ((-10, 0, 'STALE_REFERENCE'), (0, 3, 'LATE_REFERENCE')):
            fixture = self.fixture()
            def live(age, now):
                from tests.test_schema_v2_decoder import build_schema_v2_report_payload
                payload, signed = build_schema_v2_report_payload(valid_from=int(now)-60,
                    observations=int(now)+offset, expires=int(now)+60, price_int=100)
                return signed, payload['report']
            session = self.review_session(fixture, latest=lambda d: None, live=live)
            def slow(request, payload, f):
                if request.url.path == '/books':
                    f.advance(delay)
            fixture.hook = slow
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
            recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
            self.assertTrue(fixture.rows('SELECT * FROM reference_associations WHERE reason=?', (reason,)))
            self.assertFalse(fixture.rows('SELECT * FROM reference_associations WHERE available_live=1'))
            reopened = RecorderStore.open(fixture.db.path)
            reopened.recover_interrupted(wall_us=fixture.wall_us())
            self.assertFalse(reopened._rows("SELECT * FROM sample_slots WHERE status='IN_PROGRESS'"))
            reopened.close()

    def test_live_accessor_failure_and_delay_preserve_committed_books(self):
        from polymarket_bot.recorder.service import Recorder
        for delay in (False, True):
            fixture = self.fixture()
            def live(age, now):
                self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE status='COMPLETE'"))
                if delay:
                    fixture.advance(3)
                    return None
                raise ConnectionError('secret sentinel')
            session = self.review_session(fixture, latest=lambda d: None, live=live)
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
            result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
            self.assertEqual((result.state, result.slots_finalized), ('STOPPED', 1))
            self.assertTrue(fixture.rows("SELECT * FROM reference_associations WHERE reason=?",
                ('LATE_REFERENCE' if delay else 'reference_transport_failed',)))

    def test_boundary_preparation_cursor_visits_history_once_per_run(self):
        from polymarket_bot.recorder.service import Recorder, ReferenceWorker
        from tests.recorder_support import seed_writer_market
        fixture = self.fixture()
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        recorder.reference_worker = ReferenceWorker()
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        seed_writer_market(fixture.store, recorder.run_id)
        with fixture.store._connection:
            for index in range(100):
                fixture.store._connection.execute("""INSERT INTO markets SELECT ?,event_id,?,slug,question,
                    window_start_us,window_end_us,first_seen_us,up_token_id,down_token_id,
                    up_outcome_index,down_outcome_index,lifecycle,resolution_state,
                    resolution_next_due_us,resolution_attempts,selected_resolution_id FROM markets WHERE market_id='1001'""",
                    (f'history-{index:03}', f'condition-{index}'))
        recorder.offline_reference = self.review_session(fixture, latest=lambda d: None)
        recorder.clock.usable = lambda: True
        recorder._now = fixture.wall_us
        recorder._reference_submit = lambda *a: False
        traced = []
        fixture.store._connection.set_trace_callback(traced.append)
        for _ in range(60):
            recorder._reference_housekeeping(fixture.monotonic_ns()+1_000_000_000)
        cursor_queries = [q for q in traced if 'FROM markets' in q and 'market_id>' in q]
        self.assertTrue(cursor_queries)
        self.assertFalse(any('NOT EXISTS' in q and 'FROM markets' in q for q in traced))
        self.assertEqual(len(fixture.rows('SELECT * FROM reference_associations')), 202)
        self.assertEqual(recorder.reference_plan_cursor, 'history-099')

    def test_reference_required_refused_before_startup(self):
        fixture = self.fixture()
        result = self.run_recorder(fixture, config=RunConfig(mode="REFERENCE_REQUIRED"))
        self.assertEqual((result.exit_code, result.reason), (2, "reference_runtime_unavailable"))
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.rows("SELECT * FROM recording_runs"), [])

    def test_due_resolution_runs_in_same_writer_before_future_capture(self):
        fixture = self.fixture(offset_us=597_000_000)
        result = self.run_recorder(fixture)
        self.assertEqual(result.exit_code, 0)
        attempts = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts[0]["method"], attempts[0]["path"]), ("GET", "/v2/resolutions"))
        observation = fixture.rows("SELECT * FROM resolution_observations")[0]
        self.assertEqual(observation["classification"], "ABSENT")
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual((old["resolution_state"], old["resolution_attempts"]), ("RETRY_WAIT", 1))
        self.assertIsNotNone(observation["learned_server_us"])
        self.assertEqual(observation["learned_wall_us"], attempts[0]["ended_wall_us"])
        anchor = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id DESC")[0]
        self.assertEqual(observation["learned_server_us"], anchor["anchor_server_us"]+
            (attempts[0]["end_mono_ns"]-anchor["anchor_mono_ns"])//1000)
        self.assertEqual(old["resolution_next_due_us"], observation["learned_server_us"]+30_000_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM recording_runs")), 1)

    def test_resolution_delay_retains_armed_target_and_does_not_redispatch_due_job(self):
        fixture = self.fixture(offset_us=597_000_000)
        def slow(request, payload, f):
            if request.url.path == "/v2/resolutions":
                f.advance(3.2)
                return httpx.Response(503, stream=httpx.ByteStream(b"late resolution"))
        fixture.hook = slow
        self.run_recorder(fixture, pairs=3)
        calls = [row for row in fixture.requests if row[1] == "/v2/resolutions"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(0 < value < 2 for value in calls[0][3].values()))
        self.assertEqual(fixture.rows("SELECT reason FROM sample_slots WHERE market_id='1002' AND slot_index=1")[0]["reason"], "BOOKS_CAPTURED")
        self.assertEqual([row["slot_index"] for row in fixture.rows("SELECT slot_index FROM sample_slots WHERE market_id='1002' AND status='COMPLETE' ORDER BY slot_index")], [1, 2])
        self.assertEqual(fixture.rows("SELECT resolution_attempts FROM markets WHERE market_id='1001'")[0]["resolution_attempts"], 1)

    def test_resolution_backward_wall_step_does_not_stop_book_capture(self):
        fixture = self.fixture(offset_us=597_000_000)
        def wall_step(request, payload, f):
            if request.url.path == "/v2/resolutions":
                f.initial_wall_us -= 1_000_000
        fixture.hook = wall_step
        result = self.run_recorder(fixture, pairs=3)
        self.assertEqual((result.exit_code,result.slots_finalized),(0,3))
        request = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")[0]
        self.assertLess(request["ended_wall_us"],request["started_wall_us"])
        self.assertGreater(request["latency_ns"],0)
        evidence = fixture.rows("SELECT provenance_json FROM resolution_observations")[0]
        self.assertTrue(json.loads(evidence["provenance_json"])["wall_clock_movement"]["request_backward"])
        self.assertEqual(fixture.rows("SELECT COUNT(*) n FROM sample_slots WHERE status='COMPLETE'")[0]["n"],3)

    def test_invalid_numeric_metadata_pauses_until_valid_revision(self):
        fixture = self.fixture()
        calls = []
        def invalid_first(request, payload, f):
            if request.url.path.startswith("/clob-markets/"):
                calls.append(True)
                payload["mts"] = "NaN" if len(calls)==1 else "1e-2"
        fixture.hook = invalid_first
        result = self.run_recorder(fixture,pairs=1)
        self.assertEqual(result.exit_code,0)
        first_book = next(row for row in fixture.requests if row[1]=="/books")
        self.assertGreaterEqual(first_book[2]-fixture.epoch_ns,60_000_000_000)
        revisions = fixture.rows("SELECT tick_size_text FROM market_revisions ORDER BY revision_id")
        self.assertEqual([row["tick_size_text"] for row in revisions],[None,"0.01"])
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='METADATA_NOT_READY'"))

    def test_metadata_overrun_skips_passed_slots_and_clamps_http(self):
        fixture = self.fixture()
        def slow(request, payload, f):
            if request.url.path == "/markets/1001":
                f.advance(5)
        fixture.hook = slow
        self.run_recorder(fixture, pairs=34)
        calls = [row for row in fixture.requests if row[1] == "/markets/1001"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(0 < value < 2 for value in calls[0][3].values()))
        skipped = fixture.rows("SELECT slot_index,reason FROM sample_slots WHERE slot_index IN (182,183)")
        self.assertEqual([(row["slot_index"], row["reason"]) for row in skipped], [(182, "PROCESSING_OVERRUN"), (183, "PROCESSING_OVERRUN")])

    def test_identity_conflict_pauses_and_retains_expiry_resolution_queue(self):
        fixture = self.fixture()
        def conflict(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["conditionId"] = "0x"+"ff"*32
        fixture.hook = conflict
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+605_000_000)
        self.assertEqual(result.exit_code, 0)
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "INVALIDATED")
        self.assertEqual(old["resolution_attempts"], 1)
        conflicts = fixture.rows("SELECT * FROM recorder_events WHERE kind='IDENTITY_CONFLICT'")
        self.assertTrue(conflicts)
        self.assertTrue(all(row["started_us"] >= NOW_US+60_000_000 for row in conflicts))
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        old_books = fixture.rows("SELECT s.scheduled_us FROM sample_slots s JOIN book_observations b USING(slot_id) WHERE s.market_id='1001'")
        self.assertTrue(all(row["scheduled_us"] < NOW_US+64_000_000 for row in old_books))

    def test_book_only_never_looks_up_credentials_or_uses_external_attempt_callback(self):
        fixture = self.fixture()
        def forbidden(attempt):
            self.fail("Caller callback must not run inside coordinator HTTP operations.")
        fixture.public.on_attempt = forbidden
        with patch("os.getenv", side_effect=AssertionError("credential environment access")), \
             patch("pathlib.Path.read_text", side_effect=AssertionError("credential file access")):
            result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        self.assertIs(fixture.public.on_attempt, forbidden)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertTrue(all(row[1] in ("/books", "/time") or row[1].startswith(("/events/slug/", "/clob-markets/")) for row in fixture.requests))

    def test_stop_after_completed_http_starts_no_fallback_or_retry(self):
        fixture = self.fixture()
        stopping = []
        def stopped(request, payload, f):
            if request.url.path == "/books":
                stopping.append(True)
                return httpx.Response(404, stream=httpx.ByteStream(b"route missing"))
        fixture.hook = stopped
        self.run_recorder(fixture, should_stop=lambda: bool(stopping))
        self.assertEqual(len([row for row in fixture.requests if row[1] in ("/books", "/book")]), 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "INTERRUPTED")

    def test_ctrl_c_during_http_stops_and_restores_transport_state(self):
        fixture = self.fixture()
        def interrupted(request, payload, f):
            if request.url.path == "/books":
                raise KeyboardInterrupt
        fixture.hook = interrupted
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.slots_finalized), (130, 0))
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertIsNone(fixture.public.on_attempt)
        self.assertEqual(fixture.store._lock_file, None)

    def test_stop_after_transient_failure_starts_no_retry(self):
        fixture = self.fixture()
        stopping = []
        def stopped(request, payload, f):
            if request.url.path == "/books":
                stopping.append(True)
                return httpx.Response(503, stream=httpx.ByteStream(b"busy"))
        fixture.hook = stopped
        result = self.run_recorder(fixture, should_stop=lambda: bool(stopping))
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "INTERRUPTED")

    def test_anchor_expiring_during_armed_wait_pauses_without_request(self):
        fixture = self.fixture(offset_us=1_000_000)
        def failed_clock(request, payload, f):
            if request.url.path == "/time" and f.elapsed_ns:
                return httpx.Response(503, stream=httpx.ByteStream(b"time unavailable"))
        fixture.hook = failed_clock
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+130_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))
        self.assertTrue(all(row[2]-fixture.epoch_ns <= 120_005_000_000 for row in fixture.requests if row[1] == "/books"))

    def test_discontinuity_across_expiry_materializes_old_coverage_and_queue(self):
        fixture = self.fixture(offset_us=536_000_000)
        calls = []
        def shifted(request, payload, f):
            if request.url.path == "/time":
                calls.append(True)
                if len(calls) == 2:
                    f.server_shift_us = 20_000_000
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
        fixture.hook = shifted
        self.run_recorder(fixture, pairs=34)
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "EXPIRED")
        self.assertEqual(len(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001'")), 450)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001' AND reason='CLOCK_PAUSE'"))
        self.assertIsNotNone(old["resolution_next_due_us"])

    def test_restart_of_invalidated_current_market_keeps_pause_until_rollover(self):
        fixture = self.fixture()
        def conflict(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["conditionId"] = "0x"+"ff"*32
        fixture.hook = conflict
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+65_000_000)
        self.reopen(fixture, advance_us=530_000_000)
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+605_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "INVALIDATED")
        self.assertTrue(all(b"9000000000000000000001" not in row[4] for row in fixture.requests if row[1] == "/books"))

    def test_before_rollover_clock_calibration_uses_remaining_allowance(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        anchors = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id")
        self.assertEqual(len(anchors), 2)
        time_calls = [row for row in fixture.requests if row[1] == "/time"]
        self.assertTrue(all(0 < value < 2 for value in time_calls[1][3].values()))

    def test_close_failure_returns_nonzero_after_capture_stops(self):
        fixture = self.fixture()
        close = fixture.store.close
        calls = []
        def failure():
            close()
            if not calls:
                calls.append(True)
                raise sqlite3.OperationalError("close failed")
        fixture.store.close = failure
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual((result.state, result.exit_code, result.reason), ("FAILED", 1, "storage_failure"))
        self.assertEqual(fixture.book_count, 1)

    def test_armed_wake_lateness_and_expired_intervals_do_not_burst(self):
        fixture = self.fixture()
        wakes = []
        def delayed(f):
            if not wakes:
                f.advance(0.4)
            elif len(wakes) == 1:
                f.advance(4.1)
            wakes.append(True)
        fixture.sleep_hook = delayed
        self.run_recorder(fixture)
        captured = fixture.rows("SELECT slot_index,delay_us FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([(row["slot_index"], row["delay_us"]) for row in captured], [(151, 400_000), (155, 0)])
        missed = fixture.rows("SELECT status FROM sample_slots WHERE slot_index IN (152,153,154)")
        self.assertEqual([row["status"] for row in missed], ["MISSED"]*3)

    def test_restart_without_known_identity_discloses_unknown_downtime(self):
        fixture = self.fixture()
        def unavailable(request, payload, f):
            if request.url.path.startswith("/events/slug/"):
                return httpx.Response(503, stream=httpx.ByteStream(b"unknown market"))
        fixture.hook = unavailable
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+3_000_000)
        self.assertEqual(fixture.rows("SELECT * FROM markets"), [])
        self.reopen(fixture, advance_us=1_800_000_000)
        self.run_recorder(fixture, pairs=1)
        gaps = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any(json.loads(row["detail_json"]).get("unknown_windows") for row in gaps))

    def test_discovery_gap_after_forward_clock_shift_uses_server_domain(self):
        fixture = self.fixture()
        calls = []
        def shifted_outage(request, payload, f):
            if request.url.path == "/time":
                calls.append(True)
                if len(calls) == 2:
                    f.server_shift_us = 20_000_000
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
            elif request.url.path.startswith("/events/slug/") and len(calls) >= 2:
                return httpx.Response(503, stream=httpx.ByteStream(b"discovery outage after shift"))
        fixture.hook = shifted_outage
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+70_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(len(calls), 3, "Discontinuity requires prompt fresh calibration.")
        gaps = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(gaps)
        self.assertTrue(any(row["ended_us"] is None for row in gaps),
                        "Without validated reacquisition the durable acquisition gap remains open.")
        self.assertTrue(all(row["ended_us"] is None or row["ended_us"] >= row["started_us"] for row in gaps))
        self.assertTrue(all(row["ended_us"] is None or row["ended_us"] >= NOW_US+80_000_000 for row in gaps))

    def test_resolution_server_due_and_receipt_backoff_survive_offset_restart(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture, pairs=1)
        self.reopen(fixture, advance_us=0)
        fixture.server_shift_us = 300_000_000
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        original = fixture.rows("SELECT * FROM resolution_observations")
        self.assertEqual(len(original), 1, "Expired server-domain work is already due despite local wall lag.")
        observation = original[0]
        attempt = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")[0]
        self.assertEqual(observation["learned_wall_us"], attempt["ended_wall_us"])
        self.assertGreater(observation["learned_server_us"]-observation["learned_wall_us"], 299_000_000)
        queue = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(queue, {"resolution_next_due_us": observation["learned_server_us"]+30_000_000,
                                 "resolution_attempts": 1})
        self.reopen(fixture, advance_us=0)
        self.run_recorder(fixture, pairs=1)
        self.assertEqual(fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0], queue)
        self.assertFalse([row for row in fixture.requests if row[1] == "/v2/resolutions"])
        self.reopen(fixture, advance_us=31_000_000)
        def failed_resolution(request, payload, f):
            if request.url.path == "/v2/resolutions":
                return httpx.Response(503, stream=httpx.ByteStream(b"resolution unavailable"))
        fixture.hook = failed_resolution
        self.assertEqual(self.run_recorder(fixture, pairs=1).exit_code, 0)
        observations = fixture.rows("SELECT * FROM resolution_observations ORDER BY resolution_id")
        self.assertEqual(observations[:1], original)
        self.assertEqual(observations[1]["classification"], "INVALID")
        self.assertGreater(observations[1]["learned_server_us"], queue["resolution_next_due_us"])
        retried = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(retried, {"resolution_next_due_us": observations[1]["learned_server_us"]+60_000_000,
                                   "resolution_attempts": 2})

    def test_expired_resolution_progresses_during_outage_and_ambiguity(self):
        for mode in ("outage", "ambiguity"):
            with self.subTest(mode=mode):
                fixture = self.fixture(offset_us=597_000_000)
                self.run_recorder(fixture, pairs=1)
                self.reopen(fixture, advance_us=10_000_000)
                started_wall = fixture.wall_us()
                def gap(request, payload, f):
                    if request.url.path.startswith("/events"):
                        if mode == "outage":
                            return httpx.Response(503, stream=httpx.ByteStream(b"Gamma unavailable"))
                        events = payload if type(payload) is list else [payload]
                        for event in events:
                            event["markets"].append(dict(event["markets"][0], id="1999"))
                fixture.hook = gap
                result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= started_wall+5_000_000)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(fixture.book_count, 0)
                self.assertEqual(fixture.rows("SELECT * FROM market_revisions WHERE run_id=?", (result.run_id,)), [])
                observations = fixture.rows("SELECT classification FROM resolution_observations")
                self.assertEqual(observations, [{"classification": "ABSENT"}])
                requests = [row for row in fixture.requests if row[1] == "/v2/resolutions"]
                self.assertEqual(len(requests), 1)
                self.assertTrue(all(0 < value <= 2 for value in requests[0][3].values()))
                discovery = [row for row in fixture.requests if row[1].startswith("/events/slug/")]
                self.assertGreaterEqual((discovery[-1][2]-discovery[0][2])/1e9, 2)

    def test_first_identity_conflict_on_restart_preserves_pin_and_rolls_over(self):
        fixture = self.fixture()
        self.run_recorder(fixture, pairs=1)
        original = fixture.rows("SELECT up_token_id,down_token_id,condition_id FROM markets WHERE market_id='1001'")[0]
        self.reopen(fixture, advance_us=590_000_000)
        def conflict(request, payload, f):
            if f.market_row()["id"] != "1001":
                return
            if request.url.path.startswith("/events/slug/"):
                row = payload["markets"][0]
                row["clobTokenIds"] = json.dumps([str(int(token)+100) for token in json.loads(row["clobTokenIds"])])
            elif request.url.path.startswith("/clob-markets/"):
                for row in payload["t"]:
                    row["t"] = str(int(row["t"])+100)
        fixture.hook = conflict
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.rows("SELECT up_token_id,down_token_id,condition_id FROM markets WHERE market_id='1001'")[0], original)
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "INVALIDATED")
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='IDENTITY_CONFLICT' AND run_id=?", (result.run_id,)))
        revision = fixture.rows("SELECT * FROM market_revisions WHERE market_id='1001' ORDER BY revision_id DESC")[0]
        raw = decode_evidence(revision["gamma_raw_z"], json.loads(revision["raw_sizes_json"])["gamma"], revision["codec_version"])
        self.assertIn(str(int(original["up_token_id"])+100).encode(), raw)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        self.assertFalse([row for row in fixture.requests if row[1] == "/books" and original["up_token_id"].encode() in row[4]])
        self.assertTrue(fixture.rows("SELECT * FROM resolution_observations WHERE market_id='1001'"))

    def test_revalidation_gap_does_not_starve_unrelated_expired_queue(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        self.reopen(fixture, advance_us=60_000_000)
        started_wall = fixture.wall_us()
        conflict_seen = []
        def gap_after_conflict(request, payload, f):
            if request.url.path == "/markets/1002":
                payload["conditionId"] = "0x"+"ff"*32
                conflict_seen.append(f.monotonic_ns())
            elif request.url.path.startswith("/events") and conflict_seen:
                return httpx.Response(503, stream=httpx.ByteStream(b"revalidation unavailable"))
        fixture.hook = gap_after_conflict
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= started_wall+70_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(conflict_seen)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP' AND run_id=?", (result.run_id,)))
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["resolution_attempts"], 3,
                         "Revalidation must spend remaining allowance on independent old due work.")
        self.assertFalse([row for row in fixture.requests if row[1] == "/books" and row[2] >= conflict_seen[0]])

    def test_metadata_overrun_past_anchor_expiry_pauses_and_recalibrates(self):
        fixture = self.fixture()
        def blocking_metadata(request, payload, f):
            if request.url.path == "/time" and 0 < f.elapsed_ns < 126_000_000_000:
                return httpx.Response(503, stream=httpx.ByteStream(b"clock unavailable"))
            if request.url.path == "/markets/1001" and f.elapsed_ns < 126_000_000_000:
                f.advance(65)
        fixture.hook = blocking_metadata
        result = self.run_recorder(fixture, should_stop=lambda: fixture.elapsed_ns >= 132_000_000_000)
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.rows("SELECT state FROM recording_runs WHERE run_id=?", (result.run_id,))[0]["state"], "STOPPED")
        pauses = fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'")
        self.assertTrue(pauses)
        self.assertTrue(any(json.loads(row["detail_json"]).get("time_domain") == "LOCAL_WALL" for row in pauses))
        anchors = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id")
        self.assertGreaterEqual(len(anchors), 2)
        fresh_ns = anchors[-1]["request_end_mono_ns"]
        books = [row for row in fixture.requests if row[1] == "/books"]
        self.assertTrue(any(row[2] >= fresh_ns for row in books))
        self.assertFalse([row for row in books if 120_005_000_000 < row[2]-fixture.epoch_ns < fresh_ns-fixture.epoch_ns])
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='CLOCK_PAUSE'"))


if __name__ == "__main__":
    unittest.main()
