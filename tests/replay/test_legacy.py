"""Disposable recorder fixtures only; never replacement historical evidence."""
from contextlib import closing, redirect_stdout, redirect_stderr
from hashlib import sha256
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import unittest

import httpx

from tests.recorder_support import RecorderFixture
from tests.support import make_event
from tests.test_paper import OfflineTests

RUN = '0cba7ec8-c91f-4b78-a9b3-ee7d54e55b40'
MARKET = '5483041'
EVENT = '1163394'


def legacy_fixture(case, *, rising_books=False):
    """Fake HTTP/time, real recorder/store; IDs chosen to exercise bounded scope."""
    from polymarket_bot.recorder.models import RunConfig
    from polymarket_bot.recorder.service import Recorder
    f = RecorderFixture()
    case.addCleanup(f.close)
    row = f.market_row()
    row.update(id=MARKET, slug='btc-updown-15m-' + str(f.wall_us() // 900_000_000 * 900))
    f.market_row = lambda: row
    original = f.respond
    if rising_books:
        seen = []

        def hook(request, payload, fixture):
            if request.url.path == '/books' and isinstance(payload, list):
                bid = format(0.40 + 0.01 * len(seen), '.2f')
                ask = format(0.41 + 0.01 * len(seen), '.2f')
                seen.append(None)
                for book in payload:
                    book['bids'] = [{'price': bid, 'size': '4'}]
                    book['asks'] = [{'price': ask, 'size': '4'}]
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))
            return None

        f.hook = hook

    def respond(request):
        if request.url.path == '/events':
            f.advance(0.01)
            return httpx.Response(200, stream=httpx.ByteStream(
                json.dumps([make_event(id=EVENT, markets=[row])]).encode()))
        return original(request)

    f.client._transport = httpx.MockTransport(respond)
    recorder = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                        wall_us=f.wall_us, sleep=f.sleep)
    result = recorder.run(RunConfig(use_fast_path=False), should_stop=lambda: False,
                          deadline_ns=f.monotonic_ns() + 16_000_000_000)
    case.assertEqual(result.exit_code, 0)
    f.store.close()
    with closing(sqlite3.connect(f.db.path)) as c:
        with c:
            old, config = c.execute('SELECT run_id,config_json FROM recording_runs').fetchone()
            config = json.loads(config)
            config.pop('selected_market_id')
            config.pop('selected_event_id')
            c.execute('UPDATE recording_runs SET config_json=?', (json.dumps(config),))
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                if 'run_id' in [r[1] for r in c.execute('PRAGMA table_info(' + table + ')')]:
                    c.execute('UPDATE ' + table + ' SET run_id=? WHERE run_id=?', (RUN, old))
    return f


class LegacyTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.f = legacy_fixture(self)
        self.db = self.f.db.path

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute(sql, values)

    def load(self, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('polymarket_bot.replay.legacy'),
                             'explicit legacy replay loader missing')
        module = importlib.import_module('polymarket_bot.replay.legacy')
        return module.load_legacy_replay_recording(
            self.db, kwargs.pop('run_id', RUN), kwargs.pop('market_id', MARKET),
            expected_sha256=kwargs.pop('expected_sha256', sha256(self.db.read_bytes()).hexdigest()), **kwargs)

    def discovery(self, change):
        import zlib
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                r = c.execute("SELECT request_id,response_raw_z FROM request_attempts WHERE operation='GAMMA_EVENTS' LIMIT 1").fetchone()
                payload = json.loads(zlib.decompress(r[1]))
                change(payload)
                raw = json.dumps(payload).encode()
                c.execute('UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,response_bytes=? WHERE request_id=?',
                          (zlib.compress(raw), len(raw), len(raw), r[0]))

    def test_proven_scope_preserves_original_config(self):
        before = self.db.read_bytes()
        config = self.f.rows('SELECT config_json FROM recording_runs')[0]['config_json']
        result, scope = self.load()
        self.assertGreater(len(result[0]), 1)
        self.assertEqual(scope['event_id'], EVENT)
        self.assertEqual(scope['identity_proof'], 'DISCOVERY_ENVELOPE_VERIFIED')
        self.assertTrue(scope['discovery_request_ids'])
        self.assertEqual(scope['source_sha256'], sha256(before).hexdigest())
        self.assertEqual(config, self.f.rows('SELECT config_json FROM recording_runs')[0]['config_json'])
        self.assertNotIn('selected_market_id', json.loads(config))
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(any(Path(str(self.db) + s).exists() for s in ('-wal', '-shm', '-journal')))

    def test_missing_discovery_fails_closed(self):
        self.execute("UPDATE request_attempts SET operation='OTHER' WHERE operation='GAMMA_EVENTS'")
        with self.assertRaisesRegex(ValueError, 'discovery'):
            self.load()

    def test_ambiguous_discovery_fails_closed(self):
        self.discovery(lambda p: p[0]['markets'].append(dict(p[0]['markets'][0])))
        with self.assertRaisesRegex(ValueError, 'discovery'):
            self.load()

    def test_wrong_event_membership_fails_closed(self):
        self.discovery(lambda p: p[0].update(id='999'))
        with self.assertRaisesRegex(ValueError, 'discovery'):
            self.load()

    def test_discovery_token_disagreement_fails_closed(self):
        self.discovery(lambda p: p[0]['markets'][0].update(clobTokenIds='["1","2"]'))
        with self.assertRaisesRegex(ValueError, 'discovery|token'):
            self.load()

    def test_wrong_source_hash_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.load(expected_sha256='0' * 64)

    def test_other_market_or_run_fails_closed(self):
        for kwargs in ({'market_id': '5482791'}, {'run_id': 'other-run'}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'scope'):
                self.load(**kwargs)

    def test_normal_strict_loader_still_rejects_missing_selection(self):
        from polymarket_bot.replay.adapter import load_replay_recording
        self.load()
        with self.assertRaisesRegex(ValueError, 'selected run identity mismatch'):
            load_replay_recording(self.db, RUN, MARKET)

    def test_strict_legacy_recording_parity(self):
        from polymarket_bot.strategy import load_strategy_recording
        config = json.loads(self.f.rows('SELECT config_json FROM recording_runs')[0]['config_json'])
        config.update(selected_market_id=MARKET, selected_event_id=EVENT)
        self.execute('UPDATE recording_runs SET config_json=?', (json.dumps(config),))
        strict = load_strategy_recording(self.db, RUN, MARKET)
        result, _ = self.load()
        self.assertEqual(result[5], strict)

    def test_terminal_required(self):
        self.execute("UPDATE recording_runs SET state='RUNNING',ended_wall_us=NULL")
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.load()

    def test_revision_hash_tamper_fails_closed(self):
        self.execute("UPDATE market_revisions SET content_hash=?", ('0' * 64,))
        with self.assertRaisesRegex(ValueError, 'metadata hash'):
            self.load()

    def test_metadata_cannot_be_learned_after_capture(self):
        self.execute('UPDATE market_revisions SET observed_us=observed_us+900000000')
        with self.assertRaisesRegex(ValueError, 'metadata.*capture'):
            self.load()

    def test_request_receipt_tamper_excluded(self):
        self.execute("UPDATE request_attempts SET ended_server_us=ended_server_us+1 WHERE operation='BOOK_BATCH'")
        with self.assertRaisesRegex(ValueError, 'no validated snapshots'):
            self.load()

    def test_crossed_anchor_excluded(self):
        self.execute("UPDATE clock_anchors SET status='DISCONTINUITY'")
        with self.assertRaisesRegex(ValueError, 'anchor'):
            self.load()

    def test_bad_request_order_excluded(self):
        self.execute("UPDATE request_attempts SET start_mono_ns=start_mono_ns-900000000000 WHERE operation='BOOK_BATCH'")
        with self.assertRaisesRegex(ValueError, 'no validated snapshots'):
            self.load()

    def test_nonempty_sidecar_rejected_without_modification(self):
        sidecar = Path(str(self.db) + '-wal')
        sidecar.write_bytes(b'active')
        self.addCleanup(sidecar.unlink, missing_ok=True)
        with self.assertRaisesRegex(ValueError, 'sidecar'):
            self.load()
        self.assertEqual(sidecar.read_bytes(), b'active')

    def test_timing_depth_and_availability_preserved(self):
        result, _ = self.load()
        for s in result[5].snapshots:
            row = self.f.rows('SELECT * FROM request_attempts WHERE request_id=?', (s.request_id,))[0]
            self.assertEqual(s.receipt_us, row['ended_server_us'])
            self.assertEqual(s.receipt_ns, row['end_mono_ns'])
            self.assertEqual(s.start_ns, row['start_mono_ns'])
            self.assertLessEqual(s.receipt_ns, s.available_ns)
            self.assertTrue(s.book.asks)
            self.assertEqual(str(s.book.asks[0].size), '4')

    def test_missing_execution_slot_not_skipped(self):
        from polymarket_bot.paper import simulate
        slots = self.f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        missed = self.f.rows("SELECT count(*) AS n FROM sample_slots WHERE status='MISSED'")[0]['n']
        self.execute('DELETE FROM book_observations WHERE slot_id=?', (slots[1]['slot_id'],))
        self.execute("UPDATE sample_slots SET status='MISSED' WHERE slot_id=?", (slots[1]['slot_id'],))
        result, scope = self.load()
        r = result[5]
        outcome = 'UP'
        order = dict(id='missing',market_id=MARKET,token_id=r.tokens[outcome],side='BUY',quantity='1',decision_slot_id=slots[0]['slot_id'])
        execution = simulate(r, [order], initial_cash='100', fee_bps='10')
        self.assertEqual(execution['ledger'][0]['reason'], 'MISSING_EXECUTION_BOOK')
        self.assertEqual(execution['ledger'][0]['filled_quantity'], '0')
        self.assertEqual(scope['slot_status_counts']['MISSED'], missed + 1)
        self.assertTrue(any(e['reason'] == 'NO_OBSERVATIONS' for e in result[1]))

    def test_future_discovery_rejected(self):
        self.execute("UPDATE request_attempts SET start_mono_ns=start_mono_ns+15900000000,end_mono_ns=end_mono_ns+15900000000 WHERE operation='GAMMA_EVENTS'")
        with self.assertRaisesRegex(ValueError, 'discovery.*capture'):
            self.load()

    def test_missing_metadata_audit_rejected(self):
        self.execute("DELETE FROM request_attempts WHERE operation='GAMMA_MARKET'")
        with self.assertRaisesRegex(ValueError, 'linkage missing'):
            self.load()

    def test_cli_corrupt_metadata_returns_exit_two(self):
        self.execute("UPDATE request_attempts SET response_raw_z=x'00' WHERE operation='GAMMA_EVENTS'")
        out = self.db.parent / 'corrupt-evidence'
        self.assertEqual(self.cli(out, '--legacy-scope', '--expected-sha256', sha256(self.db.read_bytes()).hexdigest()), 2)
        self.assertFalse(out.exists())

    def test_settlement_uses_proven_revision_and_validates_request(self):
        import zlib
        from polymarket_bot.replay.legacy import _settlement
        market = self.f.rows('SELECT * FROM markets')[0]
        raw = b'{"data":[]}'
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            with c:
                request_id = c.execute("SELECT request_id FROM request_attempts WHERE operation='GAMMA_EVENTS'").fetchone()[0]
                c.execute("INSERT INTO resolution_observations (market_id,request_id,condition_id,provenance_json,learned_wall_us,classification,response_hash,raw_resolution_z,raw_size_bytes,codec_version) VALUES (?,?,?,'{}',?,'RESOLVED_DIRECTIONAL',?,?,?,1)",
                          (MARKET, request_id, market['condition_id'], market['window_end_us']+1, sha256(raw).hexdigest(), zlib.compress(raw), len(raw)))
            with self.assertRaisesRegex(ValueError, 'settlement audit'):
                _settlement(c, market, [1])

    def test_settlement_raw_copy_must_match_linked_request(self):
        import zlib
        from polymarket_bot.replay.legacy import _settlement
        market = self.f.rows('SELECT * FROM markets')[0]
        raw, tampered = b'{"data":[]}', b'{"data":[{}]}'
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            with c:
                rid = c.execute("SELECT request_id FROM request_attempts WHERE operation='GAMMA_EVENTS'").fetchone()[0]
                c.execute("UPDATE request_attempts SET operation='RESOLUTION',path='/v2/resolutions',selector_json=?,response_raw_z=?,raw_size_bytes=?,response_bytes=? WHERE request_id=?",
                          (json.dumps(['condition='+market['condition_id']]),zlib.compress(raw),len(raw),len(raw),rid))
                c.execute("INSERT INTO resolution_observations (market_id,request_id,condition_id,provenance_json,learned_wall_us,classification,response_hash,raw_resolution_z,raw_size_bytes,codec_version) VALUES (?,?,?,'{}',?,'RESOLVED_DIRECTIONAL',?,?,?,1)",
                          (MARKET,rid,market['condition_id'],market['window_end_us']+1,sha256(raw).hexdigest(),zlib.compress(tampered),len(tampered)))
            with self.assertRaisesRegex(ValueError, 'settlement raw'):
                _settlement(c, market, [1])

    def test_anchor_uncertainty_must_match_raw_calibration(self):
        self.execute('UPDATE clock_anchors SET uncertainty_us=uncertainty_us+1')
        with self.assertRaisesRegex(ValueError, 'CLOCK_CALIBRATION_MISMATCH'):
            self.load()

    def test_slot_grid_cannot_move(self):
        self.execute('UPDATE sample_slots SET scheduled_us=scheduled_us+1')
        with self.assertRaisesRegex(ValueError, 'slot grid'):
            self.load()

    def test_book_selector_cannot_disagree_with_evidence(self):
        self.execute("UPDATE request_attempts SET selector_json='[]' WHERE operation='BOOK_BATCH'")
        with self.assertRaisesRegex(ValueError, 'no validated snapshots'):
            self.load()

    def test_summary_tamper_not_accepted(self):
        self.execute("UPDATE book_observations SET best_ask_text='0.99'")
        with self.assertRaisesRegex(ValueError, 'no validated snapshots'):
            self.load()

    def test_crossed_revision_not_accepted(self):
        self.execute("UPDATE market_revisions SET run_id='other'")
        with self.assertRaisesRegex(ValueError, 'foreign keys|revision identity'):
            self.load()

    def test_one_sided_books_preserved(self):
        import zlib
        # Disposable fixture only: remove bids in raw audit and matching summary.
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                for request_id, raw_z in c.execute("SELECT request_id,response_raw_z FROM request_attempts WHERE operation='BOOK_BATCH'").fetchall():
                    payload = json.loads(zlib.decompress(raw_z))
                    for book in payload:
                        book['bids'] = []
                    raw = json.dumps(payload).encode()
                    c.execute('UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,response_bytes=? WHERE request_id=?',
                              (zlib.compress(raw), len(raw), len(raw), request_id))
                c.execute('UPDATE book_observations SET best_bid_text=NULL,bid_size_text=NULL,bid_level_count=0,midpoint_text=NULL,spread_text=NULL')
        result, scope = self.load()
        self.assertTrue(result[0])
        self.assertEqual(scope['one_sided_books'], len(result[5].snapshots))
        self.assertEqual(scope['two_sided_pairs'], 0)
        self.assertTrue(all(not s.book.bids and s.book.asks for s in result[5].snapshots))

    def cli(self, output, *extra):
        from polymarket_bot.replay.cli import main
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(['--db', str(self.db), '--run-id', RUN, '--market-id', MARKET,
                         '--output-dir', str(output), *extra])

    def test_cli_explicit_opt_in_and_deterministic_outputs(self):
        root = self.db.parent
        strict = root / 'strict-output'
        self.assertEqual(self.cli(strict), 2)
        self.assertFalse(strict.exists())
        before = self.db.read_bytes()
        args = ('--legacy-scope', '--expected-sha256', sha256(before).hexdigest())
        one, two = root / 'legacy-one', root / 'legacy-two'
        self.assertEqual(self.cli(one, *args), 0)
        self.assertEqual(self.cli(two, *args), 0)
        for name in ('report.json', 'decisions.jsonl', 'ledger.jsonl'):
            self.assertEqual((one / name).read_bytes(), (two / name).read_bytes())
        report = json.loads((one / 'report.json').read_text())
        self.assertEqual(report['diagnostics']['legacy_scope']['identity_proof'], 'DISCOVERY_ENVELOPE_VERIFIED')
        self.assertEqual(before, self.db.read_bytes())

    def test_cli_opt_in_requires_hash_and_rejects_bad_pin(self):
        for name, args in (('no-pin', ['--legacy-scope']),
                           ('bad-pin', ['--legacy-scope', '--expected-sha256', '0' * 64]),
                           ('no-flag', ['--expected-sha256', '0' * 64])):
            out = self.db.parent / name
            self.assertEqual(self.cli(out, *args), 2)
            self.assertFalse(out.exists())

    def resolution(self, *, tx_byte='ab'):
        """Disposable directional response, linked to its own real-shaped audit row."""
        import zlib
        from datetime import datetime, timezone
        market = self.f.rows('SELECT * FROM markets')[0]
        end = market['window_end_us']
        row = dict(condition_id=market['condition_id'], status='resolved',
                   transaction_hash='0x' + tx_byte * 32, resolved_block=123,
                   resolved_at=datetime.fromtimestamp((end + 60_000_000) / 1_000_000,
                                                       tz=timezone.utc).isoformat(),
                   extended_review=False, was_disputed=False, new_version_q=False,
                   payouts=[1000000, 0])
        raw = json.dumps({'data': [row]}, sort_keys=True).encode()
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                request_id = c.execute("""INSERT INTO request_attempts
                    (run_id,operation,attempt_number,method,path,selector_json,
                     started_wall_us,ended_wall_us,start_mono_ns,end_mono_ns,latency_ns,
                     http_status,result,response_bytes,response_raw_z,raw_size_bytes,codec_version,raw_complete)
                    VALUES (?,'RESOLUTION',1,'GET','/v2/resolutions',?,?,?,1,2,1,
                            200,'success',?,?,?,1,1)""",
                    (RUN, json.dumps(['condition=' + market['condition_id']]),
                     end + 120_000_000, end + 121_000_000, len(raw), zlib.compress(raw), len(raw))).lastrowid
                c.execute("""INSERT INTO resolution_observations
                    (market_id,request_id,condition_id,provenance_json,learned_wall_us,
                     classification,mapped_outcome,status_raw,payouts_raw_json,response_hash,
                     raw_resolution_z,raw_size_bytes,codec_version)
                    VALUES (?,?,?,'{}',?,'RESOLVED_DIRECTIONAL','UP','resolved',
                            '[1000000,0]',?,?,?,1)""",
                    (MARKET, request_id, market['condition_id'], end + 121_000_000,
                     sha256(raw).hexdigest(), zlib.compress(raw), len(raw)))
        return row

    def strict_and_legacy(self):
        from polymarket_bot.replay.adapter import load_replay_recording
        config = json.loads(self.f.rows('SELECT config_json FROM recording_runs')[0]['config_json'])
        config.update(selected_market_id=MARKET, selected_event_id=EVENT)
        self.execute('UPDATE recording_runs SET config_json=?', (json.dumps(config),))
        return load_replay_recording(self.db, RUN, MARKET), self.load()[0]

    def test_directional_resolution_nonfinal_gamma_unknown_on_both_routes(self):
        # Break caught: trusting RESOLVED_DIRECTIONAL without final Gamma.
        row = self.resolution()
        before = self.db.read_bytes()
        legacy, _ = self.load()
        self.assertEqual(self.db.read_bytes(), before)
        strict, legacy = self.strict_and_legacy()
        for loaded in (strict, legacy):
            with self.subTest(route='strict' if loaded is strict else 'legacy'):
                self.assertEqual(json.loads(loaded[3]['resolution_raw']), {'data': [row]})
                self.assertFalse(json.loads(loaded[3]['gamma_raw'])['closed'])
                self.assertEqual(loaded[4], dict(status='UNKNOWN_SETTLEMENT',
                                               winning_outcome=None, diagnostic='Gamma not final'))
        self.assertEqual(strict[3:5], legacy[3:5])

    def test_missing_resolution_does_not_create_settlement_on_both_routes(self):
        # Break caught: inferring settlement from books or metadata with no observation.
        from polymarket_bot.replay.engine import run_replay
        self.assertFalse(self.f.rows('SELECT * FROM resolution_observations'))
        for loaded in self.strict_and_legacy():
            self.assertEqual(loaded[3:5], (None, None))
            report = run_replay(loaded[0], paper_recording=loaded[5], settlement_evidence=loaded[3])
            self.assertIsNone(report.settlement)
            self.assertEqual(report.cash, '100')
            self.assertEqual(report.realized_pnl, '0')
            self.assertFalse(any(t.get('reason') == 'SETTLEMENT' for t in report.ledger))

    def test_unknown_settlement_keeps_open_inventory_cash_and_pnl(self):
        # Break caught: zeroing inventory, terminal payout or realizing marked gains on UNKNOWN.
        from decimal import Decimal
        from polymarket_bot.replay.engine import run_replay
        self.f = legacy_fixture(self, rising_books=True)
        self.db = self.f.db.path
        self.resolution()
        for loaded in self.strict_and_legacy():
            kwargs = dict(paper_recording=loaded[5], threshold='0.01', holding_period=450)
            baseline = run_replay(loaded[0], **kwargs)
            report = run_replay(loaded[0], settlement_evidence=loaded[3], **kwargs)
            self.assertGreater(Decimal(baseline.positions['UP']['quantity']), 0)
            self.assertEqual(baseline.counts, dict(requested=1, filled=0, partial=1, rejected=0))
            self.assertEqual(baseline.cash, '98.23824')
            self.assertEqual(baseline.positions['UP']['quantity'], '4')
            self.assertEqual(baseline.positions['UP']['cost_basis'], '1.76176')
            self.assertEqual(baseline.fees_paid, '0.00176')
            self.assertEqual(report.settlement['status'], 'UNKNOWN_SETTLEMENT')
            self.assertEqual(report.settlement['diagnostic'], 'Gamma not final')
            self.assertNotIn('settled', report.settlement)
            self.assertNotIn('final_cash', report.settlement)
            self.assertEqual(report.realized_pnl, '0')
            for field in ('cash', 'positions', 'realized_pnl', 'unrealized_pnl', 'total_pnl',
                          'fees_paid', 'ledger', 'counts', 'daily_pnl'):
                self.assertEqual(getattr(report, field), getattr(baseline, field), field)

    def test_directional_raw_copy_tamper_fails_closed(self):
        import zlib
        self.resolution()
        raw = b'{"data":[]}'
        self.execute('UPDATE resolution_observations SET raw_resolution_z=?,raw_size_bytes=?',
                     (zlib.compress(raw), len(raw)))
        with self.assertRaisesRegex(ValueError, 'settlement raw mismatch'):
            self.load()

    def test_resolution_wall_time_tie_uses_latest_recorded_observation(self):
        self.resolution()
        latest = self.resolution(tx_byte='cd')
        loaded, _ = self.load()
        self.assertEqual(json.loads(loaded[3]['resolution_raw']), {'data': [latest]})

    def test_legacy_ignores_unproven_final_gamma_strict_retains_latest_semantics(self):
        import zlib
        row = self.resolution()
        rev = self.f.rows('SELECT * FROM market_revisions ORDER BY revision_id DESC')[0]
        gamma = json.loads(zlib.decompress(rev['gamma_raw_z']))
        gamma.update(closed=True, umaResolutionStatus='resolved', outcomePrices='["1","0"]')
        raw = json.dumps(gamma).encode()
        self.execute("""INSERT INTO market_revisions
            (market_id,observed_us,run_id,gamma_raw_z,raw_sizes_json,description,
             resolution_source_url,codec_version,content_hash)
            VALUES (?,?,?,?,?,'test-only refresh','https://example.test/rules',1,'test-only-final-refresh')""",
            (MARKET, self.f.rows('SELECT window_end_us FROM markets')[0]['window_end_us'] + 90_000_000,
             RUN, zlib.compress(raw), json.dumps({'gamma': len(raw)})))
        strict, legacy = self.strict_and_legacy()
        self.assertEqual(strict[4]['status'], 'RESOLVED')
        self.assertEqual(strict[4]['winning_outcome'], 'UP')
        self.assertEqual(strict[4]['tx_hash'], row['transaction_hash'])
        self.assertEqual(legacy[4]['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(legacy[4]['diagnostic'], 'Gamma not final')
        self.assertFalse(json.loads(legacy[3]['gamma_raw'])['closed'])

    def test_unknown_settlement_never_becomes_payout(self):
        from polymarket_bot.replay.engine import run_replay
        import zlib
        # Real store writes resolution; only fake HTTP/time here, never real source.
        from polymarket_bot.linux_mvp.settlement import verify_evidence
        result, _ = self.load()
        r = result[5]
        market = self.f.rows('SELECT * FROM markets')[0]
        gamma = zlib.decompress(self.f.rows('SELECT gamma_raw_z FROM market_revisions')[0]['gamma_raw_z']).decode()
        evidence = dict(gamma_raw=gamma, resolution_raw='{"data":[]}', market_id=MARKET,
                        condition_id=r.condition_id, window_start_us=market['window_start_us'],
                        window_end_us=market['window_end_us'], at_us=market['window_end_us'] + 1)
        expected = verify_evidence(evidence, window=dict(market_id=MARKET, condition_id=r.condition_id,
                                   start_us=market['window_start_us'], end_us=market['window_end_us']),
                                   tokens=r.tokens, at_us=evidence['at_us'])
        report = run_replay(result[0], paper_recording=r, excluded=result[1], opportunities=result[2], settlement_evidence=evidence)
        self.assertEqual({k: report.settlement[k] for k in expected}, expected)
        self.assertEqual(report.settlement['status'], 'UNKNOWN_SETTLEMENT')
        self.assertFalse(any(t.get('reason') == 'SETTLEMENT' for t in report.ledger))


if __name__ == '__main__':
    unittest.main()
