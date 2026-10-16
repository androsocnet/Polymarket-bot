"""Behavioral capture of the complete public recorder HTTP surface."""
import json
import unittest

import httpx

from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError
from tests.support import CONDITION, TOKEN_DOWN, TOKEN_UP, FakeClock


class RecorderBoundaryTests(unittest.TestCase):
    def reader(self, handler, *, headers=None, auth=None, cookies=None, params=None):
        clock = FakeClock()
        client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers,
                              auth=auth, cookies=cookies, params=params,
                              trust_env=False, follow_redirects=False)
        return client, PublicGet(client, monotonic=clock.monotonic, sleep=clock.sleep,
                                 utcnow=clock.utcnow, max_attempts=1)

    def test_provider_final_guard_rechecks_shifted_active_window(self):
        from polymarket_bot.recorder.free_reference import CoinbaseRestProvider, KrakenRestProvider
        for cls in (CoinbaseRestProvider, KrakenRestProvider):
            sends = []
            allowed = [True]
            with httpx.Client(transport=httpx.MockTransport(lambda request: sends.append(request) or
                    httpx.Response(200, json={})), event_hooks={'request': [lambda request: allowed.__setitem__(0, False)]}) as client:
                provider = cls(client, monotonic_ns=lambda: 0, wall_us=lambda: 0, sleep=lambda seconds: None)
                def guard():
                    if not allowed[0]:
                        raise httpx.TimeoutException('active window closed')
                provider.before_attempt = guard
                provider.fetch_ticker(deadline=1)
            self.assertEqual(sends, [])

    def test_provider_hook_delay_cannot_send_after_deadline(self):
        from polymarket_bot.recorder.free_reference import CoinbaseRestProvider, KrakenRestProvider
        for cls in (CoinbaseRestProvider, KrakenRestProvider):
            clock = FakeClock()
            sends = []
            with httpx.Client(transport=httpx.MockTransport(lambda request: sends.append(request) or
                    httpx.Response(200, json={})), event_hooks={'request':[lambda request: clock.advance(2)]}) as client:
                provider = cls(client, monotonic_ns=lambda: round(clock.value*1e9), wall_us=lambda: 0, sleep=clock.sleep)
                provider.fetch_ticker(deadline=1)
            self.assertEqual(sends, [])

    def test_after_guard_hook_cannot_send_at_exclusive_end(self):
        clock = FakeClock()
        sends = []
        def guard():
            if clock.value >= 2:
                raise MarketDataError('capture_window_closed', 'closed')
        with httpx.Client(transport=httpx.MockTransport(lambda request: sends.append(request) or
                httpx.Response(200, stream=httpx.ByteStream(b'[]'))),
                event_hooks={'request':[lambda request: clock.advance(2)]}) as client:
            public = PublicGet(client, monotonic=clock.monotonic, sleep=clock.sleep,
                utcnow=clock.utcnow, before_attempt=guard)
            try:
                public.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
            except MarketDataError:
                pass
        self.assertEqual(sends, [])

    def test_actual_book_guard_rechecks_both_targets_and_exclusive_end(self):
        from polymarket_bot.recorder.service import Recorder
        self.assertTrue(callable(getattr(Recorder, '_guard_book_attempt', None)))
        from types import SimpleNamespace
        sends = []
        now = [10]
        client, public = self.reader(lambda request: sends.append(request) or
            httpx.Response(200, stream=httpx.ByteStream(b'[]')))
        try:
            from typing import Any, cast
            recorder = cast(Any, object.__new__(Recorder))
            recorder.monotonic_ns = lambda: now[0]
            recorder.run_deadline_ns = 30
            recorder.clock = SimpleNamespace(usable=lambda: True, deadline_ns=lambda us: us)
            recorder._book_guard = (12, 15, 25)
            public.before_attempt = recorder._guard_book_attempt
            for moment in (10, 12, 14, 25, 30):
                now[0] = moment
                with self.assertRaises(MarketDataError):
                    public.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
            now[0] = 15
            public.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
            self.assertEqual(len(sends), 1)
        finally:
            client.close()

    def test_only_named_public_post_is_reachable(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b"[]"))
        client, reader = self.reader(respond)
        try:
            routes = (("gamma", "/events", None),
                      ("gamma", "/events/slug/btc-updown-123", None),
                      ("gamma", "/markets/1001", None),
                      ("clob", "/time", None),
                      ("clob", "/book", {"token_id": TOKEN_UP}),
                      ("clob", "/clob-markets/" + CONDITION, None),
                      ("data", "/v2/resolutions", {"condition": CONDITION}))
            for source, path, params in routes:
                reader.get_json(source, path, params=params, deadline=10)
            reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
            self.assertEqual({r.url.path for r in requests if r.method == "POST"}, {"/books"})
            self.assertEqual([r.method for r in requests], ["GET"] * 7 + ["POST"])
            self.assertEqual({r.url.host for r in requests},
                             {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"})
            self.assertEqual(json.loads(requests[-1].content),
                             [{"token_id": TOKEN_UP}, {"token_id": TOKEN_DOWN}])
            self.assertFalse(any("authorization" in h.lower() or "api-key" in h.lower() or
                                 "signature" in h.lower() for r in requests for h in r.headers))
        finally:
            client.close()

    def test_unknown_hosts_trading_paths_and_generic_methods_never_send(self):
        requests = []
        client, reader = self.reader(lambda request: requests.append(request) or httpx.Response(200, stream=httpx.ByteStream(b"{}")))
        try:
            for source, path in (("chainlink", "/api/v1/reports/latest"),
                                 ("clob", "/orders"), ("clob", "/auth"),
                                 ("clob", "/books"), ("data", "/v2/trades"),
                                 ("gamma", "https://evil.example/events")):
                with self.subTest(source=source, path=path), self.assertRaises(MarketDataError):
                    reader.get_json(source, path, deadline=10)
            for tokens in ((TOKEN_UP, TOKEN_UP), (TOKEN_UP, "not-a-token"),
                           (TOKEN_UP, TOKEN_DOWN, "3")):
                with self.subTest(tokens=tokens), self.assertRaises(MarketDataError):
                    reader.get_books(tokens, deadline=10)
            self.assertEqual(requests, [])
            self.assertFalse(hasattr(reader, "post_json"))
            self.assertFalse(hasattr(reader, "request"))
        finally:
            client.close()

    def test_redirect_cannot_leave_fixed_host(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://evil.example/collect"}, stream=httpx.ByteStream(b""))
        client, reader = self.reader(respond)
        try:
            with self.assertRaises(MarketDataError):
                reader.get_json("clob", "/time", deadline=10)
            self.assertEqual([(r.url.host, r.url.path) for r in requests], [("clob.polymarket.com", "/time")])
        finally:
            client.close()

    def test_auth_configured_client_cannot_send_public_request(self):
        requests = []
        routes = (("gamma", "/events", None),
                  ("gamma", "/events/slug/btc-updown-123", None),
                  ("gamma", "/markets/1001", None),
                  ("clob", "/time", None),
                  ("clob", "/book", {"token_id": TOKEN_UP}),
                  ("clob", "/clob-markets/" + CONDITION, None),
                  ("data", "/v2/resolutions", {"condition": CONDITION}))
        for options in ({"headers": {"Authorization": "Bearer secret"}},
                        {"headers": {"POLY-API-KEY": "secret"}},
                        {"headers": {"POLY_PASSPHRASE": "sentinel"}},
                        {"headers": {"POLY-PASSPHRASE": "sentinel"}},
                        {"cookies": {"session": "sentinel"}},
                        {"params": {"api_key": "sentinel"}},
                        {"params": {"token_id": TOKEN_DOWN}},
                        {"auth": httpx.BasicAuth("user", "secret")}):
            with self.subTest(options=tuple(options)):
                client, reader = self.reader(lambda request: requests.append(request) or httpx.Response(200, stream=httpx.ByteStream(b"{}")), **options)
                try:
                    for source, path, params in routes:
                        with self.subTest(source=source, path=path), self.assertRaises(MarketDataError):
                            reader.get_json(source, path, params=params, deadline=10)
                    with self.assertRaises(MarketDataError):
                        reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
                finally:
                    client.close()
        self.assertEqual(requests, [])

    def test_malformed_numeric_and_deadline_remain_contained(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b'{"price":1e99999999999999999999999}'))
        client, reader = self.reader(respond)
        try:
            with self.assertRaises(MarketDataError):
                reader.get_books((TOKEN_UP, TOKEN_DOWN), deadline=10)
            self.assertEqual([(r.method, r.url.path) for r in requests], [("POST", "/books")])
            with self.assertRaises(MarketDataError):
                reader.get_json("clob", "/time", deadline=0)
            self.assertEqual(len(requests), 1)
        finally:
            client.close()

    def test_response_cookie_cannot_reach_a_public_retry(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(503,headers={"Set-Cookie":"session=sentinel; Path=/"},
                                  stream=httpx.ByteStream(b"outage"))
        client, reader = self.reader(respond)
        reader.max_attempts = 2
        try:
            with self.assertRaises(MarketDataError):
                reader.get_json("clob","/time",deadline=10)
            self.assertEqual(len(requests), 2)
            self.assertNotIn("cookie", requests[0].headers)
            self.assertNotIn("cookie", requests[1].headers)
            self.assertEqual(len(client.cookies), 0)
        finally:
            client.close()

    def test_server_set_cookie_does_not_break_subsequent_public_request(self):
        requests = []
        def respond(request):
            requests.append(request)
            headers = {"Set-Cookie": "__cf_bm=sentinel; Path=/"} if request.url.path == "/time" else {}
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"{}"))
        client, reader = self.reader(respond)
        try:
            read1 = reader.get_json("clob", "/time", deadline=10)
            self.assertEqual(read1.payload, {})
            read2 = reader.get_json("gamma", "/events", deadline=10)
            self.assertEqual(read2.payload, {})
            self.assertEqual(len(requests), 2)
            self.assertNotIn("cookie", requests[1].headers)
        finally:
            client.close()

    def test_retry_after_response_cookie_on_429_succeeds_without_cookie_forwarding(self):
        requests = []
        attempts = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Set-Cookie": "__cf_bm=sentinel; Path=/", "Retry-After": "0"},
                                      stream=httpx.ByteStream(b"{}"))
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        client, reader = self.reader(respond)
        reader.on_attempt = attempts.append
        reader.max_attempts = 2
        try:
            read = reader.get_json("clob", "/time", deadline=10)
            self.assertEqual(read.payload, {})
            self.assertEqual(len(requests), 2)
            self.assertNotIn("cookie", requests[0].headers)
            self.assertNotIn("cookie", requests[1].headers)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(client.cookies), 0)
        finally:
            client.close()

    def test_retry_after_response_cookie_on_503_succeeds_without_cookie_forwarding(self):
        requests = []
        attempts = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(503, headers={"Set-Cookie": "__cf_bm=sentinel; Path=/"},
                                      stream=httpx.ByteStream(b"{}"))
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        client, reader = self.reader(respond)
        reader.on_attempt = attempts.append
        reader.max_attempts = 2
        try:
            read = reader.get_json("clob", "/time", deadline=10)
            self.assertEqual(read.payload, {})
            self.assertEqual(len(requests), 2)
            self.assertNotIn("cookie", requests[0].headers)
            self.assertNotIn("cookie", requests[1].headers)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(client.cookies), 0)
        finally:
            client.close()


