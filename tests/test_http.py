import unittest
import httpx
from email.utils import format_datetime
from datetime import timedelta
from tests.support import NOW
from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError
from tests.support import FakeClock

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.requests = []
        self.handler = lambda r: httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        def handle(request):
            self.requests.append(request)
            return self.handler(request)
        self.client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.client.close)
        self.get = PublicGet(self.client, monotonic=self.clock.monotonic,
                             sleep=self.clock.sleep, utcnow=self.clock.utcnow)
    def call(self, **kwargs):
        return self.get.get_json("clob", "/time", deadline=kwargs.pop("deadline",120.0), **kwargs)

    def test_only_public_get_paths_are_sent(self):
        for source, path in (("gamma","/events"), ("gamma","/events/slug/test"),
                             ("gamma","/markets/123"), ("clob","/time"), ("clob","/book")):
            self.get.get_json(source, path, deadline=120.0)
        self.assertEqual(len(self.requests), 5)
        for request in self.requests:
            self.assertEqual(request.method, "GET")
            self.assertNotIn("authorization", request.headers)
            self.assertIn(request.url.host, ("gamma-api.polymarket.com","clob.polymarket.com"))
        self.requests.clear()
        for source, path in (("clob","/order"), ("clob","https://example.com/time"),
                             ("other","/time"), ("gamma","/events/slug/../auth")):
            with self.assertRaises(MarketDataError):
                self.get.get_json(source, path, deadline=120.0)
        self.assertEqual(self.requests, [])

    def error(self, code="transport_error", **kwargs):
        with self.assertRaises(MarketDataError) as caught:
            self.call(**kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_identity_stream_is_size_limited(self):
        cap = 10 * 1024 * 1024
        for length, declared in ((cap,None),(cap+1,None),(cap+1,"2"),(2,str(cap+1))):
            with self.subTest(length=length, declared=declared):
                self.requests.clear()
                stream = TrackingStream([b"{}" + b" "*(length-2)])
                headers = {"Content-Length":declared} if declared else {}
                self.handler = lambda r: httpx.Response(200, stream=stream, headers=headers)
                if length == cap:
                    self.assertEqual(self.call().payload, {})
                else:
                    self.error()
                self.assertTrue(stream.closed)
                self.assertEqual(len(self.requests), 1)

    def test_compressed_response_is_rejected_before_reading(self):
        stream = TrackingStream([b"compressed"])
        self.handler = lambda r: httpx.Response(200, stream=stream, headers={"Content-Encoding":"gzip"})
        self.error()
        self.assertEqual(stream.chunks_read, 0)
        self.assertTrue(stream.closed)

    def test_trickle_stream_hits_shared_deadline(self):
        stream = TrackingStream([b"{", b"}"], clock=self.clock, delay=4)
        self.handler = lambda r: httpx.Response(200, stream=stream)
        self.error(deadline=5)
        self.assertTrue(stream.closed)
        self.assertEqual(len(self.requests), 1)

    def test_late_success_is_rejected(self):
        def late(r):
            self.clock.advance(6)
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        self.handler = late
        self.error(deadline=5)
        self.assertEqual(len(self.requests), 1)

    def test_stream_closed_after_failure(self):
        stream = TrackingStream([b"not-json"])
        self.handler = lambda r: httpx.Response(200, stream=stream)
        self.assertEqual(self.error("invalid_metadata").http_status, 200)
        self.assertTrue(stream.closed)

    def test_retry_budget_is_three_total_attempts(self):
        self.handler = lambda r: httpx.Response(503, stream=httpx.ByteStream(b""))
        self.error()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.clock.sleeps, [0.5,1.0])
        self.requests.clear()
        self.handler = lambda r: httpx.Response(429, stream=httpx.ByteStream(b""))
        self.error("rate_limited")
        self.assertEqual(len(self.requests), 3)

    def test_backoff_is_point_five_then_one(self):
        def failing(r):
            if len(self.requests) < 3:
                raise httpx.ConnectError("offline", request=r)
            self.clock.advance(0.2)
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        self.handler = failing
        read = self.call()
        self.assertEqual(self.clock.sleeps, [0.5,1.0])
        self.assertEqual(read.started_mono, 1.5)
        self.assertAlmostEqual(read.finished_mono, 1.7)

    def test_retry_after_seconds_and_http_date(self):
        for header, expected in (("2",[2.0]),(format_datetime(NOW+timedelta(seconds=2), usegmt=True),[2.0]),
                                 ("-1",[0.5]),("nonsense",[0.5]),
                                 (format_datetime(NOW-timedelta(seconds=2), usegmt=True),[0.0])):
            with self.subTest(header=header):
                self.requests.clear()
                self.clock.value = 0
                self.clock.sleeps.clear()
                self.handler = lambda r: httpx.Response(429, headers={"Retry-After":header}, stream=httpx.ByteStream(b"")) if len(self.requests)==1 else httpx.Response(200, stream=httpx.ByteStream(b"{}"))
                self.assertEqual(self.call().payload, {})
                self.assertEqual(self.clock.sleeps, expected)

    def test_nonretryable_4xx_is_preserved(self):
        self.handler = lambda r: httpx.Response(403, stream=httpx.ByteStream(b""))
        self.assertEqual(self.error().http_status, 403)
        self.assertEqual(len(self.requests), 1)

    def test_deadline_prevents_retry_or_sleep(self):
        self.error(deadline=0)
        self.assertEqual(self.requests, [])
        self.handler = lambda r: httpx.Response(429, headers={"Retry-After":"999"}, stream=httpx.ByteStream(b""))
        self.error("rate_limited", deadline=5)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.requests), 1)

    def test_timeouts_are_clamped_and_interrupt_propagates(self):
        self.call(deadline=3)
        self.assertEqual(set(self.requests[0].extensions["timeout"].values()), {3.0})
        def interrupt(r): raise KeyboardInterrupt()
        self.handler = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.call()
        self.assertEqual(self.clock.sleeps, [])

    def test_redirects_are_not_followed(self):
        self.handler = lambda r: httpx.Response(302, headers={"Location":"https://example.com"}, stream=httpx.ByteStream(b""))
        self.error()
        self.assertEqual(len(self.requests), 1)

class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks, clock=None, delay=0):
        self.chunks, self.clock, self.delay = chunks, clock, delay
        self.closed, self.chunks_read = False, 0
    def __iter__(self):
        for chunk in self.chunks:
            if self.clock: self.clock.advance(self.delay)
            self.chunks_read += 1
            yield chunk
    def close(self): self.closed = True
