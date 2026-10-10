"""Behavioral capture of the complete public recorder HTTP surface."""
import json
import unittest

import httpx

from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError
from tests.support import CONDITION, TOKEN_DOWN, TOKEN_UP, FakeClock


class RecorderBoundaryTests(unittest.TestCase):
    def reader(self, handler, *, headers=None, auth=None):
        clock = FakeClock()
        client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers,
                              auth=auth, trust_env=False, follow_redirects=False)
        return client, PublicGet(client, monotonic=clock.monotonic, sleep=clock.sleep,
                                 utcnow=clock.utcnow, max_attempts=1)

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
