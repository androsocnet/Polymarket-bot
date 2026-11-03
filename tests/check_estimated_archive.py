"""Read-only captured-book smoke check, NOT fixed-strategy replay or P&L.

Run: PYTHONPATH=src python -B tests/check_estimated_archive.py ARCHIVE_ROOT
Only raw-probe-{0,1,2}.json read; each isolated probe is separate session.
Hypothetical DOWN BUY 5 at limit .60 submitted at Gamma receipt, not strategy
signal. Metadata already known then; subsequent book must follow order delay.
"""
import argparse
from datetime import datetime
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.estimated import Book, Estimator, Level, Order, Scenario
from polymarket_bot.linux_mvp.fees import fee_evidence


def check(root):
    scenarios = {
        'base_250ms': Scenario(max_spread=D('.04')),
        'delay_500ms': Scenario(exchange_delay_ns=250_000_000, local_delay_ns=250_000_000,
                              max_spread=D('.04')),
        'pessimistic_1000ms': Scenario(exchange_delay_ns=250_000_000, local_delay_ns=750_000_000,
                                     depth_fraction=D('.5'), extra_cost_bps=D('25'), max_spread=D('.04')),
        'tight_spread_250ms': Scenario(max_spread=D('.005')),
    }
    output = []
    for index in range(3):
        path = root / f'raw-probe-{index}.json'
        data = json.loads(path.read_text())
        snapshot = data['snapshot']
        market = dict(snapshot['market'])
        market['window_start'] = datetime.fromisoformat(market['window_start'])
        market['window_end'] = datetime.fromisoformat(market['window_end'])
        market['up'] = SimpleNamespace(**market['up'])
        market['down'] = SimpleNamespace(**market['down'])
        gamma = data['http_attempts'][1]
        assert gamma['result'] == 'success' and gamma['raw_complete']
        fees = fee_evidence(SimpleNamespace(**market), observed_us=gamma['ended_wall_us'])
        observation = snapshot['down']
        payload = observation['timing']['payload']
        assert payload['asset_id'] == market['down'].token_id
        assert payload['market'] == market['condition_id']
        book = Book(session=f'probe-{index}', market_id=market['market_id'],
                    condition_id=market['condition_id'], token_id=market['down'].token_id,
                    received_ns=observation['timing']['finished_mono_ns'],
                    bids=tuple(Level(D(x['price']), D(x['size'])) for x in payload['bids']),
                    asks=tuple(Level(D(x['price']), D(x['size'])) for x in payload['asks']),
                    min_size=D(payload['min_order_size']), tick_size=D(payload['tick_size']),
                    fees=fees, fee_received_ns=gamma['ended_mono_ns'],
                    received_wall_us=observation['timing']['finished_wall_us'])
        order = Order(session=book.session, market_id=book.market_id, condition_id=book.condition_id,
                      token_id=book.token_id, submitted_ns=gamma['ended_mono_ns'],
                      deadline_ns=gamma['ended_mono_ns'] + 2_000_000_000,
                      side='BUY', quantity=D('5'), limit=D('.60'))
        for name, scenario in scenarios.items():
            result = Estimator().execute(order, [book], scenario, cash=D('100'), held=D('0'))
            assert not result['certified']
            if result['filled']:
                assert result['received_ns'] >= result['ready_ns']
                assert sum(q for _, q in result['fills']) == result['filled'] <= order.quantity
                assert all(p <= order.limit for p, _ in result['fills'])
            output.append(dict(probe=index, scenario=name, **result))
    return dict(scope='ISOLATED_CAPTURED_BOOK_SMOKE_NOT_STRATEGY_REPLAY',
                net_pnl=None, drawdown=None, results=output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive_root', type=Path)
    print(json.dumps(check(parser.parse_args().archive_root), default=str, indent=2))
