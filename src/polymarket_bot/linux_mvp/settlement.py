"""Authoritative settlement logic for Polymarket BTC 15-minute markets."""
from decimal import Decimal
import re

from polymarket_bot.market_data.http import PublicGet
from polymarket_bot.market_data.models import MarketDataError


def classify_resolution(payload: object, *, condition_id: str, up_index: int = 0) -> dict:
    """Validate Data API v2 resolution payload and determine winning outcome."""
    if not isinstance(payload, dict):
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': 'not_a_dict'}

    if payload.get('condition_id') != condition_id:
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': 'condition_mismatch'}

    status = payload.get('status')
    if status != 'resolved':
        return {'status': 'PENDING', 'winning_outcome': None, 'diagnostic': f'status_{status}'}

    vector = payload.get('payouts')
    if not isinstance(vector, list) or len(vector) != 2:
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': 'invalid_payout_vector'}

    try:
        payouts = [Decimal(str(v)) for v in vector]
    except Exception:
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': 'non_numeric_payouts'}

    if any(p < 0 for p in payouts):
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': 'negative_payout'}

    positive = [idx for idx, p in enumerate(payouts) if p > 0]
    if len(positive) != 1:
        # Tie, split, 50/50, or dispute: unverified directional outcome
        return {'status': 'INVALID', 'winning_outcome': None, 'diagnostic': f'positive_count_{len(positive)}'}

    winning_outcome = 'UP' if positive[0] == up_index else 'DOWN'
    return {
        'status': 'RESOLVED',
        'winning_outcome': winning_outcome,
        'payouts': payouts,
        'resolved_at': payload.get('resolved_at'),
        'tx_hash': payload.get('transaction_hash'),
    }


def fetch_resolution(public: PublicGet, condition_id: str, *, deadline: float) -> dict | None:
    """Fetch public resolution row for one condition via /v2/resolutions."""
    if not re.fullmatch(r'0x[0-9a-fA-F]{64}', condition_id):
        return None
    try:
        read = public.get_json('data', '/v2/resolutions', params={'condition': condition_id},
                               deadline=deadline)
        payload = read.payload
        if isinstance(payload, dict) and isinstance(payload.get('data'), list):
            rows = payload['data']
            matches = [r for r in rows if isinstance(r, dict) and r.get('condition_id') == condition_id]
            if len(matches) == 1:
                return matches[0]
        return None
    except MarketDataError:
        return None
