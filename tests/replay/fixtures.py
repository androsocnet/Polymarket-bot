"""Real-recorder fixture helpers for Phase 3B replay tests.

Uses the real CP5 recorder (fake clocks/HTTP only) with a deliberate book-price
ladder so that momentum, execution, fees, partial fills and exits are actually
exercised — unlike the constant-book base fixture where the replay never trades.
"""
import json

import httpx

from tests.recorder_support import RecorderFixture
from tests.support import make_event
from tests.test_paper import OfflineTests


def traded_fixture(case, *, deadline_ns=30_000_000_000):
    """Real CP5 recording fixture whose books produce genuine trading.

    Slot mids form the ladder 0.605, 0.625, 0.645, 0.665, 0.685 then flat 0.505
    from the 6th book pair on. With lookback 3 this yields: a BUY signal at the
    third decision, a hold through the ladder, then a DOWN reversal signal at
    the sixth decision — so replay produces real fills, fees, and an exit.
    """
    from polymarket_bot.recorder.models import RunConfig
    from polymarket_bot.recorder.service import Recorder

    offline = OfflineTests()
    offline.setUp()
    case.addCleanup(offline.doCleanups)

    f = RecorderFixture()
    case.addCleanup(f.close)
    row = f.market_row()
    f.market_row = lambda: row
    original = f.respond
    seen = []

    def hook(request, payload, fixture):
        if request.url.path == '/books' and isinstance(payload, list):
            n = len(seen)
            seen.append(n)
            bid = '0.50' if n >= 5 else format(0.60 + 0.02 * n, '.2f')
            ask = '0.51' if n >= 5 else format(0.61 + 0.02 * n, '.2f')
            for book in payload:
                book['bids'] = [{'price': bid, 'size': '4'}]
                book['asks'] = [{'price': ask, 'size': '4'}]
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))
        return None

    f.hook = hook

    def respond(request):
        if request.url.path == '/events/2001':
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(make_event(markets=[row])).encode()))
        return original(request)

    f.client._transport = httpx.MockTransport(respond)
    recorder = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                        wall_us=f.wall_us, sleep=f.sleep)
    result = recorder.run(RunConfig(selected_event_id='2001', selected_market_id='1001'),
                          should_stop=lambda: False, deadline_ns=f.monotonic_ns() + deadline_ns)
    case.assertEqual(result.exit_code, 0)
    return f
