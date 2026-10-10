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
