import hashlib
import json
import sqlite3
import sys
from collections import Counter, deque
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from polymarket_bot.recorder.free_reference import ApproximateObservation, compute_approximate_sample_mean_60s
from scripts.cp5_analyze import analyze_db


def reconstruct(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        observations = {row['observation_id']: dict(row) for row in conn.execute('SELECT * FROM approximate_observations')}
        references = {row['reference_id']: dict(row) for row in conn.execute('SELECT * FROM approximate_references')}
        anchors = {row['anchor_id']: dict(row) for row in conn.execute('SELECT * FROM clock_anchors')}
        events = [dict(row) for row in conn.execute('SELECT * FROM recorder_events ORDER BY event_id')]
        requests = [dict(row) for row in conn.execute('SELECT request_id,path,response_raw_z FROM request_attempts')]
    histories = {}
    records = []
    for event in events:
        detail = json.loads(event['detail_json'])
        if detail.get('type') != 'APPROXIMATE_FETCH':
            continue
        ref = references[detail['reference_id']]
        history = histories.setdefault(event['run_id'], {name: deque(maxlen=64) for name in ('coinbase', 'kraken')})
        provider_records = {}
        for identifier in detail['observation_ids']:
            row = observations[identifier]
            history[row['provider']].append(row)
        for provider, queue in history.items():
            accepted, excluded, inputs = [], [], []
            for row in queue:
                fields = {name: row[name] for name in ApproximateObservation.__dataclass_fields__ if name not in ('price', 'bid_price', 'ask_price')}
                fields.update({name: Decimal(row[name + '_text']) if row[name + '_text'] is not None else None for name in ('price', 'bid_price', 'ask_price')})
                obs = ApproximateObservation(**fields)
                inputs.append(obs)
                single = compute_approximate_sample_mean_60s([obs], receipt_wall_us=ref['receipt_wall_us'], receipt_server_us=ref['receipt_server_us'], receipt_mono_ns=ref['receipt_mono_ns'])
                if single.value is not None:
                    accepted.append(row)
                else:
                    excluded.append(dict(observation_id=row['observation_id'], reason=next(reason for reason in single.diagnostics if reason.startswith('EXCLUDED_')), error_code=row['error_code']))
            accepted.sort(key=lambda row: (row['receipt_wall_us'], row['receipt_mono_ns'] or 0, row['raw_payload_hash']))
            report = compute_approximate_sample_mean_60s(inputs, receipt_wall_us=ref['receipt_wall_us'], receipt_server_us=ref['receipt_server_us'], receipt_mono_ns=ref['receipt_mono_ns'])
            gaps = [dict(first=a['observation_id'], second=b['observation_id'], wall_us=b['receipt_wall_us']-a['receipt_wall_us'], mono_ns=b['receipt_mono_ns']-a['receipt_mono_ns']) for a, b in zip(accepted, accepted[1:])]
            head = accepted[0]['receipt_wall_us']-ref['window_start_us'] if accepted else None
            tail = ref['window_end_us']-accepted[-1]['receipt_wall_us'] if accepted else None
            reasons = ([name for name, flag in (('HEAD_GT_2S', head is not None and head > 2_000_000), ('INTERIOR_GT_4S', any(gap['wall_us'] > 4_000_000 for gap in gaps)), ('TAIL_GT_4S', tail is not None and tail > 4_000_000)) if flag] if accepted else ['MISSING'])
            evicted = []
            while queue and queue[0]['receipt_wall_us'] < ref['window_start_us']:
                evicted.append(queue.popleft()['observation_id'])
            provider_records[provider] = dict(accepted_ids=[row['observation_id'] for row in accepted], excluded=excluded, oldest=accepted[0] if accepted else None, newest=accepted[-1] if accepted else None, consecutive_gaps=gaps, max_gap_us=max((gap['wall_us'] for gap in gaps), default=0), head_us=head, tail_us=tail, gap_reasons=reasons, diagnostics=list(report.diagnostics), evicted_after_report=evicted, value=str(report.value) if report.value is not None else None)
        anchor = anchors[detail['anchor_id']]
        records.append(dict(reference=ref, anchor=anchor, providers=provider_records, batch=[observations[identifier] for identifier in detail['observation_ids']]))
    assert len(records) == len(references) == 57
    assert all(row['reference']['observation_count'] == len(row['providers'][row['reference']['source_provider']]['accepted_ids']) for row in records)
    quality = analyze_db(path)
    assert quality['replay']['exact']
    diagnostics = Counter(reason for row in records for reason in json.loads(row['reference']['diagnostics_json']))
    assert dict(diagnostics) == quality['references']['diagnostics']
    rejections = [dict(reference_id=row['reference']['reference_id'], observation=obs, anchor=row['anchor'], wall_minus_server_us=obs['receipt_wall_us']-obs['receipt_server_us'], primary_accepted_ids=row['providers']['coinbase']['accepted_ids'], source_provider=row['reference']['source_provider'], was_fallback=row['reference']['was_fallback']) for row in records for obs in row['batch'] if obs['error_code']]
    return dict(database=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), nominal_cadence_us=2_000_000, capacity_evictions=0, records=records, diagnostic_categories=dict(Counter('|'.join(json.loads(row['reference']['diagnostics_json'])) for row in records)), gap_categories={provider: dict(Counter('|'.join(row['providers'][provider]['gap_reasons']) for row in records)) for provider in ('coinbase', 'kraken')}, rejections=rejections, payload_evidence=dict(tables=sorted(tables), exchange_request_rows=sum('ticker' in row['path'].lower() for row in requests), raw_exchange_payloads=sum('ticker' in row['path'].lower() and row['response_raw_z'] is not None for row in requests)), quality=quality)


if __name__ == '__main__':
    print(json.dumps(reconstruct(Path(sys.argv[1])), indent=2))
