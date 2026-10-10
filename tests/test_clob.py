import unittest
from decimal import Decimal
from tests.support import *
from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.clob import parse_book
from polymarket_bot.market_data.models import MarketDataError

class ClobTests(unittest.TestCase):
    def parse(self, **overrides):
        return parse_book(JsonRead(make_book(**overrides),2.0,2.1), token_id=TOKEN_UP, condition_id=CONDITION)
    def test_unsorted_book_uses_decimal_extrema(self):
        book = self.parse(bids=[{"price":"0.58","size":"3"},{"price":"0.60","size":"2"}],
                          asks=[{"price":"0.64","size":"5"},{"price":"0.61","size":"4"}])
        self.assertEqual((book.best_bid,book.best_ask),(Decimal("0.60"),Decimal("0.61")))
        self.assertEqual((book.midpoint,book.spread),(Decimal("0.605"),Decimal("0.01")))

    def error(self, code="invalid_book", **overrides):
        with self.assertRaises(MarketDataError) as caught:
            self.parse(**overrides)
        self.assertEqual(caught.exception.code, code)

    def test_token_and_condition_mismatch_fail(self):
        self.error("book_identity_mismatch", asset_id=TOKEN_DOWN)
        self.error("book_identity_mismatch", market="0x"+"cd"*32)

    def test_oversized_book_identifier_is_invalid_book(self):
        token = "1" * 500
        with self.assertRaises(MarketDataError) as caught:
            parse_book(JsonRead(make_book(asset_id=token), 2.0, 2.1),
                       token_id=token, condition_id=CONDITION)
        self.assertEqual(caught.exception.code, "invalid_book")

    def test_empty_sides_never_use_gamma_or_half(self):
        for sides in ({"bids":[]}, {"asks":[]}, {"bids":[], "asks":[]}):
            book = self.parse(**sides)
            self.assertIsNone(book.midpoint)
            self.assertIsNone(book.spread)
        self.assertIsNone(self.parse(bids=[]).best_bid)
        self.assertEqual(self.parse(bids=[]).best_ask, Decimal("0.61"))

    def test_zero_sizes_are_ignored_duplicate_top_sizes_sum(self):
        book = self.parse(bids=[{"price":"0.90","size":"0"},{"price":"0.60","size":"2"},{"price":"0.60","size":"3"}])
        self.assertEqual(book.best_bid, Decimal("0.60"))
        self.assertEqual(book.bid_size, Decimal("5"))

    def test_locked_book_valid_crossed_book_invalid(self):
        self.assertEqual(self.parse(asks=[{"price":"0.60","size":"1"}]).spread, Decimal("0"))
        self.error(asks=[{"price":"0.59","size":"1"}])

    def test_malformed_levels_fail_entire_book(self):
        for levels in (None, {}, [{"size":"1"}], [{"price":"0.4"}], [None],
                       [{"price":"NaN","size":"1"}], [{"price":"Infinity","size":"1"}],
                       [{"price":"-0.1","size":"1"}], [{"price":"1.1","size":"1"}],
                       [{"price":"0.1","size":"-1"}], [{"price":"0.1","size":"NaN"}]):
            with self.subTest(levels=levels):
                self.error(bids=levels)

    def test_price_size_booleans_fail(self):
        self.error(bids=[{"price":True,"size":"1"}])
        self.error(bids=[{"price":"0.1","size":False}])

    def test_extreme_exponent_is_domain_error(self):
        for raw in ('1e1000000', '1e-1000000', '1e999999999999999999999'):
            with self.subTest(raw=raw):
                self.error(bids=[{'price': '0.60', 'size': raw}])

    def test_seconds_server_time_milliseconds_book_time(self):
        from polymarket_bot.market_data.clob import ServerClock
        clock = ServerClock(JsonRead(int(NOW.timestamp()),0,0), monotonic=lambda:0)
        self.assertEqual(clock.now(), NOW)
        self.assertEqual(self.parse().source_at, NOW)
        for raw in (str(int(NOW.timestamp())), True, None, "bad"):
            self.error(timestamp=raw)
        for raw in (True, str(int(NOW.timestamp())), -1, 1.5):
            with self.assertRaises(MarketDataError) as caught:
                ServerClock(JsonRead(raw,0,0), monotonic=lambda:0)
            self.assertEqual(caught.exception.code, "time_unavailable")

    def test_server_clock_anchors_request_midpoint(self):
        from polymarket_bot.market_data.clob import ServerClock
        clock = ServerClock(JsonRead(int(NOW.timestamp()),0,2), monotonic=lambda:2)
        self.assertEqual(clock.now(), NOW+timedelta(seconds=1))
        self.assertEqual(clock.utc_at(1), NOW)
        self.assertEqual(clock.round_trip_seconds, 2)

    def test_old_unchanged_book_timestamp_is_retained(self):
        raw = str(int((NOW-timedelta(days=1)).timestamp())*1000+123)
        self.assertEqual(self.parse(timestamp=raw).source_at, NOW-timedelta(days=1)+timedelta(milliseconds=123))

    def test_client_maps_book_404_and_time_errors(self):
        import httpx
        from polymarket_bot.market_data.http import PublicGet
        from polymarket_bot.market_data.clob import ClobClient
        clock = FakeClock()
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(404,stream=httpx.ByteStream(b"")))) as client:
            clob = ClobClient(PublicGet(client,monotonic=clock.monotonic,sleep=clock.sleep,utcnow=clock.utcnow))
            with self.assertRaises(MarketDataError) as caught:
                clob.get_book(TOKEN_UP, deadline=120)
            self.assertEqual((caught.exception.code,caught.exception.http_status),("book_unavailable",404))
            with self.assertRaises(MarketDataError) as caught:
                clob.get_time(deadline=120)
            self.assertEqual(caught.exception.code,"time_unavailable")
