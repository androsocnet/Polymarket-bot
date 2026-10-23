"""CP6.2 SYNTHETIC offline lifecycle; never live observation evidence."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('polymarket_bot.full_window'),
                             'CP6.2 offline contract missing')
        from polymarket_bot.full_window import simulate_lifecycle
        self.simulate = simulate_lifecycle

    def test_normal_full_window_and_no_close_book(self):
        r = self.simulate()
        self.assertEqual(r['status'], 'COMPLETE')
        self.assertEqual((r['valid_pairs'], r['requests']), (450, 514))
        self.assertEqual(r['missing_targets'], [])
        self.assertEqual(r['last_target_us'], 898_000_000)
        self.assertEqual(r['stopped_us'], 900_000_000)
        self.assertEqual(r['active_processes'], 0)
        self.assertTrue(r['audit_reconciled'])
        self.assertEqual(r['evidence_kind'], 'SYNTHETIC_POLICY_SIMULATION')

    def test_startup_gaps_and_late_responses_never_complete(self):
        r = self.simulate(startup_us=3_000_000)
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertEqual(r['missing_targets'][:2], [0, 1])
        for fault in ('http_failure', 'partial', 'invalid', 'late'):
            with self.subTest(fault=fault):
                r = self.simulate(faults={12: fault})
                self.assertEqual(r['status'], 'PARTIAL')
                self.assertIn(12, r['missing_targets'])
                self.assertLess(r['valid_pairs'], 450)

    def test_clock_pause_requires_new_valid_anchor(self):
        r = self.simulate(faults={30: 'clock_uncertain', 90: 'clock_recalibrated'})
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertTrue(all(i in r['missing_targets'] for i in range(30, 90)))
        self.assertEqual(r['valid_pairs'], 390)
        r = self.simulate(faults={30: 'clock_discontinuity'})
        self.assertEqual(r['status'], 'FAILED')
        self.assertLessEqual(r['requests'], 40)

    def test_stale_metadata_identity_and_early_close_stop(self):
        for fault in ('metadata_stale', 'identity_conflict'):
            with self.subTest(fault=fault):
                r = self.simulate(faults={30: fault})
                self.assertEqual(r['status'], 'FAILED')
                self.assertEqual(r['valid_pairs'], 30)
        r = self.simulate(faults={100: 'closed'})
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertEqual(r['valid_pairs'], 100)
        self.assertEqual(r['stopped_us'], 200_000_000)

    def test_resource_exhaustion_refuses_next_dispatch(self):
        for budget, limit in [('request_limit', 5), ('ingress_limit', 5000),
                              ('disk_limit', 67_113_864)]:
            with self.subTest(budget=budget):
                r = self.simulate(**{budget: limit})
                self.assertEqual(r['status'], 'FAILED')
                self.assertLess(r['valid_pairs'], 450)
                self.assertEqual(r['active_processes'], 0)
                self.assertLessEqual(r['requests'], 5)
        r = self.simulate(faults={2: 'oversize'})
        self.assertEqual(r['status'], 'FAILED')
        self.assertEqual(r['valid_pairs'], 2)

    def test_interruption_and_storage_journal_failure_preserve_unknown(self):
        for fault in ('interrupt', 'storage_failure', 'journal_failure'):
            with self.subTest(fault=fault):
                r = self.simulate(faults={10: fault})
                self.assertNotEqual(r['status'], 'COMPLETE')
                self.assertEqual(r['valid_pairs'], 10)
                self.assertTrue(r['terminal_evidence'])
                self.assertEqual(r['active_processes'], 0)
                if fault != 'interrupt':
                    self.assertEqual(r['status'], 'FAILED')
                    self.assertFalse(r['audit_reconciled'])

    def test_memory_log_and_process_budget_exhaustion_fail_closed(self):
        for fault in ('memory_exhaustion', 'process_escape', 'log_exhaustion'):
            with self.subTest(fault=fault):
                r = self.simulate(faults={10:fault})
                self.assertEqual(r['status'],'FAILED')
                self.assertEqual(r['valid_pairs'],10)
                self.assertEqual(r['active_processes'],0)

    def test_cleanup_hard_ceiling_never_claims_graceful_success(self):
        r = self.simulate(cleanup_us=99_000_000)
        self.assertEqual(r['status'], 'FAILED')
        self.assertTrue(r['forced'])
        self.assertFalse(r['audit_reconciled'])
        self.assertLessEqual(r['elapsed_us'], 950_000_000)
        self.assertEqual(r['active_processes'], 0)

    def test_worst_case_fallback_and_zero_capture(self):
        r = self.simulate(faults={i:'fallback' for i in range(450)})
        self.assertEqual(r['requests'], 2764)
        self.assertEqual(r['status'], 'COMPLETE')
        r = self.simulate(startup_us=930_000_000)
        self.assertEqual(r['status'], 'FAILED')
        self.assertEqual(r['coverage_percent'], '0')

    def test_untrusted_scenario_validation(self):
        for kw in ({'startup_us': True}, {'startup_us': -1}, {'request_limit': 3001},
                   {'faults': {450: 'late'}}, {'faults': {1: 'unknown'}},
                   {'cleanup_us': -1}, {'ingress_limit': 0}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.simulate(**kw)


class ReferenceTests(unittest.TestCase):
    def test_unverified_boundary_never_authoritative_and_ambiguity_rejected(self):
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from polymarket_bot.recorder.reference import normalize_reference_fields, select_boundary_report, boundary_reference
        from polymarket_bot.market_data.models import MarketDataError
        report = normalize_reference_fields(b'SYNTHETIC_NOT_A_SIGNED_LIVE_REPORT',
            dict(feed_id=CHAINLINK_FEED_ID, report_schema_version=2, valid_from_seconds=100,
                 observations_seconds=101, expires_at_seconds=110, price_integer='6500000'),
            price_scale=2, decoder_revision='synthetic-v1', received_wall_us=102_000_000,
            received_server_us=102_000_000)
        self.assertEqual(report.decoded_price, Decimal('65000'))
        self.assertEqual(report.verification_status, 'UNVERIFIED')
        self.assertEqual(select_boundary_report([report],100_000_000),report)
        with self.assertRaises(MarketDataError):
            select_boundary_report([report,replace(report,report_hash='different')],100_000_000)
        for role,target in [('OPENING',100_000_000),('CLOSING',1_000_000_000)]:
            plan = boundary_reference('1001',role,window_start_us=100_000_000,
                window_end_us=1_000_000_000,learned_wall_us=0,learned_server_us=None)
            self.assertEqual((plan.target_us,plan.status,plan.origin),(target,'PENDING','NOT_FETCHED'))
            self.assertFalse(plan.available_live)

    def test_official_resolution_not_last_book_or_reference_price(self):
        from polymarket_bot.recorder.resolution import parse_resolution
        from polymarket_bot.recorder.models import MarketRevision
        from polymarket_bot.market_data.parsing import parse_market
        from tests.support import make_event, make_market, NOW, CONDITION
        market = parse_market(make_event(),make_market(),now=NOW)
        rev = MarketRevision(market,0,1,'synthetic','',b'{}',b'{}',0,{})
        for payload,want in [(None,'ABSENT'),({'condition_id':CONDITION,'status':'pending'},'PENDING'),
            ({'condition_id':CONDITION,'status':'resolved','payouts':['1','0']},'RESOLVED_DIRECTIONAL'),
            ({'condition_id':'wrong','status':'resolved','payouts':['1','0']},'INVALID'),
            ({'condition_id':CONDITION,'status':'resolved','payouts':['.5','.5']},'RESOLVED_OTHER')]:
            r = parse_resolution(payload,rev,raw_response=json.dumps(payload).encode(),
                                 learned_wall_us=1,learned_server_us=None)
            self.assertEqual(r.classification,want)
            if want=='RESOLVED_DIRECTIONAL':
                self.assertEqual(r.mapped_outcome,'UP')
        # Classification alone does not create CP6 settlement or normalize payouts.


class RecordingTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection', 'socket.socket.connect'):
            guard = patch(target, side_effect=AssertionError('NO HTTP/DNS CP6.2'))
            guard.start()
            self.addCleanup(guard.stop)

    def fixture(self, **kw):
        from tests.full_window_support import FullWindowFixture
        f = FullWindowFixture(**kw)
        self.addCleanup(f.close)
        return f

    def assess(self, f, result, **kw):
        import polymarket_bot.full_window as contract
        self.assertTrue(hasattr(contract, 'assess_recording'), 'read-only completeness contract missing')
        return contract.assess_recording(f.db.path, result.run_id, '1001',
                                dispatches=f.dispatches, cleanup_proven=kw.pop('cleanup_proven', True), **kw)

    def test_real_recorder_full_window_readonly_complete_and_replay(self):
        f = self.fixture()
        result = f.record()
        self.assertEqual((result.state, result.reason), ('STOPPED', 'SELECTED_MARKET_EXPIRED'))
        before = sha256(f.db.path.read_bytes()).hexdigest()
        r = self.assess(f, result)
        self.assertEqual(r['status'], 'PROVISIONAL')
        self.assertEqual(r['capture_status'], 'COMPLETE')
        self.assertFalse(r['runtime_evidence_verified'])
        self.assertEqual((r['valid_pairs'], r['coverage_percent']), (450, '100'))
        self.assertEqual(r['missing_targets'], [])
        self.assertTrue(r['replay_eligible'])
        self.assertTrue(r['opening_target_valid'])
        self.assertTrue(r['closing_target_valid'])
        self.assertIsNone(r['settlement'])
        self.assertEqual(before, sha256(f.db.path.read_bytes()).hexdigest())
        self.assertEqual(len(f.rows('SELECT * FROM markets')), 1)
        from polymarket_bot.strategy import load_strategy_recording, run_strategy
        recording = load_strategy_recording(f.db.path, result.run_id, '1001')
        self.assertEqual(len(recording.snapshots), 900)
        a = run_strategy(recording, holding_period=1000)
        b = run_strategy(recording, holding_period=1000)
        self.assertEqual(a, b)
        self.assertEqual(len(a['decisions']), 450)
        self.assertGreater(len(a['orders']), 10)
        ledger = a['execution']['ledger']
        self.assertTrue(all(t['snapshot']['request_start_mono_ns'] >
                            a['decisions'][next(i for i,d in enumerate(a['decisions'])
                              if d['instruction'] and d['instruction']['id']==t['id'])]['available_mono_ns']
                            for t in ledger if t.get('snapshot')))
        self.assertTrue(any(Decimal(p['quantity'])>0 for p in a['execution']['positions'].values()))
        self.assertIsNone(a['execution']['settlement'])

    def test_startup_and_failed_slots_never_complete(self):
        for kw in ({'startup_us':3_000_000}, {'faults':{3:'http_failure', 10:'invalid', 20:'partial', 40:'late', 50:'stale'}}):
            with self.subTest(kw=kw):
                f = self.fixture(**kw)
                result = f.record()
                r = self.assess(f, result)
                self.assertEqual(r['status'], 'PARTIAL')
                self.assertLess(r['valid_pairs'], 450)
                self.assertTrue(r['missing_targets'])
                self.assertTrue(r['replay_eligible'])
                from polymarket_bot.strategy import load_strategy_recording, run_strategy
                report = run_strategy(load_strategy_recording(f.db.path, result.run_id, '1001'))
                if kw.get('faults'):
                    self.assertTrue(any(t['status']=='REJECTED' for t in report['execution']['ledger']))

    def test_metadata_failure_and_identity_change_fail_closed(self):
        for fault in ('metadata_failure', 'identity_conflict', 'closed'):
            with self.subTest(fault=fault):
                f = self.fixture(faults={15:fault})
                result = f.record()
                self.assertNotEqual(result.state, 'STOPPED')
                r = self.assess(f, result)
                self.assertEqual(r['status'], 'FAILED')
                self.assertLess(r['valid_pairs'], 450)
                self.assertEqual(len(f.rows('SELECT * FROM markets')), 1)

    def test_interruption_preserves_terminal_and_releases_writer(self):
        f = self.fixture(faults={10:'interrupt'})
        result = f.record()
        self.assertEqual((result.state, result.exit_code), ('INTERRUPTED', 130))
        self.assertEqual(self.assess(f, result)['status'], 'PARTIAL')
        self.assertFalse(f.rows("SELECT * FROM sample_slots WHERE status='IN_PROGRESS'"))
        from polymarket_bot.recorder.store import RecorderStore
        store = RecorderStore.open(f.db.path)
        store.close()

    def test_storage_failure_never_claims_terminal_audit(self):
        f = self.fixture()
        with patch.object(f.store, 'finalize_slot', side_effect=sqlite3.OperationalError('SYNTHETIC disk full')):
            result = f.record()
        self.assertEqual((result.state, result.reason), ('FAILED', 'storage_failure'))
        r = self.assess(f, result)
        self.assertEqual(r['status'], 'FAILED')
        self.assertFalse(r['replay_eligible'])
        self.assertFalse(r['audit_reconciled'])

    def test_audit_mismatch_and_missing_terminal_evidence_fail_closed(self):
        f = self.fixture()
        result = f.record(stop=lambda:f.book_count>=8)
        self.assertEqual(self.assess(f, result)['status'], 'PARTIAL')
        f.dispatches.append(('GET', '/time', 123))
        self.assertEqual(self.assess(f, result)['status'], 'FAILED')
        self.assertFalse(self.assess(f, result)['replay_eligible'])
        r = self.assess(f, result, cleanup_proven=False)
        self.assertEqual(r['status'], 'FAILED')

    def test_resource_policy_rejects_excess_dispatches_even_if_audit_matches(self):
        f = self.fixture()
        result = f.record(stop=lambda:f.book_count>=8)
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(request_attempts)') if r[1]!='request_id']
            names = ','.join(columns)
            original = conn.execute('SELECT '+names+' FROM request_attempts LIMIT 1').fetchone()
            conn.executemany('INSERT INTO request_attempts ('+names+') VALUES ('+','.join('?' for _ in columns)+')', [original]*3000)
        method,path,start = (original[columns.index(k)] for k in ('method','path','start_mono_ns'))
        f.dispatches.extend([(method,path,start)]*3000)
        r = self.assess(f,result)
        self.assertEqual(r['status'],'FAILED')
        self.assertFalse(r['replay_eligible'])

    def test_freshness_uses_refresh_bytes_and_rejects_bad_grid(self):
        f = self.fixture()
        result = f.record()
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute("UPDATE request_attempts SET path='/markets/9999' WHERE path='/markets/1001' AND start_mono_ns>?", (f.epoch_ns+40_000_000_000,))
        f.dispatches = [(m, '/markets/9999' if p=='/markets/1001' and n>f.epoch_ns+40_000_000_000 else p,n) for m,p,n in f.dispatches]
        r = self.assess(f, result)
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertTrue(r['missing_targets'])
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('UPDATE sample_slots SET scheduled_us=scheduled_us+1 WHERE slot_index=0')
        self.assertEqual(self.assess(f, result)['status'], 'FAILED')

    def test_last_target_without_market_close_is_not_complete(self):
        f = self.fixture()
        result = f.record(stop=lambda:f.book_count>=450)
        self.assertEqual(result.reason,'STOP_REQUESTED')
        r = self.assess(f,result)
        self.assertEqual(r['valid_pairs'],450)
        self.assertEqual(r['status'],'PARTIAL')
        self.assertFalse(r['window_end_observed'])

    def test_corrupt_metadata_evidence_fails_closed(self):
        f = self.fixture()
        result = f.record(stop=lambda:f.book_count>=8)
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute("UPDATE market_revisions SET gamma_raw_z=x'00'")
        r = self.assess(f,result)
        self.assertEqual(r['status'],'FAILED')
        self.assertFalse(r['replay_eligible'])

    def test_cleanup_must_be_explicit_not_assumed(self):
        f = self.fixture()
        result = f.record(stop=lambda:f.book_count>=8)
        from polymarket_bot.full_window import assess_recording
        r = assess_recording(f.db.path,result.run_id,'1001',dispatches=f.dispatches)
        self.assertEqual(r['status'], 'PARTIAL')
        self.assertFalse(r['cleanup_proven'])
        self.assertTrue(r['replay_eligible'])

    def test_clock_recalibration_retains_multiple_anchors_and_uncertainty(self):
        f = self.fixture(faults={15:'clock_uncertain', 45:'clock_discontinuity'})
        result = f.record()
        anchors = f.rows('SELECT * FROM clock_anchors')
        self.assertGreater(len(anchors), 10)
        self.assertTrue(any(a['uncertainty_us']>1_250_000 for a in anchors))
        self.assertTrue(any(a['status']=='DISCONTINUITY' for a in anchors))
        self.assertNotEqual(self.assess(f, result)['status'], 'COMPLETE')


class AdversarialAssessmentTests(unittest.TestCase):
    """Mutation copies only; original recorder fixture never edited."""
    @classmethod
    def setUpClass(cls):
        from tests.full_window_support import FullWindowFixture
        cls.fixture = FullWindowFixture()
        cls.result = cls.fixture.record()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import shutil
        self.temp = TemporaryDirectory(dir='data', prefix='cp6-2-mutation-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'copy.sqlite'
        shutil.copyfile(self.fixture.db.path, self.db)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, args)

    def assess(self):
        from polymarket_bot.full_window import assess_recording
        with closing(sqlite3.connect(self.db)) as conn:
            dispatches = conn.execute('SELECT method,path,start_mono_ns FROM request_attempts').fetchall()
        return assess_recording(self.db, self.result.run_id, '1001', dispatches=dispatches, cleanup_proven=True)

    def insert_control(self, path, change=None, *, failure=False):
        from polymarket_bot.recorder.store import decode_evidence, encode_evidence
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM request_attempts WHERE path=? LIMIT 1', (path,)).fetchone())
            row.pop('request_id')
            start = conn.execute('SELECT start_mono_ns FROM sample_slots WHERE slot_index=100').fetchone()[0]
            row.update(start_mono_ns=start-20_000_000, end_mono_ns=start-10_000_000)
            if failure:
                row.update(result='http_error', http_status=503)
            else:
                payload = json.loads(decode_evidence(row['response_raw_z'],row['raw_size_bytes'],row['codec_version']))
                change(payload)
                blob, size, codec = encode_evidence(json.dumps(payload).encode())
                row.update(response_raw_z=blob, raw_size_bytes=size, codec_version=codec, response_bytes=size)
            conn.execute('INSERT INTO request_attempts ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))

    def test_h1_reviewer_target_100_three_seconds_late(self):
        self.sql('UPDATE sample_slots SET start_mono_ns=start_mono_ns+3000000000,end_mono_ns=end_mono_ns+3000000000 WHERE slot_index=100')
        self.sql('UPDATE request_attempts SET start_mono_ns=start_mono_ns+3000000000,end_mono_ns=end_mono_ns+3000000000,started_server_us=started_server_us+3000000,ended_server_us=ended_server_us+3000000 WHERE slot_id=(SELECT slot_id FROM sample_slots WHERE slot_index=100)')
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h1_late_availability_only(self):
        self.sql('UPDATE sample_slots SET end_mono_ns=end_mono_ns+3000000000 WHERE slot_index=100')
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h1_deadline_equality_and_one_microsecond_beyond(self):
        for delta in (0, 1000):
            with self.subTest(delta=delta):
                self.sql('UPDATE sample_slots SET end_mono_ns=(SELECT anchor_mono_ns+(scheduled_us+2000000-anchor_server_us)*1000 FROM clock_anchors WHERE anchor_id=sample_slots.anchor_id)+? WHERE slot_index=100', (delta,))
                self.assertIn(100, self.assess()['missing_targets'])

    def test_h2_newer_closed_detail(self):
        self.insert_control('/markets/1001', lambda p:p.update(active=False,closed=True))
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h2_newer_closed_event(self):
        self.insert_control('/events/2001', lambda p:p.update(closed=True))
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h2_newer_wrong_config(self):
        from tests.support import CONDITION
        self.insert_control('/clob-markets/'+CONDITION, lambda p:p.update(t=[{'t':'999'},{'t':'998'}]))
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h2_embedded_membership_contradiction(self):
        self.insert_control('/events/2001', lambda p:p['markets'][0].update(closed=True))
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h2_missing_event_refresh(self):
        self.sql("DELETE FROM request_attempts WHERE path='/events/2001'")
        self.assertEqual(self.assess()['valid_pairs'], 0)

    def test_h2_failure_between_matching_refreshes_latches_stop(self):
        self.insert_control('/markets/1001', failure=True)
        r = self.assess()
        self.assertIn(100, r['missing_targets'])
        self.assertIn(449, r['missing_targets'])

    def test_h3_reviewer_unavailable_calibration(self):
        self.sql('UPDATE clock_anchors SET request_end_mono_ns=request_end_mono_ns+60000000000')
        self.assertEqual(self.assess()['status'], 'FAILED')

    def test_h3_reviewer_underreported_uncertainty(self):
        self.sql('UPDATE clock_anchors SET uncertainty_us=999999')
        self.assertEqual(self.assess()['status'], 'FAILED')

    def test_h3_contradictory_anchor_mapping(self):
        self.sql('UPDATE clock_anchors SET anchor_server_us=anchor_server_us+1 WHERE anchor_id=2')
        self.assertEqual(self.assess()['status'], 'FAILED')

    def test_h3_anchor_raw_time_mismatch(self):
        self.sql('UPDATE clock_anchors SET server_seconds=server_seconds+1')
        self.assertEqual(self.assess()['status'], 'FAILED')

    def test_h3_calibration_available_only_after_book(self):
        # Move raw /time and its anchor together; summary agreement alone insufficient.
        self.sql('UPDATE request_attempts SET start_mono_ns=start_mono_ns+31000000000,end_mono_ns=end_mono_ns+31000000000 WHERE request_id=1')
        self.sql('UPDATE clock_anchors SET request_start_mono_ns=request_start_mono_ns+31000000000,request_end_mono_ns=request_end_mono_ns+31000000000,anchor_mono_ns=anchor_mono_ns+31000000000 WHERE anchor_id=1')
        self.assertIn(0, self.assess()['missing_targets'])

    def test_h3_duplicate_time_linkage_and_reversed_request(self):
        self.sql('UPDATE request_attempts SET end_mono_ns=start_mono_ns-1 WHERE request_id=1')
        self.assertEqual(self.assess()['status'],'FAILED')

    def test_h1_request_containment_includes_failed_attempts(self):
        # Valid observation still links original request. Extra failed attempt
        # outside target cannot hide behind valid paired snapshots.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM request_attempts WHERE slot_id=(SELECT slot_id FROM sample_slots WHERE slot_index=100) LIMIT 1').fetchone())
            row.pop('request_id')
            row.update(start_mono_ns=row['start_mono_ns']+3_000_000_000,
                       end_mono_ns=row['end_mono_ns']+3_000_000_000,
                       result='http_error',http_status=503)
            conn.execute('INSERT INTO request_attempts ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.assertIn(100,self.assess()['missing_targets'])

    def test_h2_wrong_selected_request_identity(self):
        self.sql("UPDATE request_attempts SET operation='CLOB_TIME' WHERE path='/events/2001'")
        self.assertEqual(self.assess()['valid_pairs'],0)

    def test_h2_raw_window_must_match_selected_database(self):
        from polymarket_bot.recorder.store import decode_evidence, encode_evidence
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for request_id,blob,size,codec in conn.execute("SELECT request_id,response_raw_z,raw_size_bytes,codec_version FROM request_attempts WHERE path IN ('/events/2001','/markets/1001')").fetchall():
                payload = json.loads(decode_evidence(blob,size,codec))
                row = payload['markets'][0] if 'markets' in payload else payload
                row['eventStartTime'] = '2026-01-01T00:00:00Z'
                row['endDate'] = '2026-01-01T00:15:00Z'
                encoded,n,v = encode_evidence(json.dumps(payload).encode())
                conn.execute('UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,codec_version=?,response_bytes=? WHERE request_id=?',(encoded,n,v,n,request_id))
            for revision_id,blob,sizes,codec in conn.execute('SELECT revision_id,gamma_raw_z,raw_sizes_json,codec_version FROM market_revisions').fetchall():
                sizes = json.loads(sizes)
                payload = json.loads(decode_evidence(blob,sizes['gamma'],codec))
                payload.update(eventStartTime='2026-01-01T00:00:00Z',endDate='2026-01-01T00:15:00Z')
                encoded,n,v = encode_evidence(json.dumps(payload).encode())
                sizes['gamma']=n
                conn.execute('UPDATE market_revisions SET gamma_raw_z=?,raw_sizes_json=? WHERE revision_id=?',(encoded,json.dumps(sizes),revision_id))
        self.assertEqual(self.assess()['valid_pairs'],0)

    def test_h1_one_microsecond_before_deadline_is_valid(self):
        self.sql('UPDATE sample_slots SET end_mono_ns=(SELECT anchor_mono_ns+(scheduled_us+1999999-anchor_server_us)*1000 FROM clock_anchors WHERE anchor_id=sample_slots.anchor_id),finished_server_us=scheduled_us+1999999 WHERE slot_index=100')
        self.assertNotIn(100,self.assess()['missing_targets'])

    def test_h3_duplicate_time_linkage(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(request_attempts)') if r[1]!='request_id']
            names = ','.join(columns)
            conn.execute('INSERT INTO request_attempts ('+names+') SELECT '+names+' FROM request_attempts WHERE request_id=1')
        self.assertEqual(self.assess()['status'],'FAILED')

    def test_h2_config_closed_flag(self):
        from tests.support import CONDITION
        self.insert_control('/clob-markets/'+CONDITION, lambda p:p.update(closed=True))
        self.assertIn(100, self.assess()['missing_targets'])

    def test_h4_missing_ingress_only(self):
        self.sql('UPDATE request_attempts SET response_bytes=NULL WHERE slot_id IS NULL')
        r = self.assess()
        self.assertIsNone(r['ingress_bytes'])
        self.assertIn('INGRESS_UNKNOWN',r['unverified'])
        self.assertNotEqual(r['status'],'COMPLETE')

    def test_h4_missing_storage_only(self):
        self.sql('DELETE FROM storage_checkpoints')
        r = self.assess()
        self.assertIn('STORAGE_CHECKPOINTS_MISSING',r['unverified'])
        self.assertNotEqual(r['status'],'COMPLETE')

    def test_h4_missing_ingress_and_checkpoints(self):
        self.sql('DELETE FROM storage_checkpoints')
        self.sql('UPDATE request_attempts SET response_bytes=NULL WHERE slot_id IS NULL')
        r = self.assess()
        self.assertNotEqual(r['status'], 'COMPLETE')
        self.assertIsNone(r['ingress_bytes'])
        self.assertIn('INGRESS_UNKNOWN',r['unverified'])
        self.assertIn('STORAGE_CHECKPOINTS_MISSING',r['unverified'])

    def test_h4_unsupported_cleanup_and_synthetic_success(self):
        r = self.assess()
        self.assertEqual(r['status'], 'PROVISIONAL')
        self.assertEqual(r['capture_status'], 'COMPLETE')
        self.assertEqual(r['evidence_kind'], 'SYNTHETIC')
        self.assertFalse(r['runtime_evidence_verified'])
        self.assertFalse(r['cleanup_proven'])
        self.assertTrue(r['control_evidence_verified'])
        self.assertTrue(r['replay_eligible'])

    def test_m1_corrupt_control_is_structured_failed(self):
        self.sql("UPDATE request_attempts SET response_raw_z=x'00' WHERE path='/markets/1001'")
        r = self.assess()
        self.assertEqual(r['status'], 'FAILED')
        self.assertFalse(r['replay_eligible'])
        self.assertTrue(any('EvidenceError' in p for p in r['problems']))


class FourFindingTests(unittest.TestCase):
    """Each case catches one trust-boundary bypass, using real SQLite evidence."""
    setUpClass = classmethod(AdversarialAssessmentTests.setUpClass.__func__)
    tearDownClass = classmethod(AdversarialAssessmentTests.tearDownClass.__func__)
    setUp = AdversarialAssessmentTests.setUp
    sql = AdversarialAssessmentTests.sql
    assess = AdversarialAssessmentTests.assess

    def control(self, path, *, offset=-20_000_000, seconds_delta=0, linked=False):
        from polymarket_bot.recorder.store import encode_evidence
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM request_attempts WHERE path=? ORDER BY request_id LIMIT 1', (path,)).fetchone())
            row.pop('request_id')
            slot = conn.execute('SELECT * FROM sample_slots WHERE slot_index=100').fetchone()
            book = conn.execute('SELECT * FROM request_attempts WHERE slot_id=?', (slot['slot_id'],)).fetchone()
            start = slot['start_mono_ns']+offset
            row.update(start_mono_ns=start, end_mono_ns=start+10_000_000, latency_ns=10_000_000,
                       started_wall_us=book['started_wall_us']+offset//1000,
                       ended_wall_us=book['started_wall_us']+offset//1000+10_000)
            if path=='/time':
                anchor = dict(conn.execute('SELECT * FROM clock_anchors WHERE request_end_mono_ns<=? ORDER BY request_end_mono_ns DESC LIMIT 1', (start,)).fetchone())
                seconds = (anchor['anchor_server_us']+(start+5_000_000-anchor['anchor_mono_ns'])//1000)//1_000_000+seconds_delta
                blob,size,codec = encode_evidence(json.dumps(seconds).encode())
                row.update(response_raw_z=blob,raw_size_bytes=size,codec_version=codec,response_bytes=size)
                if linked and not seconds_delta:
                    # Align midpoint to previous projection's whole second;
                    # valid recalibration must not shift existing target grid.
                    start = anchor['anchor_mono_ns']+(seconds*1_000_000-anchor['anchor_server_us'])*1000-5_000_000
                    row.update(start_mono_ns=start,end_mono_ns=start+10_000_000,
                               started_wall_us=book['started_wall_us']+(start-slot['start_mono_ns'])//1000,
                               ended_wall_us=book['started_wall_us']+(start-slot['start_mono_ns'])//1000+10_000)
                if linked:
                    anchor.pop('anchor_id')
                    anchor.update(request_start_mono_ns=start,request_end_mono_ns=start+10_000_000,
                                  request_start_wall_us=row['started_wall_us'],request_end_wall_us=row['ended_wall_us'],
                                  anchor_mono_ns=start+5_000_000,anchor_server_us=seconds*1_000_000,
                                  server_seconds=seconds,uncertainty_us=1_005_000,
                                  status='DISCONTINUITY' if seconds_delta else 'VALID')
                    conn.execute('INSERT INTO clock_anchors ('+','.join(anchor)+') VALUES ('+','.join('?' for _ in anchor)+')',tuple(anchor.values()))
            conn.execute('INSERT INTO request_attempts ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))

    def test_rh1_incomplete_groups_never_borrow_consumed_members(self):
        from tests.support import CONDITION
        config = '/clob-markets/'+CONDITION
        for paths in ((config,),('/markets/1001',config),('/events/2001',)):
            with self.subTest(paths=paths):
                for i,path in enumerate(paths):
                    self.control(path,offset=-60_000_000+i*20_000_000)
                r = self.assess()
                self.assertEqual(r['capture_status'],'PARTIAL')
                self.assertEqual(r['valid_pairs'],100)
                self.assertNotIn(99,r['missing_targets'])
                self.assertIn(449,r['missing_targets'])
                self.assertFalse(r['control_evidence_verified'])
                import shutil
                shutil.copyfile(self.fixture.db.path,self.db)

    def test_rh1_complete_group_positive(self):
        from tests.support import CONDITION
        for i,path in enumerate(('/events/2001','/markets/1001','/clob-markets/'+CONDITION)):
            self.control(path,offset=-60_000_000+i*20_000_000)
        r = self.assess()
        self.assertEqual(r['capture_status'],'COMPLETE')
        self.assertTrue(r['control_evidence_verified'])
        self.assertEqual(len(self.fixture.rows('SELECT * FROM market_revisions')),1)

    def test_rh2_successful_orphan_time_fails_even_when_matching(self):
        for delta in (0,60):
            with self.subTest(delta=delta):
                self.control('/time',seconds_delta=delta)
                r = self.assess()
                self.assertEqual(r['capture_status'],'FAILED')
                self.assertFalse(r['control_evidence_verified'])
                self.assertFalse(r['replay_eligible'])
                self.assertTrue(any('CLOCK_' in p for p in r['problems']))
                import shutil
                shutil.copyfile(self.fixture.db.path,self.db)

    def test_rh2_linked_discontinuity_terminates(self):
        self.control('/time',seconds_delta=60,linked=True)
        r = self.assess()
        self.assertEqual(r['status'],'FAILED')
        self.assertIn('CLOCK_DISCONTINUITY_STOP_REQUIRED',str(r['problems']))
        self.assertFalse(r['replay_eligible'])

    def test_rh2_properly_linked_new_calibration_positive(self):
        self.control('/time',linked=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            a = conn.execute('SELECT * FROM clock_anchors ORDER BY anchor_id DESC LIMIT 1').fetchone()
            next_end = conn.execute('SELECT min(request_end_mono_ns) FROM clock_anchors WHERE request_start_mono_ns>?',(a['request_end_mono_ns'],)).fetchone()[0]
            for slot in conn.execute('SELECT * FROM sample_slots WHERE start_mono_ns>=? AND start_mono_ns<?',(a['request_end_mono_ns'],next_end)).fetchall():
                project = lambda ns:a['anchor_server_us']+(ns-a['anchor_mono_ns'])//1000
                conn.execute('UPDATE sample_slots SET anchor_id=?,started_server_us=?,finished_server_us=? WHERE slot_id=?',(a['anchor_id'],project(slot['start_mono_ns']),project(slot['end_mono_ns']),slot['slot_id']))
                for r in conn.execute('SELECT * FROM request_attempts WHERE slot_id=?',(slot['slot_id'],)).fetchall():
                    conn.execute('UPDATE request_attempts SET anchor_id=?,started_server_us=?,ended_server_us=? WHERE request_id=?',(a['anchor_id'],project(r['start_mono_ns']),project(r['end_mono_ns']),r['request_id']))
        r = self.assess()
        self.assertEqual(r['capture_status'],'COMPLETE')
        self.assertTrue(r['control_evidence_verified'])
        self.assertTrue(r['replay_eligible'])

    def test_rh1_future_group_cannot_authorize_pending_capture(self):
        from tests.support import CONDITION
        for path,offset in (('/events/2001',-20_000_000),('/markets/1001',500_000_000),('/clob-markets/'+CONDITION,520_000_000)):
            self.control(path,offset=offset)
        r = self.assess()
        self.assertEqual(r['valid_pairs'],100)
        self.assertNotIn(99,r['missing_targets'])
        self.assertIn(100,r['missing_targets'])
        self.assertIn(449,r['missing_targets'])
        self.assertFalse(r['control_evidence_verified'])

    def test_rh1_pending_group_at_failed_capture_latches_stop(self):
        from tests.support import CONDITION
        for path,offset in (('/events/2001',-20_000_000),('/markets/1001',500_000_000),('/clob-markets/'+CONDITION,520_000_000)):
            self.control(path,offset=offset)
        self.sql("UPDATE sample_slots SET status='FAILED' WHERE slot_index=100")
        r = self.assess()
        self.assertEqual(r['valid_pairs'],100)
        self.assertIn(449,r['missing_targets'])
        self.assertFalse(r['control_evidence_verified'])

    def test_rh1_incomplete_after_last_snapshot_is_not_verified(self):
        self.control('/events/2001',offset=700_000_000_000)
        r = self.assess()
        self.assertFalse(r['control_evidence_verified'])
        self.assertNotEqual(r['capture_status'],'COMPLETE')

    def test_rh2_duplicate_anchor_cannot_consume_raw_twice(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(clock_anchors)') if r[1]!='anchor_id']
            names = ','.join(columns)
            conn.execute('INSERT INTO clock_anchors ('+names+') SELECT '+names+' FROM clock_anchors WHERE anchor_id=1')
        self.assertEqual(self.assess()['status'],'FAILED')

    def test_rm1_known_overrun_with_unknown_counter(self):
        self.sql('UPDATE request_attempts SET response_bytes=314572800 WHERE request_id=1')
        self.sql('UPDATE request_attempts SET response_bytes=NULL WHERE request_id=2')
        r = self.assess()
        self.assertEqual(r['status'],'FAILED')
        self.assertFalse(r['replay_eligible'])
        self.assertIsNone(r['ingress_bytes'])
        self.assertGreater(r['known_ingress_lower_bound'],268435456)
        self.assertIn('INGRESS_UNKNOWN',r['unverified'])
        self.assertIn('RESOURCE_BUDGET_EXCEEDED',r['problems'])

    def test_rm1_many_bounded_controls_with_unknown_counter(self):
        self.sql('UPDATE request_attempts SET response_bytes=5242880 WHERE slot_id IS NULL')
        self.sql('UPDATE request_attempts SET response_bytes=NULL WHERE request_id=2')
        r = self.assess()
        self.assertEqual(r['status'],'FAILED')
        self.assertFalse(r['replay_eligible'])
        self.assertIsNone(r['ingress_bytes'])

    def test_rm1_exact_cap_and_one_byte_over(self):
        self.sql('UPDATE request_attempts SET response_bytes=268435456-(SELECT sum(response_bytes) FROM request_attempts WHERE request_id!=1) WHERE request_id=1')
        r = self.assess()
        self.assertEqual(r['ingress_bytes'],268435456)
        self.assertEqual(r['known_ingress_lower_bound'],268435456)
        self.assertEqual(r['capture_status'],'COMPLETE')
        self.sql('UPDATE request_attempts SET response_bytes=response_bytes+1 WHERE request_id=1')
        r = self.assess()
        self.assertEqual(r['status'],'FAILED')
        self.assertFalse(r['replay_eligible'])

    def test_rm1_unknown_below_cap_and_all_known_positive(self):
        r = self.assess()
        self.assertEqual(r['known_ingress_lower_bound'],r['ingress_bytes'])
        self.assertEqual(r['capture_status'],'COMPLETE')
        self.sql('UPDATE request_attempts SET response_bytes=NULL WHERE request_id=2')
        r = self.assess()
        self.assertIsNone(r['ingress_bytes'])
        self.assertGreater(r['known_ingress_lower_bound'],0)
        self.assertLess(r['known_ingress_lower_bound'],268435456)
        self.assertIn('INGRESS_UNKNOWN',r['unverified'])
        self.assertEqual(r['capture_status'],'COMPLETE')

    def timestamp_payload(self, path, field, value, *, event_root=False):
        from polymarket_bot.recorder.store import decode_evidence,encode_evidence
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT request_id,response_raw_z,raw_size_bytes,codec_version FROM request_attempts WHERE path=? ORDER BY request_id LIMIT 1',(path,)).fetchone()
            payload = json.loads(decode_evidence(*row[1:]))
            target = payload if event_root or path.startswith('/markets/') else payload['markets'][0]
            target[field] = value
            blob,size,codec = encode_evidence(json.dumps(payload).encode())
            conn.execute('UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,codec_version=?,response_bytes=? WHERE request_id=?',(blob,size,codec,size,row[0]))

    def test_rm2_typed_timestamps_structured_failure_raw_unchanged(self):
        for path,root in (('/markets/1001',False),('/events/2001',False),('/events/2001',True)):
            for field in ('eventStartTime','endDate'):
                for value in (None,7,[],{},True,'bad-time','2026-10-10T00:45:00'):
                    with self.subTest(path=path,root=root,field=field,value=value):
                        self.timestamp_payload(path,field,value,event_root=root)
                        before = sha256(self.db.read_bytes()).hexdigest()
                        r = self.assess()
                        self.assertEqual(r['status'],'FAILED')
                        self.assertFalse(r['replay_eligible'])
                        self.assertTrue(any('METADATA_TIME' in p or 'Invalid isoformat' in p for p in r['problems']))
                        self.assertEqual(before,sha256(self.db.read_bytes()).hexdigest())
                        # Restore pristine bytes so each malformed value is isolated.
                        import shutil
                        shutil.copyfile(self.fixture.db.path,self.db)

    def test_rm2_valid_timezone_aware_positive(self):
        self.timestamp_payload('/events/2001','eventStartTime','2026-10-10T02:45:00+02:00',event_root=True)
        self.timestamp_payload('/events/2001','endDate','2026-10-10T01:00:00Z',event_root=True)
        r = self.assess()
        self.assertEqual(r['capture_status'],'COMPLETE')
        self.assertTrue(r['control_evidence_verified'])


if __name__ == '__main__':
    unittest.main()
