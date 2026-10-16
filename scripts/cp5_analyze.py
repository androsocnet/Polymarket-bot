#!/usr/bin/env python3
"""CP5 database analysis script."""
from contextlib import closing
import sqlite3
import json
import sys
from pathlib import Path
from typing import Any


def replay_rows(observations, references=None, events=None, anchors=None):
    if isinstance(observations, sqlite3.Connection):
        connection = observations
        def rows(table, order):
            cursor = connection.execute(f'SELECT * FROM {table} ORDER BY {order}')
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor]
        observations, references, events, anchors = (rows(table, order) for table, order in (
            ('approximate_observations', 'observation_id'), ('approximate_references', 'reference_id'),
            ('recorder_events', 'event_id'), ('clock_anchors', 'anchor_id')))
    try:
        return _replay_rows(observations, references, events, anchors)
    except (KeyError, TypeError, ArithmeticError, RecursionError) as exc:
        raise ValueError('incomplete malformed persisted provenance: ' + str(exc)) from exc


def _replay_rows(observations, references, events, anchors):
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
    research_reports = []
    chronology = {}
    captured_membership = {}
    completions_by_run = {}
    event_ids = [row['event_id'] for row in events if 'event_id' in row]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError('incomplete duplicate event identities')
    if len(observation_map) != len(observations) or len(reference_map) != len(references) or len(anchor_map) != len(anchors):
        raise ValueError('incomplete duplicate persisted identities')
    for event in events:
        detail = json.loads(event['detail_json'])
        if not isinstance(detail, dict):
            raise ValueError('incomplete malformed event object')
        if detail.get('type') != 'APPROXIMATE_FETCH':
            continue
        if type(detail.get('version')) is not int or detail['version'] not in (1, 2) or detail.get('diagnostic_only') is not True:
            raise ValueError('incomplete fetch version')
        if detail['version'] == 2:
            report_output = _replay_research(detail, event, observation_map, reference_map, anchor_map, events,
                seen_observations, seen_references, captured_membership, completions_by_run.get(event['run_id'], 0))
            completions_by_run[event['run_id']] = detail['completion_ns']
            previous = chronology.get(event['run_id'])
            current = (detail['reference_id'], detail['receipt_mono_ns'], *detail['observation_ids'])
            if previous is not None and (current[0] <= previous[0] or current[1] < previous[1]
                    or current[2] <= previous[3] or observation_map[current[2]]['receipt_mono_ns'] < previous[1]):
                raise ValueError('incomplete fetch chronology')
            chronology[event['run_id']] = current
            research_reports.append(report_output)
            matched += 1
            continue
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
        research_reports.append(dict(reference_id=ref['reference_id'], run_id=run, revision=1,
            qualification='UNKNOWN', warmup_status='UNKNOWN', missing_requests='UNKNOWN'))
        seen_references.add(ref['reference_id'])
        matched += 1
    if not matched or seen_observations != set(observation_map) or seen_references != set(reference_map):
        raise ValueError('incomplete persisted fetch evidence')
    result = {'matched': matched, 'observations': len(seen_observations), 'exact': True}
    if research_reports:
        result.update(reports=research_reports, qualification='RECONSTRUCTED')
    return result


def _replay_research(detail, event, observations, references, anchors, events, seen_observations, seen_references, captured_membership, previous_completion):
    from dataclasses import asdict
    from decimal import Decimal
    from typing import NoReturn
    from polymarket_bot.recorder.models import ClockAnchor
    from polymarket_bot.recorder.free_reference import (AcquisitionContext, ApproximateObservation,
        ResearchWindowEvidence, reconstruct_research_report, _captured_projection)
    from polymarket_bot.market_data.numeric import decimal_text
    def fail(reason) -> NoReturn:
        raise ValueError('incomplete revision-2 ' + reason)
    def integer(value):
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            fail('integer timing or identity')
        return value
    def bounded(value, depth=0):
        if depth > 12:
            fail('bounded JSON depth')
        if isinstance(value, dict):
            if len(value) > 64:
                fail('bounded JSON object')
            for item in value.values():
                bounded(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 512:
                fail('bounded JSON list')
            for item in value:
                bounded(item, depth + 1)
        elif type(value) is int:
            if not -(2**63) <= value <= 2**63 - 1:
                fail('bounded JSON integer')
        elif isinstance(value, float) or isinstance(value, str) and len(value) > 4096:
            fail('bounded JSON scalar')
    bounded(detail)
    if len(json.dumps(detail)) > 1_048_576:
        fail('bounded JSON')
    run = event['run_id']
    origin = integer(detail['sampling_origin_ns'])
    receipt = integer(detail['receipt_mono_ns'])
    completion = integer(detail['completion_ns'])
    if type(run) is not str or not run.strip():
        fail('run identity')
    for key in ('version', 'policy_revision', 'anchor_id', 'uncertainty_us'):
        integer(detail[key])
    if detail['run_id'] != run or detail['policy_revision'] != 2 or detail['persistence_status'] != 'COMPLETE' or not origin <= receipt <= completion:
        fail('origin or completion chronology')
    ids = detail['observation_ids']
    if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2 or any(integer(i) == 0 or i in seen_observations for i in ids):
        fail('membership')
    ref = references.get(integer(detail['reference_id']))
    if ref is None or ref['run_id'] != run or ref['reference_id'] in seen_references or ref['receipt_mono_ns'] != receipt:
        fail('reference membership')
    parsed = [(row, json.loads(row['detail_json'])) for row in events]
    if any(not isinstance(data, dict) for _, data in parsed):
        fail('malformed event object')
    owned = [(row, data) for row, data in parsed if row['run_id'] == run]
    for row, data in owned:
        if data.get('type') in ('APPROXIMATE_SESSION', 'APPROXIMATE_OPPORTUNITY',
                'APPROXIMATE_OPPORTUNITY_COMPLETION', 'APPROXIMATE_DISCONTINUITY', 'ACTUAL_SEND_ATTEMPT'):
            bounded(data)
            if type(data.get('version')) is not int or data['version'] != 2 or data.get('run_id') != run:
                fail('event version or run identity')
            integer(row['event_id'])
            for key in ('sampling_origin_ns', 'target_ns', 'opportunity_id', 'reference_id',
                    'completion_ns', 'calibration_end_ns', 'anchor_id', 'attempt', 'started_mono_ns', 'ended_mono_ns',
                    'slot_id', 'slot_index'):
                if key in data:
                    integer(data[key])
    sessions = [data for _, data in owned if data.get('type') == 'APPROXIMATE_SESSION']
    if len(sessions) != 1 or sessions[0].get('sampling_origin_ns') != origin:
        fail('session origin')
    opportunity_id = integer(detail['opportunity_id'])
    originals = [data for row, data in owned if row['event_id'] == opportunity_id and data.get('type') == 'APPROXIMATE_OPPORTUNITY']
    if len(originals) != 1 or originals[0].get('disposition') != 'submitted':
        fail('missing original submitted opportunity')
    original = originals[0]
    for field in ('market_id', 'slot_id', 'slot_index', 'target_ns'):
        if field in original or field in detail:
            if field != 'market_id':
                integer(detail[field])
            elif type(detail[field]) is not str or not detail[field].strip():
                fail('opportunity market identity')
            if original.get(field) != detail[field]:
                fail('opportunity identity mismatch')
    completions = [data for _, data in owned if data.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION' and data.get('opportunity_id') == opportunity_id]
    if len(completions) != 1 or completions[0].get('disposition') != 'completed' or any(completions[0].get(k) != detail.get(k) for k in ('reference_id', 'completion_ns', 'market_id', 'slot_id', 'slot_index', 'target_ns')):
        fail('completion barrier')
    raw = detail['evidence']
    if raw['run_id'] != run or raw['revision'] != 2 or raw['sampling_origin_ns'] != origin:
        fail('evidence origin')
    integer(raw['revision'])
    integer(raw['sampling_origin_ns'])
    for key in ('validated_window_start_ns', 'validated_window_end_ns'):
        if raw[key] is not None:
            integer(raw[key])
    for outcome in raw['request_outcomes']:
        if not isinstance(outcome, list) or len(outcome) != 5:
            fail('request outcome shape')
        for index in (0, 2, 3):
            integer(outcome[index])
    for request in raw['expected_requests']:
        if not isinstance(request, list) or len(request) != 2:
            fail('expected request shape')
        integer(request[0])
    window_ids = raw['observation_ids']
    if not 0 < len(window_ids) <= 256 or len(set(window_ids)) != len(window_ids) or window_ids[-2:] != ids or len(raw['contexts']) != len(window_ids):
        fail('window membership')
    contexts, window = [], []
    for identifier, captured in zip(window_ids, raw['contexts']):
        row = observations.get(integer(identifier))
        if row is None or row['run_id'] != run or (identifier not in ids and identifier not in seen_observations):
            fail('observation ownership')
        fields = {k: row[k] for k in ApproximateObservation.__dataclass_fields__ if k not in ('price', 'bid_price', 'ask_price')}
        fields.update({k: Decimal(row[k + '_text']) if row[k + '_text'] is not None else None for k in ('price', 'bid_price', 'ask_price')})
        obs = ApproximateObservation(**fields)
        if obs.receipt_mono_ns is None or integer(obs.receipt_mono_ns) > receipt:
            fail('post-completion observation')
        anchor = anchors.get(integer(captured['anchor_id']))
        if anchor is None or anchor['run_id'] != run or captured['run_id'] != run or captured['sampling_origin_ns'] != origin or captured['revision'] != 2:
            fail('captured context ownership')
        for key in ('revision', 'sampling_origin_ns'):
            integer(captured[key])
        if captured['discontinuity_end_ns'] is not None:
            integer(captured['discontinuity_end_ns'])
        for key, value in captured['anchor'].items():
            if key not in ('status', 'diagnostic'):
                integer(value)
        prior = captured_membership.get(identifier)
        outcome = [r for r in raw['request_outcomes'] if r[2] == identifier]
        if len(outcome) != 1 or prior is not None and prior != (captured, outcome[0]):
            fail('historical captured membership')
        model = ClockAnchor(**captured['anchor'])
        for column, field in (('server_seconds', 'server_seconds'), ('anchor_server_us', 'server_us'),
                ('anchor_mono_ns', 'midpoint_ns'), ('request_start_mono_ns', 'request_start_mono_ns'),
                ('request_end_mono_ns', 'request_end_mono_ns'), ('request_start_wall_us', 'request_start_wall_us'),
                ('request_end_wall_us', 'request_end_wall_us'), ('status', 'status'), ('uncertainty_us', 'uncertainty_us')):
            if anchor[column] != getattr(model, field):
                fail('anchor uncertainty or captured fields')
        contexts.append(AcquisitionContext(captured['revision'], run, model, captured['anchor_id'], origin, captured['discontinuity_end_ns']))
        window.append(obs)
    context = contexts[-1]
    if contexts[-2] != context or context.anchor_id != detail['anchor_id'] or context.anchor.uncertainty_us != detail['uncertainty_us']:
        fail('current anchor uncertainty')
    if ids[0] >= ids[1] or [o.provider for o in window[-2:]] != ['coinbase', 'kraken'] or window[-2].receipt_mono_ns > window[-1].receipt_mono_ns:
        fail('ordered membership')
    expected_history = {i for i, (_, outcome) in captured_membership.items()
        if observations[i]['run_id'] == run and (outcome[3] >= receipt - 60_000_000_000
            or any(r[0] == outcome[0] and r[1] >= receipt - 60_000_000_000 for r in raw['scheduled_opportunities']))}
    if not expected_history <= set(window_ids):
        fail('missing historical window membership')
    opportunities = raw['scheduled_opportunities']
    if len(opportunities) > 128:
        fail('bounded opportunities')
    for identifier, target, disposition in opportunities:
        integer(identifier)
        integer(target)
        matches = [data for row, data in owned if row.get('event_id') == identifier and data.get('type') == 'APPROXIMATE_OPPORTUNITY']
        if len(matches) != 1 or matches[0].get('target_ns') != target or matches[0].get('sampling_origin_ns') != origin or matches[0].get('disposition') != ('submitted' if disposition == 'completed' else disposition):
            fail('scheduler ownership')
    start = max(origin, receipt - 60_000_000_000)
    durable = {row['event_id'] for row, data in owned
        if data.get('type') == 'APPROXIMATE_OPPORTUNITY'
        and start - 2_000_000_000 < data['target_ns'] <= receipt}
    embedded = [identifier for identifier, target, _ in opportunities
        if start - 2_000_000_000 < target <= receipt]
    if len(embedded) != len(set(embedded)) or set(embedded) != durable:
        fail('scheduler membership')
    targets = sorted(target for _, target, _ in opportunities if start - 2_000_000_000 < target <= receipt)
    complete = bool(targets) and targets[0] <= start and receipt < targets[-1] + 2_000_000_000 and all(b - a == 2_000_000_000 for a, b in zip(targets, targets[1:]))
    if raw['validated_window_start_ns'] != (start if complete else None) or raw['validated_window_end_ns'] != (receipt if complete else None):
        fail('scheduler interval')
    ends = raw['discontinuity_ends_ns']
    if any(integer(end) > receipt for end in ends):
        fail('discontinuity chronology')
    known_ends = {data['calibration_end_ns'] for _, data in owned if data.get('type') == 'APPROXIMATE_DISCONTINUITY'}
    if any(end not in known_ends for end in ends):
        fail('invented discontinuity')
    for end in known_ends:
        if start <= end <= receipt and end not in ends:
            fail('missing discontinuity')
    attempts = detail['request_attempts']
    if not isinstance(attempts, list) or len(attempts) > 4:
        fail('bounded attempt timing')
    if any(a.get('provider') not in ('coinbase', 'kraken') for a in attempts):
        fail('attempt provider identity')
    target = integer(original['target_ns'])
    previous_provider_receipt = max(origin, target, previous_completion)
    for provider, obs in zip(('coinbase', 'kraken'), window[-2:]):
        selected = [a for a in attempts if a.get('provider') == provider]
        if not selected and obs.error_code not in ('MISSING_PROVIDER', 'PROVIDER_FAILURE', 'CLOCK_CONTEXT_UNAVAILABLE'):
            fail('missing provider attempt timing')
        previous = previous_provider_receipt
        for index, attempt in enumerate(selected, 1):
            begin, end = integer(attempt['started_mono_ns']), integer(attempt['ended_mono_ns'])
            if attempt['run_id'] != run or attempt['opportunity_id'] != opportunity_id or attempt['attempt'] != index or not previous <= begin <= end <= obs.receipt_mono_ns:
                fail('attempt chronology')
            if _captured_projection(context, begin) is None:
                fail('attempt captured anchor unavailable')
            integer(attempt['attempt'])
            previous = end
        previous_provider_receipt = max(previous_provider_receipt, obs.receipt_mono_ns)
    actual = [data for _, data in owned if data.get('type') == 'ACTUAL_SEND_ATTEMPT']
    if actual:
        identities = [integer(a['attempt']) for a in actual]
        if len(set(identities)) != len(identities):
            fail('transport duplicate attempt identity')
        for provider in ('coinbase', 'kraken'):
            wrappers = [a for a in attempts if a['provider'] == provider]
            sends = sorted((a for a in actual if a.get('provider') == provider and a.get('opportunity_id') == opportunity_id), key=lambda a: a['attempt'])
            if len(sends) != len(wrappers):
                fail('transport missing attempt timing')
            for wrapper, send in zip(wrappers, sends):
                host, path = ('api.exchange.coinbase.com', '/products/BTC-USD/ticker') if provider == 'coinbase' else ('api.kraken.com', '/0/public/Ticker')
                if (send.get('host') != host or send.get('path') != path or send.get('method') != 'GET'
                        or not wrapper['started_mono_ns'] <= send['started_mono_ns'] <= send['ended_mono_ns'] <= wrapper['ended_mono_ns']
                        or send.get('status') != wrapper['http_status']
                        or any(send.get(k) != detail.get(k) for k in ('market_id', 'slot_id', 'slot_index', 'target_ns'))):
                    fail('transport attempt provenance')
    evidence = ResearchWindowEvidence(raw['revision'], run, origin, tuple(window_ids), tuple(contexts),
        tuple(tuple(r) for r in opportunities), tuple(tuple(r) for r in raw['request_outcomes']),
        tuple(ends), raw['validated_window_start_ns'], raw['validated_window_end_ns'], tuple(tuple(r) for r in raw['expected_requests']))
    report, assessments, selected = reconstruct_research_report(window, evidence=evidence, context=context,
        report_wall_us=ref['receipt_wall_us'], report_ns=receipt)
    def encoded(value):
        return json.loads(json.dumps(asdict(value), default=lambda v: str(v) if isinstance(v, Decimal) else v))
    if [encoded(a) for a in assessments] != detail['assessments'] or encoded(selected) != detail['selected_assessment']:
        fail('assessment or selection mismatch')
    for name in report.__dataclass_fields__:
        expected = json.loads(ref['diagnostics_json']) if name == 'diagnostics' else ref['value_text'] if name == 'value' else ref[name]
        actual = list(report.diagnostics) if name == 'diagnostics' else decimal_text(report.value) if name == 'value' and report.value is not None else getattr(report, name)
        if actual != expected:
            raise ValueError('replay mismatch: ' + name)
    for identifier, captured in zip(window_ids, raw['contexts']):
        captured_membership[identifier] = (captured, next(r for r in raw['request_outcomes'] if r[2] == identifier))
    seen_observations.update(ids)
    seen_references.add(ref['reference_id'])
    return dict(reference_id=ref['reference_id'], run_id=run, revision=2, opportunity_id=opportunity_id,
        receipt_mono_ns=receipt, completion_ns=completion, selected_assessment=encoded(selected),
        warmup_status=selected.warmup_status, causal_availability='NOT_EVALUATED', qualification=selected.value_status == 'VALID' and selected.coverage_quality != 'INSUFFICIENT' and selected.warmup_status == 'FULL')


def _timing(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _qualified(report):
    assessment = report.get('selected_assessment', {})
    return (type(report.get('revision')) is int and report['revision'] == 2
        and report.get('qualification') is True and report.get('warmup_status') == 'FULL'
        and assessment.get('warmup_status') == 'FULL' and assessment.get('value_status') == 'VALID'
        and assessment.get('coverage_quality') in ('COMPLETE', 'DEGRADED')
        and _timing(report.get('receipt_mono_ns')) and _timing(report.get('completion_ns'))
        and report['receipt_mono_ns'] <= report['completion_ns'])


def smoke_continuity(reports, opportunities):
    reasons = []
    bad = [r.get('reference_id') for r in reports if r.get('warmup_status') == 'FULL' and not _qualified(r)]
    if bad:
        reasons.append('POST_WARMUP_INVALID_OR_INSUFFICIENT')
    reference_ids = [(r.get('run_id'), r.get('reference_id')) for r in reports]
    completion_ids = [(r.get('run_id'), r.get('opportunity_id')) for r in reports if r.get('revision') == 2]
    if len(set(reference_ids)) != len(reference_ids) or len(set(completion_ids)) != len(completion_ids):
        reasons.append('DUPLICATE_REFERENCE_IDENTITY')
    chains, chain = [], []
    identities = [(o.get('run_id'), o.get('event_id')) for o in opportunities]
    if len(set(identities)) != len(identities):
        reasons.append('DUPLICATE_OPPORTUNITY')
    ordered = sorted(opportunities, key=lambda o: (str(o.get('run_id')), str(o.get('market_id')), o.get('target_ns', -1)))
    for opportunity in ordered:
        matches = [r for r in reports if r.get('run_id') == opportunity.get('run_id')
            and r.get('opportunity_id') == opportunity.get('event_id')]
        valid = (opportunity.get('version') == 2 and _timing(opportunity.get('target_ns'))
            and opportunity.get('disposition') == 'submitted' and len(matches) == 1 and _qualified(matches[0]))
        if chain and (not valid or opportunity.get('run_id') != chain[-1]['run_id']
                or opportunity.get('market_id') != chain[-1].get('market_id')
                or opportunity['target_ns'] - chain[-1]['target_ns'] != 2_000_000_000
                or matches[0]['completion_ns'] <= chain[-1]['completion_ns']):
            chains.append(chain)
            chain = []
        if valid:
            chain.append(dict(opportunity, reference_id=matches[0]['reference_id'], completion_ns=matches[0]['completion_ns']))
    chains.append(chain)
    qualifying = next((c[start:start + 15] for c in chains for start in range(len(c) - 14)
        if c[start + 14]['completion_ns'] - c[start]['completion_ns'] >= 28_000_000_000), [])
    if not qualifying:
        reasons.append('NO_15_SCHEDULED_REPORTS_SPANNING_28_SECONDS')
    return dict(verdict='FAIL' if reasons else 'PASS', reasons=reasons, qualifying_chain=qualifying,
        completion_span_ns=qualifying[-1]['completion_ns'] - qualifying[0]['completion_ns'] if qualifying else None,
        post_warmup_exclusions=bad)


def pilot_causal_availability(reports, slots, attempts):
    reasons, rows, segment, best = [], [], [], []
    identities = {(s.get('run_id'), s.get('market_id')) for s in slots}
    indices = [s.get('slot_index') for s in slots]
    if (len(identities) != 1 or len(slots) != 450 or any(not _timing(i) for i in indices)
            or set(indices) != set(range(450)) or len({s.get('slot_id') for s in slots}) != 450):
        reasons.append('INCOMPLETE_OR_MIXED_450_SLOT_LEDGER')
    reference_ids = [(r.get('run_id'), r.get('reference_id')) for r in reports]
    if len(set(reference_ids)) != len(reference_ids):
        reasons.append('DUPLICATE_REFERENCE_IDENTITY')
    tokens = None
    boundaries = {(s.get('window_start_us'), s.get('window_end_us')) for s in slots}
    if len(boundaries) != 1:
        reasons.append('MIXED_MARKET_BOUNDARIES')
    for slot in sorted(slots, key=lambda s: s.get('slot_index', -1)):
        exclusion = []
        books = slot.get('books', [])
        status = slot.get('status')
        valid_count = sum(book.get('status') == 'VALID' for book in books)
        classification_valid = (
            status in ('COMPLETE', 'PARTIAL', 'FAILED') and len(books) == 2
            and valid_count == {'COMPLETE': 2, 'PARTIAL': 1, 'FAILED': 0}[status]
            or status == 'MISSED' and not books
            or status == 'INTERRUPTED' and len(books) == 2
            and all(book.get('status') == 'INTERRUPTED' for book in books))
        if not classification_valid:
            exclusion.append('UNSUPPORTED_OR_INCONSISTENT_SLOT_CLASSIFICATION')
            if 'UNSUPPORTED_OR_INCONSISTENT_SLOT_CLASSIFICATION' not in reasons:
                reasons.append('UNSUPPORTED_OR_INCONSISTENT_SLOT_CLASSIFICATION')
        mapping = {b.get('outcome'): (b.get('expected_token_id'), b.get('expected_condition_id')) for b in books}
        identity_valid = (len(books) == 2 and set(mapping) == {'UP', 'DOWN'}
            and all(isinstance(t, str) and t.strip() and isinstance(c, str) and c.strip() for t, c in mapping.values())
            and len({t for t, _ in mapping.values()}) == 2 and len({c for _, c in mapping.values()}) == 1)
        if books and (not identity_valid or tokens is not None and mapping != tokens
                or slot.get('expected_identity') is not None and mapping != slot['expected_identity']):
            exclusion.append('BOOK_IDENTITY_MISMATCH')
            if 'BOOK_IDENTITY_MISMATCH' not in reasons:
                reasons.append('BOOK_IDENTITY_MISMATCH')
        elif identity_valid and tokens is None:
            tokens = mapping
        paired = identity_valid and mapping == tokens and slot.get('status') == 'COMPLETE' and all(b.get('status') == 'VALID' for b in books)
        if not paired:
            exclusion.append('NOT_PAIRED_VALID')
        if (not _timing(slot.get('window_start_us')) or not _timing(slot.get('window_end_us'))
                or slot['window_end_us'] - slot['window_start_us'] != 900_000_000
                or not _timing(slot.get('slot_index')) or slot.get('scheduled_us') != slot['window_start_us'] + slot['slot_index'] * 2_000_000):
            exclusion.append('INVALID_SLOT_GRID')
            if 'INVALID_SLOT_GRID' not in reasons:
                reasons.append('INVALID_SLOT_GRID')
        sends = [a for a in attempts if a.get('slot_id') == slot.get('slot_id')]
        legs = {a.get('leg') for a in sends}
        timing_valid = bool(sends) and (legs == {'PAIRED'} or legs <= {'UP', 'DOWN', 'PAIRED'} and ('PAIRED' in legs or {'UP', 'DOWN'} <= legs))
        timing_valid = timing_valid and len({a.get('attempt') for a in sends}) == len(sends) and all(
            a.get('run_id') == slot.get('run_id') and a.get('market_id') == slot.get('market_id')
            and type(a.get('slot_index')) is int and a['slot_index'] == slot['slot_index']
            and _timing(a.get('attempt')) and _timing(a.get('started_mono_ns')) and _timing(a.get('ended_mono_ns'))
            and a['started_mono_ns'] <= a['ended_mono_ns'] for a in sends)
        cutoff = min(a['started_mono_ns'] for a in sends) if timing_valid else None
        if not timing_valid:
            exclusion.append('MISSING_OR_INVALID_ACTUAL_SEND_TIMING')
        candidates = [r for r in reports if r.get('run_id') == slot.get('run_id') and _qualified(r)
            and cutoff is not None and r['completion_ns'] < cutoff and r['receipt_mono_ns'] < cutoff]
        reference = max(candidates, key=lambda r: r['completion_ns'], default=None)
        age = cutoff - reference['completion_ns'] if reference is not None else None
        if reference is None:
            exclusion.append('NO_STRICTLY_PRIOR_QUALIFYING_REFERENCE')
        elif age is not None and age > 2_000_000_000:
            exclusion.append('REFERENCE_OLDER_THAN_2_SECONDS')
        row = dict(slot_index=slot.get('slot_index'), slot_id=slot.get('slot_id'), run_id=slot.get('run_id'),
            market_id=slot.get('market_id'), classification=slot.get('status'), paired_valid=paired,
            cutoff_ns=cutoff, reference_id=reference['reference_id'] if reference else None, age_ns=age,
            available=not exclusion, exclusions=exclusion, attempts=sends)
        rows.append(row)
        if segment and (not row['available'] or row['slot_index'] != segment[-1]['slot_index'] + 1
                or row['run_id'] != segment[-1]['run_id'] or row['market_id'] != segment[-1]['market_id']):
            if len(segment) > len(best):
                best = segment
            segment = []
        if row['available']:
            segment.append(row)
    if len(segment) > len(best):
        best = segment
    opening = any(r['slot_index'] == 0 and r['paired_valid'] for r in rows)
    closing = any(r['slot_index'] == 449 and r['paired_valid'] for r in rows)
    if not opening or not closing:
        reasons.append('MISSING_PAIRED_OPENING_OR_CLOSING')
    qualifying = best if len(best) >= 31 else []
    if not qualifying:
        reasons.append('NO_31_CONSECUTIVE_CAUSAL_SLOTS_SPANNING_60_SECONDS')
    return dict(verdict='FAIL' if reasons else 'PASS', reasons=reasons, slots=rows,
        qualifying_segment=qualifying, segment_span_ns=(len(qualifying) - 1) * 2_000_000_000 if qualifying else None,
        opening_present=opening, closing_present=closing, expected_slots=450,
        excluded_slot_indices=[r['slot_index'] for r in rows if not r['available']],
        source_time_semantics='SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY is not exact UTC ordering')


def intended_phase(summary, markets, slots, events, boundary_us, mode):
    selected = [row for row in markets if row['window_start_us'] == boundary_us
        and row['window_end_us'] == boundary_us+900_000_000]
    if len(selected) != 1:
        return dict(verdict='FAIL', reason='INTENDED_MARKET_UNAVAILABLE')
    market = selected[0]
    activations = [row for row in events if row.get('type') == 'PREPARED_ACTIVATION'
        and row.get('boundary_us') == boundary_us]
    arms = [row for row in events if row.get('type') == 'PREPARED_ARM' and row.get('boundary_us') == boundary_us]
    if len(activations) != 1 or len(arms) != 1:
        return dict(verdict='FAIL', reason='PREPARATION_EVIDENCE_UNAVAILABLE')
    activation, arm = activations[0], arms[0]
    run_id = activation.get('run_id')
    ledger = [row for row in slots if row['market_id'] == market['market_id'] and row['run_id'] == run_id]
    opening = [row for row in ledger if row['slot_index'] < 5]
    sends = [row for row in events if row.get('type') == 'ACTUAL_SEND_ATTEMPT'
        and row.get('market_id') == market['market_id'] and row.get('run_id') == run_id
        and row.get('path') in ('/books', '/book')]
    valid = (summary.get('integrity') == ['ok'] and not summary.get('foreign_keys')
        and summary.get('user_version') == 2 and not any(summary.get('isolation', {}).values())
        and summary.get('replay', {}).get('exact') and arm.get('run_id') == run_id
        and arm['target_ns'] == activation['target_ns']
        and arm['armed_ns'] < arm['target_ns']-1_000_000
        and len(opening) == 5
        and all(row['scheduled_us'] == boundary_us+row['slot_index']*2_000_000 for row in ledger)
        and activation['activated_ns'] >= max(activation['target_ns'], activation['active_target_ns'])
        and {row['slot_index'] for row in opening} == set(range(5))
        and all(row['status'] == 'COMPLETE' for row in opening))
    for index in range(5):
        attempts = [row for row in sends if row.get('slot_index') == index]
        valid = valid and bool(attempts)
        for row in attempts:
            lower = max(activation['target_ns'], activation['active_target_ns'])+index*2_000_000_000
            valid = valid and lower <= row['started_mono_ns'] < activation['active_target_ns']+(index+1)*2_000_000_000
    if mode == 'pilot':
        valid = valid and len(ledger) == 450 and {row['slot_index'] for row in ledger} == set(range(450)) and any(row.get('verdict') == 'PASS'
            and row.get('market_id') == market['market_id'] and row.get('run_id') == run_id
            for row in summary.get('pilot_causal_availability', {}).get('windows', []))
    return dict(verdict='PASS' if valid else 'FAIL', market_id=market['market_id'], run_id=run_id)


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
    reports = replay.get('reports', []) if replay['exact'] else []
    decoded = [dict(json.loads(event['detail_json']), event_id=event['event_id']) for event in events]
    opportunities = [event for event in decoded if event.get('type') == 'APPROXIMATE_OPPORTUNITY']
    actual_sends = [event for event in decoded if event.get('type') == 'ACTUAL_SEND_ATTEMPT'
        and event.get('host') == 'clob.polymarket.com' and event.get('path') in ('/book', '/books')]
    result['report_quality'] = dict(reports=reports, classifications=dict(Counter(
        report.get('selected_assessment', {}).get('coverage_quality', 'UNKNOWN') for report in reports)),
        warmup=dict(Counter(report.get('warmup_status', 'UNKNOWN') for report in reports)))
    result['smoke_continuity'] = smoke_continuity(reports, opportunities)
    pilots = []
    for market in rows('markets', 'window_start_us'):
        for run in runs:
            ledger = [dict(slot, window_start_us=market['window_start_us'], window_end_us=market['window_end_us'],
                expected_identity={'UP': (market['up_token_id'], market['condition_id']),
                    'DOWN': (market['down_token_id'], market['condition_id'])},
                books=[book for book in books if book['slot_id'] == slot['slot_id']])
                for slot in slots if slot['market_id'] == market['market_id'] and slot['run_id'] == run['run_id']]
            if ledger:
                pilots.append(dict(pilot_causal_availability(reports, ledger, actual_sends),
                    run_id=run['run_id'], market_id=market['market_id']))
    system_valid = bool(integrity == ['ok'] and not foreign_keys and version == 2
        and not any(isolation.values()) and replay['exact'] and runs
        and all(run['state'] == 'STOPPED' for run in runs))
    result['pilot_causal_availability'] = dict(windows=pilots,
        verdict='PASS' if system_valid and any(pilot['verdict'] == 'PASS' for pilot in pilots) else 'FAIL',
        system_evidence_valid=system_valid)
    paired_book = any(slot['status'] == 'COMPLETE' and len(pair := [book for book in books
        if book['slot_id'] == slot['slot_id']]) == 2 and {book['outcome'] for book in pair} == {'UP', 'DOWN'}
        and all(book['status'] == 'VALID' for book in pair) for slot in slots)
    result['smoke_gate'] = system_valid and paired_book and result['smoke_continuity']['verdict'] == 'PASS'
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