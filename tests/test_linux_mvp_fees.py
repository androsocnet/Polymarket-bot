"""Synthetic market fee regressions; no network or real trading."""
from dataclasses import replace
from decimal import Decimal as D
import json
import unittest

from tests import test_linux_mvp_store as store_tests, test_linux_mvp_engine as engine_tests
from tests.test_linux_mvp_store import BOT, RUN, MARKET_ID, MARKET
from tests.linux_mvp_support import START, UP_TOKEN, book, descriptor, rising, us
from polymarket_bot.market_data.models import BookLevel
from polymarket_bot.linux_mvp.report import build_report

META = {'version': 'v1', 'feesEnabled': True, 'feeType': 'crypto_fees_v2',
        'feeSchedule': {'rate': '0.07', 'exponent': 1, 'takerOnly': True}}


def evidence(metadata=None):
    from polymarket_bot.linux_mvp.fees import fee_evidence
    return fee_evidence(replace(descriptor(), metadata=META if metadata is None else metadata),
                        observed_us=us(START))


class FeeStoreTests(unittest.TestCase):
    setUp = store_tests.StoreTests.setUp
    def test_production_decimal_metadata_persists_and_reloads(self):
        from polymarket_bot.market_data.numeric import load_numeric_json
        from polymarket_bot.linux_mvp.fees import same_schedule
        ev = evidence(load_numeric_json(json.dumps(dict(META, feeSchedule={
            'rate': .07, 'exponent': 1, 'takerOnly': True, 'rebateRate': .2}))))
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        self.store.queue_order('decimal', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               D(1), MARKET_ID + ':0', 'test', us(START), fee_evidence=ev)
        saved = json.loads(self.store._conn.execute('SELECT evidence_json FROM order_fees').fetchone()[0])
        self.assertTrue(same_schedule(saved, ev))
        got = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, ask='1')}, ev, at_us=us(START))
        self.assertEqual((got['status'], got['fee']), ('FILLED', D(0)))
        self.assertEqual(build_report(self.store.path)['fee_accounting']['status'], 'MARKET_SCHEDULE_ESTIMATE')

    def test_obsolete_override_refused(self):
        from polymarket_bot.linux_mvp.config import Settings, ConfigError
        with self.assertRaisesRegex(ConfigError, 'MVP_FEE_BPS'):
            Settings.from_env({'MVP_FEE_BPS': '10'})

    def test_actual_gamma_fixture_keeps_fee_metadata(self):
        from pathlib import Path
        from polymarket_bot.market_data.parsing import parse_market
        fixture = json.loads((Path(__file__).parent / 'fixtures/cp5_live_clob/response-3.json').read_text())
        from datetime import datetime
        start = datetime.fromisoformat(fixture['eventStartTime'].replace('Z', '+00:00'))
        market = parse_market(dict(id='event', slug=fixture['slug'], title='Bitcoin Up or Down',
                                   active=True, closed=False), fixture, now=start)
        self.assertEqual(market.metadata['feeSchedule'], fixture['feeSchedule'])
        self.assertIs(market.metadata['feesEnabled'], True)

    def test_rounding_split_reserve_and_zero_size_depth(self):
        from polymarket_bot.linux_mvp.fees import level_fee, reserve_per_share
        self.assertEqual(level_fee(evidence(), D('1.01'), D('.13')), D('.00799'))
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(reserve_per_share(evidence()), D('1.0175'))
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        self.store.queue_order('round', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               D('2.02'), MARKET_ID + ':0', 'test', us(START), fee_evidence=evidence())
        depth = replace(book(UP_TOKEN), asks=(BookLevel(D('.12'), D(0)),
                                             BookLevel(D('.13'), D('1.01')),
                                             BookLevel(D('.13'), D('1.01'))))
        got = self.store.execute_pending(BOT, {'UP': depth}, evidence(), at_us=us(START))
        self.assertEqual(got['fee'], D('.01598'))
        self.assertEqual(got['filled'], D('2.02'))
        self.assertIsNone(self.store.execute_pending(BOT, {'UP': depth}, evidence(), at_us=us(START)))

    def test_insufficient_cash_does_not_persist_candidate_fills(self):
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        self.store.queue_order('poor', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               D(200), MARKET_ID + ':0', 'test', us(START), fee_evidence=evidence())
        got = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, ask='.5', size='200')}, evidence(), at_us=us(START))
        self.assertEqual(got['reason'], 'INSUFFICIENT_CASH')
        self.assertEqual(self.store.cash(BOT), D(100))
        self.assertEqual(self.store._conn.execute('SELECT fills_json FROM order_fees').fetchone()[0], '[]')

    def test_official_examples_and_invalid_metadata(self):
        from polymarket_bot.linux_mvp import fees
        self.assertEqual(evidence()['status'], 'MARKET_SCHEDULE_ESTIMATE')
        for price, want in [('.20', '1.12'), ('.50', '1.75'), ('.80', '1.12')]:
            self.assertEqual(fees.level_fee(evidence(), D('100'), D(price)), D(want))
        for metadata in [{}, dict(META, version='v2'), dict(META, feesEnabled=False),
                         dict(META, feeSchedule={'rate': 'NaN', 'exponent': 1, 'takerOnly': True}),
                         dict(META, feeSchedule={'rate': True, 'exponent': 1, 'takerOnly': True}),
                         dict(META, feeSchedule={'rate': '.07', 'exponent': 2, 'takerOnly': True})]:
            self.assertEqual(evidence(metadata)['status'], 'UNVERIFIED')
        self.assertEqual(fees.level_fee(evidence(), D('.0001'), D('.5')), D(0))
        self.assertEqual(fees.level_fee(evidence(), D(1), D(0)), D(0))
        self.assertEqual(fees.level_fee(evidence(), D(1), D(1)), D(0))
        self.assertEqual(evidence({'version': 'v1', 'feesEnabled': False})['rate'], '0')

    def test_partial_multilevel_buy_sell_cash_basis_provenance(self):
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        ev = evidence()
        self.store.queue_order('buy', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               D('110'), MARKET_ID + ':0', 'test', us(START), fee_evidence=ev)
        depth = replace(book(UP_TOKEN), asks=(BookLevel(D('.2'), D('50')),
                                             BookLevel(D('.8'), D('50'))))
        got = self.store.execute_pending(BOT, {'UP': depth}, ev, at_us=us(START))
        self.assertEqual((got['status'], got['filled'], got['notional'], got['fee']),
                         ('PARTIAL', D(100), D(50), D('1.12')))
        self.assertEqual(self.store.cash(BOT), D('48.88'))
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['cost_basis'], D('51.12'))
        self.store.queue_order('sell', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'SELL',
                               D('100'), MARKET_ID + ':1', 'test', us(START), fee_evidence=ev)
        depth = replace(depth, bids=(BookLevel(D('.8'), D('25')), BookLevel(D('.2'), D('25'))))
        got = self.store.execute_pending(BOT, {'UP': depth}, ev, at_us=us(START))
        self.assertEqual((got['status'], got['fee'], got['realized']),
                         ('PARTIAL', D('.56'), D('-1.12')))
        self.assertEqual(self.store.cash(BOT), D('73.32'))
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['cost_basis'], D('25.56'))
        self.store.reconcile(BOT)
        row = self.store._conn.execute('SELECT evidence_json, fills_json FROM order_fees WHERE order_id=?', ('buy',)).fetchone()
        self.assertEqual(json.loads(row[0])['collateral'], 'pUSD')
        self.assertEqual([D(f['fee']) for f in json.loads(row[1])], [D('.56'), D('.56')])
        report = build_report(self.store.path)
        self.assertEqual(report['fee_accounting']['status'], 'MARKET_SCHEDULE_ESTIMATE')
        self.assertFalse(report['performance']['profitability_verified'])

    def test_missing_changed_and_legacy_pending_cannot_get_market_fill(self):
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        for name, queued, current in [('missing', evidence(), evidence({})),
                                       ('changed', evidence(), evidence(dict(META, feeSchedule={'rate': '.08', 'exponent': 1, 'takerOnly': True}))),
                                       ('legacy', None, evidence())]:
            kwargs = {} if queued is None else {'fee_evidence': queued}
            self.store.queue_order(name, BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                                   D(1), MARKET_ID + ':0', 'test', us(START), **kwargs)
            got = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN)}, current, at_us=us(START))
            self.assertEqual(got['status'], 'REJECTED')
            self.assertIn('FEE', got['reason'])
        self.assertEqual(self.store.cash(BOT), D(100))

    def test_read_only_old_schema_and_idempotent_migration(self):
        import hashlib
        from polymarket_bot.linux_mvp.store import MvpStore
        self.store._conn.execute('DROP TABLE order_fees')
        path = self.store.path
        self.store._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        report = build_report(path)
        self.assertEqual(report['fee_accounting']['status'], 'UNVERIFIED_NO_FILLS')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.store.close()
        self.store = MvpStore.open(path)
        self.store.close()
        self.store = MvpStore.open(path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store._conn.execute('SELECT count(*) FROM order_fees').fetchone()[0], 0)
        self.assertEqual(self.store.cash(BOT), D(100))

    def test_legacy_report_never_claims_verified_fees(self):
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        self.store.queue_order('legacy', BOT, RUN, MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               D(1), MARKET_ID + ':0', 'test', us(START))
        self.store.execute_pending(BOT, {'UP': book(UP_TOKEN)}, D(10), at_us=us(START))
        report = build_report(self.store.path)
        self.assertEqual(report['fee_accounting']['status'], 'LEGACY_ESTIMATE_UNVERIFIED')


class FeeEngineTests(unittest.TestCase):
    setUp = engine_tests.EngineTests.setUp
    engine = engine_tests.EngineTests.engine
    step = engine_tests.EngineTests.step
    decisions = engine_tests.EngineTests.decisions
    def test_changed_fee_rejects_pending_without_reusing_queue_schedule(self):
        changed = replace(descriptor(), metadata=dict(META, feeSchedule={
            'rate': '.08', 'exponent': 1, 'takerOnly': True}))
        engine = self.engine([rising(0), rising(1), rising(2), replace(rising(3), market=changed)])
        for _ in range(4):
            self.step(engine)
        order = self.store.orders('default')[0]
        self.assertEqual(order['status'], 'REJECTED')
        self.assertEqual(order['reason'], 'FEE_UNVERIFIED_OR_CHANGED')
        self.assertEqual(self.store.cash('default'), D(100))

    def test_missing_fee_metadata_keeps_signal_but_blocks_orders(self):
        samples = [replace(rising(i), market=replace(descriptor(), metadata={})) for i in range(4)]
        engine = self.engine(samples)
        for _ in samples:
            self.step(engine)
        self.assertEqual(self.decisions()[2]['signal'], 'UP')
        self.assertEqual(self.decisions()[2]['reason'], 'FEE_UNVERIFIED')
        self.assertEqual(self.store.orders('default'), [])
