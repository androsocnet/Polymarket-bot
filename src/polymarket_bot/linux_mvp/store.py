"""SQLite state for the PAPER_ONLY engine: idempotent writes, one writer, exactly-once fills."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import json
from pathlib import Path
import sqlite3

SCHEMA = 'mvp-v1'
ARITHMETIC = Context(prec=512, rounding=ROUND_HALF_EVEN)
ZERO = Decimal(0)
WINDOW_STATUSES = ('ACTIVE', 'CLOSED', 'ABORTED', 'UNKNOWN_SETTLEMENT', 'SETTLED')

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    started_us INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode = 'PAPER_ONLY'),
    initial_cash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    schema TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
    bot_id TEXT PRIMARY KEY,
    initial_cash TEXT NOT NULL,
    created_us INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS windows(
    bot_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','CLOSED','ABORTED','UNKNOWN_SETTLEMENT','SETTLED')),
    note TEXT NOT NULL DEFAULT '',
    updated_us INTEGER NOT NULL,
    PRIMARY KEY(bot_id, market_id));
CREATE TABLE IF NOT EXISTS observations(
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('UP','DOWN')),
    token_id TEXT NOT NULL,
    bid TEXT,
    ask TEXT,
    midpoint TEXT,
    source_at INTEGER NOT NULL,
    received_us INTEGER NOT NULL,
    PRIMARY KEY(bot_id, market_id, slot_index, outcome));
CREATE TABLE IF NOT EXISTS decisions(
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    signal TEXT NOT NULL,
    momentum TEXT,
    mid_up TEXT,
    cash TEXT NOT NULL,
    exposure TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    PRIMARY KEY(bot_id, market_id, slot_id));
CREATE TABLE IF NOT EXISTS orders(
    order_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('UP','DOWN')),
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    quantity TEXT NOT NULL,
    decision_slot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','FILLED','PARTIAL','REJECTED')),
    reason TEXT NOT NULL,
    filled TEXT NOT NULL DEFAULT '0',
    notional TEXT NOT NULL DEFAULT '0',
    fee TEXT NOT NULL DEFAULT '0',
    realized TEXT NOT NULL DEFAULT '0',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS positions(
    bot_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('UP','DOWN')),
    quantity TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    PRIMARY KEY(bot_id, market_id, outcome));
CREATE TABLE IF NOT EXISTS equity(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    market_id TEXT,
    cash TEXT NOT NULL,
    exposure TEXT NOT NULL,
    realized TEXT NOT NULL,
    unrealized TEXT,
    missing_marks INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeat(
    bot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    market_id TEXT,
    status TEXT NOT NULL,
    cycles INTEGER NOT NULL,
    last_error TEXT);
CREATE TABLE IF NOT EXISTS settlements(
    bot_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    status TEXT NOT NULL,
    winning_outcome TEXT,
    payout_total TEXT NOT NULL DEFAULT '0',
    realized_pnl TEXT NOT NULL DEFAULT '0',
    resolved_at TEXT,
    tx_hash TEXT,
    settled_us INTEGER NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    position_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(bot_id, market_id));
CREATE TABLE IF NOT EXISTS resolution_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    at_us INTEGER NOT NULL,
    next_us INTEGER NOT NULL,
    status TEXT NOT NULL,
    diagnostic TEXT NOT NULL,
    evidence_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_status ON orders(bot_id, status);
CREATE INDEX IF NOT EXISTS observations_market ON observations(bot_id, market_id, slot_index);
CREATE INDEX IF NOT EXISTS decisions_market ON decisions(bot_id, market_id, slot_index);
"""

_MONEY_COLUMNS = ('quantity', 'filled', 'notional', 'fee', 'realized')
_ORDER_COLUMNS = ('order_id', 'bot_id', 'run_id', 'market_id', 'token_id', 'outcome', 'side',
                  'quantity', 'decision_slot_id', 'status', 'reason', 'filled', 'notional',
                  'fee', 'realized', 'created_us', 'updated_us')


def _d(value) -> Decimal:
    return ZERO if value is None else Decimal(str(value))


def _row(row, columns) -> dict | None:
    return None if row is None else dict(zip(columns, row))


class MvpStore:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path = Path(path)
        self._conn = connection

    @classmethod
    def open(cls, path: Path) -> 'MvpStore':
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(_SCHEMA_SQL)
        try:
            conn.execute('BEGIN IMMEDIATE')
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='windows'").fetchone()[0]
            if "'SETTLED'" not in sql:
                conn.execute(sql.replace('CREATE TABLE windows', 'CREATE TABLE windows_new', 1)
                             .replace("'UNKNOWN_SETTLEMENT')", "'UNKNOWN_SETTLEMENT','SETTLED')"))
                conn.execute('INSERT INTO windows_new SELECT * FROM windows')
                conn.execute('DROP TABLE windows')
                conn.execute('ALTER TABLE windows_new RENAME TO windows')
            columns = {r[1] for r in conn.execute('PRAGMA table_info(settlements)')}
            if 'evidence_json' not in columns:
                conn.execute("ALTER TABLE settlements ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
            if 'position_count' not in columns:
                conn.execute('ALTER TABLE settlements ADD COLUMN position_count INTEGER')
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            conn.close()
            raise
        return cls(path, conn)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self):
        if self._conn is None:
            raise RuntimeError('store is closed')
        self._conn.execute('BEGIN IMMEDIATE')
        try:
            yield self._conn
        except BaseException:
            self._conn.execute('ROLLBACK')
            raise
        else:
            self._conn.execute('COMMIT')

    @property
    def schema_version(self) -> str:
        row = self._conn.execute("SELECT schema FROM runs ORDER BY started_us DESC LIMIT 1").fetchone()
        return SCHEMA if row is None else row[0]

    @property
    def journal_mode(self) -> str:
        return self._conn.execute('PRAGMA journal_mode').fetchone()[0]

    @property
    def synchronous(self) -> int:
        return self._conn.execute('PRAGMA synchronous').fetchone()[0]

    def table_names(self) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    # -- runs / account ------------------------------------------------------
    def start_run(self, run_id: str, bot_id: str, *, initial_cash: Decimal, config: dict,
                  started_us: int, mode: str = 'PAPER_ONLY') -> None:
        with self.transaction() as conn:
            conn.execute('INSERT INTO runs(run_id, bot_id, started_us, mode, initial_cash, config_json, schema) '
                         'VALUES(?,?,?,?,?,?,?)',
                         (run_id, bot_id, started_us, mode, str(initial_cash),
                          json.dumps(config, sort_keys=True, default=str), SCHEMA))
            conn.execute('INSERT OR IGNORE INTO accounts(bot_id, initial_cash, created_us) VALUES(?,?,?)',
                         (bot_id, str(initial_cash), started_us))

    # -- windows -------------------------------------------------------------
    def ensure_window(self, bot_id: str, run_id: str, *, market_id: str, condition_id: str,
                      event_id: str, start_us: int, end_us: int, at_us: int) -> bool:
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO windows(bot_id, market_id, run_id, condition_id, event_id, '
            'start_us, end_us, status, note, updated_us) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (bot_id, market_id, run_id, condition_id, event_id, start_us, end_us, 'ACTIVE', '', at_us))
        return cur.rowcount == 1

    def close_window(self, bot_id: str, market_id: str, status: str, *, at_us: int, note: str) -> None:
        if status not in WINDOW_STATUSES:
            raise sqlite3.IntegrityError(f'invalid window status {status!r}')
        self._conn.execute('UPDATE windows SET status=?, note=?, updated_us=? '
                           'WHERE bot_id=? AND market_id=?', (status, note, at_us, bot_id, market_id))

    def window(self, bot_id: str, market_id: str) -> dict | None:
        cur = self._conn.execute('SELECT * FROM windows WHERE bot_id=? AND market_id=?', (bot_id, market_id))
        return _row(cur.fetchone(), [c[0] for c in cur.description])

    def active_window(self, bot_id: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM windows WHERE bot_id=? AND status='ACTIVE' "
                                 'ORDER BY start_us DESC LIMIT 1', (bot_id,))
        return _row(cur.fetchone(), [c[0] for c in cur.description])

    # -- observations / decisions -------------------------------------------
    def observe(self, bot_id: str, run_id: str, market_id: str, slot_index: int, outcome: str,
                token_id: str, bid: Decimal | None, ask: Decimal | None, midpoint: Decimal | None,
                source_at: int, received_us: int) -> bool:
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO observations(bot_id, run_id, market_id, slot_index, outcome, token_id, '
            'bid, ask, midpoint, source_at, received_us) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (bot_id, run_id, market_id, slot_index, outcome, token_id,
             None if bid is None else str(bid), None if ask is None else str(ask),
             None if midpoint is None else str(midpoint), source_at, received_us))
        return cur.rowcount == 1

    def observations_for_market(self, bot_id: str, market_id: str) -> list[dict]:
        cur = self._conn.execute('SELECT * FROM observations WHERE bot_id=? AND market_id=? '
                                 'ORDER BY slot_index, outcome', (bot_id, market_id))
        columns = [c[0] for c in cur.description]
        rows = [_row(r, columns) for r in cur.fetchall()]
        for row in rows:
            for key in ('bid', 'ask', 'midpoint'):
                row[key] = None if row[key] is None else Decimal(row[key])
        return rows

    def decide(self, bot_id: str, run_id: str, market_id: str, slot_index: int, slot_id: str,
               action: str, reason: str, signal: str, momentum: Decimal | None,
               mid_up: Decimal | None, cash: Decimal, exposure: Decimal,
               inputs: dict, at_us: int) -> bool:
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO decisions(bot_id, run_id, market_id, slot_id, slot_index, action, reason, '
            'signal, momentum, mid_up, cash, exposure, inputs_json, at_us) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (bot_id, run_id, market_id, slot_id, slot_index, action, reason, signal,
             None if momentum is None else str(momentum), None if mid_up is None else str(mid_up),
             str(cash), str(exposure), json.dumps(inputs, sort_keys=True, default=str), at_us))
        return cur.rowcount == 1

    def decision(self, bot_id: str, market_id: str, slot_id: str) -> dict | None:
        cur = self._conn.execute('SELECT * FROM decisions WHERE bot_id=? AND market_id=? AND slot_id=?',
                                 (bot_id, market_id, slot_id))
        return _row(cur.fetchone(), [c[0] for c in cur.description])

    def recent_decisions(self, bot_id: str, market_id: str, limit: int) -> list[dict]:
        cur = self._conn.execute(
            'SELECT * FROM decisions WHERE bot_id=? AND market_id=? ORDER BY slot_index DESC LIMIT ?',
            (bot_id, market_id, limit))
        columns = [c[0] for c in cur.description]
        return list(reversed([_row(r, columns) for r in cur.fetchall()]))

    def decisions_for_market(self, bot_id: str, market_id: str) -> list[dict]:
        cur = self._conn.execute('SELECT * FROM decisions WHERE bot_id=? AND market_id=? '
                                 'ORDER BY slot_index', (bot_id, market_id))
        columns = [c[0] for c in cur.description]
        return [_row(r, columns) for r in cur.fetchall()]

    # -- orders / positions --------------------------------------------------
    def queue_order(self, order_id: str, bot_id: str, run_id: str, market_id: str, token_id: str,
                    outcome: str, side: str, quantity: Decimal, decision_slot_id: str,
                    reason: str, at_us: int) -> bool:
        cur = self._conn.execute(
            'INSERT OR IGNORE INTO orders(order_id, bot_id, run_id, market_id, token_id, outcome, side, '
            'quantity, decision_slot_id, status, reason, created_us, updated_us) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (order_id, bot_id, run_id, market_id, token_id, outcome, side, str(quantity),
             decision_slot_id, 'PENDING', reason, at_us, at_us))
        return cur.rowcount == 1

    def _order_from_row(self, row) -> dict | None:
        if row is None:
            return None
        order = dict(zip(_ORDER_COLUMNS, row))
        for key in _MONEY_COLUMNS:
            order[key] = _d(order[key])
        return order

    def order(self, order_id: str) -> dict | None:
        return self._order_from_row(self._conn.execute('SELECT * FROM orders WHERE order_id=?',
                                                       (order_id,)).fetchone())

    def pending_order(self, bot_id: str) -> dict | None:
        return self._order_from_row(self._conn.execute(
            "SELECT * FROM orders WHERE bot_id=? AND status='PENDING' ORDER BY created_us, order_id LIMIT 1",
            (bot_id,)).fetchone())

    def orders(self, bot_id: str, market_id: str | None = None) -> list[dict]:
        sql = 'SELECT order_id FROM orders WHERE bot_id=?'
        args = [bot_id]
        if market_id is not None:
            sql += ' AND market_id=?'
            args.append(market_id)
        sql += ' ORDER BY created_us, order_id'
        return [self.order(r[0]) for r in self._conn.execute(sql, args)]

    def position(self, bot_id: str, market_id: str, outcome: str) -> dict:
        row = self._conn.execute('SELECT quantity, cost_basis FROM positions WHERE bot_id=? AND market_id=? '
                                 'AND outcome=?', (bot_id, market_id, outcome)).fetchone()
        return {'quantity': ZERO if row is None else _d(row[0]),
                'cost_basis': ZERO if row is None else _d(row[1])}

    def positions(self, bot_id: str, market_id: str) -> dict[str, dict]:
        return {outcome: self.position(bot_id, market_id, outcome) for outcome in ('UP', 'DOWN')}

    def open_positions(self, bot_id: str) -> list[dict]:
        rows = self._conn.execute('SELECT market_id, outcome, quantity, cost_basis FROM positions '
                                  'WHERE bot_id=? ORDER BY market_id, outcome', (bot_id,)).fetchall()
        return [{'market_id': market_id, 'outcome': outcome, 'quantity': _d(quantity),
                 'cost_basis': _d(cost_basis)}
                for market_id, outcome, quantity, cost_basis in rows if _d(quantity) > 0]

    def _cash_in(self, conn, bot_id: str) -> Decimal:
        with localcontext(ARITHMETIC):
            return self._cash_sum(conn, bot_id)

    def _cash_sum(self, conn, bot_id: str) -> Decimal:
        row = conn.execute('SELECT initial_cash FROM accounts WHERE bot_id=?', (bot_id,)).fetchone()
        cash = ZERO if row is None else _d(row[0])
        for side, status, notional, fee in conn.execute(
                "SELECT side, status, notional, fee FROM orders WHERE bot_id=? AND status IN ('FILLED','PARTIAL')",
                (bot_id,)):
            if side == 'BUY':
                cash -= _d(notional) + _d(fee)
            else:
                cash += _d(notional) - _d(fee)
        settled_payouts = conn.execute(
            "SELECT payout_total FROM settlements WHERE bot_id=? AND status='RESOLVED'", (bot_id,)).fetchall()
        for payout_row in settled_payouts:
            cash += _d(payout_row[0])
        return cash

    def cash(self, bot_id: str) -> Decimal:
        return self._cash_in(self._conn, bot_id)

    def realized(self, bot_id: str, *, since_us: int | None = None) -> Decimal:
        with localcontext(ARITHMETIC):
            return self._realized_sum(bot_id, since_us=since_us)

    def _realized_sum(self, bot_id: str, *, since_us: int | None) -> Decimal:
        sql = ("SELECT realized FROM orders WHERE bot_id=? AND side='SELL' AND status IN ('FILLED','PARTIAL')")
        args = [bot_id]
        if since_us is not None:
            sql += ' AND updated_us>=?'
            args.append(since_us)
        order_realized = sum((_d(r[0]) for r in self._conn.execute(sql, args)), ZERO)
        settle_sql = "SELECT realized_pnl FROM settlements WHERE bot_id=? AND status='RESOLVED'"
        settle_args = [bot_id]
        if since_us is not None:
            settle_sql += ' AND settled_us>=?'
            settle_args.append(since_us)
        settled_realized = sum((_d(r[0]) for r in self._conn.execute(settle_sql, settle_args)), ZERO)
        return order_realized + settled_realized

    def unsettled_windows(self, bot_id: str, *, at_us: int) -> list[dict]:
        cur = self._conn.execute(
            "SELECT w.* FROM windows w WHERE w.bot_id=? AND w.status!='SETTLED' AND w.end_us<=? "
            "AND NOT EXISTS (SELECT 1 FROM resolution_attempts a WHERE a.bot_id=w.bot_id "
            "AND a.market_id=w.market_id AND a.next_us>?) ORDER BY w.start_us LIMIT 1",
            (bot_id, at_us, at_us))
        cols = [c[0] for c in cur.description]
        return [_row(r, cols) for r in cur.fetchall()]

    def settlements(self, bot_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT bot_id, market_id, condition_id, status, winning_outcome, payout_total, realized_pnl, "
            "resolved_at, tx_hash, settled_us, evidence_json, position_count FROM settlements "
            "WHERE bot_id=? ORDER BY settled_us", (bot_id,))
        cols = [c[0] for c in cur.description]
        return [_row(r, cols) for r in cur.fetchall()]

    def settle_window(self, bot_id: str, market_id: str, *, evidence: dict, at_us: int) -> dict:
        from .settlement import verify_evidence
        with self.transaction() as conn, localcontext(ARITHMETIC):
            existing = conn.execute(
                "SELECT status, winning_outcome, payout_total, realized_pnl FROM settlements "
                "WHERE bot_id=? AND market_id=?", (bot_id, market_id)).fetchone()
            if existing is not None:
                return {
                    'status': existing[0],
                    'winning_outcome': existing[1],
                    'payout_total': _d(existing[2]),
                    'realized_pnl': _d(existing[3]),
                    'already_settled': True,
                }
            window = self.window(bot_id, market_id)
            if window is None:
                raise ValueError(f'window {market_id} not found')
            identities = conn.execute(
                'SELECT DISTINCT outcome, token_id FROM observations WHERE bot_id=? AND market_id=?',
                (bot_id, market_id)).fetchall()
            tokens = dict(identities) if len(identities) == 2 else {}
            classified = verify_evidence(evidence, window=window, tokens=tokens, at_us=at_us)
            if window['status'] == 'ACTIVE':
                classified = dict(status='UNKNOWN_SETTLEMENT', diagnostic='active window')
            orders = conn.execute('SELECT outcome, token_id FROM orders WHERE bot_id=? AND market_id=?',
                                  (bot_id, market_id)).fetchall()
            if any(tokens.get(outcome) != token for outcome, token in orders):
                classified = dict(status='UNKNOWN_SETTLEMENT', diagnostic='order token mismatch')
            attempts = conn.execute('SELECT COUNT(*) FROM resolution_attempts WHERE bot_id=? AND market_id=?',
                                    (bot_id, market_id)).fetchone()[0]
            conn.execute('INSERT INTO resolution_attempts(bot_id,market_id,at_us,next_us,status,diagnostic,evidence_json) '
                         'VALUES(?,?,?,?,?,?,?)',
                         (bot_id, market_id, at_us, at_us + min(300, 10 * 2 ** min(attempts, 5)) * 1_000_000,
                          classified['status'], classified.get('diagnostic', ''), json.dumps(evidence, sort_keys=True)))
            if classified['status'] != 'RESOLVED':
                return dict(status='UNKNOWN_SETTLEMENT', winning_outcome=None,
                            payout_total=ZERO, realized_pnl=ZERO, already_settled=False)
            winning_outcome = classified['winning_outcome']
            pos_rows = [r for r in conn.execute(
                'SELECT outcome, quantity, cost_basis FROM positions WHERE bot_id=? AND market_id=?',
                (bot_id, market_id)) if _d(r[1]) > 0]
            total_payout = ZERO
            total_pnl = ZERO
            for outcome, qty_str, basis_str in pos_rows:
                qty = _d(qty_str)
                basis = _d(basis_str)
                payout = qty * Decimal('1.00') if outcome == winning_outcome else ZERO
                pnl = payout - basis
                total_payout += payout
                total_pnl += pnl
                conn.execute("UPDATE positions SET quantity='0', cost_basis='0' "
                             "WHERE bot_id=? AND market_id=? AND outcome=?",
                             (bot_id, market_id, outcome))

            conn.execute("UPDATE windows SET status='SETTLED', note=?, updated_us=? "
                         "WHERE bot_id=? AND market_id=?",
                         (f'SETTLED_{winning_outcome}', at_us, bot_id, market_id))
            conn.execute(
                "INSERT INTO settlements(bot_id, market_id, condition_id, status, winning_outcome, "
                "payout_total, realized_pnl, resolved_at, tx_hash, settled_us, evidence_json, position_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (bot_id, market_id, window['condition_id'], 'RESOLVED', winning_outcome,
                 str(total_payout), str(total_pnl), classified['resolved_at'], classified['tx_hash'], at_us,
                 json.dumps(evidence, sort_keys=True), len(pos_rows)))
            conn.execute(
                "INSERT INTO events(bot_id, run_id, at_us, kind, detail) VALUES(?,?,?,?,?)",
                (bot_id, window['run_id'], at_us, 'window_settled',
                 f'{market_id}:{winning_outcome}:payout={total_payout}:pnl={total_pnl}'))
            self.reconcile(bot_id)
            positions = self.open_positions(bot_id)
            self.equity(bot_id, window['run_id'], at_us=at_us, market_id=market_id,
                        cash=self.cash(bot_id), exposure=sum((p['cost_basis'] for p in positions), ZERO),
                        realized=self.realized(bot_id), unrealized=None if positions else ZERO,
                        missing_marks=len(positions))
            return {
                'status': 'RESOLVED',
                'winning_outcome': winning_outcome,
                'payout_total': total_payout,
                'realized_pnl': total_pnl,
                'already_settled': False,
            }

    def reconcile(self, bot_id: str) -> None:
        with localcontext(ARITHMETIC):
            initial = self._conn.execute('SELECT initial_cash FROM accounts WHERE bot_id=?', (bot_id,)).fetchone()
            basis = sum((p['cost_basis'] for p in self.open_positions(bot_id)), ZERO)
            if initial is None or self.cash(bot_id) + basis != _d(initial[0]) + self.realized(bot_id):
                raise sqlite3.IntegrityError('paper ledger reconciliation failed')

    def trade_count(self, bot_id: str, market_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE bot_id=? AND market_id=? AND side='BUY' "
            "AND status IN ('FILLED','PARTIAL')", (bot_id, market_id)).fetchone()
        return int(row[0])

    def entry_order(self, bot_id: str, market_id: str, outcome: str) -> dict | None:
        return self._order_from_row(self._conn.execute(
            "SELECT * FROM orders WHERE bot_id=? AND market_id=? AND outcome=? AND side='BUY' "
            "AND status IN ('FILLED','PARTIAL') ORDER BY updated_us DESC, order_id DESC LIMIT 1",
            (bot_id, market_id, outcome)).fetchone())

    def reject_order(self, order_id: str, reason: str, *, at_us: int) -> bool:
        cur = self._conn.execute("UPDATE orders SET status='REJECTED', reason=?, updated_us=? "
                                 "WHERE order_id=? AND status='PENDING'", (reason, at_us, order_id))
        return cur.rowcount == 1

    def execute_pending(self, bot_id: str, books: dict, fee_bps: Decimal, *, at_us: int) -> dict | None:
        """Execute the oldest PENDING order exactly once against the supplied books.

        Price-time walks visible depth; a short book leaves a terminal PARTIAL (the remainder is
        abandoned, never retried) and an empty book is a terminal REJECTED with no position change.
        """
        with self.transaction() as conn, localcontext(ARITHMETIC):
            self.reconcile(bot_id)
            row = conn.execute("SELECT order_id FROM orders WHERE bot_id=? AND status='PENDING' "
                               'ORDER BY created_us, order_id LIMIT 1', (bot_id,)).fetchone()
            if row is None:
                return None
            order = self._order_from_row(conn.execute('SELECT * FROM orders WHERE order_id=?',
                                                      (row[0],)).fetchone())
            book = books.get(order['outcome'])
            window = self.window(bot_id, order['market_id'])
            if book is not None and (book.token_id != order['token_id'] or window is None
                                     or book.condition_id != window['condition_id']):
                raise sqlite3.IntegrityError('execution book identity mismatch')
            quantity = order['quantity']
            cash = self._cash_in(conn, bot_id)
            position = conn.execute('SELECT quantity, cost_basis FROM positions WHERE bot_id=? AND '
                                    'market_id=? AND outcome=?',
                                    (bot_id, order['market_id'], order['outcome'])).fetchone()
            held = ZERO if position is None else _d(position[0])
            basis = ZERO if position is None else _d(position[1])
            filled, notional, realized = ZERO, ZERO, ZERO
            reason = order['reason']
            available = quantity
            if order['side'] == 'SELL':
                available = min(quantity, held)
            with localcontext(ARITHMETIC):
                if book is not None and available > 0:
                    levels = sorted(book.asks if order['side'] == 'BUY' else book.bids,
                                    key=lambda level: level.price, reverse=order['side'] == 'SELL')
                    for level in levels:
                        if filled >= available or level.size <= 0:
                            break
                        take = min(available - filled, level.size)
                        notional += take * level.price
                        filled += take
                fee = (notional * fee_bps / Decimal(10000)) if filled > 0 else ZERO
                if order['side'] == 'BUY' and filled > 0 and notional + fee > cash:
                    filled, notional, fee = ZERO, ZERO, ZERO
                    reason = 'INSUFFICIENT_CASH'
                if order['side'] == 'SELL' and held <= 0:
                    reason = 'INSUFFICIENT_POSITION'
                if filled == 0:
                    status = 'REJECTED'
                    if reason == order['reason']:
                        reason = 'INSUFFICIENT_LIQUIDITY'
                elif filled < quantity:
                    status = 'PARTIAL'
                    reason = 'PARTIAL_BOOK'
                else:
                    status = 'FILLED'
                if order['side'] == 'BUY' and filled > 0:
                    held += filled
                    basis += notional + fee
                elif order['side'] == 'SELL' and filled > 0:
                    allocated = basis * filled / held
                    realized = notional - fee - allocated
                    held -= filled
                    basis -= allocated
            conn.execute('INSERT INTO positions(bot_id, market_id, outcome, quantity, cost_basis) '
                         'VALUES(?,?,?,?,?) ON CONFLICT(bot_id, market_id, outcome) DO UPDATE SET '
                         'quantity=excluded.quantity, cost_basis=excluded.cost_basis',
                         (bot_id, order['market_id'], order['outcome'], str(held), str(basis)))
            conn.execute('UPDATE orders SET status=?, reason=?, filled=?, notional=?, fee=?, realized=?, '
                         'updated_us=? WHERE order_id=?',
                         (status, reason, str(filled), str(notional), str(fee), str(realized), at_us,
                          order['order_id']))
            self.reconcile(bot_id)
            return {'order_id': order['order_id'], 'status': status, 'reason': reason,
                    'filled': filled, 'notional': notional, 'fee': fee, 'realized': realized}

    # -- marks / events / heartbeat -----------------------------------------
    def equity(self, bot_id: str, run_id: str, *, at_us: int, market_id: str | None, cash: Decimal,
               exposure: Decimal, realized: Decimal, unrealized: Decimal | None,
               missing_marks: int) -> None:
        self._conn.execute('INSERT INTO equity(bot_id, run_id, at_us, market_id, cash, exposure, realized, '
                           'unrealized, missing_marks) VALUES(?,?,?,?,?,?,?,?,?)',
                           (bot_id, run_id, at_us, market_id, str(cash), str(exposure), str(realized),
                            None if unrealized is None else str(unrealized), missing_marks))

    def equity_rows(self, bot_id: str, limit: int = 1000) -> list[dict]:
        cur = self._conn.execute('SELECT * FROM equity WHERE bot_id=? ORDER BY id DESC LIMIT ?', (bot_id, limit))
        columns = [c[0] for c in cur.description]
        return list(reversed([_row(r, columns) for r in cur.fetchall()]))

    def event(self, bot_id: str, run_id: str, *, at_us: int, kind: str, detail: str) -> None:
        self._conn.execute('INSERT INTO events(bot_id, run_id, at_us, kind, detail) VALUES(?,?,?,?,?)',
                           (bot_id, run_id, at_us, kind, detail))

    def events(self, bot_id: str) -> list[dict]:
        cur = self._conn.execute('SELECT * FROM events WHERE bot_id=? ORDER BY id', (bot_id,))
        columns = [c[0] for c in cur.description]
        return [_row(r, columns) for r in cur.fetchall()]

    def heartbeat(self, bot_id: str, run_id: str, *, at_us: int, market_id: str | None,
                  status: str, cycles: int, last_error: str | None) -> None:
        self._conn.execute('INSERT INTO heartbeat(bot_id, run_id, at_us, market_id, status, cycles, last_error) '
                           'VALUES(?,?,?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET run_id=excluded.run_id, '
                           'at_us=excluded.at_us, market_id=excluded.market_id, status=excluded.status, '
                           'cycles=excluded.cycles, last_error=excluded.last_error',
                           (bot_id, run_id, at_us, market_id, status, cycles, last_error))

    def heartbeat_row(self, bot_id: str) -> dict | None:
        cur = self._conn.execute('SELECT * FROM heartbeat WHERE bot_id=?', (bot_id,))
        return _row(cur.fetchone(), [c[0] for c in cur.description])
