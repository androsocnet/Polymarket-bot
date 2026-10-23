"""Replay four retained public responses; never contact live endpoints."""
import hashlib
import itertools
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

import httpx

from polymarket_bot.market_data.gamma import GammaClient
from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.clock import RecorderClock
from polymarket_bot.recorder.metadata import acquire_selected_market
from tests.support import FakeClock


CONDITION = '0x18871c4246919a2ce652d0828a34e8d1490ce9f8cc03b4c8ffc9eb6b6b182666'
ALIASES = ('c', 'condition_id', 'conditionId')
FIXTURES = Path(__file__).parent / 'fixtures' / 'cp5_live_clob'
HASHES = (
    '1195c747060ba464136e4b33c12b80d069efdd46b0d99d5428e6a9869f2520c9',
    'c90ad011b3ff5127b66ec062c0aa6acd43c6c88dda2b6992b4eab52c7c3d4d4f',
    '20a3104bb24d612571dcbbdd087fc57e782e6c2ce70aa9834db438159b5fca2e',
    'd4f576c33d7e18ef6383966fccaef39c40e663e8f7008c17fc029dccd7639f72',
)


class LiveClobTests(unittest.TestCase):
    def setUp(self):
        for target, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
                             (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            guard = patch.object(target, name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.raw = [(FIXTURES / f'response-{i}.json').read_bytes() for i in range(1, 5)]
        self.assertEqual(tuple(hashlib.sha256(raw).hexdigest() for raw in self.raw), HASHES)
        self.config = json.loads(self.raw[3])

    def replay(self, config=None, event=None):
        time = FakeClock()
        paths = ['/time', '/events/1217823', '/markets/5923166', '/clob-markets/' + CONDITION]
        bodies = list(self.raw)
        if config is not None:
            bodies[3] = json.dumps(config).encode()
        if event is not None:
            bodies[1] = json.dumps(event).encode()
        pending = list(zip(paths, bodies))
        attempts = []

        def respond(request):
            expected, body = pending.pop(0)
            self.assertEqual((request.method, request.url.path), ('GET', expected))
            return httpx.Response(200, stream=httpx.ByteStream(body))

        with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
            public = PublicGet(client, monotonic=time.monotonic, sleep=time.sleep,
                               monotonic_ns=lambda: int(time.value * 1e9), utcnow=time.utcnow,
                               max_attempts=1, on_attempt=attempts.append)
            clock = RecorderClock(monotonic_ns=lambda: int(time.value * 1e9),
                                  wall_us=lambda: 1792503513000000)
            clock.calibrate(public.get_json('clob', '/time', deadline=30))
            revision = acquire_selected_market(GammaClient(public), public, clock=clock,
                deadline=30, event_id='1217823', market_id='5923166')
        self.assertFalse(pending)
        self.assertEqual([attempt.raw_body for attempt in attempts], bodies)
        return revision

    def test_captured_responses_acquire_selected_revision(self):
        revision = self.replay()
        self.assertTrue(revision.capture_ready)
        self.assertEqual(revision.market.condition_id, CONDITION)
        self.assertEqual((revision.up_index, revision.down_index), (0, 1))
        self.assertEqual((revision.gamma_raw, revision.clob_config_raw), (self.raw[2], self.raw[3]))
        self.assertEqual(revision.market.metadata['selected_event_raw'], self.raw[1].decode())
        self.assertEqual(revision.config['c'], CONDITION)

    def test_each_alias_and_matching_combinations_accepted(self):
        for size in (1, 2, 3):
            for aliases in itertools.combinations(ALIASES, size):
                with self.subTest(aliases=aliases):
                    config = {k: v for k, v in self.config.items() if k not in ALIASES}
                    config.update(dict.fromkeys(aliases, CONDITION))
                    self.assertTrue(self.replay(config).capture_ready)

    def test_missing_condition_alias_rejected(self):
        config = {k: v for k, v in self.config.items() if k not in ALIASES}
        with self.assertRaises(MarketDataError):
            self.replay(config)

    def test_malformed_or_conflicting_alias_never_hidden_by_matching_alias(self):
        for alias in ALIASES:
            for value in (None, '', True, 123, [], {}, 'bad', CONDITION + ' ',
                          CONDITION.upper(), '0x' + 'ff' * 32):
                for matching in (False, True):
                    with self.subTest(alias=alias, value=value, matching=matching):
                        config = {k: v for k, v in self.config.items() if k not in ALIASES}
                        if matching:
                            config.update(dict.fromkeys(ALIASES, CONDITION))
                        config[alias] = value
                        with self.assertRaises(MarketDataError):
                            self.replay(config)

    def test_c_alias_preserves_token_and_outcome_rejection(self):
        tokens = self.config['t']
        for rows in (None, [], tokens[:1], [tokens[0], tokens[0]],
                     [dict(tokens[0], t=123), tokens[1]],
                     [dict(tokens[0], t='1'), tokens[1]],
                     [dict(tokens[0], o='Down'), tokens[1]]):
            with self.subTest(rows=rows), self.assertRaises(MarketDataError):
                self.replay(dict(self.config, t=rows))
        config = dict(self.config)
        del config['t']
        with self.assertRaises(MarketDataError):
            self.replay(config)

    def test_c_alias_does_not_override_gamma_identity(self):
        event = json.loads(self.raw[1])
        event['markets'][0]['conditionId'] = '0x' + 'ff' * 32
        with self.assertRaises(MarketDataError) as caught:
            self.replay(event=event)
        self.assertEqual(caught.exception.code, 'identity_conflict')
