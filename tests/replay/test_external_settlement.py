"""Synthetic disposable provenance bundles, never historical market evidence."""
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import zlib

from tests.test_paper import OfflineTests
from tests.replay import test_legacy
from tests.replay.test_legacy import legacy_fixture, MARKET, RUN


class ExternalSettlementTests(OfflineTests):
    execute = test_legacy.LegacyTests.execute
    def load(self, **kwargs):
        try:
            return test_legacy.LegacyTests.load(self, **kwargs)
        except TypeError as exc:
            if 'unexpected keyword argument' in str(exc):
                self.fail('External settlement opt-in missing: ' + str(exc))
            raise

    def cli(self, *args):
        try:
            return test_legacy.LegacyTests.cli(self, *args)
        except SystemExit as exc:
            self.fail('External settlement CLI rejected arguments: ' + str(exc))

    resolution = test_legacy.LegacyTests.resolution

    def setUp(self):
        super().setUp()
        self.f = legacy_fixture(self, rising_books=True)
        self.db = self.f.db.path
        self.resolution()
        market = self.f.rows('SELECT * FROM markets')[0]
        gamma = json.loads(zlib.decompress(self.f.rows('SELECT gamma_raw_z FROM market_revisions LIMIT 1')[0]['gamma_raw_z']))
        gamma.update(closed=True, umaResolutionStatus='resolved', outcomePrices='["1","0"]')
        raw = self.f.rows('SELECT raw_resolution_z FROM resolution_observations')[0]['raw_resolution_z']
        end = market['window_end_us']
        sources = {}
        for name, url, body in (
            ('gamma', 'https://gamma-api.polymarket.com/markets/' + MARKET, json.dumps(gamma)),
            ('data_api', 'https://data-api.polymarket.com/v2/resolutions?condition=' + market['condition_id'], zlib.decompress(raw).decode()),
        ):
            sources[name] = dict(method='GET', url=url, final_url=url, http_status=200,
                                 started_utc=datetime.fromtimestamp((end + 180_000_000) / 1_000_000, timezone.utc).isoformat(),
                                 retrieved_utc=datetime.fromtimestamp((end + 181_000_000) / 1_000_000, timezone.utc).isoformat(),
                                 size_bytes=len(body.encode()), sha256=sha256(body.encode()).hexdigest(), raw=body)
        self.bundle = dict(schema='phase3b-external-settlement-v1', market_id=MARKET, run_id=RUN,
                           condition_id=market['condition_id'], source_sha256=sha256(self.db.read_bytes()).hexdigest(),
                           window_start_us=market['window_start_us'], window_end_us=end, sources=sources)
        self.path = self.db.parent / 'external.json'

    def save(self):
        raw = (json.dumps(self.bundle, sort_keys=True) + '\n').encode()
        self.path.write_bytes(raw)
        return dict(settlement_file=self.path, settlement_sha256=sha256(raw).hexdigest())

    def update_raw(self, name, change):
        source = self.bundle['sources'][name]
        row = json.loads(source['raw'])
        change(row)
        source['raw'] = json.dumps(row)
        source.update(size_bytes=len(source['raw'].encode()), sha256=sha256(source['raw'].encode()).hexdigest())

    def test_later_evidence_settles_open_inventory_without_decision_leakage(self):
        # Break: ignoring opt-in, backdating at_us, exposing final data to strategy, or wrong payout.
        from polymarket_bot.replay.engine import run_replay
        before = self.db.read_bytes()
        baseline, _ = self.load()
        loaded, scope = self.load(**self.save())
        self.assertEqual(loaded[:3], baseline[:3])
        self.assertEqual(loaded[5], baseline[5])
        self.assertEqual(loaded[4]['status'], 'RESOLVED')
        self.assertEqual(loaded[4]['winning_outcome'], 'UP')
        self.assertEqual(loaded[3]['at_us'], self.bundle['window_end_us'] + 181_000_000)
        self.assertEqual(scope['external_settlement']['bundle_sha256'], sha256(self.path.read_bytes()).hexdigest())
        kwargs = dict(paper_recording=loaded[5], threshold='0.01', holding_period=450)
        unknown = run_replay(baseline[0], settlement_evidence=baseline[3], **kwargs)
        settled = run_replay(loaded[0], settlement_evidence=loaded[3], **kwargs)
        self.assertEqual(unknown.cash, '98.23824')
        self.assertEqual(unknown.positions['UP']['quantity'], '4')
        self.assertEqual(settled.cash, '102.23824')
        self.assertEqual(settled.realized_pnl, '2.23824')
        self.assertEqual(settled.total_pnl, '2.23824')
        self.assertEqual(settled.unrealized_pnl, '0')
        self.assertEqual(settled.settlement['settled']['UP']['payout'], '4')
        self.assertEqual(settled.positions['UP']['quantity'], '0')
        for field in ('decisions', 'ledger', 'counts', 'fees_paid', 'config_hash', 'source_fingerprint'):
            self.assertEqual(getattr(settled, field), getattr(unknown, field), field)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(any(Path(str(self.db) + s).exists() for s in ('-wal', '-shm', '-journal')))

    def test_losing_open_inventory_reconciles_without_payout(self):
        # Break: paying losing holders, retaining their basis, or double-counting fees.
        from polymarket_bot.replay.engine import run_replay
        self.update_raw('gamma', lambda r: r.update(outcomePrices='["0","1"]'))
        self.update_raw('data_api', lambda r: r['data'][0].update(payouts=[0, 1000000]))
        source = self.bundle['sources']['data_api']
        raw = source['raw'].encode()
        self.execute('UPDATE resolution_observations SET raw_resolution_z=?,raw_size_bytes=?,response_hash=?',
                     (zlib.compress(raw), len(raw), source['sha256']))
        self.execute("UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,response_bytes=? WHERE operation='RESOLUTION'",
                     (zlib.compress(raw), len(raw), len(raw)))
        self.bundle['source_sha256'] = sha256(self.db.read_bytes()).hexdigest()
        loaded, _ = self.load(**self.save())
        report = run_replay(loaded[0], paper_recording=loaded[5], settlement_evidence=loaded[3], threshold='0.01', holding_period=450)
        self.assertEqual(report.settlement['winning_outcome'], 'DOWN')
        self.assertEqual(report.cash, '98.23824')
        self.assertEqual(report.realized_pnl, '-1.76176')
        self.assertEqual(report.total_pnl, '-1.76176')
        self.assertEqual(report.positions['UP']['quantity'], '0')
        self.assertEqual(report.settlement['settled']['UP']['payout'], '0')

    def test_rejects_adversarial_bundles(self):
        # Break: accepting body/hash edits, untrusted origins, false times, or bypassing verifier.
        original = json.dumps(self.bundle)
        cases = [
            ('market', lambda b: b.update(market_id='999')),
            ('condition', lambda b: b.update(condition_id='0x' + 'ff' * 32)),
            ('run', lambda b: b.update(run_id='other')),
            ('source', lambda b: b.update(source_sha256='0' * 64)),
            ('window', lambda b: b.update(window_end_us=b['window_end_us'] + 1)),
            ('schema', lambda b: b.update(schema='other')),
            ('missing source', lambda b: b['sources'].pop('data_api')),
            ('raw hash', lambda b: b['sources']['gamma'].update(raw='{}')),
            ('raw size', lambda b: b['sources']['gamma'].update(size_bytes=0)),
            ('endpoint', lambda b: b['sources']['gamma'].update(url='https://example.test/markets/' + MARKET)),
            ('redirect', lambda b: b['sources']['gamma'].update(final_url='https://example.test/')),
            ('method', lambda b: b['sources']['gamma'].update(method='POST')),
            ('status', lambda b: b['sources']['gamma'].update(http_status=500)),
            ('no zone', lambda b: b['sources']['gamma'].update(retrieved_utc='2026-10-28T06:00:00')),
            ('backdated', lambda b: b['sources']['gamma'].update(retrieved_utc='2000-01-01T00:00:00+00:00')),
            ('future', lambda b: b['sources']['gamma'].update(retrieved_utc='2099-01-01T00:00:00+00:00')),
            ('nonfinal', lambda b: self.update_raw('gamma', lambda g: g.update(closed=False))),
            ('tokens', lambda b: self.update_raw('gamma', lambda g: g.update(clobTokenIds='["1","2"]'))),
            ('contradiction', lambda b: self.update_raw('gamma', lambda g: g.update(outcomePrices='["0","1"]'))),
            ('resolution mismatch', lambda b: self.update_raw('data_api', lambda r: r['data'][0].update(was_disputed=True))),
        ]
        for name, change in cases:
            with self.subTest(name=name):
                self.bundle = json.loads(original)
                change(self.bundle)
                with self.assertRaises(ValueError):
                    self.load(**self.save())

    def test_unchanged_verifier_rejects_invalid_recorded_resolution(self):
        # Break: honoring recorded directional classification over verifier requirements.
        original = json.dumps(self.bundle)
        changes = (
            lambda r: r['data'][0].update(was_disputed=True),
            lambda r: r['data'][0].update(extended_review=True),
            lambda r: r['data'][0].update(payouts=[500000, 500000]),
            lambda r: r['data'][0].update(transaction_hash='invalid'),
            lambda r: r['data'][0].update(resolved_at='2099-01-01T00:00:00Z'),
            lambda r: r['data'].append(dict(r['data'][0])),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.bundle = json.loads(original)
                self.update_raw('data_api', change)
                source = self.bundle['sources']['data_api']
                raw = source['raw'].encode()
                self.execute('UPDATE resolution_observations SET raw_resolution_z=?,raw_size_bytes=?,response_hash=?',
                             (zlib.compress(raw), len(raw), source['sha256']))
                self.execute("UPDATE request_attempts SET response_raw_z=?,raw_size_bytes=?,response_bytes=? WHERE operation='RESOLUTION'",
                             (zlib.compress(raw), len(raw), len(raw)))
                self.bundle['source_sha256'] = sha256(self.db.read_bytes()).hexdigest()
                with self.assertRaisesRegex(ValueError, 'external settlement rejected'):
                    self.load(**self.save())

    def test_saved_genuine_bundle_passes_unchanged_verifier(self):
        # Real retained responses only; no network or genuine DB access in test suite.
        from polymarket_bot.linux_mvp.settlement import verify_evidence
        bundle = json.loads((Path(__file__).resolve().parents[2] / 'docs/superpowers/evidence/5483041/settlement.json').read_text())
        for source in bundle['sources'].values():
            self.assertEqual(sha256(source['raw'].encode()).hexdigest(), source['sha256'])
            self.assertEqual(len(source['raw'].encode()), source['size_bytes'])
        gamma = json.loads(bundle['sources']['gamma']['raw'])
        tokens = dict(zip(('UP', 'DOWN'), json.loads(gamma['clobTokenIds'])))
        at_us = int(datetime.fromisoformat(bundle['sources']['data_api']['retrieved_utc']).timestamp() * 1_000_000)
        evidence = dict(gamma_raw=bundle['sources']['gamma']['raw'], resolution_raw=bundle['sources']['data_api']['raw'])
        result = verify_evidence(evidence, window=dict(market_id=MARKET, condition_id=bundle['condition_id'],
                                 start_us=bundle['window_start_us'], end_us=bundle['window_end_us']), tokens=tokens, at_us=at_us)
        self.assertEqual(result['status'], 'RESOLVED')
        self.assertEqual(result['winning_outcome'], 'DOWN')
        self.assertEqual(result['resolved_at'], '2026-10-11T07:30:52Z')

    def test_duplicate_manifest_keys_fail_closed(self):
        self.save()
        raw = self.path.read_bytes().replace(b'{', b'{"market_id":"wrong",', 1)
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.load(settlement_file=self.path, settlement_sha256=sha256(raw).hexdigest())

    def test_requires_bundle_pin_and_rejects_coordinated_edits(self):
        args = self.save()
        with self.assertRaises(ValueError):
            self.load(settlement_file=self.path)
        with self.assertRaises(ValueError):
            self.load(settlement_sha256=args['settlement_sha256'])
        self.update_raw('gamma', lambda r: r.update(closed=False))
        self.save()
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.load(**args)
        self.path.write_text('[]')
        with self.assertRaises(ValueError):
            self.load(settlement_file=self.path, settlement_sha256=sha256(self.path.read_bytes()).hexdigest())

    def test_cli_external_opt_in_deterministic_and_fail_closed(self):
        args = self.save()
        pin = sha256(self.db.read_bytes()).hexdigest()
        extra = ('--legacy-scope', '--expected-sha256', pin, '--settlement-file', str(self.path),
                 '--settlement-sha256', args['settlement_sha256'])
        a, b = self.db.parent / 'one', self.db.parent / 'two'
        self.assertEqual(self.cli(a, *extra), 0)
        self.assertEqual(self.cli(b, *extra), 0)
        for name in ('report.json', 'decisions.jsonl', 'ledger.jsonl'):
            self.assertEqual((a / name).read_bytes(), (b / name).read_bytes())
        self.assertEqual(json.loads((a / 'report.json').read_text())['settlement']['status'], 'RESOLVED')
        for index, flags in enumerate((extra[3:], extra[:5], extra[:3] + extra[5:])):
            out = self.db.parent / ('bad-' + str(index))
            self.assertEqual(self.cli(out, *flags), 2)
            self.assertFalse(out.exists())
