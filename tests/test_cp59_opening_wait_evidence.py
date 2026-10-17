import json
from pathlib import Path
import sys
import unittest
from typing import Any, cast

from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.store import RecorderStoreError
from tests.recorder_support import RecorderFixture

_scripts = str(Path(__file__).resolve().parents[1] / 'scripts')
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

BOUNDARY = 1792228500


class OpeningWaitEvidenceTests(unittest.TestCase):
    """The aligned-child OPENING_WAIT producer must persist canonical evidence."""

    def fixture(self):
        fixture = RecorderFixture()
        self.addCleanup(fixture.close)
        return fixture

    def run_opening_wait(self, evidence):
        from cp5_run import CP5Recorder
        fixture = self.fixture()
        recorder = cast(Any, CP5Recorder(fixture.store, fixture.public,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.opening_wait_evidence = evidence
        recorder.intended_boundary_us = evidence['intended_boundary'] * 1_000_000
        result = recorder.run(RunConfig(), should_stop=lambda: True)
        return fixture, result

    def opening_events(self, fixture):
        details = [json.loads(row['detail_json'])
                   for row in fixture.rows('SELECT detail_json FROM recorder_events')]
        return [detail for detail in details if detail.get('type') == 'OPENING_WAIT']

    def test_aligned_child_producer_evidence_persists_canonically(self):
        from cp5_run import opening_wait_evidence
        wait_started = 1792228386.607
        wait_ended = 1792228440.006
        wait_end_ns = 162794468000000
        evidence = opening_wait_evidence(BOUNDARY, wait_started=wait_started,
            wait_ended=wait_ended, wait_end_ns=wait_end_ns)
        fixture, result = self.run_opening_wait(evidence)
        self.assertEqual((result.exit_code, result.reason), (0, 'STOP_REQUESTED'))
        events = self.opening_events(fixture)
        self.assertEqual(len(events), 1)
        detail = events[0]
        self.assertEqual(detail['intended_boundary'], BOUNDARY)
        self.assertEqual(detail['wait_end_ns'], wait_end_ns)
        self.assertEqual(detail['wait_started_wall_us'], round(wait_started * 1_000_000))
        self.assertEqual(detail['wait_ended_wall_us'], round(wait_ended * 1_000_000))
        for field in ('intended_boundary', 'wait_started_wall_us', 'wait_ended_wall_us', 'wait_end_ns'):
            self.assertIs(type(detail[field]), int)
        self.assertNotIn('wait_started', detail)
        self.assertNotIn('wait_ended', detail)

    def test_raw_float_opening_wait_evidence_still_fails_closed(self):
        evidence = dict(intended_boundary=BOUNDARY, wait_started=1792228386.607,
            wait_ended=1792228440.006, wait_end_ns=162794468000000)
        fixture, result = self.run_opening_wait(evidence)
        self.assertEqual((result.exit_code, result.reason), (1, 'fatal_invariant'))
        self.assertEqual(self.opening_events(fixture), [])

    def test_producer_evidence_is_deterministic_and_pure(self):
        from cp5_run import opening_wait_evidence
        first = opening_wait_evidence(BOUNDARY, wait_started=1792228386.6070957,
            wait_ended=1792228440.0059602, wait_end_ns=162794468000000)
        second = opening_wait_evidence(BOUNDARY, wait_started=1792228386.6070957,
            wait_ended=1792228440.0059602, wait_end_ns=162794468000000)
        self.assertEqual(first, second)
        self.assertEqual(first['wait_started_wall_us'], 1792228386607096)
        self.assertEqual(first['wait_ended_wall_us'], 1792228440005960)

    def test_aligned_wait_delay_matches_b_minus_60_schedule(self):
        from cp5_run import aligned_wait_delay
        self.assertEqual(aligned_wait_delay(BOUNDARY, BOUNDARY - 60), 0)
        self.assertAlmostEqual(aligned_wait_delay(BOUNDARY, BOUNDARY - 60 - 53.393), 53.393, places=3)
        self.assertEqual(aligned_wait_delay(BOUNDARY, BOUNDARY - 60 - 900), 900)

    def test_alignment_window_rejection_unchanged(self):
        from cp5_run import aligned_wait_delay
        for now in (BOUNDARY - 60 + 0.001, BOUNDARY - 60 - 901):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    aligned_wait_delay(BOUNDARY, now)

    def test_late_admission_rejection_unchanged(self):
        from cp5_run import require_admission
        require_admission(BOUNDARY, BOUNDARY - 23)
        for now in (BOUNDARY - 22, BOUNDARY - 21):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    require_admission(BOUNDARY, now)

    def assert_canonical(self, value):
        self.assertNotIsInstance(value, float)
        if value is None or isinstance(value, (bool, str, int)):
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                self.assert_canonical(child)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertIsInstance(key, str)
                self.assert_canonical(child)
            return
        self.fail(f'non-canonical value {value!r}')

    def timing_recorder(self, fixture):
        from cp5_run import CP5Recorder
        recorder = object.__new__(CP5Recorder)
        recorder.store = fixture.store
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder.monotonic_ns = fixture.monotonic_ns
        return recorder

    def test_scheduler_timing_flush_persists_canonical_evidence(self):
        fixture = self.fixture()
        recorder = self.timing_recorder(fixture)
        recorder._timing('WAKE', target_ns=100, slot_index=2)
        recorder._timing('SELECTION', market_id='m', slot_index=2, target_ns=100)
        recorder._flush_timing()
        payloads = [json.loads(row['detail_json'])
                    for row in fixture.rows('SELECT detail_json FROM recorder_events')]
        self.assertEqual(len(payloads), 3)
        for payload in payloads:
            self.assert_canonical(payload)

    def test_scheduler_timing_flush_fails_closed_on_float(self):
        fixture = self.fixture()
        recorder = self.timing_recorder(fixture)
        recorder._timing('WAKE', target_ns=1.5)
        with self.assertRaises(RecorderStoreError):
            recorder._flush_timing()
        self.assertEqual(len(recorder._timing_buffer), 1)
        self.assertEqual(fixture.rows('SELECT count(*) AS n FROM recorder_events')[0]['n'], 0)

    def test_healthy_writer_survives_rejected_opening_wait_evidence(self):
        fixture = self.fixture()
        run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        rejected = dict(type='OPENING_WAIT', diagnostic_only=True, run_id=run_id,
            intended_boundary=BOUNDARY, wait_started=1792228386.607,
            wait_ended=1792228440.006, wait_end_ns=162794468000000)
        with self.assertRaises(RecorderStoreError):
            fixture.store.record_event(run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail=rejected)
        self.assertFalse(fixture.store._write_failed)
        fixture.store.record_event(run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(),
            detail=dict(type='OPENING_WAIT', diagnostic_only=True, run_id=run_id,
                intended_boundary=BOUNDARY, wait_started_wall_us=1792228386607000,
                wait_ended_wall_us=1792228440006000, wait_end_ns=162794468000000))
        self.assertEqual(len(fixture.rows('SELECT detail_json FROM recorder_events')), 1)

    def test_audit_attempt_event_is_canonical_through_store(self):
        from cp5_run import CP5Recorder
        fixture = self.fixture()
        recorder = object.__new__(CP5Recorder)
        recorder.store = fixture.store
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder._audit_events = [dict(attempt=1, host='clob.polymarket.com', path='/book', method='GET',
            started_wall_us=fixture.wall_us(), started_mono_ns=fixture.monotonic_ns(), status=200,
            request_bytes=0, response_bytes=12, redirect=False, request_header_presence=['accept'],
            response_header_presence=[], error=None, complete=True, stream_closed=True,
            set_cookie_present=False, ended_wall_us=fixture.wall_us(), ended_mono_ns=fixture.monotonic_ns())]
        recorder._audit_persisted_attempts = set()
        recorder._flush_audit()
        payloads = [json.loads(row['detail_json'])
                    for row in fixture.rows('SELECT detail_json FROM recorder_events')]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['type'], 'ACTUAL_SEND_ATTEMPT')
        self.assert_canonical(payloads[0])


if __name__ == '__main__':
    unittest.main()
