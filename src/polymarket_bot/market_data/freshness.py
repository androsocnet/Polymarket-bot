"""PAPER clock evidence; legacy discovery's point clock is not freshness proof.

Assumption: /time samples same running UTC clock during request bracket. Integer
seconds may be floored or rounded: +/- one full second conservatively covers both.
Project each bracket by monotonic elapsed (no local/server wall offset assumption),
then intersect. Disjoint intervals cannot certify timing. Clock steps outside this
model fail closed; steps hidden inside uncertainty cannot be detected from /time.
"""
import math
import re
from datetime import datetime, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
QUANTIZATION_US = 1_000_000


def wall_us(moment):
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError('Expected aware datetime')
    delta = moment - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('Expected nonnegative integer timing')
    return value


def _read(read):
    a, b = _integer(read.started_mono_ns), _integer(read.finished_mono_ns)
    x, y = _integer(read.started_wall_us), _integer(read.finished_wall_us)
    if b < a or y < x:
        raise ValueError('Reversed request timing')
    # Calls sample clocks sequentially. Allow 1ms sampling skew, not clock steps.
    if abs((y - x) * 1000 - (b - a)) > 1_000_000:
        raise ValueError('Local wall/monotonic disagreement')
    for seconds, ns in ((read.started_mono, a), (read.finished_mono, b)):
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or abs(seconds * 1_000_000_000 - ns) > 1_000_000):
            raise ValueError('Conflicting monotonic clocks')
    return a, b, x, y


def _interval(read, ns):
    a, b, _, _ = _read(read)
    if type(read.payload) is not int or read.payload <= 0:
        raise ValueError('Expected integer server seconds')
    center = read.payload * 1_000_000
    return (center - QUANTIZATION_US + (ns - b) // 1000,
            center + QUANTIZATION_US - (-(ns - a) // 1000))


def _intersection(first, final, ns):
    a, b = _interval(first, ns), _interval(final, ns)
    lower, upper = max(a[0], b[0]), min(a[1], b[1])
    if lower > upper:
        raise ValueError('Disjoint server intervals')
    return lower, upper


def _safe(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else dict(invalid_type='float', value=str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    # Do not call arbitrary repr/str or serialize full malformed payloads/books.
    return dict(invalid_type=type(value).__name__)


def _captured_read(read, *, clock=False):
    if read is None:
        return None
    fields = ('started_mono', 'finished_mono', 'started_mono_ns', 'finished_mono_ns',
              'started_wall_us', 'finished_wall_us')
    if clock:
        fields += ('payload',)
    return {name: _safe(getattr(read, name, None)) for name in fields}


def evaluate(snapshot, *, decision_mono_ns, max_age_seconds, sample_seconds):
    """Return JSON-safe proof, including signed ages. Never clamp negative ages."""
    result = dict(valid=False, reason='TIMING_INVALID', window_valid=False,
                  slot_valid=False, quantization_uncertainty_us=QUANTIZATION_US,
                  decision_mono_ns=_safe(decision_mono_ns),
                  captured=dict(initial_time=_captured_read(snapshot.initial_time, clock=True),
                                final_time=_captured_read(snapshot.final_time, clock=True),
                                validated_at=_safe(snapshot.validated_at),
                                validated_mono_ns=_safe(snapshot.validated_mono_ns)),
                  outcomes={name: dict(fresh=False, reason='TIMING_INVALID',
                                      source_timestamp_raw=_safe(observation.book.source_timestamp_raw),
                                      source_at=_safe(observation.book.source_at),
                                      timing=_captured_read(observation.timing))
                            for name, observation in (('UP', snapshot.up), ('DOWN', snapshot.down))})
    try:
        now = _integer(decision_mono_ns)
        first, final = snapshot.initial_time, snapshot.final_time
        a, b, x, y = _read(first)
        c, d, z, w = _read(final)
        validated_ns = _integer(snapshot.validated_mono_ns)
        if not a <= b <= c <= d <= validated_ns <= now:
            raise ValueError('Invalid sample ordering')
        if abs((z - y) * 1000 - (c - b)) > 1_000_000:
            raise ValueError('Local wall step between clock samples')
        lower, upper = _intersection(first, final, now)
        vl, vu = _intersection(first, final, validated_ns)
        if not vl <= wall_us(snapshot.validated_at) <= vu:
            raise ValueError('Validation clock conflicts with evidence')
        start, end = wall_us(snapshot.market.window_start), wall_us(snapshot.market.window_end)
        width = int(sample_seconds * 1_000_000)
        if width <= 0:
            raise ValueError('Invalid slot width')
        slot = (wall_us(snapshot.validated_at) - start) // width
        window_valid = start <= lower <= upper < end
        slot_valid = window_valid and (lower - start) // width == slot == (upper - start) // width
        result.update(valid=True, reason='', window_valid=window_valid, slot_valid=slot_valid,
                      slot_index=slot, server_lower_us=lower, server_upper_us=upper,
                      initial_sample=dict(server_seconds=first.payload, start_mono_ns=a, end_mono_ns=b,
                                          start_wall_us=x, end_wall_us=y),
                      final_sample=dict(server_seconds=final.payload, start_mono_ns=c, end_mono_ns=d,
                                        start_wall_us=z, end_wall_us=w))
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        result['error'] = str(exc)
        return result
    limit = int(max_age_seconds * 1_000_000)
    for name, observation in (('UP', snapshot.up), ('DOWN', snapshot.down)):
        evidence = result['outcomes'][name]
        try:
            ra, rb, rx, ry = _read(observation.timing)
            evidence.update(request_started_mono_ns=ra, receipt_mono_ns=rb,
                            request_started_wall_us=rx, receipt_wall_us=ry,
                            local_receipt_age_us=-(-(now - rb) // 1000),
                            response_elapsed_us=-(-(rb - ra) // 1000))
            if not b <= ra <= rb <= c:
                raise ValueError('Book outside collection brackets')
            if name == 'DOWN' and ra < _read(snapshot.up.timing)[1]:
                raise ValueError('Sequential book requests overlap or reverse')
            if abs((rx - y) * 1000 - (ra - b)) > 1_000_000:
                raise ValueError('Book local clocks conflict')
            raw = observation.book.source_timestamp_raw
            if type(raw) is not str or re.fullmatch(r'[0-9]{13}', raw) is None:
                raise ValueError('Invalid raw source timestamp')
            source = int(raw) * 1000
            if wall_us(observation.book.source_at) != source:
                raise ValueError('Raw/source_at mismatch')
            rl, ru = _intersection(first, final, rb)
            evidence.update(source_timestamp_raw=raw, source_us=source,
                            receipt_server_lower_us=rl, receipt_server_upper_us=ru,
                            receipt_source_age_lower_us=rl - source,
                            receipt_source_age_upper_us=ru - source,
                            source_age_lower_us=lower - source, source_age_upper_us=upper - source)
            reason = ('FUTURE_SOURCE' if source > ru else
                      'STALE_RESPONSE' if evidence['response_elapsed_us'] > limit else
                      'STALE_RECEIPT' if evidence['local_receipt_age_us'] > limit else
                      'STALE_SOURCE' if upper - source > limit else '')
            evidence.update(fresh=not reason, reason=reason)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            evidence['error'] = str(exc)
    return result
