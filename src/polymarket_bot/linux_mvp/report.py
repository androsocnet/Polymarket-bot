"""Read-only performance report for the PAPER MVP. Self-reported state, clearly labelled."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from decimal import Decimal, localcontext
from .store import ARITHMETIC
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
    with localcontext(ARITHMETIC):
        return _build_report(db_path, run_id=run_id, now_us=now_us)


def _build_report(db_path: Path, *, run_id: str | None, now_us: int | None) -> dict:
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
        conn.execute('BEGIN')
        run_id, bot_id, started_us, mode, initial_cash = run
        initial_cash = conn.execute('SELECT initial_cash FROM accounts WHERE bot_id=?', (bot_id,)).fetchone()[0]
        run_count = conn.execute('SELECT COUNT(*) FROM runs WHERE bot_id=?', (bot_id,)).fetchone()[0]

        windows = conn.execute('SELECT market_id, status, start_us, end_us FROM windows '
                               'WHERE bot_id=? ORDER BY start_us', (bot_id,)).fetchall()
        decisions = conn.execute('SELECT market_id, action, reason FROM decisions WHERE bot_id=?',
                                 (bot_id,)).fetchall()
        orders = conn.execute('SELECT market_id, status, side, outcome, notional, fee, realized, reason '
                              'FROM orders WHERE bot_id=?', (bot_id,)).fetchall()
        positions_rows = conn.execute('SELECT market_id, outcome, quantity, cost_basis FROM positions '
                                      'WHERE bot_id=?', (bot_id,)).fetchall()
        equity_rows = conn.execute('SELECT cash, exposure, unrealized, missing_marks, at_us FROM equity '
                                   'WHERE bot_id=? ORDER BY id', (bot_id,)).fetchall()
        columns = {r[1] for r in conn.execute('PRAGMA table_info(settlements)')}
        settlements_rows = []
        if columns:
            count = 'position_count' if 'position_count' in columns else 'NULL'
            settlements_rows = conn.execute(
                'SELECT market_id, status, winning_outcome, payout_total, realized_pnl, resolved_at, tx_hash, ' + count +
                ' FROM settlements WHERE bot_id=?', (bot_id,)).fetchall()

    actions = Counter(row[1] for row in decisions)
    decision_reasons = Counter(row[2] for row in decisions)
    statuses = Counter(row[1] for row in orders)
    sides = Counter(row[2] for row in orders)
    fees = sum((_d(row[5]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    filled_notional = sum((_d(row[4]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    order_realized = sum((_d(row[6]) for row in orders if row[1] in ('FILLED', 'PARTIAL')), ZERO)
    settled_realized = sum((_d(row[4]) for row in settlements_rows if row[1] == 'RESOLVED'), ZERO)
    total_realized = order_realized + settled_realized
    settled_wins = sum(1 for row in settlements_rows if row[1] == 'RESOLVED' and _d(row[4]) > ZERO)
    settled_losses = sum(1 for row in settlements_rows if row[1] == 'RESOLVED' and _d(row[4]) < ZERO)
    verified_settled_trades = (None if any(row[7] is None for row in settlements_rows) else
                              sum(row[7] for row in settlements_rows if row[1] == 'RESOLVED'))
    win_rate = (
        (Decimal(settled_wins) / Decimal(verified_settled_trades)).quantize(Decimal('0.0001'))
        if verified_settled_trades else None
    )
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
            if window_status_map.get(market_id) != 'SETTLED':
                unresolved_positions.append(pos_dict)
    per_window = []
    for market_id, status, start_us, end_us in windows:
        owned = [row for row in orders if row[0] == market_id]
        window_decisions = [row for row in decisions if row[0] == market_id]
        window_settled_pnl = sum((_d(row[4]) for row in settlements_rows if row[0] == market_id and row[1] == 'RESOLVED'), ZERO)
        per_window.append({
            'market_id': market_id,
            'status': status,
            'start_us': start_us,
            'end_us': end_us,
            'decisions': len(window_decisions),
            'buys': sum(1 for row in owned if row[2] == 'BUY' and row[1] in ('FILLED', 'PARTIAL')),
            'sells': sum(1 for row in owned if row[2] == 'SELL' and row[1] in ('FILLED', 'PARTIAL')),
            'rejected': sum(1 for row in owned if row[1] == 'REJECTED'),
            'realized': str(sum((_d(row[6]) for row in owned), ZERO) + window_settled_pnl),
        })

    cash = _d(initial_cash)
    for row in orders:
        if row[1] in ('FILLED', 'PARTIAL'):
            cash += (-_d(row[4]) - _d(row[5])) if row[2] == 'BUY' else (_d(row[4]) - _d(row[5]))
    cash += sum((_d(row[3]) for row in settlements_rows if row[1] == 'RESOLVED'), ZERO)
    basis = sum((_d(p['cost_basis']) for p in open_positions), ZERO)
    reconciled = cash + basis == _d(initial_cash) + total_realized
    last_equity = equity_rows[-1] if equity_rows else None
    net_points = [(_d(cash) + _d(exposure) + _d(unrealized))
                  for cash, exposure, unrealized, _, at_us in equity_rows if unrealized not in (None, '')]
    peak = max(net_points) if net_points else None
    last_net = None
    if last_equity is not None and last_equity[2] not in (None, ''):
        last_net = _d(last_equity[0]) + _d(last_equity[1]) + _d(last_equity[2])
    drawdown = None if peak is None or last_net is None else max(ZERO, peak - last_net)
    last_unrealized = None if last_equity is None else (
        None if last_equity[2] in (None, '') else _d(last_equity[2]))
    if not open_positions:
        last_unrealized, last_net = ZERO, cash
    elif (last_equity is None or _d(last_equity[0]) != cash or _d(last_equity[1]) != basis
          or not 0 <= generated - last_equity[4] <= 5_000_000):
        last_unrealized, last_net = None, None
    drawdown = None if peak is None or last_net is None else max(ZERO, peak - last_net)
    if not open_positions:
        net_points.append(cash)
    running_peak, maximum_drawdown = _d(initial_cash), ZERO
    for point in net_points:
        running_peak = max(running_peak, point)
        maximum_drawdown = max(maximum_drawdown, running_peak - point)

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
                   'realized': str(order_realized)},
        'rejections': dict(rejection_reasons),
        'positions': {
            'open': len(open_positions),
            'unresolved': len(unresolved_positions),
            'unknown_settlement': len(unresolved_positions),
            'items': open_positions,
        },
        'performance': {
            'realized_pnl': str(total_realized),
            'unrealized_pnl': None if last_unrealized is None else str(last_unrealized),
            'drawdown': None if drawdown is None else str(drawdown),
            'max_drawdown': str(maximum_drawdown),
            'max_drawdown_complete': False,  # sampled marks cannot prove intra-cycle extrema
            'drawdown_coverage': 'SAMPLED_MARKS_ONLY',
            'verified_settled_trades': verified_settled_trades,
            'verified_wins': settled_wins,
            'verified_losses': settled_losses,
            'win_rate_verified_settled': None if win_rate is None else str(win_rate),
        },
        'account': {
            'initial_cash': str(_d(initial_cash)),
            'cash': str(cash),
            'exposure': str(basis),
            'reconciled': reconciled,
            'realized': str(total_realized),
            'unrealized': None if last_unrealized is None else str(last_unrealized),
            'net_value': None if last_net is None else str(last_net),
        },
        'equity': {
            'points': len(equity_rows),
            'mark_at_us': None if last_equity is None else last_equity[4],
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
