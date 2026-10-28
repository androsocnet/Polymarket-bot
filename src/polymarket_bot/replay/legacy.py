"""Explicit, replay-only recovery of one automatic-discovery CP5 scope.

ponytail: bounded direct-market legacy format; add formats only with new evidence
and strict parity tests. Runtime loaders and persisted selection intent stay intact.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from polymarket_bot.full_window import _timestamp_us, _validate_anchors
from polymarket_bot.market_data.clob import parse_book
from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import load_numeric_json, parse_decimal
from polymarket_bot.paper import Recording, Snapshot, _missing_slot_start, _require
from polymarket_bot.recorder.store import EvidenceError, decode_evidence, _selected_market
from polymarket_bot.linux_mvp.settlement import verify_evidence
from .adapter import _market_descriptor, _pair_paper_snapshots

RUN_ID = '0cba7ec8-c91f-4b78-a9b3-ee7d54e55b40'
MARKET_ID = '5483041'
EVENT_ID = '1163394'


def _raw(request):
    _require(request['http_status'] == 200 and request['result'] == 'success' and
             request['raw_complete'] == 1, 'incomplete metadata audit')
    _require(request['start_mono_ns'] <= request['end_mono_ns'], 'metadata request ordering')
    raw = decode_evidence(request['response_raw_z'], request['raw_size_bytes'], request['codec_version'])
    _require(len(raw) == request['response_bytes'], 'metadata raw length mismatch')
    return raw


def _identity(row, market):
    _require(isinstance(row, dict) and row.get('id') == market['market_id'] and
             row.get('conditionId') == market['condition_id'], 'metadata identity mismatch')
    _require(row.get('active') is True and row.get('closed') is False and
             row.get('archived') is not True and row.get('acceptingOrders') is True and
             row.get('enableOrderBook') is True, 'metadata not open for books')
    _require(_timestamp_us(row['eventStartTime']) == market['window_start_us'] and
             _timestamp_us(row['endDate']) == market['window_end_us'], 'metadata window mismatch')
    outcomes, ids = row['outcomes'], row['clobTokenIds']
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    ids = json.loads(ids) if isinstance(ids, str) else ids
    _require(len(outcomes) == len(ids) == 2 and
             dict(zip((o.upper() for o in outcomes), ids)) ==
             {'UP': market['up_token_id'], 'DOWN': market['down_token_id']}, 'metadata token mismatch')


def _prove_scope(conn, market, slots, anchors):
    """Require unique recorded discovery membership and causally linked raw metadata."""
    requests = conn.execute('SELECT * FROM request_attempts WHERE run_id=? ORDER BY end_mono_ns,request_id',
                            (RUN_ID,)).fetchall()
    discovery = []
    for request in requests:
        if request['operation'] != 'GAMMA_EVENTS' or request['path'] != '/events':
            continue
        if request['http_status'] != 200 or request['result'] != 'success':
            continue
        _require(request['method'] == 'GET' and request['slot_id'] is None and
                 request['market_id'] is None, 'discovery audit mismatch')
        selectors = json.loads(request['selector_json'])
        _require(isinstance(selectors, list) and 'active=true' in selectors and
                 'closed=false' in selectors, 'discovery selector mismatch')
        payload = json.loads(_raw(request))
        _require(isinstance(payload, list), 'discovery envelope not list')
        members = [(event, row) for event in payload if isinstance(event, dict)
                   for row in event.get('markets', [])
                   if isinstance(row, dict) and row.get('id') == market['market_id']]
        if not members:
            continue
        _require(len(members) == 1, 'ambiguous discovery membership')
        event, row = members[0]
        _require(event.get('id') == market['event_id'] and event.get('active') is True and
                 event.get('closed') is False and event.get('archived') is not True,
                 'discovery event mismatch')
        _identity(row, market)
        discovery.append(request)
    _require(bool(discovery), 'missing discovery membership')
    revision_ids = sorted({s['metadata_revision_id'] for s in slots})
    for revision_id in revision_ids:
        rev = conn.execute('SELECT * FROM market_revisions WHERE revision_id=?', (revision_id,)).fetchone()
        _require(rev is not None and rev['run_id'] == RUN_ID and rev['market_id'] == MARKET_ID,
                 'metadata revision identity mismatch')
        sizes = json.loads(rev['raw_sizes_json'])
        gamma_raw = decode_evidence(rev['gamma_raw_z'], sizes['gamma'], rev['codec_version'])
        clob_raw = decode_evidence(rev['clob_config_raw_z'], sizes['clob'], rev['codec_version'])
        gamma = json.loads(gamma_raw)
        _identity(gamma, market)
        canonical = json.dumps(_selected_market(gamma_raw, MARKET_ID), sort_keys=True,
                               separators=(',', ':'), default=str).encode()
        _require(sha256(canonical + b'\x00' + clob_raw).hexdigest() == rev['content_hash'],
                 'metadata hash mismatch')
        linked = [s for s in slots if s['metadata_revision_id'] == revision_id and s['start_mono_ns'] is not None]
        if not linked:
            continue
        first = min(linked, key=lambda s: s['start_mono_ns'])
        anchor = anchors.get(first['anchor_id'])
        _require(anchor is not None, 'metadata missing anchor')
        capture_us = anchor['anchor_server_us'] + (first['start_mono_ns'] - anchor['anchor_mono_ns']) // 1000
        _require(rev['observed_us'] <= capture_us, 'metadata learned after capture')
        _require(any(r['end_mono_ns'] <= first['start_mono_ns'] for r in discovery),
                 'discovery learned after capture')
        for operation, path, selector, raw in (
                ('GAMMA_MARKET', '/markets/' + MARKET_ID, ['market_id=' + MARKET_ID], gamma_raw),
                ('CLOB_CONFIG', '/clob-markets/' + market['condition_id'],
                 ['condition=' + market['condition_id']], clob_raw)):
            _require(any(r['operation'] == operation and r['path'] == path and
                         r['method'] == 'GET' and r['slot_id'] is None and
                         r['market_id'] in (None, MARKET_ID) and
                         json.loads(r['selector_json']) == selector and
                         r['end_mono_ns'] <= first['start_mono_ns'] and
                         r['http_status'] == 200 and r['result'] == 'success' and _raw(r) == raw
                         for r in requests), 'metadata raw audit linkage missing')
    return [r['request_id'] for r in discovery], revision_ids


def _settlement(conn, market, revision_ids):
    """Proven capture Gamma only; later wall-clock learning never joins capture epochs."""
    obs = conn.execute("SELECT * FROM resolution_observations WHERE market_id=? AND classification='RESOLVED_DIRECTIONAL' ORDER BY learned_wall_us DESC,resolution_id DESC LIMIT 1",
                       (MARKET_ID,)).fetchone()
    rev = conn.execute('SELECT * FROM market_revisions WHERE revision_id=? AND run_id=? AND market_id=?',
                       (max(revision_ids), RUN_ID, MARKET_ID)).fetchone()
    if obs is None or rev is None:
        return None, None
    request = conn.execute('SELECT * FROM request_attempts WHERE request_id=?', (obs['request_id'],)).fetchone()
    learned_run = conn.execute('SELECT * FROM recording_runs WHERE run_id=?', (request['run_id'],)).fetchone() if request else None
    _require(request is not None and learned_run is not None and
             learned_run['state'] in ('STOPPED', 'FAILED', 'INTERRUPTED') and learned_run['ended_wall_us'] is not None and
             request['method'] == 'GET' and request['operation'] == 'RESOLUTION' and
             request['path'] == '/v2/resolutions' and request['slot_id'] is None and
             request['market_id'] in (None, MARKET_ID) and obs['condition_id'] == market['condition_id'] and
             'condition=' + market['condition_id'] in json.loads(request['selector_json']), 'settlement audit mismatch')
    raw = decode_evidence(obs['raw_resolution_z'], obs['raw_size_bytes'], obs['codec_version'])
    _require(raw == _raw(request) and sha256(raw).hexdigest() == obs['response_hash'], 'settlement raw mismatch')
    sizes = json.loads(rev['raw_sizes_json'])
    gamma = decode_evidence(rev['gamma_raw_z'], sizes['gamma'], rev['codec_version']).decode('utf-8')
    resolution = json.loads(raw)
    rows = [r for r in resolution.get('data', []) if isinstance(r, dict) and r.get('condition_id') == market['condition_id']]
    at_us = obs['learned_server_us'] if obs['learned_server_us'] is not None else obs['learned_wall_us']
    evidence = dict(gamma_raw=gamma, resolution_raw=json.dumps({'data': rows}, sort_keys=True, separators=(',', ':')),
                    market_id=MARKET_ID, condition_id=market['condition_id'],
                    window_start_us=market['window_start_us'], window_end_us=market['window_end_us'], at_us=at_us)
    window = dict(market_id=MARKET_ID, condition_id=market['condition_id'],
                  start_us=market['window_start_us'], end_us=market['window_end_us'])
    tokens = {'UP': market['up_token_id'], 'DOWN': market['down_token_id']}
    return evidence, verify_evidence(evidence, window=window, tokens=tokens, at_us=at_us)


def _external_settlement(path, expected_sha256, market, source_sha256, recorded, recorded_sha256):
    """Later knowledge only. Pins attest file identity, not independent on-chain finality."""
    raw = Path(path).read_bytes()
    _require(sha256(raw).hexdigest() == expected_sha256, 'settlement bundle SHA256 mismatch')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result, 'duplicate settlement bundle key')
                result[key] = value
            return result

        bundle = json.loads(raw, object_pairs_hook=unique)
        _require(isinstance(bundle, dict) and bundle['schema'] == 'phase3b-external-settlement-v1',
                 'settlement bundle schema mismatch')
        for key, expected in dict(market_id=MARKET_ID, run_id=RUN_ID,
                                  condition_id=market['condition_id'], source_sha256=source_sha256,
                                  window_start_us=market['window_start_us'],
                                  window_end_us=market['window_end_us']).items():
            _require(bundle[key] == expected, 'settlement bundle identity mismatch: ' + key)
        sources = bundle['sources']
        at_us = market['window_end_us']
        now_us = _timestamp_us(datetime.now(timezone.utc).isoformat())
        for name, url in (
                ('gamma', 'https://gamma-api.polymarket.com' + '/markets/' + MARKET_ID),
                ('data_api', 'https://data-api.polymarket.com' + '/v2/resolutions?condition=' + market['condition_id'])):
            source = sources[name]
            _require(source['method'] == 'GET' and source['url'] == source['final_url'] == url and
                     source['http_status'] == 200, 'settlement source provenance mismatch')
            body = source['raw'].encode('utf-8')
            _require(len(body) == source['size_bytes'] and sha256(body).hexdigest() == source['sha256'],
                     'settlement source raw hash/size mismatch')
            start, end = _timestamp_us(source['started_utc']), _timestamp_us(source['retrieved_utc'])
            _require(market['window_end_us'] <= start <= end <= now_us, 'settlement retrieval time mismatch')
            at_us = max(at_us, end)
        # Existing loader already proves recorded request/run/raw coupling. Do not replace that gate.
        _require(recorded is not None and sources['data_api']['sha256'] == recorded_sha256,
                 'settlement differs from recorded resolution')
        evidence = dict(recorded, gamma_raw=sources['gamma']['raw'],
                        resolution_raw=sources['data_api']['raw'], at_us=at_us)
        window = dict(market_id=MARKET_ID, condition_id=market['condition_id'],
                      start_us=market['window_start_us'], end_us=market['window_end_us'])
        tokens = {'UP': market['up_token_id'], 'DOWN': market['down_token_id']}
        result = verify_evidence(evidence, window=window, tokens=tokens, at_us=at_us)
        _require(result['status'] == 'RESOLVED', 'external settlement rejected: ' + str(result.get('diagnostic')))
        return evidence, result, dict(bundle_sha256=expected_sha256, at_us=at_us,
                                      availability='LATER_SETTLEMENT_ONLY',
                                      sources={name: {k: sources[name][k] for k in ('url', 'retrieved_utc', 'sha256')}
                                               for name in ('gamma', 'data_api')})
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as exc:
        raise ValueError('malformed settlement bundle') from exc


def load_legacy_replay_recording(db, run_id, market_id, *, expected_sha256,
                                 settlement_file=None, settlement_sha256=None):
    """Return (adapter six-tuple, scope diagnostics); never rewrite run selection."""
    _require((run_id, market_id) == (RUN_ID, MARKET_ID), 'unsupported legacy scope')
    _require((settlement_file is None) == (settlement_sha256 is None),
             'settlement file and SHA256 required together')
    db = Path(db).resolve(strict=True)
    _require(db.is_file(), 'database must be file')
    _require(not any(Path(str(db) + s).exists() for s in ('-wal', '-shm', '-journal')), 'SQLite sidecar not supported')
    digest = sha256(db.read_bytes()).hexdigest()
    _require(digest == expected_sha256, 'source SHA256 mismatch')
    snapshots, excluded, opportunities = [], [], []
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        _require(conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'database integrity failed')
        _require(not conn.execute('PRAGMA foreign_key_check').fetchall(), 'foreign keys failed')
        run = conn.execute('SELECT * FROM recording_runs WHERE run_id=?', (run_id,)).fetchone()
        market = conn.execute('SELECT * FROM markets WHERE market_id=?', (market_id,)).fetchone()
        _require(run is not None and market is not None, 'unknown run/market')
        _require(market['event_id'] == EVENT_ID, 'unsupported legacy event scope')
        _require(run['state'] in ('STOPPED', 'FAILED', 'INTERRUPTED') and run['ended_wall_us'] is not None,
                 'recording must be terminal')
        tokens = {'UP': market['up_token_id'], 'DOWN': market['down_token_id']}
        condition = market['condition_id']
        _require(tokens['UP'] != tokens['DOWN'], 'token collision')
        slots = conn.execute('SELECT * FROM sample_slots WHERE run_id=? AND market_id=? ORDER BY start_mono_ns,slot_id',
                             (run_id, market_id)).fetchall()
        anchors = {r['anchor_id']: r for r in conn.execute('SELECT * FROM clock_anchors WHERE run_id=?', (run_id,))}
        horizon_ns = max((s['end_mono_ns'] or 0 for s in slots), default=0)
        _require(horizon_ns > 0 and anchors, 'no timed recording')
        anchor = max((a for a in anchors.values() if a['status'] == 'VALID'), key=lambda a: a['anchor_mono_ns'], default=None)
        _require(anchor is not None, 'no valid anchor')
        requests = conn.execute('SELECT * FROM request_attempts WHERE run_id=?', (run_id,)).fetchall()
        _validate_anchors(anchors, requests)
        _require(all(s['scheduled_us'] == market['window_start_us'] + s['slot_index'] * 2_000_000
                     for s in slots), 'slot grid mismatch')
        discovery_ids, revision_ids = _prove_scope(conn, market, slots, anchors)
        horizon_us = anchor['anchor_server_us'] + (horizon_ns - anchor['anchor_mono_ns']) // 1000
        revisions = {}
        # Keep strict paper loop semantics; parity test guards isolated duplication.
        for slot in slots:
            slot_start = slot['start_mono_ns']
            if slot_start is None:
                slot_start = _missing_slot_start(slot, anchors, slots)
            slot_opportunities = {outcome: dict(slot_id=slot['slot_id'], slot_index=slot['slot_index'], outcome=outcome, start_ns=slot_start,
                                               reason='MISSING_EXECUTION_BOOK' if slot_start is not None else 'UNSAFE_MISSING_SLOT_TIMING') for outcome in tokens}
            opportunities.extend(slot_opportunities.values())
            observations = conn.execute('SELECT * FROM book_observations WHERE slot_id=? ORDER BY outcome', (slot['slot_id'],)).fetchall()
            for outcome in tokens.keys() - {o['outcome'] for o in observations}:
                attempts = conn.execute("SELECT start_mono_ns,selector_json FROM request_attempts WHERE run_id=? AND market_id=? AND slot_id=? AND operation IN ('BOOK_BATCH','BOOK_SINGLE') ORDER BY start_mono_ns",
                                        (run_id, market_id, slot['slot_id'])).fetchall()
                for attempt in attempts:
                    try:
                        selectors = json.loads(attempt['selector_json'])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(selectors, list) and tokens[outcome] in selectors:
                        slot_opportunities[outcome]['start_ns'] = attempt['start_mono_ns']
                        break
            if not observations:
                excluded.append({'slot_id': slot['slot_id'], 'reason': 'NO_OBSERVATIONS'})
            elif slot['status'] == 'COMPLETE':
                for outcome in sorted(tokens.keys() - {o['outcome'] for o in observations}):
                    excluded.append({'slot_id': slot['slot_id'], 'outcome': outcome, 'reason': 'MISSING_OBSERVATION'})
            for obs in observations:
                opportunity = slot_opportunities[obs['outcome']]
                attempt_start = conn.execute('SELECT start_mono_ns FROM request_attempts WHERE request_id=? AND run_id=? AND market_id=? AND slot_id=?',
                                             (obs['request_id'], run_id, market_id, slot['slot_id'])).fetchone()
                if attempt_start is not None:
                    opportunity['start_ns'] = attempt_start[0]
                opportunity['reason'] = 'INVALID_EXECUTION_BOOK'
                try:
                    _require(slot['status'] == 'COMPLETE' and obs['status'] == 'VALID', 'slot/book not COMPLETE/VALID')
                    token = tokens[obs['outcome']]
                    _require(obs['expected_token_id'] == token and obs['expected_condition_id'] == condition, 'observation identity mismatch')
                    a = anchors[slot['anchor_id']]
                    _require(a['status'] == 'VALID', 'invalid clock anchor')
                    _require(a['request_start_mono_ns'] <= a['anchor_mono_ns'] <= a['request_end_mono_ns'] <= slot['start_mono_ns'],
                             'noncausal clock anchor')
                    revision_id = slot['metadata_revision_id']
                    if revision_id not in revisions:
                        rev = conn.execute('SELECT * FROM market_revisions WHERE revision_id=?', (revision_id,)).fetchone()
                        _require(rev is not None and rev['market_id'] == market_id and rev['run_id'] == run_id, 'revision identity mismatch')
                        _require(rev['active'] == 1 and rev['closed'] == 0 and rev['accepting_orders'] == 1 and
                                 rev['enable_order_book'] == 1, 'revision not open for books')
                        sizes = json.loads(rev['raw_sizes_json'])
                        gamma = load_numeric_json(decode_evidence(rev['gamma_raw_z'], sizes['gamma'], rev['codec_version']))
                        _require(gamma['id'] == market_id and gamma['conditionId'] == condition, 'Gamma identity mismatch')
                        outcomes = json.loads(gamma['outcomes']) if isinstance(gamma['outcomes'], str) else gamma['outcomes']
                        ids = json.loads(gamma['clobTokenIds']) if isinstance(gamma['clobTokenIds'], str) else gamma['clobTokenIds']
                        _require(dict(zip((o.upper() for o in outcomes), ids)) == tokens and len(ids) == len(outcomes) == 2, 'Gamma token mismatch')
                        clob = load_numeric_json(decode_evidence(rev['clob_config_raw_z'], sizes['clob'], rev['codec_version']))
                        _require(isinstance(clob, dict) and any(k in clob for k in ('condition_id', 'conditionId', 'c')), 'missing CLOB condition')
                        _require(all(clob[k] == condition for k in ('condition_id', 'conditionId', 'c') if k in clob), 'CLOB condition mismatch')
                        _require(isinstance(clob.get('t'), list) and len(clob['t']) == 2 and
                                 sorted(t['t'] for t in clob['t']) == sorted(tokens.values()), 'CLOB token mismatch')
                        for row in clob['t']:
                            if row.get('o') in ('Up', 'Down', 'UP', 'DOWN'):
                                _require(tokens[row['o'].upper()] == row['t'], 'CLOB outcome mismatch')
                        revisions[revision_id] = True
                    attempt = conn.execute('SELECT * FROM request_attempts WHERE request_id=?', (obs['request_id'],)).fetchone()
                    _require(attempt is not None and attempt['run_id'] == run_id and attempt['market_id'] == market_id and
                             attempt['slot_id'] == slot['slot_id'] and attempt['anchor_id'] == slot['anchor_id'], 'audit linkage mismatch')
                    _require(attempt['http_status'] == 200 and attempt['result'] == 'success' and attempt['raw_complete'] == 1, 'incomplete audit')
                    _require((attempt['method'], attempt['operation'], attempt['path']) in
                             (('POST', 'BOOK_BATCH', '/books'), ('GET', 'BOOK_SINGLE', '/book')), 'not book request')
                    _require(slot['start_mono_ns'] <= attempt['start_mono_ns'] <= attempt['end_mono_ns'] <= slot['end_mono_ns'], 'bad availability ordering')
                    selectors = json.loads(attempt['selector_json'])
                    _require(isinstance(selectors, list) and token in selectors and
                             len(selectors) == len(set(selectors)) and set(selectors) <= set(tokens.values()),
                             'book selector mismatch')
                    raw = decode_evidence(attempt['response_raw_z'], attempt['raw_size_bytes'], attempt['codec_version'])
                    _require(len(raw) == attempt['response_bytes'], 'raw length mismatch')
                    payload = load_numeric_json(raw)
                    if attempt['method'] == 'POST':
                        _require(isinstance(payload, list), 'batch not list')
                        matches = [b for b in payload if isinstance(b, dict) and b.get('asset_id') == token]
                        _require(len(matches) == 1, 'missing/duplicate token')
                        payload = matches[0]
                    book = parse_book(JsonRead(payload, 0, 0), token_id=token, condition_id=condition)
                    source_us = int(book.source_timestamp_raw) * 1000
                    _require(source_us == obs['source_us'] and book.source_timestamp_raw == obs['source_timestamp_raw'], 'source mismatch')
                    for name in ('best_bid', 'best_ask', 'bid_size', 'ask_size'):
                        text = obs[name + '_text']
                        _require((None if text is None else parse_decimal(text, field=name)) == getattr(book, name), 'summary mismatch')
                    receipt_us = a['anchor_server_us'] + (attempt['end_mono_ns'] - a['anchor_mono_ns']) // 1000
                    _require(attempt['ended_server_us'] == receipt_us, 'receipt clock mismatch')
                    _require(market['window_start_us'] <= receipt_us < market['window_end_us'], 'outside market window')
                    snapshots.append(Snapshot(slot['slot_id'], slot['slot_index'], obs['outcome'], obs['request_id'],
                                              attempt['start_mono_ns'], slot['end_mono_ns'], receipt_us, source_us, a['uncertainty_us'], sha256(raw).hexdigest(), book,
                                              attempt['end_mono_ns']))
                    opportunity['reason'] = None
                except (ValueError, TypeError, KeyError, MarketDataError, EvidenceError) as exc:
                    excluded.append({'slot_id': slot['slot_id'], 'outcome': obs['outcome'], 'reason': str(exc)})
        _require(bool(snapshots), 'no validated snapshots')
        ends = {s['slot_id']: s['end_mono_ns'] for s in slots}
        recording = Recording(run_id, market_id, condition, tokens, digest,
                              tuple(sorted(snapshots, key=lambda s: (s.start_ns, s.request_id, s.outcome))),
                              tuple(excluded), horizon_ns, horizon_us,
                              tuple(dict(o, available_ns=ends[o['slot_id']]) for o in sorted(opportunities, key=lambda o: (-1 if o['start_ns'] is None else o['start_ns'], o['slot_id'], o['outcome']))))
        pairs = _pair_paper_snapshots(recording, _market_descriptor(recording, market))
        evidence, settlement = _settlement(conn, market, revision_ids)
        external = None
        if settlement_file is not None:
            observation = conn.execute("SELECT response_hash FROM resolution_observations WHERE market_id=? AND classification='RESOLVED_DIRECTIONAL' ORDER BY learned_wall_us DESC,resolution_id DESC LIMIT 1",
                                       (MARKET_ID,)).fetchone()
            evidence, settlement, external = _external_settlement(
                settlement_file, settlement_sha256, market, digest, evidence,
                observation['response_hash'] if observation is not None else None)
        scope = dict(identity_proof='DISCOVERY_ENVELOPE_VERIFIED', run_id=run_id, market_id=market_id,
                     event_id=market['event_id'], source_sha256=digest,
                     original_config_json=run['config_json'], discovery_request_ids=discovery_ids,
                     metadata_revision_ids=revision_ids, slot_status_counts=dict(Counter(s['status'] for s in slots)),
                     recorded_slot_count=len(slots), expected_slot_count=450,
                     missing_slot_indices=sorted(s['slot_index'] for s in slots if s['status'] == 'MISSED'),
                     absent_slot_indices=sorted(set(range(450)) - {s['slot_index'] for s in slots}),
                     paired_captures=len(pairs), valid_books=len(snapshots), excluded_records=len(excluded),
                     one_sided_books=sum(not s.book.bids or not s.book.asks for s in snapshots),
                     two_sided_pairs=sum(bool(p.up.book.bids and p.up.book.asks and p.down.book.bids and p.down.book.asks) for p in pairs))
        if external is not None:
            scope['external_settlement'] = external
    _require(sha256(db.read_bytes()).hexdigest() == digest, 'source database changed during read')
    _require(not any(Path(str(db) + s).exists() for s in ('-wal', '-shm', '-journal')), 'SQLite sidecar appeared during read')
    return (pairs, recording.excluded, recording.opportunities, evidence, settlement, recording), scope
