#!/usr/bin/env python3
"""CP5 database analysis script."""
from contextlib import closing
import sqlite3
import json
import sys
from pathlib import Path
from typing import Any


def replay_rows(observations, references, events, anchors):
    from collections import deque
    from dataclasses import replace
    from decimal import Decimal
    from polymarket_bot.recorder.free_reference import ApproximateObservation, compute_approximate_sample_mean_60s
    from polymarket_bot.market_data.numeric import decimal_text
    observation_map = {row['observation_id']: row for row in observations}
    reference_map = {row['reference_id']: row for row in references}
    anchor_map = {row['anchor_id']: row for row in anchors}
    seen_observations, seen_references = set(), set()
    histories = {}
    matched = 0
    chronology = {}
    if len(observation_map) != len(observations) or len(reference_map) != len(references) or len(anchor_map) != len(anchors):
        raise ValueError('incomplete duplicate persisted identities')
    for event in events:
        detail = json.loads(event['detail_json'])
        if not isinstance(detail, dict):
            raise ValueError('incomplete malformed event object')
        if detail.get('type') != 'APPROXIMATE_FETCH':
            continue
        if detail.get('version') != 1 or detail.get('diagnostic_only') is not True:
            raise ValueError('incomplete fetch version')
        run = event['run_id']
        if (type(detail.get('anchor_id')) is not int or type(detail.get('reference_id')) is not int
                or not isinstance(detail.get('observation_ids'), list)
                or any(type(identifier) is not int for identifier in detail['observation_ids'])):
            raise ValueError('incomplete malformed fetch identities')
        anchor = anchor_map.get(detail.get('anchor_id'))
        ref = reference_map.get(detail.get('reference_id'))
        ids = detail.get('observation_ids')
        if (anchor is None or ref is None or anchor['run_id'] != run or ref['run_id'] != run
                or not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2
                or ref['reference_id'] in seen_references):
            raise ValueError('incomplete fetch membership')
        history = histories.setdefault(run, {name: deque(maxlen=64) for name in ('coinbase', 'kraken')})
        batch = []
        for identifier, provider in zip(ids, ('coinbase', 'kraken')):
            row = observation_map.get(identifier)
            if row is None or row['run_id'] != run or row['provider'] != provider or identifier in seen_observations:
                raise ValueError('incomplete observation membership')
            fields = {name: row[name] for name in ApproximateObservation.__dataclass_fields__
                      if name not in ('price', 'bid_price', 'ask_price')}
            for name in ('price', 'bid_price', 'ask_price'):
                fields[name] = Decimal(row[name + '_text']) if row[name + '_text'] is not None else None
            obs = ApproximateObservation(**fields)
            if obs.receipt_mono_ns is None or obs.receipt_server_us != anchor['anchor_server_us'] + (obs.receipt_mono_ns - anchor['anchor_mono_ns']) // 1000:
                raise ValueError('incomplete observation clock provenance')
            batch.append(obs)
            history[provider].append(obs)
            seen_observations.add(identifier)
        if ref['receipt_mono_ns'] is None or ref['receipt_server_us'] != anchor['anchor_server_us'] + (ref['receipt_mono_ns'] - anchor['anchor_mono_ns']) // 1000:
            raise ValueError('incomplete reference clock provenance')
        previous = chronology.get(run)
        current = (ref['reference_id'], ref['receipt_mono_ns'], ids[0], ids[1])
        if (ids[0] >= ids[1] or batch[0].receipt_mono_ns > batch[1].receipt_mono_ns
                or batch[1].receipt_mono_ns > ref['receipt_mono_ns'] or
                (previous is not None and (current[0] <= previous[0] or current[1] < previous[1]
                    or current[2] <= previous[3] or batch[0].receipt_mono_ns < previous[1]))):
            raise ValueError('incomplete fetch chronology')
        chronology[run] = current
        reports = {}
        for provider, queue in history.items():
            reports[provider] = compute_approximate_sample_mean_60s(list(queue),
                receipt_wall_us=ref['receipt_wall_us'], receipt_server_us=ref['receipt_server_us'],
                receipt_mono_ns=ref['receipt_mono_ns'])
            while queue and queue[0].receipt_wall_us < ref['receipt_wall_us'] - 60_000_000:
                queue.popleft()
        primary, fallback = reports['coinbase'], reports['kraken']
        if primary.value is not None:
            report = replace(primary, source_provider='coinbase')
        elif fallback.value is not None:
            report = replace(fallback, source_provider='kraken', was_fallback=True,
                diagnostics=fallback.diagnostics + ('FALLBACK_COINBASE_' + (batch[0].error_code or 'STALE_OBSERVATION'),))
        else:
            report = replace(primary, was_fallback=True, diagnostics=tuple(dict.fromkeys(
                primary.diagnostics + fallback.diagnostics + tuple('FALLBACK_' + obs.provider.upper() + '_' +
                (obs.error_code or 'STALE_OBSERVATION') for obs in batch))))
        for name in report.__dataclass_fields__:
            expected = json.loads(ref['diagnostics_json']) if name == 'diagnostics' else ref['value_text'] if name == 'value' else ref[name]
            actual = list(report.diagnostics) if name == 'diagnostics' else decimal_text(report.value) if name == 'value' and report.value is not None else getattr(report, name)
            if actual != expected:
                raise ValueError('replay mismatch: ' + name)
        seen_references.add(ref['reference_id'])
        matched += 1
    if not matched or seen_observations != set(observation_map) or seen_references != set(reference_map):
        raise ValueError('incomplete persisted fetch evidence')
    return {'matched': matched, 'observations': len(seen_observations), 'exact': True}


def analyze_db(db_path: Path):
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        return quality(conn)


def window_coverage(market, slots, books, runs):
    grid = [row for row in slots if row['market_id'] == market['market_id']]
    candidates = []
    for run in runs:
        selected = [row for row in grid if row['run_id'] == run['run_id']]
        paired = set()
        tokens = {}
        for row in selected:
            pair = [book for book in books if book['slot_id'] == row['slot_id'] and book['status'] == 'VALID']
            if len(pair) != 2 or {book['outcome'] for book in pair} != {'UP', 'DOWN'}:
                continue
            identity = {book['outcome']: book['expected_token_id'] for book in pair}
            if len(set(identity.values())) != 2 or (tokens and identity != tokens):
                continue
            tokens = identity
            if row['scheduled_us'] != market['window_start_us'] + row['slot_index'] * 2_000_000:
                continue
            paired.add(row['slot_index'])
        missing = sorted(set(range(450)) - paired)
        bounded = (market['window_end_us'] - market['window_start_us'] == 900_000_000
            and run['started_wall_us'] <= market['window_start_us']
            and run['ended_wall_us'] is not None and run['ended_wall_us'] >= market['window_end_us'])
        candidates.append(dict(run_id=run['run_id'], paired_valid_slots=len(paired), missing_slot_indices=missing,
            opening_present=0 in paired, closing_present=449 in paired,
            complete_window=not missing and bounded, run_spans_boundaries=bounded, tokens=tokens))
    best = max(candidates, key=lambda row: (row['complete_window'], row['paired_valid_slots']), default=dict(
        run_id=None, paired_valid_slots=0, missing_slot_indices=list(range(450)), opening_present=False,
        closing_present=False, complete_window=False, run_spans_boundaries=False, tokens={}))
    return dict(best, market_id=market['market_id'], window_start_us=market['window_start_us'],
        window_end_us=market['window_end_us'], expected_slots=450, verdict='PASS' if best['complete_window'] else 'FAIL')


def slot_diagnosis(market, slots, attempts, events, anchors, books=()):
    from collections import Counter
    participating = sorted({row['run_id'] for row in slots if row['market_id'] == market['market_id']})
    if len(participating) > 1:
        return dict(runs={run: slot_diagnosis(market,
            [row for row in slots if row['run_id'] == run], attempts, events, anchors, books)
            for run in participating}, cause_policy='Per-run grids; no cross-run slot overwrite')
    selected = {row['slot_index']: row for row in slots if row['market_id'] == market['market_id']}
    run_ids = {row['run_id'] for row in selected.values()}
    controls = [row for row in attempts if row['run_id'] in run_ids and row.get('market_id') is None and row.get('slot_id') is None]
    attempts = [row for row in attempts if row['run_id'] in run_ids and row.get('market_id') == market['market_id']]
    anchors = [row for row in anchors if row['run_id'] in run_ids]
    timeline = []
    for event in events:
        detail = json.loads(event['detail_json'])
        if (detail.get('type') == 'SCHEDULER_TIMING' and event['run_id'] in run_ids
                and detail.get('market_id') == market['market_id']):
            timeline.append(dict(run_id=event['run_id'], event_id=event['event_id'], **detail))
    result = []
    gaps = []
    gap = 0
    for index in range(450):
        slot = selected.get(index)
        requests = [row for row in attempts if slot is not None and row.get('slot_id') == slot['slot_id']]
        status = slot['status'] if slot else 'ABSENT'
        outcomes = [row for row in books if slot is not None and row['slot_id'] == slot['slot_id']]
        errors = {str(row.get('error_code') or '').lower() for row in requests + outcomes}
        if status == 'COMPLETE':
            category = 'COMPLETE'
        elif status == 'PARTIAL':
            category = 'PARTIAL'
        elif not requests:
            category = 'ABSENT' if slot is None else 'UNSENT'
        elif any(row.get('status') == 'LATE' for row in outcomes) or any('late' in error or 'deadline' in error for error in errors):
            category = 'ATTEMPTED_LATE'
        elif any('timeout' in error for error in errors):
            category = 'ATTEMPTED_TIMEOUT'
        elif any('http' in error for error in errors) or any((row.get('http_status') or 0) >= 400 for row in requests):
            category = 'ATTEMPTED_HTTP'
        elif any('invalid' in error or 'malformed' in error for error in errors):
            category = 'ATTEMPTED_MALFORMED'
        else:
            category = 'ATTEMPTED_UNKNOWN'
        result.append(dict(slot_index=index, scheduled_us=market['window_start_us'] + index * 2_000_000,
            category=category, cause='UNKNOWN', slot=slot, requests=requests,
            anchor=next((row for row in anchors if slot and row['anchor_id'] == slot.get('anchor_id')), None)))
        if status != 'COMPLETE':
            gap += 1
        elif gap:
            gaps.append(gap)
            gap = 0
    if gap:
        gaps.append(gap)
    unassigned_timing = [dict(run_id=event['run_id'], event_id=event['event_id'], **json.loads(event['detail_json']))
        for event in events if event['run_id'] in run_ids
        and json.loads(event['detail_json']).get('type') == 'SCHEDULER_TIMING'
        and json.loads(event['detail_json']).get('market_id') is None]
    return dict(rows=result, categories=dict(Counter(row['category'] for row in result)),
        unassigned_timing=unassigned_timing,
        contiguous_uncollected_slots=gaps, timing_events=timeline,
        request_timeline=attempts, clock_timeline=anchors, unassigned_controls=controls,
        cause_policy='UNKNOWN unless directly established; reason labels are not root causes')


def quality(conn):
    from collections import Counter
    from decimal import Decimal
    from math import ceil
    from statistics import mean, median
    conn.row_factory = sqlite3.Row
    def rows(table, order):
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY {order}')]
    def distribution(values):
        values = sorted(values)
        return dict(count=len(values), min=min(values), median=median(values), mean=mean(values),
            p95=values[ceil(len(values) * .95) - 1], max=max(values)) if values else {'count': 0}
    observations = rows('approximate_observations', 'observation_id')
    references = rows('approximate_references', 'reference_id')
    events = rows('recorder_events', 'event_id')
    anchors = rows('clock_anchors', 'anchor_id')
    runs = rows('recording_runs', 'started_wall_us')
    integrity = [row[0] for row in conn.execute('PRAGMA integrity_check')]
    foreign_keys = [tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    isolation = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table in ('reference_reports', 'reference_fetches')}
    isolation['linked_associations'] = conn.execute('SELECT COUNT(*) FROM reference_associations WHERE report_id IS NOT NULL OR fetch_id IS NOT NULL OR available_live <> 0').fetchone()[0]
    try:
        replay = replay_rows(observations, references, events, anchors)
    except (ValueError, KeyError, TypeError) as exc:
        replay = {'exact': False, 'error': str(exc)}
    try:
        valid_events = all(isinstance(json.loads(event['detail_json']), dict) and
            (json.loads(event['detail_json']).get('type') != 'APPROXIMATE_FETCH' or
             isinstance(json.loads(event['detail_json']).get('observation_ids'), list) and
             all(type(identifier) is int for identifier in json.loads(event['detail_json'])['observation_ids']))
            for event in events)
    except (ValueError, TypeError, KeyError):
        valid_events = False
    if not valid_events:
        return dict(integrity=integrity, foreign_keys=foreign_keys, user_version=version,
            replay=replay, smoke_gate=False, full_window_verdict='FAIL', error='malformed persisted event')
    providers = {}
    for provider in ('coinbase', 'kraken'):
        selected = [row for row in observations if row['provider'] == provider]
        accepted = [row for row in selected if row['result'] == 'success' and row['error_code'] is None]
        gaps, wall_gaps = [], []
        for run in runs:
            ordered = [row for row in accepted if row['run_id'] == run['run_id']]
            gaps.extend((b['receipt_mono_ns'] - a['receipt_mono_ns']) / 1e9 for a, b in zip(ordered, ordered[1:]))
            wall_gaps.extend((b['receipt_wall_us'] - a['receipt_wall_us']) / 1e6 for a, b in zip(ordered, ordered[1:]))
        providers[provider] = dict(total=len(selected), accepted=len(accepted),
            availability=len(accepted) / len(selected) if selected else None,
            errors=dict(Counter(row['error_code'] for row in selected if row['error_code'])),
            monotonic_gaps_seconds=distribution(gaps), wall_gaps_seconds=distribution(wall_gaps),
            wall_gap_counts={str(bound): sum(gap > bound for gap in wall_gaps) for bound in (4, 6, 10)},
            gap_counts={str(bound): sum(gap > bound for gap in gaps) for bound in (4, 6, 10)})
    usable = [row for row in references if row['value_text'] is not None and not
        {'OBSERVATION_GAP', 'MISSING_OBSERVATIONS'} & set(json.loads(row['diagnostics_json']))]
    fallback = [row for row in references if row['was_fallback']]
    durations = []
    for run in runs:
        ordered = [row for row in references if row['run_id'] == run['run_id']]
        for index, row in enumerate(ordered):
            if row['was_fallback']:
                end = ordered[index + 1]['receipt_wall_us'] if index + 1 < len(ordered) else run['ended_wall_us']
                if end is not None:
                    durations.append(max(0, end - row['receipt_wall_us']) / 1e6)
    observation_map = {row['observation_id']: row for row in observations}
    usd, bps, receipt_offsets = [], [], []
    for event in events:
        detail = json.loads(event['detail_json'])
        if detail.get('type') != 'APPROXIMATE_FETCH':
            continue
        pair = [observation_map.get(identifier) for identifier in detail.get('observation_ids', [])]
        if len(pair) != 2 or any(row is None or row['result'] != 'success' for row in pair):
            continue
        first_row, second_row = pair
        assert first_row is not None and second_row is not None
        offset = abs(first_row['receipt_mono_ns'] - second_row['receipt_mono_ns']) / 1e9
        receipt_offsets.append(offset)
        if offset <= 2:
            first, second = (Decimal(row['price_text']) for row in (first_row, second_row))
            difference = abs(first - second)
            usd.append(float(difference))
            bps.append(float(difference / first * 10000))
    slots = rows('sample_slots', 'slot_id')
    result: dict[str, Any] = dict(integrity=integrity, foreign_keys=foreign_keys, user_version=version,
        isolation=isolation, runs=runs, replay=replay, providers=providers,
        references=dict(total=len(references), usable=len(usable), fallback=len(fallback),
            usable_fraction=len(usable) / len(references) if references else None,
            fallback_seconds=sum(durations), fallback_interval_seconds=distribution(durations),
            diagnostics=dict(Counter(reason for row in references for reason in json.loads(row['diagnostics_json'])))),
        disagreement=dict(matching='same fetch, receipt offset <=2 seconds; not simultaneous trades',
            usd=distribution(usd), bps=distribution(bps), receipt_offset_seconds=distribution(receipt_offsets)),
        slots=dict(Counter(row['status'] for row in slots)),
        clock_uncertainty_us=distribution([row['uncertainty_us'] for row in anchors]))
    post_warmup = [row for row in references if any(run['run_id'] == row['run_id'] and
        row['receipt_wall_us'] >= run['started_wall_us'] + 60_000_000 for run in runs)]
    result['post_warmup_references'] = len(post_warmup)
    result['post_warmup_diagnosticclean'] = sum(row in usable for row in post_warmup)
    result['references']['diagnosticclean'] = result['references'].pop('usable')
    result['references']['diagnosticclean_fraction'] = result['references'].pop('usable_fraction')
    span = sum(max(0, run['ended_wall_us'] - run['started_wall_us']) / 1e6 for run in runs if run['ended_wall_us'] is not None)
    result['references'].update(fallback_percentage=100 * len(fallback) / len(references) if references else None,
        fallback_denominator_reports=len(references), fallback_denominator_seconds=span,
        fallback_duration_percentage=100 * sum(durations) / span if span else None,
        fallback_reasons=dict(Counter(reason for row in fallback for reason in json.loads(row['diagnostics_json']) if reason.startswith('FALLBACK_'))))
    books = rows('book_observations', 'slot_id,outcome')
    windows = []
    for market in rows('markets', 'window_start_us'):
        windows.append(window_coverage(market, slots, books, runs))
    result['slot_diagnosis'] = {market['market_id']: slot_diagnosis(market, slots,
        [{key: value for key, value in row.items() if key != 'response_raw_z'}
                   for row in rows('request_attempts', 'request_id')], events, anchors, books) for market in rows('markets', 'window_start_us')}
    result['windows'] = windows
    result['full_window_verdict'] = 'PASS' if any(window['complete_window'] for window in windows) else 'FAIL'
    result['smoke_gate'] = bool(references and all(row['value_text'] is not None for row in references) and integrity == ['ok'] and not foreign_keys and version == 2
        and not any(isolation.values()) and replay['exact'] and usable
        and any(row['status'] == 'COMPLETE' for row in slots)
        and all(run['state'] == 'STOPPED' for run in runs))
    return result


def _analyze_connection(conn, db_path):
    conn.row_factory = sqlite3.Row

    print("=== DB INTEGRITY ===")
    print("integrity_check:", conn.execute("PRAGMA integrity_check").fetchall())
    print("foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())
    print("user_version:", conn.execute("PRAGMA user_version").fetchone()[0])

    print("\n=== TABLES ===")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(tables)

    print("\n=== APPROXIMATE TABLE COUNTS ===")
    for t in ["approximate_feeds", "approximate_observations", "approximate_references"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {count}")

    print("\n=== CHAINLINK TABLE COUNTS (should be 0 or schema-only) ===")
    for t in ["reference_feeds", "reference_reports", "reference_fetches", "reference_associations"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {count}")

    print("\n=== BOOK TABLES ===")
    for t in ["recording_runs", "sample_slots", "request_attempts", "book_observations", "clock_anchors", "markets"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {count}")

    print("\n=== RUNS ===")
    for r in conn.execute("SELECT run_id, mode, state, reference_activation, started_wall_us, ended_wall_us FROM recording_runs"):
        print(dict(r))

    print("\n=== REQUEST ATTEMPTS BY OPERATION ===")
    for r in conn.execute("SELECT operation, method, path, result, COUNT(*) as n, MIN(ended_wall_us) as first_end, MAX(ended_wall_us) as last_end FROM request_attempts GROUP BY operation, method, path, result ORDER BY n DESC"):
        print(dict(r))

    print("\n=== APPROXIMATE OBSERVATIONS BY PROVIDER/RESULT ===")
    for r in conn.execute("SELECT provider, result, error_code, COUNT(*) as n FROM approximate_observations GROUP BY provider, result, error_code ORDER BY provider, result"):
        print(dict(r))

    print("\n=== APPROXIMATE REFERENCES ===")
    for r in conn.execute("SELECT reference_id, source_provider, was_fallback, mode, reference_kind, verification_status, value_text, observation_count, window_start_us, window_end_us, receipt_wall_us, diagnostics_json FROM approximate_references ORDER BY reference_id"):
        print(dict(r))

    print("\n=== BOOK SLOTS ===")
    for r in conn.execute("SELECT status, COUNT(*) as n, MIN(committed_wall_us) as min_wall, MAX(committed_wall_us) as max_wall FROM sample_slots GROUP BY status ORDER BY n DESC"):
        print(dict(r))

    print("\n=== RECORDER EVENTS ===")
    for r in conn.execute("SELECT event_id, kind, started_us, ended_us, detail_json FROM recorder_events ORDER BY event_id"):
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
        d = dict(r)
        d["detail"] = detail
        print(d)

    conn.close()
    print("\n=== LOCK FILE CHECK ===")
    lock = Path(str(db_path) + ".writer.lock")
    print("lock file exists:", lock.exists())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit('Explicit new CP5 database path required')
    print(json.dumps(analyze_db(Path(sys.argv[1]))))