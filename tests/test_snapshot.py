import unittest
from tests.support import *
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.snapshot import collect_snapshot

NEW_UP, NEW_DOWN = "3", "4"
NEW_CONDITION = "0x"+"cd"*32

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
    def collect(self, gamma, clob):
        return collect_snapshot(gamma,clob,monotonic=self.clock.monotonic)
    def rolling(self, second_rollover=False):
        new = make_market(id="1002",conditionId=NEW_CONDITION,clobTokenIds='["3","4"]',
                          eventStartTime=(NOW+timedelta(minutes=10)).isoformat(),
                          endDate=(NOW+timedelta(minutes=25)).isoformat())
        class RollingGamma(FakeGamma):
            def event_by_slug(gamma, slug, *, deadline):
                gamma.slug_calls.append((slug,deadline))
                return make_event(markets=[new]) if len(gamma.slug_calls)>1 else make_event()
        seconds = int(NOW.timestamp())
        times = [seconds,seconds+600,seconds+601,seconds+(1500 if second_rollover else 601)]
        clob = FakeClob(self.clock,times=times)
        clob.books.update({NEW_UP:make_book(asset_id=NEW_UP,market=NEW_CONDITION),
                           NEW_DOWN:make_book(asset_id=NEW_DOWN,market=NEW_CONDITION)})
        return RollingGamma(), clob
    def test_rollover_discards_both_books_and_rediscover_once(self):
        gamma, clob = self.rolling()
        result = self.collect(gamma,clob)
        self.assertTrue(result.rolled_over)
        self.assertEqual(result.market.market_id,"1002")
        self.assertEqual((result.up.book.token_id,result.down.book.token_id),(NEW_UP,NEW_DOWN))
        self.assertEqual([c[0] for c in clob.book_calls],[TOKEN_UP,TOKEN_DOWN,NEW_UP,NEW_DOWN])
        self.assertEqual(set(clob.time_calls+[c[1] for c in clob.book_calls]+[c[1] for c in gamma.slug_calls]),{120.0})

    def error(self, gamma, clob, code):
        with self.assertRaises(MarketDataError) as caught:
            self.collect(gamma,clob)
        self.assertEqual(caught.exception.code,code)
        return caught.exception

    def test_second_rollover_exhausts(self):
        gamma, clob = self.rolling(second_rollover=True)
        self.error(gamma,clob,"rollover_exhausted")
        self.assertEqual(len(gamma.slug_calls),2)

    def test_independent_book_timestamps_are_preserved(self):
        result = self.collect(FakeGamma(),FakeClob(self.clock))
        self.assertNotEqual(result.up.book.source_at,result.down.book.source_at)
        self.assertLess(result.up.request_finished_at,result.down.request_finished_at)

    def test_no_book_reuse_across_runs_or_conditions(self):
        clob = FakeClob(self.clock)
        self.collect(FakeGamma(),clob)
        self.collect(FakeGamma(),clob)
        self.assertEqual([c[0] for c in clob.book_calls],[TOKEN_UP,TOKEN_DOWN,TOKEN_UP,TOKEN_DOWN])

    def test_book_404_during_rollover_rediscover_once(self):
        gamma, clob = self.rolling()
        clob.books[TOKEN_DOWN] = MarketDataError("book_unavailable","missing",http_status=404)
        result = self.collect(gamma,clob)
        self.assertTrue(result.rolled_over)
        self.assertEqual(result.up.book.token_id,NEW_UP)
        self.assertEqual(len(gamma.slug_calls),2)

    def test_book_404_inside_window_fails(self):
        gamma, clob = FakeGamma(), FakeClob(self.clock)
        clob.books[TOKEN_UP] = MarketDataError("book_unavailable","missing",http_status=404)
        self.error(gamma,clob,"book_unavailable")
        self.assertEqual(len(gamma.slug_calls),1)
        self.assertEqual(len(clob.time_calls),2)

    def test_partial_book_failure_is_not_success(self):
        gamma, clob = FakeGamma(), FakeClob(self.clock)
        clob.books[TOKEN_DOWN] = MarketDataError("transport_error","offline")
        self.error(gamma,clob,"transport_error")
        self.assertEqual(len(gamma.slug_calls),1)

    def test_invalid_time_never_uses_local_wall_clock(self):
        for times in ([True], [int(NOW.timestamp()),True]):
            with self.subTest(times=times):
                self.error(FakeGamma(),FakeClob(self.clock,times=times),"time_unavailable")

    def test_readiness_failure_keeps_current_metadata(self):
        for readiness in (None,False):
            gamma = FakeGamma(fast=make_event(markets=[make_market(acceptingOrders=readiness)]))
            clob = FakeClob(self.clock)
            error = self.error(gamma,clob,"book_unavailable")
            self.assertEqual(error.market.market_id,MARKET_ID)
            self.assertEqual(clob.book_calls,[])

    def test_identity_mismatch_does_not_rediscover(self):
        gamma, clob = FakeGamma(), FakeClob(self.clock)
        clob.books[TOKEN_UP] = make_book(asset_id=TOKEN_DOWN)
        self.error(gamma,clob,"book_identity_mismatch")
        self.assertEqual(len(gamma.slug_calls),1)

    def test_total_deadline_includes_final_validation(self):
        class SlowFinal(FakeClob):
            def get_time(clob, *, deadline):
                if clob.time_calls: self.clock.advance(121)
                return super().get_time(deadline=deadline)
        self.error(FakeGamma(),SlowFinal(self.clock),"transport_error")
