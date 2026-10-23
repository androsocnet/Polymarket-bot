"""Fail-closed verification of public resolution evidence; never infer from BTC prices."""
from datetime import datetime, timezone
import re

from ..market_data.models import MarketDataError
from ..market_data.numeric import load_numeric_json, parse_decimal


def _time_us(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    return int(moment.timestamp() * 1_000_000)


def verify_evidence(evidence: object, *, window: dict, tokens: dict, at_us: int) -> dict:
    """Require matching identities, binary unit payout, and agreeing final status.

    This validates source consistency, not independent on-chain finality. Deployment
    remains gated on verification of the public endpoint's final-resolution contract.
    """
    try:
        if not isinstance(evidence, dict):
            raise ValueError('missing evidence')
        gamma = load_numeric_json(evidence.get('gamma_raw', ''))
        envelope = load_numeric_json(evidence.get('resolution_raw', ''))
        if not isinstance(gamma, dict) or not isinstance(envelope, dict):
            raise ValueError('malformed evidence')
        if (gamma.get('id') != window['market_id']
                or gamma.get('conditionId') != window['condition_id']):
            raise ValueError('market identity mismatch')
        labels = load_numeric_json(gamma.get('outcomes', ''))
        ids = load_numeric_json(gamma.get('clobTokenIds', ''))
        if (not isinstance(labels, list) or not isinstance(ids, list)
                or len(labels) != 2 or len(ids) != 2
                or not all(isinstance(label, str) for label in labels)
                or not all(isinstance(token, str) and re.fullmatch(r'[0-9]{1,128}', token) for token in ids)
                or ids[0] == ids[1]):
            raise ValueError('invalid outcome-token mapping')
        labels = [label.strip().upper() for label in labels]
        if sorted(labels) != ['DOWN', 'UP'] or dict(zip(labels, ids)) != tokens:
            raise ValueError('recorded outcome-token identity mismatch')
        if _time_us(gamma.get('endDate')) != window['end_us'] or at_us < window['end_us']:
            raise ValueError('window time mismatch')
        expected_slug = 'btc-updown-15m-' + str(window['start_us'] // 1_000_000)
        if gamma.get('slug') != expected_slug:
            raise ValueError('window slug mismatch')
        if gamma.get('closed') is not True or gamma.get('umaResolutionStatus') != 'resolved':
            raise ValueError('Gamma not final')
        rows = envelope.get('data')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('missing, extra, or duplicate resolution')
        row = rows[0]
        if row.get('condition_id') != window['condition_id'] or row.get('status') != 'resolved':
            raise ValueError('resolution identity or status mismatch')
        if (row.get('extended_review') is not False or row.get('was_disputed') is not False
                or row.get('new_version_q') is not False):
            raise ValueError('review/dispute unresolved or unspecified')
        if not isinstance(row.get('transaction_hash'), str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', row['transaction_hash']):
            raise ValueError('invalid resolution transaction')
        block = row.get('resolved_block')
        if type(block) is not int or block <= 0:
            raise ValueError('missing resolution block')
        resolved_us = _time_us(row.get('resolved_at'))
        if not window['end_us'] <= resolved_us <= at_us:
            raise ValueError('resolution timestamp outside allowed interval')
        payouts = row.get('payouts')
        if not isinstance(payouts, list) or len(payouts) != 2:
            raise ValueError('invalid payout vector')
        payouts = [parse_decimal(p, field='payout') for p in payouts]
        # ponytail: binary 1e6-unit directional payouts only; add split redemption when specified/tested.
        if sorted(payouts) != [0, 1000000]:
            raise ValueError('non-unit, split, or unknown payout scale')
        prices = load_numeric_json(gamma.get('outcomePrices', ''))
        if not isinstance(prices, list) or len(prices) != 2:
            raise ValueError('missing final Gamma outcomes')
        prices = [parse_decimal(p, field='final outcome price') for p in prices]
        if prices != [p / 1000000 for p in payouts]:
            raise ValueError('contradictory final sources')
        return dict(status='RESOLVED', winning_outcome=labels[payouts.index(1000000)],
                    resolved_at=row['resolved_at'], tx_hash=row['transaction_hash'])
    except (ValueError, TypeError, KeyError, MarketDataError) as exc:
        return dict(status='UNKNOWN_SETTLEMENT', winning_outcome=None, diagnostic=str(exc))


def fetch_resolution(public, window: dict, *, deadline: float) -> dict:
    """Read exact Gamma identity and Data API resolution, retaining original response bytes."""
    market_id, condition = window['market_id'], window['condition_id']
    if not re.fullmatch(r'[0-9]{1,128}', market_id) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', condition):
        raise MarketDataError('identity_conflict', 'Invalid persisted market identity.')
    gamma = public.get_json('gamma', '/markets/' + market_id, deadline=deadline)
    resolution = public.get_json('data', '/v2/resolutions', params={'condition': condition}, deadline=deadline)
    return dict(gamma_raw=gamma.raw_body.decode('utf-8'),
                resolution_raw=resolution.raw_body.decode('utf-8'))
