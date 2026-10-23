"""SYNTHETIC CP6.2 fixtures. Real recorder/store; fake time and HTTP only."""
from decimal import Decimal
import json
import httpx
from tests.recorder_support import RecorderFixture, WINDOW_START_US
from tests.support import make_book, make_event, make_market, TOKEN_UP, TOKEN_DOWN


class FullWindowFixture(RecorderFixture):
    def __init__(self, *, startup_us=-30_000_000, faults=None):
        self.faults = faults or {}
        super().__init__(offset_us=startup_us-300_000_000)
        self.fixed_market = make_market()
        self.dispatches = []

    def market_row(self):
        return self.fixed_market

    def respond(self, request):
        assert not self.store._connection.in_transaction
        path = request.url.path
        now = self.wall_us()
        index = (now-WINDOW_START_US)//2_000_000
        fault = self.faults.get(index)
        if path in ('/events/2001', '/markets/1001'):
            # Metadata refresh need not coincide with a capture target.
            fault = next((v for i,v in sorted(self.faults.items(), reverse=True)
                          if i<=index and v in {'metadata_failure', 'identity_conflict', 'closed'}), fault)
        self.dispatches.append((request.method, path, self.monotonic_ns()))
        self.requests.append((request.method, path, self.monotonic_ns(),
                              dict(request.extensions['timeout']), bytes(request.content)))
        row = dict(self.fixed_market)
        if path == '/time':
            payload = now//1_000_000
            if fault == 'clock_uncertain':
                self.advance(0.6)
            elif fault == 'clock_discontinuity':
                payload += 10
        elif path in ('/events/2001', '/markets/1001'):
            if fault == 'metadata_failure':
                self.advance(0.01)
                return httpx.Response(503, stream=httpx.ByteStream(b'{"synthetic":true}'))
            if fault == 'closed':
                row['closed'] = True
                row['active'] = False
            if fault == 'identity_conflict':
                row['conditionId'] = '0x'+'cd'*32
            payload = make_event(markets=[row]) if path.startswith('/events/') else row
        elif path in ('/clob-markets/'+row['conditionId'], '/clob-markets/0x'+'cd'*32):
            payload = {'condition_id': row['conditionId'],
                       't': [{'t': token} for token in (TOKEN_UP, TOKEN_DOWN)]}
        elif path in ('/books', '/book'):
            self.book_count += 1
            if fault == 'interrupt':
                raise KeyboardInterrupt('SYNTHETIC_INTERRUPT')
            if fault == 'http_failure':
                self.advance(0.01)
                return httpx.Response(503, stream=httpx.ByteStream(b'{"synthetic":true}'))
            up = Decimal('.30')+Decimal(index % 20)*Decimal('.02')
            payload = []
            for token, mid in ((TOKEN_UP, up), (TOKEN_DOWN, 1-up)):
                payload.append(make_book(asset_id=token, timestamp=str(now//1000),
                    bids=[{'price': str(mid-Decimal('.01')), 'size': '100'}],
                    asks=[{'price': str(mid+Decimal('.01')), 'size': '100'}],
                    fixture_evidence='SYNTHETIC_CP6_2_NOT_LIVE'))
            if fault == 'invalid':
                payload[1]['bids'][0]['size'] = 'NaN'
            elif fault == 'partial':
                payload = payload[:1]
            elif fault == 'late':
                self.advance(2.1)
            elif fault == 'stale':
                for book in payload:
                    book['timestamp'] = str((now-10_000_000)//1000)
            if path == '/book':
                token = request.url.params['token_id']
                payload = next((b for b in payload if b['asset_id']==token), {})
        else:
            raise AssertionError('SYNTHETIC_ONLY route refused: '+str(request.url))
        self.advance(0.01)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))

    def record(self, *, stop=None):
        from polymarket_bot.recorder.models import RunConfig
        from polymarket_bot.recorder.service import Recorder
        recorder = Recorder(self.store, self.public, monotonic_ns=self.monotonic_ns,
                            wall_us=self.wall_us, sleep=self.sleep)
        result = recorder.run(RunConfig(selected_event_id='2001', selected_market_id='1001'),
                              should_stop=stop or (lambda: False))
        return result
