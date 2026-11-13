"""Prospective receipt-time protocol v1. Not used by PAPER or recorder acceptance.

Bounds are nominal UTC projections, not independent Coinbase clock calibration.
Caller must verify anchor provenance/continuity, raw identity/parser/depth and
latest-receipt selection. No prices, fallback selection or future targets here.
"""


def assess_receipt_timing(*, run_id, receipt_run_id, source_us,
        request_started_mono_ns, receipt_mono_ns, cutoff_mono_ns,
        receipt_bounds_us, cutoff_bounds_us, window_start_us, window_end_us,
        raw_present, raw_complete, clock_valid, anchor_age_ns):
    result = dict(eligible=False, reasons=[], source_order='UNKNOWN',
                  source_age_bounds_us=None, execution_certified=False,
                  protocol='receipt-time-spot-v1')
    reasons = result['reasons']
    integers = (source_us, request_started_mono_ns, receipt_mono_ns, cutoff_mono_ns,
                window_start_us, window_end_us, anchor_age_ns)
    if (not all(type(v) is int and 0 <= v < 2**63 for v in integers)
            or source_us == 0 or window_start_us >= window_end_us
            or request_started_mono_ns > receipt_mono_ns):
        reasons.append('TIMING_INVALID')
        return result
    for bounds in (receipt_bounds_us, cutoff_bounds_us):
        if (type(bounds) not in (tuple, list) or len(bounds) != 2
                or not all(type(v) is int and 0 <= v < 2**63 for v in bounds)
                or bounds[0] > bounds[1]):
            reasons.append('TIMING_INVALID')
            return result
    rl, ru = receipt_bounds_us
    cl, cu = cutoff_bounds_us
    if not isinstance(run_id, str) or not run_id.strip() or run_id != receipt_run_id:
        reasons.append('RUN_MISMATCH')
    if receipt_mono_ns > cutoff_mono_ns:
        reasons.append('RECEIPT_AFTER_CUTOFF')
    if raw_present is not True or raw_complete is not True:
        reasons.append('RAW_EVIDENCE_UNAVAILABLE')
    if clock_valid is not True or anchor_age_ns > 120_000_000_000:
        reasons.append('CLOCK_UNAVAILABLE')
    if max(ru - rl, cu - cl) > 5_000_000:
        reasons.append('CLOCK_UNCERTAINTY_EXCESSIVE')
    # Projected intervals must be compatible with same-run monotonic elapsed.
    elapsed_us = (cutoff_mono_ns - receipt_mono_ns) // 1000
    if max(cl, rl + elapsed_us) > min(cu, ru + elapsed_us):
        reasons.append('CLOCK_RELATIONSHIP_INVALID')
    result['source_age_bounds_us'] = (cl - source_us, cu - source_us)
    result['source_order'] = ('DEFINITE_FUTURE_SOURCE' if source_us > ru else
        'SOURCE_ORDER_UNCERTAIN' if source_us > rl else 'SOURCE_NOT_AFTER_RECEIPT')
    if source_us > ru or source_us > cu:
        reasons.append('DEFINITE_FUTURE_SOURCE')
    if cutoff_mono_ns - receipt_mono_ns > 5_000_000_000:
        reasons.append('STALE_RECEIPT')
    if receipt_mono_ns - request_started_mono_ns > 5_000_000_000:
        reasons.append('STALE_RESPONSE')
    if cu - source_us > 5_000_000:
        reasons.append('STALE_SOURCE')
    if not (window_start_us <= min(rl, cl, source_us)
            and max(ru, cu, source_us) < window_end_us):
        reasons.append('MARKET_BOUNDARY')
    result['eligible'] = not reasons
    return result
