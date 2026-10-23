"""Read-only performance report for the PAPER MVP. Self-reported state, clearly labelled."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from decimal import Decimal
import sqlite3
import time
from pathlib import Path

ZERO = Decimal(0)
SCHEMA = 'mvp-report-v1'

ASSUMPTIONS = (
    'paper fills walk visible book depth at the next fresh slot; queue position is not modelled',
    'fees are the configured constant assumption, not exchange-confirmed',
    'latency is the accepted snapshot cycle, not measured order-path latency',
    'settlement is never inferred; open positions are marked at the last fresh bid when available',
    'the report aggregates one paper account across service runs; run_id selects the run header',
)

LIMITATIONS = (
    'PAPER_ONLY: no order, credential, signing, or authenticated path exists in this package',
    'no profitability, win-rate, or live-readiness claim follows from these counts',
    'missing marks are flagged, not filled in; an unmarked book is not a price',
    'this report does not establish any CP5/CP6 strict-resource or long-window contract',
)


def _d(value) -> Decimal:
    return ZERO if value in (None, '') else Decimal(str(value))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def build_report(db_path: Path, *, run_id: str | None = None, now_us: int | None = None) -> dict:
    generated = time.time_ns() // 1000 if now_us is None else now_us
    with closing(_connect(db_path)) as conn:
        if run_id is None:
            run = conn.execute('SELECT run_id, bot_id, started_us, mode, initial_cash FROM runs '
                               'ORDER BY started_us DESC, run_id DESC LIMIT 1').fetchone()
        else:
            run = conn.execute('SELECT run_id, bot_id, started_us, mode, initial_cash FROM runs '
                               'WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise ValueError('no matching run recorded')
        run_id, bot_id, started_us, mode, initial_cash = run
        run_count = conn.execute('SELECT COUNT(*) FROM runs WHERE bot_id=?', (bot_id,)).fetchone()[0]

        windows = conn.execute('SELECT market_id, status, start_us, end_us FROM windows '
                               'WHERE bot_id=? ORDER BY start_us', (bot_id,)).fetchall()
        decisions = conn.execute('SELECT market_id, action, reason FROM decisions WHERE bot_id=?',
                                 (bot_id,)).fetchall()
        orders = conn.execute('SELECT market_id, status, side, outcome, notional, fee, realized, reason '
                              'FROM orders WHERE bot_id=?', (bot_id,)).fetchall()
        positions_rows = conn.execute('SELECT market_id, outcome, quantity, cost_basis FROM positions '
                                      'WHERE bot_id=?', (bot_id,)).fetchall()
        equity_rows = conn.execute('SELECT cash, exposure, unrealized, missing_marks FROM equity '
                                   'WHERE bot_id=? ORDER BY id', (bot_id,)).fetchall()

    actions = Counter(row[1] for row in decisions)
    decision_reasons = Counter(row[2] for row in decisions)
    statuses = Counter(row[1] for row in orders)
    sides = Counter(row[2] for row in orders)
    fees = sum((_d(row[5]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    filled_notional = sum((_d(row[4]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    realized = sum((_d(row[6]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    rejection_reasons = Counter(row[7] for row in orders if row[1] == 'REJECTED')
    open_positions = []
    unresolved_positions = []
    window_status_map = {row[0]: row[1] for row in windows}
    for market_id, outcome, quantity, cost_basis in positions_rows:
        q = _d(quantity)
        if q > ZERO:
            pos_dict = {
                'market_id': market_id,
                'outcome': outcome,
                'quantity': str(q),
                'cost_basis': str(_d(cost_basis)),
                'window_status': window_status_map.get(market_id, 'UNKNOWN'),
            }
            open_positions.append(pos_dict)
            if window_status_map.get(market_id) in ('ACTIVE', 'UNKNOWN_SETTLEMENT', None):
                unresolved_positions.append(pos_dict)
    per_window = []
    for market_id, status, start_us, end_us in windows:
        owned = [row for row in orders if row[0] == market_id]
        window_decisions = [row for row in decisions if row[0] == market_id]
        per_window.append({
            'market_id': market_id,
            'status': status,
            'start_us': start_us,
            'end_us': end_us,
            'decisions': len(window_decisions),
            'buys': sum(1 for row in owned if row[2] == 'BUY' and row[1] in ('FILLED', 'PARTIAL')),
            'sells': sum(1 for row in owned if row[2] == 'SELL' and row[1] in ('FILLED', 'PARTIAL')),
            'rejected': sum(1 for row in owned if row[1] == 'REJECTED'),
            'realized': str(sum((_d(row[6]) for row in owned), ZERO)),
        })

    last_equity = equity_rows[-1] if equity_rows else None
    net_points = [(_d(cash) + _d(exposure) + _d(unrealized))
                  for cash, exposure, unrealized, _ in equity_rows if unrealized not in (None, '')]
    peak = max(net_points) if net_points else None
    last_net = None
    if last_equity is not None and last_equity[2] not in (None, ''):
        last_net = _d(last_equity[0]) + _d(last_equity[1]) + _d(last_equity[2])
    drawdown = None if peak is None or last_net is None else max(ZERO, peak - last_net)
    last_unrealized = None if last_equity is None else (
        None if last_equity[2] in (None, '') else _d(last_equity[2]))

    return {
        'schema': SCHEMA,
        'mode': 'PAPER_ONLY',
        'execution': 'HYPOTHETICAL_PAPER_ONLY',
        'settlement': 'UNKNOWN_NOT_INFERRED',
        'scope': 'bot_account_across_runs',
        'bot_id': bot_id,
        'run_id': run_id,
        'generated_us': generated,
        'runs': run_count,
        'windows': {'total': len(windows), 'by_status': dict(Counter(row[1] for row in windows))},
        'decisions': {'total': len(decisions), 'by_action': dict(actions),
                      'by_reason': dict(decision_reasons)},
        'orders': {'total': len(orders), 'by_status': dict(statuses), 'by_side': dict(sides),
                   'fees': str(fees), 'filled_notional': str(filled_notional),
                   'realized': str(realized)},
        'rejections': dict(rejection_reasons),
        'positions': {
            'open': len(open_positions),
            'unresolved': len(unresolved_positions),
            'unknown_settlement': len(unresolved_positions),
            'items': open_positions,
        },
        'performance': {
            'realized_pnl': str(realized),
            'unrealized_pnl': None if last_unrealized is None else str(last_unrealized),
            'drawdown': None if drawdown is None else str(drawdown),
            'verified_settled_trades': 0,
            'win_rate_verified_settled': None,
        },
        'account': {
            'initial_cash': str(_d(initial_cash)),
            'cash': None if last_equity is None else str(_d(last_equity[0])),
            'exposure': None if last_equity is None else str(_d(last_equity[1])),
            'realized': str(realized),
            'unrealized': None if last_unrealized is None else str(last_unrealized),
            'net_value': None if last_net is None else str(last_net),
        },
        'equity': {
            'points': len(equity_rows),
            'peak_net': None if peak is None else str(peak),
            'last_net': None if last_net is None else str(last_net),
            'drawdown': None if drawdown is None else str(drawdown),
            'missing_marks_last': 0 if last_equity is None else int(last_equity[3]),
            'unmarked_points': sum(1 for row in equity_rows if int(row[3]) > 0),
        },
        'per_window': per_window,
        'assumptions': list(ASSUMPTIONS),
        'limitations': list(LIMITATIONS),
    }


def render(report: dict) -> str:
    account = report['account']
    equity = report['equity']
    lines = [
        f"PAPER_ONLY MVP report ({report['schema']})",
        f"bot={report['bot_id']} run={report['run_id']} runs={report['runs']} "
        f"execution={report['execution']}",
        f"windows: total={report['windows']['total']} by_status={report['windows']['by_status']}",
        f"decisions: total={report['decisions']['total']} by_action={report['decisions']['by_action']}",
        f"orders: total={report['orders']['total']} by_status={report['orders']['by_status']} "
        f"fees={report['orders']['fees']}",
        f"positions: open={report['positions']['open']} unresolved={report['positions']['unresolved']}",
        f"performance: win_rate_verified={report['performance']['win_rate_verified_settled']}",
        f"account: cash={account['cash']} exposure={account['exposure']} "
        f"realized={account['realized']} unrealized={account['unrealized']} "
        f"net_value={account['net_value']}",
        f"equity: points={equity['points']} peak_net={equity['peak_net']} "
        f"drawdown={equity['drawdown']} missing_marks_last={equity['missing_marks_last']}",
        f"settlement={report['settlement']}",
    ]
    for row in report['per_window']:
        lines.append(f"window {row['market_id']} [{row['status']}] decisions={row['decisions']} "
                     f"buys={row['buys']} sells={row['sells']} rejected={row['rejected']} "
                     f"realized={row['realized']}")
    lines.append('assumptions:')
    lines.extend(f"- {item}" for item in report['assumptions'])
    lines.append('limitations:')
    lines.extend(f"- {item}" for item in report['limitations'])
    return '\n'.join(lines) + '\n'
