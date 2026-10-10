import unittest
from decimal import Decimal, localcontext
from datetime import timedelta

from tests.recorder_support import BookFixture, down_book
from tests.support import NOW, TOKEN_UP, TOKEN_DOWN, CONDITION, make_book


class BatchBookTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = []

    def tearDown(self):
        for fixture in self.fixtures:
            fixture.close()

    def fixture(self, **kwargs):
        fixture = BookFixture(**kwargs)
        self.fixtures.append(fixture)
        return fixture

    def test_one_extreme_numeric_book_preserves_other_outcome(self):
        up = make_book(bids=[{"price": "0.60", "size": "1e999999999999999999"}])
        fixture = self.fixture(batch=[down_book(), up])
        pair = fixture.collect()
        self.assertEqual((pair.up.status, pair.down.status), ("INVALID", "VALID"))
        self.assertIsNone(pair.up.book)
        self.assertEqual(pair.down.book.best_ask, Decimal("0.61"))
        self.assertEqual((pair.up.attempt_index, pair.down.attempt_index), (0, 0))
        self.assertEqual(len(pair.attempts), 1)
        self.assertEqual(pair.attempts[0].raw_body, fixture.observed[0].raw_body)
        self.assertEqual(fixture.requests, [("POST", "/books")])

    def test_reordered_and_duplicate_tokens(self):
        fixture = self.fixture(batch=[down_book(), make_book()])
        pair = fixture.collect()
        self.assertEqual((pair.up.token_id, pair.down.token_id), (TOKEN_UP, TOKEN_DOWN))
        self.assertEqual((pair.up.status, pair.down.status), ("VALID", "VALID"))
        duplicate = self.fixture(batch=[down_book(), make_book(), make_book()]).collect()
        self.assertEqual((duplicate.up.status, duplicate.down.status), ("INVALID", "VALID"))
        self.assertEqual(duplicate.up.error_code, "duplicate_token")
        self.assertIsNone(duplicate.up.book)
        self.assertEqual(duplicate.down.book.best_bid, Decimal("0.60"))

    def test_identity_extra_empty_and_crossed_are_independent(self):
        wrong = self.fixture(batch=[make_book(market="0x" + "cd" * 32), down_book()]).collect()
        self.assertEqual((wrong.up.status, wrong.down.status), ("INVALID", "VALID"))
        self.assertEqual(wrong.up.error_code, "book_identity_mismatch")
        self.assertIsNone(wrong.up.book)
        extra = self.fixture(batch=[make_book(), down_book(), make_book(asset_id="1234")]).collect()
        self.assertEqual((extra.up.status, extra.down.status), ("VALID", "VALID"))
        self.assertIn("unexpected_token", extra.up.quality)
        empty = self.fixture(batch=[make_book(bids=[]), down_book()]).collect()
        self.assertEqual(empty.up.status, "VALID")
        self.assertIsNone(empty.up.book.best_bid)
        self.assertIsNone(empty.up.book.midpoint)
        self.assertIsNone(empty.up.book.spread)
        crossed = self.fixture(batch=[make_book(asks=[{"price":"0.59","size":"1"}]), down_book()]).collect()
        self.assertEqual((crossed.up.status, crossed.down.status), ("INVALID", "VALID"))
        self.assertIsNone(crossed.up.book)

    def test_nonatomic_and_independent_source_times(self):
        later = str(int((NOW + timedelta(milliseconds=500)).timestamp() * 1000))
        fixture = self.fixture(batch=[down_book(timestamp=later),
                                      make_book(hash="0xabc", tick_size="0.01", min_order_size="5")])
        pair = fixture.collect()
        self.assertEqual(pair.up.book.source_at, NOW)
        self.assertEqual(pair.down.book.source_at, NOW + timedelta(milliseconds=500))
        self.assertTrue(pair.up.quality["batch_not_guaranteed_atomic"])
        self.assertEqual(pair.up.book_metadata["hash"], "0xabc")
        self.assertEqual(pair.up.book_metadata["tick_size"], Decimal("0.01"))
        self.assertEqual(pair.up.book_metadata["min_order_size"], Decimal("5"))
        future = str(int((NOW + timedelta(seconds=7)).timestamp() * 1000))
        incompatible = self.fixture(batch=[make_book(timestamp=future), down_book()]).collect()
        self.assertEqual((incompatible.up.status, incompatible.down.status), ("INVALID", "VALID"))
        self.assertEqual(incompatible.up.error_code, "future_source_time")
        self.assertIsNone(incompatible.up.book)
        tolerated = self.fixture(batch=[make_book(timestamp=future), down_book()]).collect(uncertainty_us=2_000_000)
        self.assertEqual(tolerated.up.status, "VALID")

    def test_fallback_is_bounded_and_justified(self):
        singles = {TOKEN_UP: (200, make_book()), TOKEN_DOWN: (200, down_book())}
        for status in (404, 405):
            with self.subTest(status=status):
                fixture = self.fixture(status=status, batch={}, singles=singles)
                pair = fixture.collect()
                self.assertEqual((pair.up.status, pair.down.status), ("VALID", "VALID"))
                self.assertEqual(fixture.requests, [("POST", "/books"), ("GET", "/book"), ("GET", "/book")])
                self.assertEqual((pair.up.attempt_index, pair.down.attempt_index), (1, 2))
                self.assertEqual(len(pair.attempts), 3)
        missing = self.fixture(batch=[down_book()], singles=singles)
        pair = missing.collect()
        self.assertEqual((pair.up.status, pair.down.status), ("VALID", "VALID"))
        self.assertEqual((pair.up.attempt_index, pair.down.attempt_index), (1, 0))
        self.assertEqual(missing.requests, [("POST", "/books"), ("GET", "/book")])
        for kwargs in ({"status": 429, "batch": {}}, {"batch": {}},
                       {"batch": [make_book(market="0x"+"cd"*32), down_book()]}):
            with self.subTest(kwargs=kwargs):
                fixture = self.fixture(**kwargs, singles=singles)
                pair = fixture.collect()
                self.assertEqual(fixture.requests, [("POST", "/books")])
                self.assertNotEqual(pair.up.status, "VALID")
                self.assertEqual(len(pair.attempts), 1)

    def test_extreme_numerics_each_request_shape(self):
        invalid = ("1e999999999999999999", "1e-999999999999999999", "9"*129,
                   "NaN", "Infinity")
        for size in invalid:
            with self.subTest(size=size):
                up = make_book(bids=[{"price":"0.60", "size":size}])
                batch = self.fixture(batch=[up, down_book()]).collect()
                self.assertEqual((batch.up.status, batch.down.status), ("INVALID", "VALID"))
                self.assertIsNone(batch.up.book)
                single = self.fixture(batch=[], singles={TOKEN_UP:(200,up), TOKEN_DOWN:(200,down_book())}).collect()
                self.assertEqual((single.up.status, single.down.status), ("INVALID", "VALID"))
                self.assertIsNone(single.up.book)

    def test_deadline_prevents_fallback_and_late_quote(self):
        before = self.fixture(batch=[down_book()], delays={"/books": 0.6})
        pair = before.collect(deadline=0.5)
        self.assertEqual((pair.up.status, pair.down.status), ("LATE", "LATE"))
        self.assertIsNone(pair.up.book)
        self.assertEqual(before.requests, [("POST", "/books")])
        no_start = self.fixture(batch=[make_book(), down_book()])
        pair = no_start.collect(deadline=0)
        self.assertEqual(no_start.requests, [])
        self.assertEqual((pair.up.status, pair.down.status), ("LATE", "LATE"))
        self.assertEqual(pair.attempts, ())

    def test_observer_is_preserved_after_collection(self):
        fixture = self.fixture(batch=[make_book(), down_book()])
        original = fixture.public.on_attempt
        pair = fixture.collect()
        self.assertIs(fixture.public.on_attempt, original)
        self.assertEqual(tuple(fixture.observed), pair.attempts)

    def test_malformed_batch_element_does_not_trigger_fallback(self):
        fixture = self.fixture(batch=[{"market": CONDITION}, down_book()],
                               singles={TOKEN_UP: (200, make_book())})
        pair = fixture.collect()
        self.assertEqual((pair.up.status, pair.down.status), ("INVALID", "VALID"))
        self.assertEqual(pair.up.error_code, "invalid_batch_element")
        self.assertIsNone(pair.up.book)
        self.assertEqual(pair.up.attempt_index, 0)
        self.assertEqual(pair.down.book.best_ask, Decimal("0.61"))
        self.assertEqual(pair.down.quality["malformed_batch_elements"], 1)
        self.assertEqual(fixture.requests, [("POST", "/books")])

    def test_expired_market_never_starts_request(self):
        fixture = self.fixture(batch=[make_book(), down_book()])
        fixture.clock.advance(601)
        pair = fixture.collect(deadline=603)
        self.assertEqual(fixture.requests, [])
        self.assertEqual((pair.up.status, pair.down.status), ("LATE", "LATE"))
        self.assertEqual(pair.attempts, ())

    def test_exact_large_size_and_old_source_time_survive_local_context(self):
        very_large = "1" + "0" * 127
        old = str(int((NOW - timedelta(days=1)).timestamp() * 1000))
        fixture = self.fixture(batch=[make_book(timestamp=old, bids=[
            {"price":"0.60", "size":very_large}, {"price":"0.60", "size":very_large}]),
                                      down_book()])
        with localcontext() as context:
            context.prec = 3
            pair = fixture.collect()
        self.assertEqual(pair.up.status, "VALID")
        self.assertEqual(pair.up.book.bid_size, Decimal("2e127"))
        self.assertEqual(pair.up.quality["source_age_us"], 86_400_100_000)

    def test_late_fallback_keeps_other_ontime_batch_book(self):
        fixture = self.fixture(batch=[down_book()], singles={TOKEN_UP:(200, make_book())},
                               delays={"/book":0.5})
        pair = fixture.collect(deadline=0.4)
        self.assertEqual((pair.up.status, pair.down.status), ("LATE", "VALID"))
        self.assertIsNone(pair.up.book)
        self.assertEqual(pair.down.book.best_ask, Decimal("0.61"))
        self.assertEqual((pair.up.attempt_index, pair.down.attempt_index), (1, 0))

    def test_collector_limits_retryable_batch_and_fallback_to_one_retry(self):
        for status in (429, 503):
            with self.subTest(status=status):
                fixture = self.fixture(status=status, batch={})
                fixture.public.max_attempts = 3
                pair = fixture.collect()
                self.assertEqual(len(pair.attempts), 2)
                self.assertEqual(fixture.requests, [("POST", "/books"), ("POST", "/books")])
                self.assertEqual((pair.up.status, pair.down.status), ("UNAVAILABLE", "UNAVAILABLE"))
                self.assertEqual(fixture.public.max_attempts, 3)

        fallback = self.fixture(status=404, batch={}, singles={
            TOKEN_UP: (503, {}), TOKEN_DOWN: (200, down_book())})
        fallback.public.max_attempts = 3
        pair = fallback.collect()
        self.assertEqual((pair.up.status, pair.down.status), ("UNAVAILABLE", "VALID"))
        self.assertEqual(fallback.requests, [("POST", "/books"),
                                             ("GET", "/book"), ("GET", "/book"),
                                             ("GET", "/book")])
        self.assertEqual((pair.up.attempt_index, pair.down.attempt_index), (2, 3))
        self.assertEqual(fallback.public.max_attempts, 3)


if __name__ == "__main__":
    unittest.main()
