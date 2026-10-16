import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx

from polymarket_bot.market_data.gamma import GammaClient
from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.metadata import acquire_market, acquire_next_market
from polymarket_bot.recorder.models import RunConfig
from scripts.cp5_run import CP5Recorder
from tests.test_recorder_metadata import MetadataTests
from tests.support import make_event, make_market


def preparation_cases():
    fixture = MetadataTests()
    fixture.setUp()
    try:
        gamma, public, *_ = fixture.transport()
        previous = acquire_market(gamma, public, clock=fixture.clock, deadline=120, use_fast_path=False)
        previous = replace(previous, market=replace(previous.market, condition_id='0x'+'ef'*32))
        start = previous.market.window_end
        future = make_market(id='1002', clobTokenIds='["3","4"]', eventStartTime=start.isoformat(),
                             endDate=(start+timedelta(seconds=900)).isoformat())
        event = make_event(id='2002', markets=[future])
        compact = json.dumps([event], separators=(',', ':')).encode()
        large = compact + b' ' * (1689540-len(compact))
        ambiguous = [event, make_event(id='2003', markets=[dict(future, id='1003',
                     conditionId='0x'+'cd'*32, clobTokenIds='["5","6"]')])]
        cases = [('A', large, None), ('B', compact, None),
                 ('C', b'[{', 'invalid_metadata'), ('D', b'[{"x":1e999999999999999999}]', 'invalid_metadata'),
                 ('E', b'[]', 'no_next_market'),
                 ('F', json.dumps(ambiguous).encode(), 'ambiguous_market')]
        for label, body, expected in cases:
            paths, attempts = [], []
            def respond(request):
                paths.append(request.url.path)
                if request.url.path == '/events':
                    raw = body
                elif request.url.path == '/markets/1002':
                    raw = json.dumps(future).encode()
                elif request.url.path.startswith('/clob-markets/'):
                    raw = b'{"t":[{"t":"3"},{"t":"4"}]}'
                else:
                    raise AssertionError(request.url.path)
                return httpx.Response(200, stream=httpx.ByteStream(raw))
            with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
                public = PublicGet(client, monotonic=fixture.time.monotonic, sleep=fixture.time.sleep,
                                   utcnow=fixture.time.utcnow, max_attempts=1, on_attempt=attempts.append)
                error = None
                try:
                    revision = acquire_next_market(previous, GammaClient(public), public,
                                                   clock=fixture.clock, deadline=120)
                    assert revision.market.market_id == '1002' and revision.capture_ready
                except MarketDataError as exc:
                    error = exc
                assert (error.code if error else None) == expected
                chain = []
                current = error
                while current is not None:
                    chain.append(dict(type=type(current).__name__, code=getattr(current, 'code', None), message=str(current)))
                    current = current.__cause__
                print(json.dumps(dict(case=label, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                    paths=paths, outcome=expected or 'prepared', exceptions=chain,
                    forwarded=[dict(path=a.path, result=a.result, error_code=a.error_code,
                                    raw_bytes=len(a.raw_body), raw_complete=a.raw_complete) for a in attempts])))
                assert public.on_attempt == attempts.append
                if label == 'A':
                    assert len(attempts) == 3 and attempts[0].raw_body == body
                if label in ('C', 'D'):
                    assert len(attempts) == 1 and attempts[0].raw_body == body
                    assert attempts[0].error_code == 'invalid_metadata'
    finally:
        fixture.tearDown()


def exceptional_audit_case():
    class Store:
        _write_failed = False
        def __init__(self):
            self.events, self.calls = [], []
        def record_event(self, *args, **kwargs):
            self.events.append(kwargs['detail'])
        def recover_interrupted(self, **kwargs):
            self.calls.append('recover')
        def finish_run(self, *args, **kwargs):
            self.calls.append(('finish', kwargs['state'], kwargs['reason']))
        def close(self):
            self.calls.append('close')
    recorder = cast(Any, object.__new__(CP5Recorder))
    recorder.store = Store()
    recorder.public = SimpleNamespace(max_attempts=2, before_attempt=None)
    recorder.run_id, recorder.finalized = 'synthetic-run', 0
    recorder.reference_worker = recorder.approximate_worker = None
    recorder.wall_us = lambda: 1
    recorder.monotonic_ns = lambda: 1
    recorder._audit_events = [dict(attempt=1, started_wall_us=1, path='/time')]
    def record(config, **kwargs):
        raise MarketDataError('opening_blocked', 'Synthetic opening cutoff.')
    recorder._record = record
    result = recorder._execute_started_run(RunConfig(mode='BOOK_ONLY'), should_stop=lambda: False)
    assert result.reason == 'opening_blocked' and result.exit_code == 2
    assert len(recorder.store.events) == 1 and 'close' in recorder.store.calls
    print(json.dumps(dict(case='G', result=result.reason, pending=len(recorder._audit_events),
                          persisted=len(recorder.store.events), calls=recorder.store.calls)))
    recorder._flush_audit()
    recorder._flush_audit()
    assert len(recorder.store.events) == 1
    print(json.dumps(dict(case='G-control', explicit_flush_persisted=len(recorder.store.events), idempotent=True)))


if __name__ == '__main__':
    preparation_cases()
    exceptional_audit_case()
