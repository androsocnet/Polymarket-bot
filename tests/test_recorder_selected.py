"""Selected-market offline acceptance; HTTP always mocked, sockets denied."""
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch
import httpx
from tests.recorder_support import RecorderFixture
from tests.support import make_event
from polymarket_bot.recorder import cli, metadata
from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.service import Recorder

class SelectedTests(unittest.TestCase):
    def setUp(self):
        self.guards = [patch.object(socket, name, side_effect=AssertionError('external network forbidden'))
                       for name in ('getaddrinfo', 'create_connection')]
        self.guards += [patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))]
        for guard in self.guards:
            guard.start(); self.addCleanup(guard.stop)

    def fixture(self, offset=596_000_000):
        f = RecorderFixture(offset_us=offset)
        self.addCleanup(f.close)
        row = f.market_row()
        f.market_row = lambda: row
        old = f.respond
        def respond(request):
            if request.url.path == '/events/2001':
                f.requests.append((request.method, request.url.path, f.monotonic_ns(), {}, b''))
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(make_event(markets=[row])).encode()))
            self.assertNotEqual(request.url.path, '/events')
            self.assertNotIn('/slug/', request.url.path)
            return old(request)
        f.client._transport.inner = httpx.MockTransport(respond)
        f.handler = respond
        return f

    def config(self):
        return RunConfig(selected_event_id='2001', selected_market_id='1001')

    def test_pair_and_mode_validation(self):
        for args in [dict(selected_event_id='2001'), dict(selected_market_id='1001'),
                     dict(selected_event_id='../x', selected_market_id='1001'),
                     dict(selected_event_id='2001', selected_market_id='1001', mode='APPROXIMATE_REFERENCE')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                RunConfig(**args)

    def test_cli_pair_rejected_before_io(self):
        for flags in [['--event-id','2001'], ['--market-id','1001']]:
            self.assertEqual(cli.main(['record','--mode','book-only','--db','unused.sqlite', *flags]), 2)

    def test_expiry_records_without_successor(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        with patch('polymarket_bot.recorder.service.acquire_next_market', side_effect=AssertionError('successor')):
            result = r.run(self.config(), should_stop=lambda: False)
        self.assertEqual((result.exit_code, result.reason), (0, 'SELECTED_MARKET_EXPIRED'))
        self.assertGreater(f.book_count, 0)
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        self.assertEqual(len(f.rows('SELECT * FROM markets')), 1)
        self.assertEqual(len(f.rows('SELECT * FROM sample_slots')), 450)

    def test_future_wait_and_early_stop(self):
        f = self.fixture(offset=-2_000_000)
        # Pin next window beginning two seconds after initial wall clock.
        from datetime import datetime, timezone
        row = f.market_row()
        start = f.initial_wall_us + 2_000_000
        row['eventStartTime'] = datetime.fromtimestamp(start/1e6, timezone.utc).isoformat()
        row['endDate'] = datetime.fromtimestamp(start/1e6+900, timezone.utc).isoformat()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        result = r.run(self.config(), should_stop=lambda: False, deadline_ns=f.monotonic_ns()+7_000_000_000)
        self.assertEqual(result.exit_code, 0)
        books = [x for x in f.requests if x[1] in ('/books','/book')]
        self.assertTrue(books)
        self.assertTrue(all(f.initial_wall_us+(x[2]-f.epoch_ns)//1000 >= start for x in books))
        self.assertLess(len(f.rows('SELECT * FROM sample_slots')), 450)

    def test_invalid_relationship_refused(self):
        f = self.fixture()
        f.market_row()['id'] = '9999'
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        result = r.run(self.config(), should_stop=lambda: False)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(f.book_count, 0)

    def test_bad_metadata_and_config_before_books(self):
        for change in ('condition', 'tokens', 'duration', 'malformed', 'expired'):
            with self.subTest(change=change):
                f = self.fixture()
                row = f.market_row()
                if change == 'tokens': row['clobTokenIds'] = '["1","1"]'
                if change == 'condition': row['conditionId'] = 'bad'
                if change == 'duration': row['endDate'] = row['eventStartTime']
                if change == 'malformed': del row['eventStartTime']
                if change == 'expired': f.advance(10)
                r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
                result = r.run(self.config(), should_stop=lambda: False)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(f.book_count, 0)

    def test_storage_failure_is_not_success(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        with patch.object(f.store, 'reserve_slot', side_effect=sqlite3.OperationalError('disk full')):
            result = r.run(self.config(), should_stop=lambda: False)
        self.assertEqual((result.exit_code, result.reason), (1, 'storage_failure'))
        self.assertEqual(f.book_count, 0)

    def test_restart_refuses_prior_run(self):
        f = self.fixture()
        f.store.start_run(RunConfig(), wall_us=f.wall_us(), app_revision='test')
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        result = r.run(self.config(), should_stop=lambda: False)
        self.assertEqual(result.reason, 'selected_database_not_fresh')
        self.assertFalse(f.requests)

    def test_clock_unavailable_never_fetches_metadata_or_books(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        with patch.object(r, '_calibrate', return_value=False):
            result = r.run(self.config(), should_stop=lambda: False, deadline_ns=f.monotonic_ns()+3_000_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(f.requests)

    def test_cli_simulated_record_and_report(self):
        import io
        from contextlib import redirect_stdout
        from pathlib import Path
        f = self.fixture()
        # Fixture opened an empty owned store; release it before real CLI acquires fresh path.
        f.store.close()
        f.db.path.unlink()
        original_open = cli.RecorderStore.open
        def opened(path, **kwargs):
            f.store = original_open(path, **kwargs)
            return f.store
        with patch.object(cli.RecorderStore, 'open', side_effect=opened), \
             patch.object(cli.httpx, 'Client', return_value=f.client), \
             patch.object(cli.time, 'monotonic', f.monotonic), \
             patch.object(cli.time, 'monotonic_ns', f.monotonic_ns), \
             patch.object(cli.time, 'sleep', f.sleep), patch.object(cli, '_wall_us', f.wall_us):
            code = cli.main(['record','--mode','book-only','--db',str(f.db.path),
                             '--event-id','2001','--market-id','1001'])
        self.assertEqual(code, 0)
        self.assertTrue(f.client.is_closed)
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        self.assertEqual(len(f.rows('SELECT * FROM sample_slots')), 450)
        self.assertGreater(f.book_count, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(['report','--db',str(f.db.path),'--start','2026-01-01T00:00:00Z',
                             '--end','2027-01-01T00:00:00Z'])
        self.assertEqual(code, 0)
        self.assertIn('eligible_scheduled_slots', out.getvalue())
        self.assertEqual(cli.main(['record','--mode','book-only','--db',str(f.db.path),
                                 '--event-id','2001','--market-id','1001']), 1)

    def test_explicit_duration_stops_before_expiry(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        result = r.run(self.config(), should_stop=lambda: False, deadline_ns=f.monotonic_ns()+3_000_000_000)
        self.assertEqual((result.exit_code, result.reason), (0, 'STOP_REQUESTED'))
        self.assertGreater(f.book_count, 0)
        self.assertLess(f.elapsed_ns, 4_000_000_000)
        self.assertEqual(f.rows('SELECT lifecycle FROM markets')[0]['lifecycle'], 'CURRENT')

    def test_hostile_metadata_change_during_refresh_blocks(self):
        f = self.fixture(offset=750_000_000)
        row = f.market_row()
        market_id = row['id']
        first = row.copy()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        state = {'events': 0}
        old = f.handler
        def respond(request):
            if request.url.path.startswith('/events/'):
                state['events'] += 1
                if state['events'] > 1:
                    f.requests.append((request.method, request.url.path, f.monotonic_ns(), {}, b''))
                    row = dict(first, conditionId='0x'+'ff'*32)
                    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(make_event(markets=[row])).encode()))
            return old(request)
        f.client._transport.inner = httpx.MockTransport(respond)
        # Run past the 60-second refresh age but stop before the ~750s window remainder.
        result = r.run(RunConfig(selected_event_id='2001', selected_market_id=market_id),
                       should_stop=lambda: False, deadline_ns=f.monotonic_ns()+75_000_000_000)
        self.assertEqual(result.reason, 'identity_conflict')
        # Capture is legitimate before the hostile refresh; it must stop after it.
        refresh_at = [x[2] for x in f.requests if x[1] == '/events/2001'][1]
        books = [x for x in f.requests if x[1] == '/books']
        self.assertGreater(len(books), 0)
        self.assertTrue(all(x[2] < refresh_at for x in books),
                        'books attempted after hostile metadata refresh')
        # The failed refresh is a hard stop: revision stays CURRENT (never retired as
        # INVALIDATED), the run ends FAILED, and no successor is ever contacted.
        lifecycles = [row['lifecycle'] for row in f.rows('SELECT lifecycle FROM markets')]
        self.assertEqual(lifecycles, ['CURRENT'])
        self.assertEqual(len(f.rows('SELECT * FROM markets')), 1)

    def test_housekeeping_never_calls_successor_or_enumeration(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        ran = []
        class Probe:
            def __call__(self, *a, **k):
                ran.append(a)
        with patch('polymarket_bot.recorder.service.acquire_next_market', Probe()): \
             result = r.run(self.config(), should_stop=lambda: False)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(ran, [])
        for method, path, *_ in f.requests:
            self.assertNotEqual(path, '/events')
            self.assertNotIn('/slug/', path)
            self.assertNotEqual(path, '/markets/1002')

    def test_clock_loss_near_expiry_pauses_without_capture(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        real_usable = r.clock.usable
        losses = []
        def usable():
            if f.elapsed_ns >= 3_000_000_000:
                losses.append(f.monotonic_ns())
                return False
            return real_usable()
        def unavailable(request, payload, fixture):
            if request.url.path == '/time' and fixture.elapsed_ns >= 3_000_000_000:
                return httpx.Response(503, stream=httpx.ByteStream(b'clock unavailable'))
        f.hook = unavailable
        with patch.object(r.clock, 'usable', usable):
            result = r.run(self.config(), should_stop=lambda: False, deadline_ns=f.monotonic_ns()+6_000_000_000)
        self.assertEqual((result.exit_code, result.reason), (0, 'STOP_REQUESTED'))
        self.assertTrue(losses, 'clock loss was not injected')
        books = [x for x in f.requests if x[1] in ('/books', '/book')]
        self.assertGreater(len(books), 0, 'expected capture before clock loss')
        self.assertTrue(all(monotonic < f.epoch_ns + 3_000_000_000
                            for _, _, monotonic, *_ in books),
                        'books attempted after clock loss')

    def test_shutdown_records_clean_state(self):
        f = self.fixture()
        r = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns, wall_us=f.wall_us, sleep=f.sleep)
        result = r.run(self.config(), should_stop=lambda: False)
        self.assertEqual(result.exit_code, 0)
        rows = f.rows("SELECT state, exit_reason FROM recording_runs")
        self.assertEqual((rows[0]['state'], rows[0]['exit_reason']),
                         ('STOPPED', 'SELECTED_MARKET_EXPIRED'))
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'], 'ok')

    def test_selected_acquisition_exists(self):
        self.assertTrue(callable(getattr(metadata, 'acquire_selected_market', None)))
