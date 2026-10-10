"""Offline contract tests for the recorder's narrowly scoped public reads."""
import json
import unittest
from datetime import timedelta

import httpx

from polymarket_bot.market_data.http import JsonRead, PublicGet
from polymarket_bot.market_data.models import MarketDataError
from tests.support import CONDITION, FakeClock, NOW, TOKEN_DOWN, TOKEN_UP
from tests.test_http import TrackingStream


class RecorderHttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.requests = []
        self.handler = lambda request: httpx.Response(200, stream=httpx.ByteStream(b"[]"))

        def handle(request):
            self.requests.append(request)
            return self.handler(request)

        self.client = httpx.Client(transport=httpx.MockTransport(handle),
                                   trust_env=False, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.reader = PublicGet(self.client, monotonic=self.clock.monotonic,
                                sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                                max_attempts=2)

    def test_named_batch_request_only(self):
        read = self.reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=2)
        self.assertEqual(read.payload, [])
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((request.method, request.url.host, request.url.path),
                         ("POST", "clob.polymarket.com", "/books"))
        self.assertEqual(json.loads(request.content),
                         [{"token_id": TOKEN_UP}, {"token_id": TOKEN_DOWN}])
        self.assertEqual(request.headers.get("accept-encoding"), "identity")
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(self.client.follow_redirects, False)
        self.assertEqual(self.client._trust_env, False)

    def test_batch_rejects_bad_tokens_before_request(self):
        bad_pairs = ((TOKEN_UP, TOKEN_UP), ("", TOKEN_DOWN),
                     ("abc", TOKEN_DOWN), ("1" * 129, TOKEN_DOWN),
                     (123, TOKEN_DOWN), (TOKEN_UP,), [TOKEN_UP, TOKEN_DOWN])
        for pair in bad_pairs:
            with self.subTest(pair=pair), self.assertRaises(MarketDataError):
                self.reader.get_books(pair, deadline=2)
        self.assertEqual(self.requests, [])

    def test_snapshot_defaults_unchanged(self):
        old_fixture = JsonRead({}, 1.0, 2.0)
        self.assertEqual(old_fixture.raw_body, b"")
        with self.assertRaises(MarketDataError):
            self.reader.get_json("clob", "/orders", deadline=2)
        self.assertEqual(self.requests, [])

    def test_successful_time_read_retains_observed_request_clocks(self):
        # A wall step during a request must remain visible; RTT reconstruction
        # must not overwrite the two actual local wall observations.
        wall = [NOW + timedelta(microseconds=1), NOW + timedelta(microseconds=7)]
        self.handler = lambda request: httpx.Response(
            200, stream=TrackingStream([b"1791583800"], self.clock, delay=0.125))
        reader = PublicGet(self.client, monotonic=self.clock.monotonic,
                         monotonic_ns=lambda: int(self.clock.value * 1_000_000_000) + 17,
                         sleep=self.clock.sleep, utcnow=lambda: wall.pop(0), max_attempts=1)
        read = reader.get_json("clob", "/time", deadline=2)
        self.assertTrue(hasattr(read, "started_mono_ns"), "Read lacks observed request timing")
        self.assertEqual((read.started_mono_ns, read.finished_mono_ns), (17, 125_000_017))
        self.assertEqual((read.started_wall_us, read.finished_wall_us),
                         (1_791_593_400_000_001, 1_791_593_400_000_007))

    def test_time_read_after_retry_anchors_successful_attempt_not_operation_start(self):
        attempts = []
        def respond(request):
            status = 503 if len(self.requests) == 1 else 200
            return httpx.Response(status, stream=TrackingStream(
                [b"1791593400"], self.clock, delay=0.1))
        self.handler = respond
        reader = PublicGet(self.client, monotonic=self.clock.monotonic,
                         sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                         max_attempts=2, on_attempt=attempts.append)
        read = reader.get_json("clob", "/time", deadline=2)
        self.assertTrue(hasattr(read, "started_mono_ns"), "Read lacks successful attempt timing")
        self.assertEqual((read.started_mono_ns, read.finished_mono_ns), (600_000_000, 700_000_000))
        self.assertEqual((read.started_wall_us, read.finished_wall_us),
                         (1_791_593_400_600_000, 1_791_593_400_700_000))
        self.assertEqual(read.started_mono_ns, attempts[1].started_mono_ns)
        self.assertEqual(read.finished_mono_ns, attempts[1].ended_mono_ns)

    def tracked(self):
        self.attempts = []
        return PublicGet(self.client, monotonic=self.clock.monotonic,
                         monotonic_ns=lambda: int(self.clock.value * 1_000_000_000),
                         sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                         max_attempts=2, phase_timeout=10, on_attempt=self.attempts.append)

    def test_raw_body_and_trickle_deadline(self):
        reader = self.tracked()
        raw = b' [ {"asset_id" : "1"} ] '
        self.handler = lambda request: httpx.Response(200, stream=httpx.ByteStream(raw))
        self.assertEqual(reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=2).raw_body, raw)
        self.assertEqual(len(self.attempts), 1)
        attempt = self.attempts[0]
        self.assertEqual((attempt.operation, attempt.method, attempt.path),
                         ("BOOK_BATCH", "POST", "/books"))
        self.assertEqual(attempt.selectors, (TOKEN_UP, TOKEN_DOWN))
        self.assertEqual((attempt.attempt_number, attempt.http_status, attempt.result,
                          attempt.error_code, attempt.raw_body, attempt.raw_complete),
                         (1, 200, "success", None, raw, True))
        self.assertLessEqual(attempt.started_mono_ns, attempt.ended_mono_ns)
        self.assertLessEqual(attempt.started_wall_us, attempt.ended_wall_us)
        self.assertFalse(hasattr(attempt, "headers"))
        self.attempts.clear()
        self.handler = lambda request: httpx.Response(200, stream=TrackingStream(
            [b"[", b"]"], self.clock, delay=1.1))
        with self.assertRaises(MarketDataError):
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=self.clock.value + 2)
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.attempts[0].raw_body, b"[")
        self.assertFalse(self.attempts[0].raw_complete)
        self.assertEqual(self.attempts[0].error_code, "transport_error")

    def test_attempt_limits_and_retry_after(self):
        reader = self.tracked()
        self.handler = lambda request: httpx.Response(503, stream=httpx.ByteStream(b'{"error":"busy"}'))
        with self.assertRaises(MarketDataError):
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([attempt.attempt_number for attempt in self.attempts], [1, 2])
        self.assertEqual([attempt.http_status for attempt in self.attempts], [503, 503])
        self.assertEqual([attempt.raw_body for attempt in self.attempts],
                         [b'{"error":"busy"}', b'{"error":"busy"}'])
        self.assertTrue(all(attempt.raw_complete for attempt in self.attempts))
        self.requests.clear()
        self.attempts.clear()
        self.handler = lambda request: httpx.Response(429, headers={"Retry-After":"999"},
                                                      stream=httpx.ByteStream(b""))
        with self.assertRaises(MarketDataError) as caught:
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=self.clock.value + 2)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.attempts[0].error_code, "rate_limited")

    def test_failed_reads_emit_one_bounded_attempt_each(self):
        cap = 10 * 1024 * 1024
        cases = (
            ("announced", {"Content-Length": str(cap + 1)}, [b"{}"], "transport_error", b"", False),
            ("streamed", {}, [b"[", b" " * cap], "transport_error", b"[", False),
            ("encoded", {"Content-Encoding": "gzip"}, [b"xyz"], "transport_error", b"", False),
            ("json", {}, [b"not-json"], "invalid_metadata", b"not-json", True),
            ("numeric", {}, [b'{"x":1e999999999999999999}'], "invalid_metadata", b'{"x":1e999999999999999999}', True),
        )
        for label, headers, chunks, code, raw, complete in cases:
            with self.subTest(label=label):
                self.requests.clear()
                reader = self.tracked()
                stream = TrackingStream(chunks)
                self.handler = lambda request: httpx.Response(200, headers=headers, stream=stream)
                with self.assertRaises(MarketDataError) as caught:
                    reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=2)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(len(self.attempts), 1)
                self.assertEqual((self.attempts[0].raw_body,
                                  self.attempts[0].raw_complete), (raw, complete))
                self.assertLessEqual(len(self.attempts[0].raw_body), cap)
                self.assertTrue(stream.closed)

    def test_late_parse_and_huge_retry_header_are_bounded(self):
        reader = self.tracked()
        def late(request):
            self.clock.advance(2.1)
            return httpx.Response(200, stream=httpx.ByteStream(b"[]"))
        self.handler = late
        with self.assertRaises(MarketDataError):
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=2)
        self.assertEqual(len(self.attempts), 1)
        self.assertFalse(self.attempts[0].raw_complete)
        self.requests.clear()
        self.attempts.clear()
        self.handler = lambda request: httpx.Response(429,
            headers={"Retry-After": "9" * 5000}, stream=httpx.ByteStream(b""))
        with self.assertRaises(MarketDataError):
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=self.clock.value + 0.3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.attempts), 1)

    def test_config_and_resolution_are_fixed_public_gets(self):
        reader = self.tracked()
        self.handler = lambda request: httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        reader.get_json("clob", "/clob-markets/" + CONDITION, deadline=2)
        reader.get_json("data", "/v2/resolutions", params={"condition": CONDITION}, deadline=2)
        self.assertEqual([(r.method, r.url.host, r.url.path) for r in self.requests], [
            ("GET", "clob.polymarket.com", "/clob-markets/" + CONDITION),
            ("GET", "data-api.polymarket.com", "/v2/resolutions")])
        self.assertEqual(self.attempts[1].selectors, ("condition=" + CONDITION,))
        self.assertEqual((self.attempts[0].operation, self.attempts[0].selectors),
                         ("CLOB_CONFIG", ("condition=" + CONDITION,)))
        self.assertEqual(self.attempts[1].operation, "RESOLUTION")
        self.requests.clear()
        for source, path, params in (("data", "/v2/resolutions/anything", None),
                                     ("clob", "/clob-markets/bad", None),
                                     ("clob", "/books", None),
                                     ("data", "/v2/resolutions", {"api_key":"secret"})):
            with self.subTest(source=source, path=path), self.assertRaises(MarketDataError):
                reader.get_json(source, path, params=params, deadline=2)
        self.assertEqual(self.requests, [])

    def test_resolution_condition_limit_allows_twenty(self):
        conditions = ["0x" + format(number, "064x") for number in range(20)]
        self.handler = lambda request: httpx.Response(200, stream=httpx.ByteStream(b"[]"))
        self.reader.get_json("data", "/v2/resolutions",
                             params={"condition": ",".join(conditions)}, deadline=2)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.params["condition"], ",".join(conditions))
        with self.assertRaises(MarketDataError):
            self.reader.get_json("data", "/v2/resolutions",
                                 params={"condition": ",".join(conditions + ["0x" + "f" * 64])},
                                 deadline=2)
        self.assertEqual(len(self.requests), 1)

    def test_interrupt_propagates_with_failed_attempt_evidence(self):
        reader = self.tracked()
        def interrupt(request):
            raise KeyboardInterrupt()
        self.handler = interrupt
        with self.assertRaises(KeyboardInterrupt):
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=2)
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.attempts[0].result, "failure")
        self.assertEqual(self.attempts[0].error_code, "transport_error")


if __name__ == "__main__":
    unittest.main()
