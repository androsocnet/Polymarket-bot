import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

import httpx


from polymarket_bot.recorder.service import Recorder


class CP5Recorder(Recorder):
    def _reconcile(self, deadline_ns):
        return None


_retained_runs = []


def wall_us():
    return time.time_ns() // 1000


class AuditStream(httpx.SyncByteStream):
    def __init__(self, inner, event, finish):
        self.inner, self.event, self.finish = inner, event, finish
        self.finished = False

    def __iter__(self):
        try:
            for chunk in self.inner:
                self.event['response_bytes'] += len(chunk)
                yield chunk
            self.event['complete'] = True
        except BaseException as exc:
            self.event['error'] = type(exc).__name__
            raise

    def close(self):
        try:
            self.inner.close()
        except BaseException as exc:
            self.event['error'] = type(exc).__name__
            raise
        finally:
            if not self.finished:
                self.finished = True
                self.event['stream_closed'] = True
                self.finish(self.event)


class AuditTransport(httpx.BaseTransport):
    def __init__(self, inner, emit):
        self.inner, self.emit = inner, emit
        self.attempt = 0
        self.print_events = False
        self.lock = threading.Lock()
        self.closed = False

    def handle_request(self, request):
        url = request.url
        routes = {
            'api.exchange.coinbase.com': r'/products/BTC-USD/ticker',
            'api.kraken.com': r'/0/public/Ticker',
            'gamma-api.polymarket.com': r'/events|/events/slug/[A-Za-z0-9_-]+|/markets/[0-9]+',
            'clob.polymarket.com': r'/time|/book|/books|/clob-markets/0x[0-9a-fA-F]{64}',
        }
        method = 'POST' if url.host == 'clob.polymarket.com' and url.path == '/books' else 'GET'
        forbidden = any(any(word in key.lower().replace('_', '-') for word in
            ('authorization', 'cookie', 'api-key', 'apikey', 'signature', 'passphrase', 'secret', 'token'))
            for key in request.headers)
        keys = {key.lower() for key in url.params.keys()}
        allowed_keys = {'pair', 'assetVersion'} if url.host == 'api.kraken.com' else {'token_id'} if url.path == '/book' else {
            'active', 'closed', 'end_date_min', 'end_date_max', 'limit', 'offset', 'order', 'ascending'} if url.path == '/events' else set()
        if (url.scheme != 'https' or url.port not in (None, 443) or url.userinfo or url.fragment
                or url.host not in routes or re.fullmatch(routes.get(url.host, ''), url.path) is None
                or request.method != method or forbidden or not keys <= {key.lower() for key in allowed_keys}):
            raise ValueError('CP5 rejected unapproved or authenticated request')
        selectors = list(url.params.multi_items())
        if len(selectors) != len({key for key, value in selectors}):
            raise ValueError('CP5 duplicate query selector')
        params = dict(selectors)
        if method == 'GET' and request.content:
            raise ValueError('CP5 GET body forbidden')
        if url.path == '/book' and (set(params) != {'token_id'} or
                re.fullmatch(r'[0-9]{1,128}', params['token_id']) is None):
            raise ValueError('CP5 invalid public token selector')
        if url.host == 'api.kraken.com' and params != {'pair': 'XBTUSD', 'assetVersion': '1'}:
            raise ValueError('CP5 invalid public pair')
        if url.path == '/events':
            for key, value in selectors:
                pattern = r'true|false' if key in ('active', 'closed', 'ascending') else r'[0-9]{1,6}' if key in ('limit', 'offset') else r'[A-Za-z_]{1,64}' if key == 'order' else r'[0-9TZ:+.\-]{1,64}'
                if re.fullmatch(pattern, value) is None:
                    raise ValueError('CP5 invalid public event selector')
        if method == 'POST':
            try:
                body = json.loads(request.content)
            except (ValueError, UnicodeError) as exc:
                raise ValueError('CP5 invalid public books body') from exc
            if (not isinstance(body, list) or len(body) != 2 or any(not isinstance(item, dict)
                    or set(item) != {'token_id'} or not isinstance(item['token_id'], str)
                    or re.fullmatch(r'[0-9]{1,128}', item['token_id']) is None for item in body)
                    or body[0]['token_id'] == body[1]['token_id']):
                raise ValueError('CP5 invalid public books body')
        with self.lock:
            self.attempt += 1
            attempt = self.attempt
        event = dict(attempt=attempt, host=url.host, path=url.path, method=request.method,
            started_wall_us=wall_us(), started_mono_ns=time.monotonic_ns(), status=None,
            request_bytes=len(request.content), response_bytes=0, redirect=False,
            request_header_presence=sorted(request.headers.keys()), response_header_presence=[], error=None)
        event.update(complete=False, stream_closed=False, set_cookie_present=False)
        try:
            response = self.inner.handle_request(request)
            event.update(status=response.status_code, redirect=300 <= response.status_code < 400,
                response_header_presence=sorted(response.headers.keys()),
                set_cookie_present='set-cookie' in response.headers)
            if 'set-cookie' in response.headers:
                del response.headers['set-cookie']
            if event['redirect']:
                response.close()
                raise ValueError('CP5 rejected redirect')
            if response.is_stream_consumed:
                response.stream = httpx.ByteStream(response.content)
                event['response_bytes'] = len(response.content)
                event['complete'] = True
                event['stream_closed'] = True
                self.finish(event)
            else:
                response.stream = AuditStream(response.stream, event, self.finish)
            return response
        except BaseException as exc:
            event['error'] = type(exc).__name__
            self.finish(event)
            raise

    def finish(self, event):
        event.update(ended_wall_us=wall_us(), ended_mono_ns=time.monotonic_ns())
        self.emit(event)
        if getattr(self, 'print_events', False):
            print(json.dumps({'actual_send': event}), flush=True)

    def close(self):
        self.inner.close()
        self.closed = True


def align(now=time.time, sleep=time.sleep):
    delay = (-now()) % 900
    if not 0 <= delay <= 900:
        raise ValueError('CP5 alignment wait exceeds bound')
    sleep(delay)
    return delay


def supervise(command, timeout):
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        killed = True
        process.kill()
        stdout, stderr = process.communicate(timeout=15)
    return dict(returncode=process.returncode, watchdog_killed=killed,
        elapsed_seconds=time.monotonic() - started, stdout=stdout, stderr=stderr)


def child(path, seconds):
    from polymarket_bot.market_data.http import PublicGet
    from polymarket_bot.recorder.free_reference import ApproximateReferenceSession, CoinbaseRestProvider, KrakenRestProvider
    from polymarket_bot.recorder.models import RunConfig
    from polymarket_bot.recorder.service import Recorder
    from polymarket_bot.recorder.store import RecorderStore
    if path.exists() or path.name == 'cp5_smoke.sqlite3' or seconds not in (120, 1200):
        raise ValueError('CP5 requires new bounded database')
    events = []
    transport = AuditTransport(httpx.HTTPTransport(retries=0, trust_env=False), events.append)
    transport.print_events = True
    client = httpx.Client(transport=transport, trust_env=False, auth=None, follow_redirects=False)
    store = recorder = result = None
    try:
        store = RecorderStore.open_approximate(path)
        public = PublicGet(client, monotonic=time.monotonic, monotonic_ns=time.monotonic_ns,
            sleep=time.sleep, utcnow=lambda: datetime.now(timezone.utc), max_attempts=2)
        providers = {name: provider(client, monotonic_ns=time.monotonic_ns, wall_us=wall_us)
            for name, provider in [('coinbase', CoinbaseRestProvider), ('kraken', KrakenRestProvider)]}
        session = ApproximateReferenceSession(providers=providers, monotonic_ns=time.monotonic_ns,
            wall_us=wall_us, sleep=time.sleep)
        recorder = CP5Recorder(store, public, monotonic_ns=time.monotonic_ns,
            wall_us=wall_us, sleep=time.sleep, approximate_reference=session)
        result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False,
            deadline_ns=time.monotonic_ns() + seconds * 1_000_000_000)
    finally:
        active = recorder is not None and recorder.approximate_worker is not None and recorder.approximate_worker._thread is not None and recorder.approximate_worker._thread.is_alive()
        try:
            if active:
                _retained_runs.append((recorder, client, store))
            else:
                try:
                    client.close()
                finally:
                    if store is not None:
                        store.close()
        finally:
            print(json.dumps({'lifecycle': dict(worker_active=active,
                client_closed=bool(client.is_closed), transport_closed=transport.closed,
                store_closed=store is None or store._closed,
                remaining_threads=[thread.name for thread in threading.enumerate()
                    if thread is not threading.main_thread()])}), flush=True)
        if active:
            raise RuntimeError('CP5 worker still alive; owners retained until watchdog')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    reopened = RecorderStore.open_approximate(path)
    reopened.close()
    evidence = dict(result=asdict(result), sends=events, integrity=integrity,
        foreign_keys=foreign_keys, user_version=version, client_closed=client.is_closed,
        transport_closed=transport.closed, worker_active=active,
        remaining_threads=[thread.name for thread in threading.enumerate() if thread is not threading.main_thread()],
        writer_reacquired=True)
    print(json.dumps(evidence))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--seconds', type=int, choices=(120, 1200), required=True)
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.db, args.seconds)
    else:
        if args.seconds == 1200:
            print(json.dumps({'alignment_wait_seconds': align()}), flush=True)
        print(json.dumps(supervise([sys.executable, __file__, '--child', '--db', str(args.db),
            '--seconds', str(args.seconds)], args.seconds + 45)))
