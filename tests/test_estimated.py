"""Offline fixture checks only; no strategy returns or certified execution claims."""
from decimal import Decimal as D
import unittest

from polymarket_bot.estimated import Book, Estimator, Level, Order, Scenario
from polymarket_bot.linux_mvp.fees import MODEL, STATUS

MS = 1_000_000


def fee():
    return dict(status=STATUS, model=MODEL, market_id='m', condition_id='c', tokens=['t', 'other'],
                rate='0.07', exponent=1, observed_us=1793688300000000,
                window_start_us=1793688300000000, window_end_us=1793689200000000,
                raw=dict(version='v1', feesEnabled=True,
                         feeSchedule=dict(rate='0.07', exponent=1, takerOnly=True)))


def book(at=300, **kw):
    values = dict(session='s', market_id='m', condition_id='c', token_id='t',
                  received_ns=at * MS, bids=(Level(D('.48'), D('20')),),
                  asks=(Level(D('.50'), D('3')), Level(D('.52'), D('4'))),
                  min_size=D('5'), tick_size=D('.01'), fees=fee(), fee_received_ns=0,
                  received_wall_us=1793688300000000 + at * 1000)
    values.update(kw)
    return Book(**values)


def order(**kw):
    values = dict(session='s', market_id='m', condition_id='c', token_id='t',
                  submitted_ns=0, deadline_ns=2000 * MS, side='BUY', quantity=D('5'),
                  limit=D('.52'), kind='FAK', cancel_requested_ns=None)
    values.update(kw)
    return Order(**values)


class EstimatedTests(unittest.TestCase):
    def run_order(self, books=None, request=None, scenario=None, cash=D('100'), held=D('0')):
        return Estimator().execute(request or order(), books if books is not None else [book()],
                                   scenario or Scenario(), cash=cash, held=held)

    def test_next_receipt_depth_and_fee(self):
        result = self.run_order([book(100), book(300), book(400, bids=(Level(D('.08'), D('20')),), asks=(Level(D('.10'), D('90')),))])
        self.assertEqual(result['mode'], 'PAPER_ONLY_ESTIMATED')
        self.assertFalse(result['certified'])
        self.assertEqual(result['received_ns'], 300 * MS)
        self.assertEqual(result['filled'], D('5'))
        self.assertEqual(result['notional'], D('2.54'))
        self.assertEqual(result['fee'], D('.08744'))
        self.assertEqual(result['cash_delta'], -D('2.62744'))
        self.assertEqual(result['fills'], ((D('.50'), D('3')), (D('.52'), D('2'))))
        self.assertEqual(result['upstream_freshness'], 'UNKNOWN')

    def test_partial_remainder_cancelled(self):
        r = self.run_order(request=order(quantity=D('10')))
        self.assertEqual((r['status'], r['filled'], r['cancelled']), ('PARTIAL', D('7'), D('3')))

    def test_fok_cannot_partial(self):
        r = self.run_order(request=order(quantity=D('10'), kind='FOK'))
        self.assertEqual((r['status'], r['filled'], r['fee']), ('NO_FILL', D('0'), D('0')))
        self.assertEqual(r['fills'], ())

    def test_limit_not_cherry_picked(self):
        r = self.run_order([book(300), book(400, bids=(Level(D('.38'), D('20')),), asks=(Level(D('.40'), D('9')),))],
                           order(limit=D('.49')))
        self.assertEqual(r['status'], 'NO_FILL')
        self.assertEqual(r['received_ns'], 300 * MS)

    def test_missing_book_is_unevaluated_not_no_fill(self):
        for books in ([], [book(100)], [book(2000)]):
            r = self.run_order(books)
            self.assertEqual(r['status'], 'UNEVALUATED')
            self.assertIsNone(r['filled'])

    def test_empty_observed_asks_is_no_fill(self):
        r = self.run_order([book(asks=())])
        self.assertEqual((r['status'], r['filled']), ('NO_FILL', D('0')))

    def test_cancel_during_pending_delay_waits_until_ready(self):
        r = self.run_order(request=order(cancel_requested_ns=100 * MS))
        self.assertEqual((r['status'], r['terminal_ns']), ('CANCELLED', 250 * MS))
        r = self.run_order(request=order(cancel_requested_ns=300 * MS))
        self.assertEqual(r['status'], 'CANCELLED')
        self.assertIsNone(r['filled'])  # receipt gap before cancel, not proof of zero exchange fills

    def test_delay_and_deadline_boundaries(self):
        r = self.run_order([book(250)])
        self.assertEqual(r['status'], 'FILLED')
        r = self.run_order([book(300)], scenario=Scenario(exchange_delay_ns=250 * MS,
                                                       local_delay_ns=100 * MS))
        self.assertEqual(r['status'], 'UNEVALUATED')
        r = self.run_order(request=order(deadline_ns=200 * MS))
        self.assertEqual(r['status'], 'UNEVALUATED')
        self.assertIsNone(r['filled'])

    def test_cash_and_held_are_explicit(self):
        r = self.run_order(cash=D('2'))
        self.assertEqual((r['reason'], r['filled']), ('INSUFFICIENT_CASH', D('0')))
        r = self.run_order(request=order(side='SELL', limit=D('.48')), held=D('3'))
        self.assertEqual((r['reason'], r['filled']), ('INSUFFICIENT_POSITION', D('0')))
        r = self.run_order(request=order(side='SELL', limit=D('.48')), held=D('5'))
        self.assertEqual(r['cash_delta'], D('2.31264'))
        self.assertEqual(r['position_delta'], -D('5'))

    def test_scenarios_cost_depth_spread(self):
        r = self.run_order(scenario=Scenario(depth_fraction=D('.5'), extra_cost_bps=D('25')))
        self.assertEqual((r['status'], r['filled']), ('PARTIAL', D('3.5')))
        self.assertEqual(r['extra_cost'], r['notional'] * D('.0025'))
        self.assertEqual(r['fills'][0][0], D('.50'))  # cost never invents quote price
        r = self.run_order(scenario=Scenario(max_spread=D('.01')))
        self.assertEqual((r['status'], r['reason']), ('NO_FILL', 'SPREAD_LIMIT'))

    def test_fixture_sensitivity_not_strategy_performance(self):
        books = [book(300),
                 book(600, bids=(Level(D('.49'), D('20')),),
                      asks=(Level(D('.51'), D('3')), Level(D('.53'), D('4')))),
                 book(1100, bids=(Level(D('.52'), D('20')),),
                      asks=(Level(D('.54'), D('3')), Level(D('.56'), D('4'))))]
        scenarios = [Scenario(max_spread=D('.04')),
                     Scenario(exchange_delay_ns=250 * MS, local_delay_ns=250 * MS,
                              max_spread=D('.04')),
                     Scenario(exchange_delay_ns=250 * MS, local_delay_ns=750 * MS,
                              depth_fraction=D('.5'), extra_cost_bps=D('25'), max_spread=D('.04'))]
        results = [self.run_order(books, order(limit=D('.55')), s) for s in scenarios]
        self.assertEqual([r['filled'] for r in results], [D('5'), D('5'), D('1.5')])
        self.assertEqual([r['cash_delta'] for r in results],
                         [-D('2.62744'), -D('2.67734'), -D('.838105')])
        self.assertEqual([r['notional'] / r['filled'] for r in results],
                         [D('.508'), D('.518'), D('.54')])

    def test_snapshot_cannot_supply_liquidity_twice_in_one_call(self):
        # Duplicate receipt (same received_ns) in a single call is rejected by the
        # strictly-increasing monotonic check, not by cross-call instance state.
        estimator = Estimator()
        with self.assertRaises(ValueError):
            estimator.execute(order(), [book(300), book(300)], Scenario(), cash=D('100'), held=D('0'))

    def test_separate_orders_on_same_token_are_allowed(self):
        # Second BUY on the same token (a legitimate later round-trip) must succeed.
        # The DepthLedger in CausalReplay tracks consumption; Estimator is stateless.
        estimator = Estimator()
        r1 = estimator.execute(order(), [book(300)], Scenario(), cash=D('100'), held=D('0'))
        self.assertEqual(r1['status'], 'FILLED')
        # Second order uses a later distinct receipt — must not raise.
        r2 = estimator.execute(order(submitted_ns=1_000_000), [book(600)],
                               Scenario(), cash=D('100'), held=D('0'))
        self.assertEqual(r2['status'], 'FILLED')
        # Both receipts are genuinely different (600 MS vs 300 MS).
        self.assertNotEqual(r1['received_ns'], r2['received_ns'])

    def test_buy_and_sell_on_same_token_both_allowed(self):
        # BUY and SELL on the same token in separate calls must both proceed.
        estimator = Estimator()
        r_buy = estimator.execute(order(side='BUY', limit=D('.52')), [book(300)],
                                  Scenario(), cash=D('100'), held=D('0'))
        self.assertEqual(r_buy['status'], 'FILLED')
        r_sell = estimator.execute(order(side='SELL', limit=D('.48')), [book(600)],
                                   Scenario(), cash=D('100'), held=r_buy['filled'])
        self.assertEqual(r_sell['status'], 'FILLED')

    def test_review_duplicate_receipt_in_same_books_list_raises(self):
        # Same receipt timestamp appearing twice in books is monotonic-order violation.
        estimator = Estimator()
        with self.assertRaises(ValueError):
            estimator.execute(order(), [book(300), book(300)], Scenario(), cash=D('100'), held=D('0'))

    def test_review_observation_deadline_does_not_cancel(self):
        r = self.run_order([], order(cancel_requested_ns=3000 * MS))
        self.assertEqual(r['status'], 'UNEVALUATED')
        self.assertIsNone(r['filled'])
        r = self.run_order([], order(deadline_ns=200 * MS))
        self.assertEqual(r['status'], 'UNEVALUATED')
        self.assertIsNone(r['filled'])

    def test_review_precision_boundary(self):
        for quantity in (D('5.' + '9' * 600), D('1e1000'), D('1e-1000')):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.run_order(request=order(quantity=quantity))
        with self.assertRaises(ValueError):
            self.run_order([book(asks=(Level(D('.5'), D('1e1000')),))])

    def test_review_fee_trust_boundary(self):
        for changes in (dict(rate=True), dict(tokens='not-t'), dict(model='unsupported'),
                        dict(raw={}), dict(observed_us=999999999999999999)):
            with self.subTest(changes=changes):
                r = self.run_order([book(fees={**fee(), **changes})])
                self.assertEqual(r['status'], 'UNEVALUATED')

    def test_identity_session_and_monotonic_validation(self):
        for changes in (dict(session='restart'), dict(token_id='bad'), dict(market_id='bad'),
                        dict(condition_id='bad')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_order([book(**changes)])
        with self.assertRaises(ValueError):
            self.run_order([book(400), book(300)])
        with self.assertRaises(ValueError):
            self.run_order([book(300), book(300)])

    def test_unverified_missing_or_future_fees_fail_closed(self):
        for changes in (dict(fees=None), dict(fees={**fee(), 'status': 'UNVERIFIED'}),
                        dict(fees={**fee(), 'market_id': 'bad'}),
                        dict(fees={**fee(), 'rate': 'NaN'}),
                        dict(fees={**fee(), 'exponent': 2}), dict(fee_received_ns=301 * MS)):
            with self.subTest(changes=changes):
                r = self.run_order([book(**changes)])
                self.assertEqual(r['status'], 'UNEVALUATED')

    def test_boundaries_reject_invalid_inputs(self):
        for changes in (dict(quantity=D('NaN')), dict(quantity=D('-1')), dict(limit=D('2')),
                        dict(submitted_ns=True), dict(kind='GTC'), dict(side='BAD')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_order(request=order(**changes))
        for changes in (dict(asks=None), dict(min_size=None), dict(tick_size=D('0')),
                        dict(asks=(Level(D('.5'), D('NaN')),)),
                        dict(bids=(Level(D('.6'), D('5')),)),
                        dict(asks=(Level(D('.501'), D('5')),))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_order([book(**changes)])
        with self.assertRaises(ValueError):
            self.run_order(cash=D('NaN'))
        with self.assertRaises(ValueError):
            self.run_order(scenario=Scenario(depth_fraction=D('1.1')))
        self.assertEqual(self.run_order(request=order(quantity=D('4')))['reason'], 'MINIMUM_SIZE')
        self.assertEqual(self.run_order(request=order(limit=D('.511')))['reason'], 'INVALID_TICK')


if __name__ == '__main__':
    unittest.main()
