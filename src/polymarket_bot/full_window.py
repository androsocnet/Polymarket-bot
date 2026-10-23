"""CP6.2 OFFLINE contracts. No transport, live runner or settlement path."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import sqlite3

from .paper import load_recording, _fresh
from .recorder.store import decode_evidence, RecorderStoreError

MIB = 1024 * 1024
FAULTS = {'http_failure', 'partial', 'invalid', 'late', 'clock_uncertain',
          'clock_recalibrated', 'clock_discontinuity', 'metadata_stale',
          'identity_conflict', 'closed', 'oversize', 'interrupt',
          'storage_failure', 'journal_failure', 'fallback', 'memory_exhaustion',
          'process_escape', 'log_exhaustion'}


def simulate_lifecycle(*, startup_us=0, faults=None, request_limit=3000,
                       ingress_limit=256*MIB, disk_limit=1024*MIB, cleanup_us=0):
    """Finite policy model, not proof of OS containment or real disk accounting.

    Times relative to opening; launch at -30s. Synthetic 4096-byte requests
    model admission. One reserved 64 MiB terminal budget never used by capture.
    """
    for name, value, low, high in (
        ('startup_us', startup_us, 0, 930_000_000),
        ('cleanup_us', cleanup_us, 0, 950_000_000),
        ('request_limit', request_limit, 1, 3000),
        ('ingress_limit', ingress_limit, 1, 256*MIB),
        ('disk_limit', disk_limit, 64*MIB+1, 1024*MIB)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('invalid '+name)
    faults = {} if faults is None else faults
    if type(faults) is not dict or any(type(i) is not int or not 0 <= i < 450 or
            type(f) is not str or f not in FAULTS for i, f in faults.items()):
        raise ValueError('invalid synthetic faults')
    requests = ingress = disk = 0
    valid, events = [], []
    reason, failed, audit, paused = 'WINDOW_ENDED', False, True, False
    stopped = 900_000_000

    def dispatch(count):
        nonlocal requests, ingress, disk
        for _ in range(count):
            if requests >= request_limit or ingress+4096 > ingress_limit or disk+4096 > disk_limit-64*MIB:
                return False
            requests += 1
            ingress += 4096
            disk += 4096
        return True

    # Startup and 60s refresh: one /time + selected event/detail/config, no discovery.
    if not dispatch(4):
        reason, failed, stopped = 'RESOURCE_BUDGET', True, 0
    else:
        next_control = 30_000_000
        for i in range(450):
            target = i * 2_000_000
            if target < startup_us:
                continue
            if target >= next_control:
                if not dispatch(4):
                    reason, failed, stopped = 'RESOURCE_BUDGET', True, target
                    break
                next_control = target + 60_000_000
            fault = faults.get(i)
            if fault:
                events.append(dict(index=i, fault=fault, target_us=target))
            if fault in {'identity_conflict', 'metadata_stale', 'clock_discontinuity',
                         'storage_failure', 'journal_failure', 'oversize', 'memory_exhaustion',
                         'process_escape', 'log_exhaustion'}:
                reason, failed, stopped = fault.upper(), True, target
                audit = fault not in {'storage_failure', 'journal_failure'}
                break
            if fault in {'interrupt', 'closed'}:
                reason, stopped = fault.upper(), target
                break
            if fault == 'clock_uncertain':
                paused = True
            elif fault == 'clock_recalibrated':
                paused = False
            if paused:
                continue
            if not dispatch(6 if fault == 'fallback' else 2 if fault == 'http_failure' else 1):
                reason, failed, stopped = 'RESOURCE_BUDGET', True, target
                break
            if fault not in {'http_failure', 'partial', 'invalid', 'late'}:
                valid.append(i)
    elapsed = stopped + 30_000_000 + cleanup_us
    forced = elapsed > 940_000_000
    if forced:
        failed, audit, reason = True, False, 'FORCED_TERMINATION'
    return dict(evidence_kind='SYNTHETIC_POLICY_SIMULATION',
        status='FAILED' if failed or not valid else 'COMPLETE' if len(valid)==450 else 'PARTIAL',
        valid_pairs=len(valid), coverage_percent=str(Decimal(len(valid))*100/450),
        missing_targets=[i for i in range(450) if i not in valid],
        last_target_us=valid[-1]*2_000_000 if valid else None,
        requests=requests, ingress_bytes=ingress, capture_disk_bytes=disk,
        events=events, reason=reason, stopped_us=stopped,
        elapsed_us=min(elapsed, 950_000_000), forced=forced,
        active_processes=0, terminal_evidence=True, audit_reconciled=audit,
        limitation='process and terminal reserve are modeled, not OS enforcement')


def _raw(request):
    return decode_evidence(request['response_raw_z'], request['raw_size_bytes'], request['codec_version'])


def _success(request):
    return (request['result']=='success' and request['raw_complete']==1 and
            request['http_status']==200)


def _validate_anchors(anchors, requests):
    """Schema has no /time foreign key: require unique exact raw/timing linkage."""
    successful = [r for r in requests if _success(r) and
                  (r['path']=='/time' or r['operation']=='CLOB_TIME')]
    previous = None
    for r in sorted(successful, key=lambda r:(r['start_mono_ns'],r['request_id'])):
        if (r['path']!='/time' or r['method']!='GET' or r['operation']!='CLOB_TIME' or
                r['slot_id'] is not None or r['selector_json']!='[]'):
            raise ValueError('CLOCK_REQUEST_MISMATCH: '+str(r['request_id']))
        seconds = json.loads(_raw(r))
        if type(seconds) is not int or not 1<=seconds<=253_402_300_799:
            raise ValueError('CLOCK_CALIBRATION_MISMATCH: '+str(r['request_id']))
        midpoint = (r['start_mono_ns']+r['end_mono_ns'])//2
        uncertainty = 1_000_000+(r['end_mono_ns']-r['start_mono_ns']+1999)//2000
        if previous is not None:
            end, mono, server, error = previous
            if r['start_mono_ns']<end:
                raise ValueError('CLOCK_EPOCH_ORDER')
            if abs(seconds*1_000_000-(server+(midpoint-mono)//1000))>error+uncertainty+1_000_000:
                raise ValueError('CLOCK_DISCONTINUITY_STOP_REQUIRED')
        previous = (r['end_mono_ns'],midpoint,seconds*1_000_000,uncertainty)
    consumed = set()
    previous = None
    for a in sorted(anchors.values(), key=lambda a:a['request_start_mono_ns']):
        start, end = a['request_start_mono_ns'], a['request_end_mono_ns']
        matches = [r for r in requests if r['path']=='/time' and
                   r['start_mono_ns']==start and r['end_mono_ns']==end]
        if len(matches)!=1:
            raise ValueError('CLOCK_RAW_LINKAGE: '+str(a['anchor_id']))
        r = matches[0]
        if r['request_id'] in consumed:
            raise ValueError('CLOCK_DUPLICATE_LINKAGE: '+str(r['request_id']))
        consumed.add(r['request_id'])
        if (not _success(r) or r['method']!='GET' or r['operation']!='CLOB_TIME' or
                r['slot_id'] is not None or r['selector_json']!='[]' or
                r['started_wall_us']!=a['request_start_wall_us'] or
                r['ended_wall_us']!=a['request_end_wall_us']):
            raise ValueError('CLOCK_REQUEST_MISMATCH: '+str(a['anchor_id']))
        seconds = json.loads(_raw(r))
        if (type(seconds) is not int or not 1<=seconds<=253_402_300_799 or
                seconds!=a['server_seconds'] or a['anchor_server_us']!=seconds*1_000_000 or
                a['anchor_mono_ns']!=(start+end)//2 or
                a['uncertainty_us']!=1_000_000+(end-start+1999)//2000):
            raise ValueError('CLOCK_CALIBRATION_MISMATCH: '+str(a['anchor_id']))
        discontinuity = False
        if previous is not None:
            if start<previous['request_end_mono_ns']:
                raise ValueError('CLOCK_EPOCH_ORDER')
            projected = previous['anchor_server_us']+(a['anchor_mono_ns']-previous['anchor_mono_ns'])//1000
            discontinuity = abs(a['anchor_server_us']-projected)>(
                previous['uncertainty_us']+a['uncertainty_us']+1_000_000)
        if a['status']!=('DISCONTINUITY' if discontinuity else 'VALID'):
            raise ValueError('CLOCK_STATUS_CONTRADICTION')
        if discontinuity:
            raise ValueError('CLOCK_DISCONTINUITY_STOP_REQUIRED')
        previous = a
    if consumed!={r['request_id'] for r in successful}:
        raise ValueError('CLOCK_ORPHAN_SUCCESS: missing calibration linkage')


def _timestamp_us(text):
    """Raw metadata supports ISO 8601 strings with explicit timezone only."""
    if type(text) is not str:
        raise ValueError('METADATA_TIME_TYPE: expected timezone-aware timestamp string')
    dt = datetime.fromisoformat(text.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('METADATA_TIME_WITHOUT_ZONE')
    delta = dt-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds


def _metadata_history(requests, market):
    """Consume event/detail/config once, in order; failure/orphan latches stop.

    Pending group supplies no freshness. If capture occurs before completion,
    assessor latches stop at that capture, not using future completion. Before
    any response no group has started. Superseded/overlapping groups stop here.
    """
    paths = ('/events/'+market['event_id'], '/markets/'+market['market_id'],
             '/clob-markets/'+market['condition_id'])
    latest, history = {}, []
    stopped = False
    identity = None
    for r in sorted((r for r in requests if r['path'] in paths),
                    key=lambda r:(r['end_mono_ns'],r['request_id'])):
        raw = _raw(r) if r['response_raw_z'] is not None else None
        payload = json.loads(raw) if _success(r) else None
        kind = paths.index(r['path'])
        expected_operation = 'CLOB_CONFIG' if kind==2 else 'GAMMA_MARKET'
        expected_selector = (['condition='+market['condition_id']] if kind==2 else
                             ['market_id='+market['market_id']] if kind==1 else [])
        good = (_success(r) and r['method']=='GET' and r['operation']==expected_operation and
                json.loads(r['selector_json'])==expected_selector and
                r['slot_id'] is None and isinstance(payload,dict))
        if good and kind in (0,1):
            if kind==0:
                for key in ('eventStartTime','endDate'):
                    if key in payload:
                        _timestamp_us(payload[key])
            row = payload
            if kind==0:
                members = payload.get('markets')
                members = [m for m in members if isinstance(m,dict) and m.get('id')==market['market_id']] if isinstance(members,list) else []
                good = (payload.get('id')==market['event_id'] and payload.get('active') is True and
                        payload.get('closed') is False and payload.get('archived') is not True and len(members)==1)
                row = members[0] if len(members)==1 else {}
            keys = ('id','conditionId','eventStartTime','endDate','clobTokenIds','outcomes')
            signature = tuple(row.get(k) for k in keys)
            tokens, outcomes = json.loads(row.get('clobTokenIds','null')), json.loads(row.get('outcomes','null'))
            good = (good and _timestamp_us(row['eventStartTime'])==market['window_start_us'] and
                    _timestamp_us(row['endDate'])==market['window_end_us'] and
                    row.get('id')==market['market_id'] and row.get('conditionId')==market['condition_id'] and
                    row.get('active') is True and row.get('closed') is False and row.get('archived') is not True and
                    row.get('acceptingOrders') is True and row.get('enableOrderBook') is True and
                    isinstance(tokens,list) and isinstance(outcomes,list) and len(tokens)==len(outcomes)==2 and
                    tokens[market['up_outcome_index']]==market['up_token_id'] and
                    tokens[market['down_outcome_index']]==market['down_token_id'] and
                    isinstance(outcomes[market['up_outcome_index']],str) and
                    outcomes[market['up_outcome_index']].strip().lower()=='up' and
                    isinstance(outcomes[market['down_outcome_index']],str) and
                    outcomes[market['down_outcome_index']].strip().lower()=='down')
            if identity is None and good:
                identity = signature
            good = good and signature==identity
        elif good:
            conditions = [payload[k] for k in ('condition_id','conditionId','c') if k in payload]
            rows = payload.get('t')
            good = (bool(conditions) and all(c==market['condition_id'] for c in conditions) and
                    isinstance(rows,list) and len(rows)==2 and all(isinstance(t,dict) for t in rows) and
                    sorted(t.get('t','') for t in rows)==sorted((market['up_token_id'],market['down_token_id'])))
            if good:
                for t in rows:
                    if 'o' in t:
                        label = t['o'].strip().lower() if isinstance(t['o'],str) else ''
                        good = good and label in ('up','down') and t['t']==market[label+'_token_id']
                for key,want in (('active',True),('closed',False),('archived',False),
                                 ('accepting_orders',True),('acceptingOrders',True),
                                 ('enable_order_book',True),('enableOrderBook',True)):
                    if key in payload:
                        good = good and payload[key] is want
        if not good:
            stopped = True
        if kind==0:
            if latest and (len(latest)!=3 or latest[2][0]['end_mono_ns']>r['start_mono_ns']):
                stopped = True
            latest = {}
        if kind!=len(latest) or (latest and latest[kind-1][0]['end_mono_ns']>r['start_mono_ns']):
            stopped = True
        latest[kind] = (r,raw)
        coherent = not stopped and len(latest)==3
        history.append((r['end_mono_ns'], coherent, dict(latest)))
    return paths, history


def assess_recording(db, run_id, market_id, *, dispatches, cleanup_proven=False):
    """Read-only offline gate, not runtime certification.

    Legacy cleanup assertion is recorded, never trusted. No supervisor verifier
    exists: otherwise full captures remain PROVISIONAL, never COMPLETE.
    """
    if (type(run_id) is not str or not run_id or type(market_id) is not str or
            not market_id or type(cleanup_proven) is not bool):
        raise ValueError('invalid evidence identity/cleanup')
    if not isinstance(dispatches, (list, tuple)) or any(
            not isinstance(d, (list, tuple)) or len(d)!=3 or
            type(d[0]) is not str or type(d[1]) is not str or
            type(d[2]) is not int or d[2]<0 for d in dispatches):
        raise ValueError('invalid independent dispatch evidence')
    db = Path(db).resolve(strict=True)
    digest = sha256(db.read_bytes()).hexdigest()
    valid, missing, problems = set(), list(range(450)), []
    audit = False
    terminal = window_end_observed = controls_verified = False
    ingress = None
    known_ingress_lower_bound = 0
    evidence_kind = 'UNVERIFIED_ORIGIN'
    unverified = ['RUNTIME_PROVENANCE_UNAVAILABLE', 'ALL_ARTIFACT_PEAK_UNKNOWN',
                  'TERMINAL_RESERVE_UNVERIFIED', 'SUPERVISOR_OUTCOME_UNVERIFIED',
                  'CONTAINMENT_UNVERIFIED', 'CLEANUP_UNVERIFIED',
                  'MEMORY_CPU_PROCESS_LOG_PEAKS_UNKNOWN', 'DISPATCH_PROVENANCE_UNVERIFIED']
    try:
        wal = Path(str(db)+'-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('active WAL not supported')
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            if (conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or
                    conn.execute('PRAGMA foreign_key_check').fetchall()):
                raise ValueError('database integrity failed')
            run = conn.execute('SELECT * FROM recording_runs WHERE run_id=?', (run_id,)).fetchone()
            market = conn.execute('SELECT * FROM markets WHERE market_id=?', (market_id,)).fetchone()
            if run is None or market is None:
                raise ValueError('unknown run/market')
            config = json.loads(run['config_json'])
            if (config.get('selected_market_id')!=market_id or config.get('selected_event_id')!=market['event_id'] or
                    run['mode']!='BOOK_ONLY' or market['window_end_us']-market['window_start_us']!=900_000_000 or
                    conn.execute('SELECT count(*) FROM markets').fetchone()[0]!=1):
                raise ValueError('immutable selected identity mismatch')
            terminal = run['state'] in ('STOPPED', 'INTERRUPTED', 'FAILED') and run['ended_wall_us'] is not None
            window_end_observed = (run['state']=='STOPPED' and run['exit_reason']=='SELECTED_MARKET_EXPIRED' and
                                   market['lifecycle']=='EXPIRED')
            requests = conn.execute('SELECT * FROM request_attempts WHERE run_id=?', (run_id,)).fetchall()
            actual = Counter(dispatches)
            recorded = Counter((r['method'],r['path'],r['start_mono_ns']) for r in requests)
            audit = actual==recorded
            for r in requests:
                if not 0<=r['start_mono_ns']<=r['end_mono_ns']<=2**63-1 or r['latency_ns']!=r['end_mono_ns']-r['start_mono_ns']:
                    raise ValueError('REQUEST_TIMING_INVALID: '+str(r['request_id']))
                if r['response_raw_z'] is not None:
                    raw = _raw(r)
                    if r['response_bytes'] is not None and r['response_bytes']<len(raw):
                        raise ValueError('INGRESS_COUNTER_CONTRADICTION')
                    if r['operation'] in ('BOOK_BATCH','BOOK_SINGLE') and b'SYNTHETIC_CP6_2_NOT_LIVE' in raw:
                        evidence_kind = 'SYNTHETIC'
            if any(r['response_bytes'] is not None and
                   (type(r['response_bytes']) is not int or r['response_bytes']<0) for r in requests):
                raise ValueError('INGRESS_COUNTER_INVALID')
            known_ingress_lower_bound = sum(r['response_bytes'] for r in requests if r['response_bytes'] is not None)
            if any(r['response_bytes'] is None for r in requests):
                unverified.append('INGRESS_UNKNOWN')
            else:
                ingress = known_ingress_lower_bound
            if not conn.execute('SELECT 1 FROM storage_checkpoints WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
                unverified.append('STORAGE_CHECKPOINTS_MISSING')
            books = sum(r['operation'] in ('BOOK_BATCH','BOOK_SINGLE') for r in requests)
            if (len(requests)>3000 or books>2700 or len(requests)-books>300 or
                    known_ingress_lower_bound>256*MIB or
                    sum(p.stat().st_size for p in db.parent.glob(db.name+'*') if p.is_file())>960*MIB):
                problems.append('RESOURCE_BUDGET_EXCEEDED')
            slots = {s['slot_id']:s for s in conn.execute(
                'SELECT * FROM sample_slots WHERE run_id=? AND market_id=?', (run_id,market_id))}
            if any(s['scheduled_us']!=market['window_start_us']+s['slot_index']*2_000_000 for s in slots.values()):
                raise ValueError('invalid capture grid')
            if not terminal or any(s['status']=='IN_PROGRESS' for s in slots.values()):
                raise ValueError('missing terminal recording evidence')
            recording = load_recording(db, run_id, market_id)
            grouped = {}
            revisions = {r['revision_id']:r for r in conn.execute('SELECT * FROM market_revisions WHERE run_id=?', (run_id,))}
            anchors = {a['anchor_id']:a for a in conn.execute('SELECT * FROM clock_anchors WHERE run_id=?', (run_id,))}
            _validate_anchors(anchors, requests)
            paths, history = _metadata_history(requests, market)
            by_id = {r['request_id']:r for r in requests}
            controls_verified = bool(history) and history[-1][1]
            metadata_stop_ns = None
            for row in sorted(slots.values(), key=lambda row:row['start_mono_ns'] or 0):
                if row['start_mono_ns'] is None:
                    continue
                applicable = [h for h in history if h[0]<=row['start_mono_ns']]
                if applicable and not applicable[-1][1]:
                    metadata_stop_ns = row['start_mono_ns']
                    break
            for s in recording.snapshots:
                row = slots[s.slot_id]
                rev, anchor = revisions[row['metadata_revision_id']], anchors[row['anchor_id']]
                sizes = json.loads(rev['raw_sizes_json'])
                gamma = decode_evidence(rev['gamma_raw_z'],sizes['gamma'],rev['codec_version'])
                clob = decode_evidence(rev['clob_config_raw_z'],sizes['clob'],rev['codec_version'])
                applicable = [h for h in history if h[0]<=row['start_mono_ns']]
                metadata_fresh = False
                if applicable:
                    _, coherent, latest = applicable[-1]
                    metadata_fresh = ((metadata_stop_ns is None or row['start_mono_ns']<metadata_stop_ns) and coherent and all(0<=row['start_mono_ns']-r['end_mono_ns']<=65_000_000_000
                                      for r,raw in latest.values()) and
                                      latest[1][1]==gamma and latest[2][1]==clob)
                available = [a for a in anchors.values() if a['request_end_mono_ns']<=row['start_mono_ns']]
                causal_anchor = bool(available) and max(available,key=lambda a:a['request_end_mono_ns'])['anchor_id']==anchor['anchor_id']
                project = lambda ns: anchor['anchor_server_us']+(ns-anchor['anchor_mono_ns'])//1000
                target_ns = anchor['anchor_mono_ns']+(row['scheduled_us']-anchor['anchor_server_us'])*1000
                deadline_ns = anchor['anchor_mono_ns']+(min(row['scheduled_us']+2_000_000,market['window_end_us'])-anchor['anchor_server_us'])*1000
                attempt = by_id[s.request_id]
                slot_requests = [r for r in requests if r['slot_id']==s.slot_id]
                contained = all(r['market_id']==market_id and r['anchor_id']==anchor['anchor_id'] and
                                row['start_mono_ns']<=r['start_mono_ns']<=r['end_mono_ns']<=row['end_mono_ns'] and
                                r['started_server_us']==project(r['start_mono_ns']) and
                                r['ended_server_us']==project(r['end_mono_ns']) for r in slot_requests)
                timing = (contained and target_ns<=row['start_mono_ns']<=attempt['start_mono_ns']<=attempt['end_mono_ns']<=row['end_mono_ns']<deadline_ns and
                          s.available_ns==row['end_mono_ns'] and
                          row['started_server_us']==project(row['start_mono_ns']) and
                          row['finished_server_us']==project(row['end_mono_ns']) and
                          attempt['started_server_us']==project(attempt['start_mono_ns']) and
                          attempt['ended_server_us']==project(attempt['end_mono_ns']))
                clock_valid = (causal_anchor and anchor['request_end_mono_ns']<=s.start_ns and
                               0<=row['end_mono_ns']-anchor['anchor_mono_ns']<=120_000_000_000 and
                               anchor['uncertainty_us']<=1_250_000)
                controls_verified = controls_verified and metadata_fresh and clock_valid
                fresh = (_fresh(s, now_us=s.receipt_us, max_age_us=5_000_000) and
                    s.book.best_bid is not None and s.book.best_ask is not None and
                    clock_valid and metadata_fresh and timing)
                if fresh:
                    grouped.setdefault(s.slot_index,set()).add(s.outcome)
            valid = {i for i,outcomes in grouped.items() if outcomes=={'UP','DOWN'}}
            missing = [i for i in range(450) if i not in valid]
            if len(valid)==450 and history and not history[-1][1]:
                problems.append('METADATA_GROUP_INCOMPLETE_OR_INVALID_AT_TERMINATION')
            if run['state']=='FAILED':
                problems.append('RECORDER_FAILED')
    except (ValueError, KeyError, TypeError, IndexError, RecorderStoreError, sqlite3.Error, OSError) as exc:
        controls_verified = False
        problems.append(type(exc).__name__+': '+str(exc))
    if not audit:
        problems.append('AUDIT_UNRECONCILED')
    if sha256(db.read_bytes()).hexdigest()!=digest:
        problems.append('SOURCE_CHANGED')
    eligible = not problems and bool(valid)
    capture_status = 'FAILED' if problems or not valid else 'COMPLETE' if len(valid)==450 and window_end_observed else 'PARTIAL'
    return dict(schema='cp6-full-window-readiness-v2', db_sha256=digest,
        status='PROVISIONAL' if capture_status=='COMPLETE' else capture_status,
        capture_status=capture_status, evidence_kind=evidence_kind,
        control_evidence_verified=controls_verified and audit,
        runtime_evidence_verified=False, ingress_bytes=ingress,
        known_ingress_lower_bound=known_ingress_lower_bound, unverified=unverified,
        valid_pairs=len(valid), expected_targets=450, missing_targets=missing,
        coverage_percent=format(Decimal(len(valid))*100/450, 'f'),
        opening_target_valid=0 in valid, closing_target_valid=449 in valid,
        terminal=terminal, window_end_observed=window_end_observed,
        audit_reconciled=audit, cleanup_asserted=cleanup_proven, cleanup_proven=False,
        replay_eligible=eligible, replay_scope='OFFLINE_ONLY; runtime safety unverified',
        problems=problems, settlement=None,
        scope='BOOK_ONLY; closing target is t+898s, not closing reference or settlement')
