"""Deterministic public CLOB responses for recorder tests."""
import json

import httpx

from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.parsing import parse_market
from tests.support import (CONDITION, NOW, TOKEN_DOWN, TOKEN_UP, FakeClock,
                           make_book, make_event, make_market)


class BookFixture:
    def __init__(self, *, batch=None, singles=None, status=200, delays=None):
        self.clock = FakeClock()
        self.batch = batch
        self.singles = singles or {}
        self.status = status
        self.delays = delays or {}
        self.requests = []
        self.observed = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._respond))
        self.public = PublicGet(self.client, monotonic=self.clock.monotonic,
                                monotonic_ns=lambda: int(self.clock.value * 1_000_000_000),
                                sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                                max_attempts=1, on_attempt=self.observed.append)
        self.market = parse_market(make_event(), make_market(), now=NOW)

    def close(self):
        self.client.close()

    def _respond(self, request):
        self.requests.append((request.method, request.url.path))
        self.clock.advance(self.delays.get(request.url.path, 0.1))
        if request.url.path == "/books":
            status, data = self.status, self.batch
        else:
            token = request.url.params["token_id"]
            status, data = self.singles.get(token, (404, {}))
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return httpx.Response(status, stream=httpx.ByteStream(raw))

    def collect(self, *, deadline=2, uncertainty_us=0):
        from polymarket_bot.recorder.books import collect_books
        return collect_books(self.public, self.market, deadline=deadline,
                             receipt_server_us=lambda moment: int((NOW.timestamp() + moment) * 1_000_000),
                             uncertainty_us=uncertainty_us)


def down_book(**overrides):
    return make_book(asset_id=TOKEN_DOWN, **overrides)
