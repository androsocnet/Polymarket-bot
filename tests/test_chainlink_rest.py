import json
import unittest
from unittest.mock import patch

import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.chainlink_auth import build_chainlink_auth_headers
from polymarket_bot.recorder.chainlink_rest import ChainlinkRestClient
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID


def make_valid_report_payload(report_bytes=b'signed-report', valid_from=100, obs=160, exp=220, price=6500012345678):
    return {'report': {'feedId': CHAINLINK_FEED_ID, 'fullReport': report_bytes.hex(),
                       'validFromTimestamp': valid_from, 'observationsTimestamp': obs,
                       'expiresAt': exp, 'benchmarkPrice': str(price)}}


class ChainlinkRestClientTests(unittest.TestCase):
    def reader(self, handler, **kwargs):
        reader = ChainlinkRestClient('https://api.dataengine.chain.link', 'cid', 'sec',
                                    httpx.MockTransport(handler), monotonic=lambda: 100,
                                    clock_seconds=lambda: 1746000000, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def test_signed_actual_query_and_approved_interfaces(self):
        requests = []
        def handler(request):
            requests.append(request)
            expected = build_chainlink_auth_headers('cid', 'sec', 'GET', request.url.raw_path.decode(), None, 1746000000000)
            self.assertEqual(request.headers['X-Authorization-Signature-SHA256'], expected['X-Authorization-Signature-SHA256'])
            self.assertNotIn('cookie', request.headers)
            self.assertTrue(all(v <= 2 for v in request.extensions['timeout'].values()))
            value = {'reports': [make_valid_report_payload()]} if request.url.path.endswith('/page') else make_valid_report_payload()
            return httpx.Response(200, json=value, headers={'Set-Cookie': 'session=secret'})
        reader = self.reader(handler)
        self.assertEqual(reader.get_latest_report(CHAINLINK_FEED_ID, 102)[0], b'signed-report')
        reader.get_report_at_timestamp(CHAINLINK_FEED_ID, 160, 102)
        self.assertEqual(len(reader.get_reports_page(CHAINLINK_FEED_ID, 100, 200, 1, 102)), 1)
        self.assertEqual(len(requests), 3)

    def test_exact_https_host(self):
        for url in ('http://api.dataengine.chain.link', 'https://ws.dataengine.chain.link',
                    'https://api.dataengine.chain.link.evil', 'https://user@api.dataengine.chain.link',
                    'https://api.dataengine.chain.link:443', 'https://api.dataengine.chain.link/x',
                    'https://api.dataengine.chain.link?x=1'):
            with self.subTest(url=url), self.assertRaises(MarketDataError):
                ChainlinkRestClient(url, 'cid', 'sec')

    def test_expired_deadline_never_sends(self):
        sent = []
        reader = self.reader(lambda r: sent.append(r) or httpx.Response(200, json=make_valid_report_payload()))
        with self.assertRaises(MarketDataError):
            reader.get_latest_report(CHAINLINK_FEED_ID, 100)
        self.assertEqual(sent, [])

    def test_stream_ceiling_and_absolute_deadline(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(12):
                    yield b'x' * (1024 * 1024)
        reader = self.reader(lambda r: httpx.Response(200, stream=Stream()))
        with self.assertRaises(MarketDataError) as caught:
            reader.get_latest_report(CHAINLINK_FEED_ID, 102)
        self.assertEqual(caught.exception.code, 'response_too_large')
        ticks = iter([100, 100, 100, 103])
        reader._monotonic = lambda: next(ticks)
        with self.assertRaises(MarketDataError) as caught:
            reader.get_latest_report(CHAINLINK_FEED_ID, 102)
        self.assertEqual(caught.exception.code, 'chainlink_timeout')

    def test_fail_closed_and_sanitized(self):
        for status, payload in ((302, {}), (401, {}), (200, {'reports': [{}]}),
                                (200, make_valid_report_payload(report_bytes=b''))):
            reader = self.reader(lambda r: httpx.Response(status, json=payload))
            with self.subTest(status=status, payload=payload), self.assertRaises(MarketDataError):
                if 'reports' in payload:
                    reader.get_reports_page(CHAINLINK_FEED_ID, 0, 200, 1, 102)
                else:
                    reader.get_latest_report(CHAINLINK_FEED_ID, 102)
        def fail(request):
            raise httpx.ConnectError('sec cid credential leak')
        reader = self.reader(fail)
        with self.assertRaises(MarketDataError) as caught:
            reader.get_latest_report(CHAINLINK_FEED_ID, 102)
        self.assertNotIn('credential leak', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_each_stream_read_clamps_remaining_budget(self):
        clock = [100.0]
        class Stream(httpx.SyncByteStream):
            def __iter__(stream):
                yield b' '
                clock[0] = 101.75
                yield b' '
                self.assertLessEqual(requests[0].extensions['timeout']['read'], 0.25)
                yield json.dumps(make_valid_report_payload()).encode()
        requests = []
        reader = self.reader(lambda r: requests.append(r) or httpx.Response(200, stream=Stream()))
        reader._monotonic = lambda: clock[0]
        self.assertEqual(reader.get_latest_report(CHAINLINK_FEED_ID, 102)[0], b'signed-report')

    def test_invalid_parameters_never_send(self):
        reader = self.reader(lambda r: self.fail('sent invalid request'))
        for args in ((CHAINLINK_FEED_ID, True, 200, 1, 102), (CHAINLINK_FEED_ID, 0, 200, True, 102),
                     ('wrong', 0, 200, 1, 102)):
            with self.assertRaises(MarketDataError):
                reader.get_reports_page(*args)
