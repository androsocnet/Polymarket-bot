from __future__ import annotations

from contextlib import closing
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import load_numeric_json, parse_integer
from polymarket_bot.recorder.chainlink_auth import build_chainlink_auth_headers, validate_credentials
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
from polymarket_bot.recorder.reference import decode_schema_v2_report


def validate_endpoint(url, scheme, host, paths):
    if not isinstance(url, str) or url != url.strip():
        raise MarketDataError('invalid_host', 'Invalid Chainlink endpoint.')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == scheme and parsed.netloc.lower() == host
                 and parsed.path in paths and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise MarketDataError('invalid_host', 'Invalid Chainlink endpoint.')


def validate_feed(feed_id):
    if feed_id != CHAINLINK_FEED_ID:
        raise MarketDataError('invalid_reference_identity', 'Unexpected Chainlink feed.')


def parse_report(payload):
    if not isinstance(payload, dict):
        raise MarketDataError('invalid_response', 'Invalid Chainlink report.')
    report = payload.get('report', payload)
    if not isinstance(report, dict):
        raise MarketDataError('invalid_response', 'Invalid Chainlink report.')
    hex_value = report.get('fullReport', report.get('full_report'))
    if not isinstance(hex_value, str):
        raise MarketDataError('invalid_response', 'Invalid Chainlink report bytes.')
    hex_value = hex_value.removeprefix('0x').removeprefix('0X')
    if not hex_value or len(hex_value) > 20_971_520 or re.fullmatch('[0-9a-fA-F]+', hex_value) is None or len(hex_value) % 2:
        raise MarketDataError('invalid_response', 'Invalid Chainlink report bytes.')
    draft = decode_schema_v2_report(report, price_scale=None, decoder_revision=None,
                                    receipt_wall_us=0, receipt_server_us=None)
    if draft.decode_status == 'INVALID':
        raise MarketDataError('invalid_response', 'Invalid Chainlink report metadata.')
    return bytes.fromhex(hex_value), dict(report), draft


class ChainlinkRestClient:
    _BASE = 'https://api.dataengine.chain.link'
    _MAX_RESPONSE_BYTES = 10 * 1024 * 1024

    def __init__(self, base_url, client_id, client_secret, transport=None, *,
                 monotonic=time.monotonic, clock_seconds=time.time):
        validate_endpoint(base_url, 'https', 'api.dataengine.chain.link', ('', '/'))
        self._client_id, self._client_secret = validate_credentials(client_id, client_secret)
        self._monotonic = monotonic
        self._clock_seconds = clock_seconds
        self.on_attempt = None
        self._client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)

    def close(self):
        self._client.close()

    def _remaining(self, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise MarketDataError('invalid_deadline', 'Invalid absolute deadline.')
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise MarketDataError('chainlink_timeout', 'Chainlink deadline expired.')
        return remaining

    def _get(self, path, params, deadline):
        if path not in ('/api/v1/reports/latest', '/api/v1/reports', '/api/v1/reports/page'):
            raise MarketDataError('invalid_path', 'Invalid Chainlink read path.')
        remaining = self._remaining(deadline)
        url = httpx.URL(self._BASE + path, params=params)
        headers = build_chainlink_auth_headers(self._client_id, self._client_secret, 'GET',
                                               url.raw_path.decode('ascii'), None,
                                               int(self._clock_seconds() * 1000))
        headers['Accept-Encoding'] = 'identity'
        request = httpx.Request('GET', url, headers=headers,
                                extensions={'timeout': {phase: min(remaining, 2.0)
                                            for phase in ('connect', 'read', 'write', 'pool')}})
        started_ns = round(self._monotonic() * 1_000_000_000)
        started_wall = round(self._clock_seconds() * 1_000_000)
        body, status, complete, error_code = bytearray(), None, False, None
        try:
            self._client.cookies.clear()
            with closing(self._client.send(request, stream=True, follow_redirects=False)) as response:
                self._remaining(deadline)
                status = response.status_code
                if status != 200:
                    code = 'chainlink_auth_failed' if status in (401, 403) else 'chainlink_http_error'
                    raise MarketDataError(code, 'Chainlink request rejected.', http_status=status)
                length = response.headers.get('Content-Length')
                if length is not None:
                    size = parse_integer(length, field='content length', minimum=0, maximum=2**63-1)
                    if size > self._MAX_RESPONSE_BYTES:
                        raise MarketDataError('response_too_large', 'Chainlink response exceeds 10 MiB.')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise MarketDataError('invalid_response', 'Compressed Chainlink responses are unsupported.')
                body = bytearray()
                chunks = iter((response.content,)) if response.is_stream_consumed else response.iter_raw()
                while True:
                    remaining = self._remaining(deadline)
                    request.extensions['timeout']['read'] = min(remaining, 2.0)
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    self._remaining(deadline)
                    if len(body) + len(chunk) > self._MAX_RESPONSE_BYTES:
                        raise MarketDataError('response_too_large', 'Chainlink response exceeds 10 MiB.')
                    body.extend(chunk)
                self._remaining(deadline)
                complete = True
                payload = load_numeric_json(bytes(body))
                self._remaining(deadline)
                return payload
        except MarketDataError as exc:
            error_code = exc.code
            raise
        except httpx.TimeoutException:
            error_code = 'chainlink_timeout'
            raise MarketDataError('chainlink_timeout', 'Chainlink transport timed out.') from None
        except httpx.HTTPError:
            error_code = 'chainlink_http_error'
            raise MarketDataError('chainlink_http_error', 'Chainlink transport failed.') from None
        finally:
            self._client.cookies.clear()
            if self.on_attempt is not None:
                from polymarket_bot.market_data.http import HttpAttempt
                operation = {'/api/v1/reports/latest': 'REFERENCE_LATEST',
                             '/api/v1/reports': 'REFERENCE_LOOKUP',
                             '/api/v1/reports/page': 'REFERENCE_PAGE'}[path]
                self.on_attempt(HttpAttempt(operation, 'GET', path,
                    ('feed_id=' + CHAINLINK_FEED_ID,), 1, started_ns,
                    round(self._monotonic() * 1_000_000_000), started_wall,
                    round(self._clock_seconds() * 1_000_000), status,
                    'error' if error_code else 'success', error_code, bytes(body), complete))

    def get_latest_report(self, feed_id, deadline):
        validate_feed(feed_id)
        raw, fields, _ = parse_report(self._get('/api/v1/reports/latest', {'feedId': feed_id}, deadline))
        self._remaining(deadline)
        return raw, fields

    def get_report_at_timestamp(self, feed_id, timestamp_s, deadline):
        validate_feed(feed_id)
        timestamp_s = parse_integer(timestamp_s, field='timestamp', minimum=0, maximum=253_402_300_799)
        raw, fields, _ = parse_report(self._get('/api/v1/reports', {'feedId': feed_id, 'timestamp': timestamp_s}, deadline))
        self._remaining(deadline)
        return raw, fields

    def get_reports_page(self, feed_id, start_s, end_s, limit, deadline):
        validate_feed(feed_id)
        start_s = parse_integer(start_s, field='start', minimum=0, maximum=253_402_300_799)
        end_s = parse_integer(end_s, field='end', minimum=start_s, maximum=253_402_300_799)
        limit = parse_integer(limit, field='limit', minimum=1, maximum=100)
        payload = self._get('/api/v1/reports/page', {'feedId': feed_id, 'start': start_s,
                                                   'end': end_s, 'limit': limit}, deadline)
        if not isinstance(payload, dict) or not isinstance(payload.get('reports'), list) or len(payload['reports']) > limit:
            raise MarketDataError('invalid_response', 'Invalid Chainlink page.')
        results = []
        for report in payload['reports']:
            self._remaining(deadline)
            raw, fields, _ = parse_report(report)
            results.append((raw, fields))
        self._remaining(deadline)
        return results
