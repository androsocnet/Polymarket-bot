"""Deterministic public CLOB responses for recorder tests."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

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


class DatabaseFixture:
    """An absent database path in a disposable directory; no live services."""

    def __init__(self):
        fixture_root = Path(__file__).resolve().parents[1] / "data"
        fixture_root.mkdir(exist_ok=True)
        self.directory = TemporaryDirectory(dir=fixture_root, prefix="recorder-test-")
        self.path = Path(self.directory.name) / "recorder.sqlite3"

    def close(self):
        self.directory.cleanup()


NOW_US = int(NOW.timestamp()) * 1_000_000
WINDOW_START_US = NOW_US - 300_000_000


def paired_bundle(kind="complete"):
    """Actual collector results with full original bytes, never fabricated quotes."""
    up = make_book(bids=[{"price": "0.60", "size": "2.000000000000000000000000000000001"}]
                   + [{"price": "0.59", "size": "0"} for _ in range(256)],
                   tick_size="0.01", min_order_size="1.000", hash="up-hash")
    down = down_book(timestamp=str(int(NOW.timestamp()) * 1000 + 100),
                     bids=[{"price": "0.30", "size": "3"}], asks=[])
    if kind == "partial":
        down["bids"][0]["size"] = "NaN"
    fixture = BookFixture(batch=[down, up], status=503 if kind == "failed" else 200)
    try:
        return fixture.collect()
    finally:
        fixture.close()


def seed_writer_market(store, run_id):
    """Until acquisition exists, seed known identities using only the owned writer."""
    import zlib
    from tests.support import MARKET_ID
    connection = store._connection
    with connection:
        connection.execute("""INSERT INTO markets
            (market_id,event_id,condition_id,slug,question,window_start_us,window_end_us,
             first_seen_us,up_token_id,down_token_id,up_outcome_index,down_outcome_index,
             lifecycle,resolution_state,resolution_attempts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (MARKET_ID, "2001", CONDITION, "known-market", "Bitcoin Up or Down?",
             WINDOW_START_US, WINDOW_START_US + 900_000_000, NOW_US,
             TOKEN_UP, TOKEN_DOWN, 1, 0, "CURRENT", "PENDING", 0))
        connection.execute("""INSERT INTO market_revisions
            (revision_id,market_id,observed_us,run_id,description,resolution_source_url,
             gamma_raw_z,raw_sizes_json,codec_version,content_hash)
            VALUES (1,?,?,?,?,?,?,?,?,?)""",
            (MARKET_ID, NOW_US, run_id, "known rules", "https://example.test/rules",
             zlib.compress(b"{}"), '{"gamma":2}', 1, "fixture-revision"))
        connection.execute("""INSERT INTO clock_anchors
            (anchor_id,run_id,server_seconds,request_start_mono_ns,request_end_mono_ns,
             request_start_wall_us,request_end_wall_us,anchor_mono_ns,anchor_server_us,
             uncertainty_us,status) VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, NOW_US // 1_000_000, 0, 0, NOW_US, NOW_US,
             0, NOW_US, 1_000_000, "VALID"))
