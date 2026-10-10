import json
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 10, 0, 50, tzinfo=timezone.utc)
TOKEN_UP = "9000000000000000000001"
TOKEN_DOWN = "9000000000000000000002"
CONDITION = "0x" + "ab" * 32
MARKET_ID = "1001"

def make_market(**overrides):
    value = dict(id=MARKET_ID, slug="renamed-test-market", question="Bitcoin Up or Down?",
                 eventStartTime=(NOW-timedelta(minutes=5)).isoformat(),
                 endDate=(NOW+timedelta(minutes=10)).isoformat(),
                 startDate=(NOW-timedelta(days=1)).isoformat(),
                 active=True, closed=False, archived=False, acceptingOrders=True,
                 enableOrderBook=True, conditionId=CONDITION, outcomes='["Up", "Down"]',
                 clobTokenIds=json.dumps([TOKEN_UP, TOKEN_DOWN]), outcomePrices='["0.4", "0.6"]')
    value.update(overrides)
    return value

def make_event(**overrides):
    value = dict(id="2001", slug="renamed-test-event", title="Bitcoin Up or Down?",
                 seriesSlug="btc-up-or-down-15m", active=True, closed=False, archived=False,
                 markets=[make_market()])
    value.update(overrides)
    return value

def make_book(**overrides):
    value = dict(market=CONDITION, asset_id=TOKEN_UP, timestamp=str(int(NOW.timestamp())*1000),
                 bids=[{"price":"0.60","size":"2"}], asks=[{"price":"0.61","size":"4"}])
    value.update(overrides)
    return value

class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []
    def monotonic(self): return self.value
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)
    def advance(self, seconds): self.value += seconds
    def utcnow(self): return NOW + timedelta(seconds=self.value)

class FakeGamma:
    def __init__(self, events=None, fast=None, refresh=None):
        self.events = [make_event()] if events is None else events
        self.fast = make_event() if fast is None else fast
        self.refresh = refresh
        self.slug_calls, self.list_calls, self.refresh_calls = [], [], []
    def event_by_slug(self, slug, *, deadline):
        self.slug_calls.append((slug, deadline))
        if isinstance(self.fast, BaseException): raise self.fast
        return self.fast
    def list_events(self, *, end_min, end_max, offset, deadline):
        self.list_calls.append((end_min, end_max, offset, deadline))
        return self.events[offset:offset+100]
    def market_by_id(self, market_id, *, deadline):
        self.refresh_calls.append((market_id, deadline))
        if self.refresh is not None: return self.refresh
        return next(m for e in self.events for m in e["markets"] if m["id"] == market_id)
