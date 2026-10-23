"""Offline final-review regressions: real collector/store, mocked public HTTP."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import httpx
from tests import test_recorder_selected as selected
from tests.support import make_event
from polymarket_bot.recorder import cli
from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.service import Recorder
from polymarket_bot.recorder.store import RecorderStore, decode_evidence


class RemediationTests(unittest.TestCase):
    setUp = selected.SelectedTests.setUp
    fixture = selected.SelectedTests.fixture

    def run_selected(self, f, stop=lambda: False):
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                     wall_us=f.wall_us, sleep=f.sleep)
        return r.run(RunConfig(selected_event_id='2001', selected_market_id='1001'),
                     should_stop=stop, deadline_ns=f.monotonic_ns()+6_000_000_000)

    def test_equal_ids_require_explicit_unique_consistent_membership(self):
        for kind in ('absent', 'duplicate', 'conflict', 'valid'):
            with self.subTest(kind=kind):
                f = self.fixture(offset=0)
                old = f.handler
                def handler(req):
                    if req.url.path == '/events/1001':
                        row = f.market_row().copy()
                        event = make_event(markets=[row]); event['id'] = '1001'
                        if kind == 'absent':
                            del event['markets']; event.update(row)
                        if kind == 'duplicate': event['markets'].append(row.copy())
                        if kind == 'conflict': row['conditionId'] = '0x'+'ff'*32
                        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(event).encode()))
                    return old(req)
                f.client._transport.inner = httpx.MockTransport(handler)
                r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
                result = r.run(RunConfig(selected_event_id='1001', selected_market_id='1001'),
                               should_stop=lambda: False, deadline_ns=f.monotonic_ns()+6_000_000_000)
                self.assertEqual(result.exit_code == 0, kind == 'valid')
                self.assertEqual(f.book_count > 0, kind == 'valid')

    def test_attempt_raw_correspondence_success_and_failed_response(self):
        for status in (200, 503):
            with self.subTest(status=status):
                f = self.fixture(offset=0)
                sent = []
                def hook(req, payload, f):
                    if req.url.path == '/books':
                        raw = b'busy' if status == 503 else json.dumps(payload).encode()
                        sent.append(raw)
                        return httpx.Response(status, stream=httpx.ByteStream(raw))
                f.hook = hook
                result = self.run_selected(f)
                self.assertEqual(result.exit_code, 0)
                attempts = f.rows("SELECT * FROM request_attempts WHERE path='/books' ORDER BY request_id")
                self.assertEqual(len(attempts), f.book_count)
                self.assertGreater(len(attempts), 0)
                self.assertTrue(all(a['http_status'] == status for a in attempts))
                for a, expected_raw in zip(attempts, sent):
                    raw = decode_evidence(a['response_raw_z'], a['raw_size_bytes'], a['codec_version'])
                    self.assertEqual(raw, expected_raw)
                    self.assertEqual(a['raw_complete'], 1)
                    if status == 503: self.assertEqual(raw, b'busy')
                    else: self.assertIsInstance(json.loads(raw), list)

    def test_stop_and_keyboard_interrupt_preserve_attempts_without_quotes(self):
        for interrupt in ('stop', 'keyboard'):
            with self.subTest(interrupt=interrupt):
                f = self.fixture(offset=0)
                def hook(req, payload, f):
                    if req.url.path == '/books':
                        return httpx.Response(503, stream=httpx.ByteStream(b'busy'))
                f.hook = hook
                def stop():
                    if f.book_count and interrupt == 'keyboard': raise KeyboardInterrupt
                    return f.book_count > 0
                result = self.run_selected(f, stop)
                self.assertEqual(result.exit_code, 0 if interrupt == 'stop' else 130)
                self.assertEqual(result.slots_finalized, 0)
                self.assertEqual(f.book_count, 1, 'no sends after stop admission')
                attempts = f.rows("SELECT * FROM request_attempts WHERE path='/books'")
                self.assertEqual(len(attempts), 1)
                a = attempts[0]
                self.assertEqual(decode_evidence(a['response_raw_z'], a['raw_size_bytes'], a['codec_version']), b'busy')
                self.assertEqual(a['http_status'], 503)
                self.assertEqual(f.rows("SELECT status FROM sample_slots WHERE status!='MISSED'"), [{'status':'INTERRUPTED'}])
                outcomes = f.rows('SELECT status,best_bid_text,request_id FROM book_observations')
                self.assertEqual(len(outcomes), 2)
                self.assertTrue(all(o['status']=='INTERRUPTED' and o['best_bid_text'] is None for o in outcomes))
                self.assertEqual(f.rows('PRAGMA foreign_key_check'), [])

    def test_interrupted_audit_persistence_failure_is_nonzero(self):
        f = self.fixture(offset=0)
        f.hook = lambda req,payload,f: (httpx.Response(503, stream=httpx.ByteStream(b'busy'))
                                       if req.url.path == '/books' else None)
        original = f.store._insert
        def insert(table, values):
            if table == 'request_attempts' and values.get('path') == '/books':
                raise sqlite3.OperationalError('disk full')
            return original(table, values)
        with patch.object(f.store, '_insert', side_effect=insert):
            result = self.run_selected(f, lambda: f.book_count > 0)
        self.assertEqual((result.exit_code, result.reason), (1, 'storage_failure'))
        self.assertEqual(result.slots_finalized, 0)
        self.assertEqual(f.book_count, 1)
        self.assertEqual(f.rows('SELECT * FROM book_observations'), [])

    def test_initialized_empty_db_raced_after_cli_precheck_is_rejected_unchanged(self):
        f = self.fixture()
        f.store.close(); f.db.path.unlink()
        real_open = RecorderStore.open
        prior_bytes = []
        def raced(path, **kwargs):
            prior = real_open(path); prior.close()
            prior_bytes.append(f.db.path.read_bytes())
            return real_open(path, **kwargs)
        with patch.object(cli.RecorderStore, 'open', side_effect=raced), \
             patch.object(cli.httpx, 'Client', side_effect=AssertionError('HTTP after stale path')):
            code = cli.main(['record','--mode','book-only','--db',str(f.db.path),
                             '--event-id','2001','--market-id','1001'])
        self.assertEqual(code, 1)
        self.assertEqual(f.db.path.read_bytes(), prior_bytes[0])
        # Automatic mode still opens valid existing empty store.
        reopened = real_open(f.db.path); reopened.close()

    def test_retry_deadline_cancellation_preserves_failed_response(self):
        f = self.fixture(offset=0)
        def slow(req, payload, f):
            if req.url.path == '/books':
                f.advance(1.8)
                return httpx.Response(503, stream=httpx.ByteStream(b'no retry budget'))
        f.hook = slow
        result = self.run_selected(f, lambda: f.book_count > 0)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(f.book_count, 1)
        attempts = f.rows("SELECT * FROM request_attempts WHERE path='/books'")
        self.assertEqual(len(attempts), 1)
        a = attempts[0]
        self.assertEqual(decode_evidence(a['response_raw_z'], a['raw_size_bytes'], a['codec_version']), b'no retry budget')
        self.assertEqual(a['attempt_number'], 1)
        self.assertEqual(result.slots_finalized, 1)
        self.assertEqual(f.rows("SELECT status FROM sample_slots WHERE status!='MISSED'"), [{'status':'FAILED'}])

    def test_stop_between_single_fallbacks_preserves_completed_attempts(self):
        f = self.fixture(offset=0)
        old = f.handler
        singles = []
        def handler(req):
            if req.url.path == '/book':
                from tests.support import make_book
                singles.append(req.url.params['token_id'])
                payload = make_book(asset_id=singles[-1], market=f.market_row()['conditionId'],
                                    timestamp=str(f.wall_us()//1000))
                f.requests.append((req.method, req.url.path, f.monotonic_ns(), {}, b''))
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))
            return old(req)
        f.client._transport.inner = httpx.MockTransport(handler)
        f.hook = lambda req,payload,f: (httpx.Response(404, stream=httpx.ByteStream(b'not found'))
                                       if req.url.path == '/books' else None)
        result = self.run_selected(f, lambda: len(singles) > 0)
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 0))
        self.assertEqual(len(singles), 1, 'second single must not send after stop')
        attempts = f.rows("SELECT * FROM request_attempts WHERE path IN ('/books','/book') ORDER BY request_id")
        self.assertEqual([a['http_status'] for a in attempts], [404, 200])
        self.assertEqual([a['path'] for a in attempts], ['/books', '/book'])
        self.assertTrue(all(a['raw_complete'] for a in attempts))
        self.assertEqual(len(f.rows('SELECT * FROM book_observations')), 2)
        self.assertTrue(all(o['status']=='INTERRUPTED' and o['best_bid_text'] is None
                            for o in f.rows('SELECT * FROM book_observations')))

    def test_interrupted_finalization_duplicate_is_idempotent(self):
        f = self.fixture(offset=0)
        f.hook = lambda req,payload,f: (httpx.Response(503, stream=httpx.ByteStream(b'busy'))
                                       if req.url.path == '/books' else None)
        finalize = f.store.finalize_slot
        def twice(*args, **kwargs):
            finalize(*args, **kwargs)
            finalize(*args, **kwargs)
        with patch.object(f.store, 'finalize_slot', side_effect=twice):
            result = self.run_selected(f, lambda: f.book_count > 0)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(f.book_count, 1)
        self.assertEqual(len(f.rows("SELECT * FROM request_attempts WHERE path='/books'")), 1)
        self.assertEqual(len(f.rows('SELECT * FROM book_observations')), 2)

    def test_keyboard_interrupt_before_normal_finalization_preserves_attempt(self):
        f = self.fixture(offset=0)
        finalize = f.store.finalize_slot
        calls = []
        def interrupt_once(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return finalize(*args, **kwargs)
        with patch.object(f.store, 'finalize_slot', side_effect=interrupt_once):
            result = self.run_selected(f)
        self.assertEqual((result.exit_code, result.slots_finalized), (130, 0))
        self.assertEqual(f.book_count, 1)
        self.assertEqual(len(f.rows("SELECT * FROM request_attempts WHERE path='/books'")), 1)
        self.assertEqual(f.rows("SELECT status FROM sample_slots WHERE status!='MISSED'"), [{'status':'INTERRUPTED'}])
        self.assertTrue(all(o['status']=='INTERRUPTED' and o['best_bid_text'] is None
                            for o in f.rows('SELECT * FROM book_observations')))

    def test_late_interrupt_keeps_committed_evidence_and_truthful_count(self):
        f = self.fixture(offset=0)
        finalize = f.store.finalize_slot
        def interrupt_after_commit(*args, **kwargs):
            finalize(*args, **kwargs)
            raise KeyboardInterrupt
        with patch.object(f.store, 'finalize_slot', side_effect=interrupt_after_commit):
            result = self.run_selected(f)
        self.assertEqual((result.exit_code, result.slots_finalized), (130, 1))
        self.assertEqual(f.book_count, 1)
        self.assertEqual(len(f.rows("SELECT * FROM request_attempts WHERE path='/books'")), 1)
        self.assertEqual(f.rows("SELECT status FROM sample_slots WHERE status!='MISSED'"), [{'status':'COMPLETE'}])

    def test_stop_before_first_send_creates_no_attempt(self):
        f = self.fixture(offset=0)
        reserve = f.store.reserve_slot
        reserved = []
        def stop_after_reserve(*args, **kwargs):
            slot = reserve(*args, **kwargs); reserved.append(slot)
            return slot
        with patch.object(f.store, 'reserve_slot', side_effect=stop_after_reserve):
            result = self.run_selected(f, lambda: bool(reserved))
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 0))
        self.assertEqual(f.book_count, 0)
        self.assertEqual(f.rows("SELECT * FROM request_attempts WHERE path IN ('/books','/book')"), [])
        self.assertEqual(f.rows("SELECT status FROM sample_slots WHERE status!='MISSED'"), [{'status':'INTERRUPTED'}])

    def test_direct_selected_recorder_rejects_reopened_empty_database(self):
        f = self.fixture()
        f.store.close(); f.store = RecorderStore.open(f.db.path)
        before = f.db.path.read_bytes()
        result = self.run_selected(f)
        self.assertEqual(result.reason, 'selected_database_not_fresh')
        self.assertFalse(f.requests)
        self.assertEqual(f.db.path.read_bytes(), before)
