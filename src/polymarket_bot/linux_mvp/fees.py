"""Modern CTF taker PAPER estimates, not exchange-exact charged fees."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
import json

MODEL = 'ctf-clob-v2-taker-level-floor5-v1'
STATUS = 'MARKET_SCHEDULE_ESTIMATE'
SOURCE = 'https://docs.polymarket.com/trading/fees.md'
FIELDS = ('version', 'feesEnabled', 'feeType', 'feeSchedule', 'updatedAt')
CUTOVER_US = 1777334400000000  # 2026-04-28 UTC; earlier regimes unsupported.


def fee_evidence(market, *, observed_us):
    raw = json.loads(json.dumps({k: market.metadata[k] for k in FIELDS if k in market.metadata},
                                default=str))
    result = dict(status='UNVERIFIED', reason='MISSING_OR_UNSUPPORTED_FEE_METADATA',
                  model=MODEL, source='Gamma market descriptor', documentation=SOURCE,
                  observed_us=observed_us, market_id=market.market_id,
                  condition_id=market.condition_id, tokens=[market.up.token_id, market.down.token_id],
                  window_start_us=int(market.window_start.timestamp() * 1_000_000),
                  window_end_us=int(market.window_end.timestamp() * 1_000_000),
                  raw=raw, role='taker', collateral='pUSD',
                  rounding='ROUND_DOWN_5DP_PER_VISIBLE_LEVEL',
                  aggregation='sum_level_fees', rebates='excluded', builder_fees='none')
    try:
        if (raw.get('version') != 'v1' or result['window_start_us'] < CUTOVER_US
                or not result['window_start_us'] <= observed_us < result['window_end_us']):
            return result
        enabled = raw.get('feesEnabled')
        schedule = raw.get('feeSchedule')
        if enabled is False and schedule is None:
            rate = Decimal(0)
        else:
            if enabled is not True or not isinstance(schedule, dict):
                return result
            value = schedule.get('rate')
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                return result
            rate = Decimal(str(value))
            if (not rate.is_finite() or not 0 <= rate <= 1
                    or type(schedule.get('exponent')) is not int or schedule['exponent'] != 1
                    or schedule.get('takerOnly') is not True):
                return result
        result.update(status=STATUS, reason='ROUNDING_AND_MATCH_BOUNDARIES_UNVERIFIED',
                      rate=str(rate), exponent=1)
    except (InvalidOperation, ValueError, TypeError):
        pass
    return result


def same_schedule(first, second):
    return (all(first.get(k) == second.get(k) for k in
                ('model', 'market_id', 'condition_id', 'tokens', 'rate', 'exponent', 'status'))
            and all(first.get('raw', {}).get(k) == second.get('raw', {}).get(k)
                    for k in ('version', 'feesEnabled', 'feeType', 'feeSchedule')))


def level_fee(evidence, quantity, price):
    from .store import ARITHMETIC
    if evidence.get('status') != STATUS or not quantity.is_finite() or quantity <= 0 or not price.is_finite() or not 0 <= price <= 1:
        raise ValueError('unverified fee or invalid execution level')
    with localcontext(ARITHMETIC):
        return (quantity * Decimal(evidence['rate']) * price * (1 - price)).quantize(
            Decimal('.00001'), rounding=ROUND_DOWN)


def reserve_per_share(evidence):
    # ponytail: exponent 1 only; extend bound with any future supported curve.
    if evidence.get('status') != STATUS:
        raise ValueError('unverified fee schedule')
    from .store import ARITHMETIC
    with localcontext(ARITHMETIC):
        return 1 + Decimal(evidence['rate']) / 4
