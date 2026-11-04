"""Offline fixed-strategy replay. Estimated fills never certify strict execution."""
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile

from .estimated import Book, Estimator, Level, Order, Scenario
from .linux_mvp.config import Settings
from .linux_mvp.engine import PaperEngine
from .linux_mvp.fees import fee_evidence, same_schedule
from .linux_mvp.store import ARITHMETIC, MvpStore, ZERO, SAFETY_STOPS
from .market_data.models import MarketDataError

ORDER_LIFETIME_NS = 30_000_000_000  # Observation lifetime, NOT exchange GTD.
SCENARIOS = {
    'OPTIMISTIC': Scenario(max_spread=Decimal('.04')),
    'CONSERVATIVE': Scenario(local_delay_ns=250_000_000, exchange_delay_ns=250_000_000,
                             max_spread=Decimal('.04')),
    'PESSIMISTIC': Scenario(local_delay_ns=750_000_000, exchange_delay_ns=250_000_000,
                            depth_fraction=Decimal('.5'), extra_cost_bps=Decimal('25'),
                            max_spread=Decimal('.04')),
}


@dataclass(frozen=True)
class ReplayResult:
    report: dict
    db_path: Path


class ReplayHalted(Exception):
    pass


class DepthLedger:
    """Lifetime own consumption; updated source versions never replenish depth."""
    def __init__(self):
        self.used = {}

    def adapt(self, book, fraction):
        # Debit prior consumption from full depth first, then apply scenario fraction.
        def levels(rows):
            return tuple(Level(r.price, max(ZERO, (r.size - self.used.get((book.token_id, r.price), ZERO)) * fraction)) for r in rows)
        return replace(book, bids=levels(book.bids), asks=levels(book.asks))

    def charge(self, token, fills):
        for price, quantity in fills:
            key = token, price
            self.used[key] = self.used.get(key, ZERO) + quantity


class _Collector:
    def __init__(self, replay):
        self.replay = replay

    def collect(self):
        row = self.replay.current
        if row.get('snapshot') is None:
            error = row.get('error') or {}
            raise MarketDataError(error.get('code', 'capture_error'), str(error))
        return row['snapshot']


class _Engine(PaperEngine):
    def _execute_pending(self, *args):
        pass  # Offline execution occurs at receipt events, never at decision time.

    def _reject_pending_of(self, market_id, at):
        pending = self.store.pending_order(self.bot_id)
        if pending and pending['market_id'] == market_id:
            self.collector.replay.cancel(pending)
            if self.collector.replay.halted:
                raise ReplayHalted('unknown cancellation stops decisions')
        super()._reject_pending_of(market_id, at)


class CausalReplay:
    def __init__(self, records, *, scenario, db_dir=None):
        self.records = list(records)
        self.scenario = scenario
        self.root = Path(db_dir) if db_dir is not None else Path(tempfile.mkdtemp(prefix='causal-replay-'))
        self.db_path = self.root / 'replay.sqlite'
        self.ledger = DepthLedger()
        self.estimator = Estimator()  # Single instance for identity reuse protection
        self.executions = []
        self.context = {}
        self.halted = None
        self.current = None
        self.last_mark = None
        self.statuses = {}
        self.cycles = 0
        self.markets = set()

    def run(self):
        with localcontext(ARITHMETIC):
            return self._run()

    def _run(self):
        starts = [r for r in self.records if r.get('kind') in ('start', 'session')]
        if len(starts) != 1 or not isinstance(starts[0].get('session'), str) or not starts[0]['session']:
            raise ValueError('exactly one session start required')
        self.session = starts[0]['session']
        self.root.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            raise ValueError('replay requires a new disposable database')
        env = {k: str(v) for k, v in starts[0].get('settings', {}).items()}
        env.update(MVP_DB_PATH=str(self.db_path), MVP_STATE_DIR=str(self.root / 'state'))
        self.settings = Settings.from_env(env, base=self.root)
        self.bot = self.settings.bot_id
        rows = [r for r in self.records if r.get('kind') == 'cycle']
        if not rows:
            raise ValueError('capture has no cycles')
        self.store = MvpStore.open(self.db_path)
        try:
            self.store.start_run('replay', self.bot, initial_cash=self.settings.initial_cash,
                                 config=self.settings.as_dict(), started_us=rows[0]['decision_wall_us'])
            self.engine = _Engine(self.store, _Collector(self), self.settings, run_id='replay',
                                  now_us=lambda: self.current['decision_wall_us'],
                                  monotonic_ns=lambda: self.current['decision_mono_ns'])
            previous = -1
            for row in self.records:
                kind = row.get('kind')
                if kind in ('start', 'session', 'attempt', 'summary'):
                    continue
                if kind not in ('cycle', 'resolution'):
                    raise ValueError('unsupported capture record')
                moment = row['decision_mono_ns'] if kind == 'cycle' else row['received_mono_ns']
                if type(moment) is not int or moment <= previous:
                    raise ValueError('capture monotonic event order invalid')
                if kind == 'cycle':
                    snapshot = row.get('snapshot')
                    if snapshot is not None:
                        receipts = sorted((snapshot.up, snapshot.down),
                                          key=lambda o: getattr(o.timing, 'finished_mono_ns', moment))
                        for obs in receipts:
                            ns = getattr(obs.timing, 'finished_mono_ns', None)
                            if type(ns) is not int or not previous < ns <= moment:
                                raise ValueError('snapshot receipt outside causal cycle')
                            if not self.stopped():
                                self.receipt(snapshot, obs)
                if not self.stopped():
                    self.expire(moment)
                previous = moment
                if self.stopped():
                    continue
                if kind == 'resolution':
                    self.resolve(row)
                    continue
                self.current = row
                self.cycles += 1
                try:
                    result = self.engine.step()
                except ReplayHalted:
                    continue
                self.statuses[result.status + ':' + result.reason] = self.statuses.get(
                    result.status + ':' + result.reason, 0) + 1
                if snapshot is not None:
                    self.markets.add(snapshot.market.market_id)
                    pending = self.store.pending_order(self.bot)
                    if pending and pending['order_id'] not in self.context:
                        obs = snapshot.up if pending['outcome'] == 'UP' else snapshot.down
                        book = obs.book
                        self.context[pending['order_id']] = dict(
                            submitted=moment, condition=snapshot.market.condition_id,
                            mid=book.midpoint,
                            top=book.best_ask if pending['side'] == 'BUY' else book.best_bid,
                            fees=fee_evidence(snapshot.market, observed_us=row['decision_wall_us']))
                self.last_mark = self.store.equity_rows(self.bot, limit=1)
            pending = self.store.pending_order(self.bot)
            if pending and not self.halted:
                self.unknown(pending, 'CAPTURE_END_MISSING_SUBSEQUENT_BOOK')
            self.store.reconcile(self.bot)
            return ReplayResult(self.report(), self.db_path)
        finally:
            self.store.close()

    def stopped(self):
        return self.halted is not None or self.store.safety_state(self.bot)['status'] in SAFETY_STOPS

    def loss_stop(self, at):
        day = at // 86_400_000_000 * 86_400_000_000
        if (not self.stopped() and self.store.realized(self.bot, since_us=day)
                <= -self.settings.max_daily_loss):
            self.store.heartbeat(self.bot, 'replay', at_us=at, market_id=None,
                                 status='RISK_STOPPED', cycles=self.cycles,
                                 last_error='DAILY_LOSS_LIMIT')

    def unknown(self, pending, reason):
        self.executions.append(dict(order_id=pending['order_id'], side=pending['side'],
                                    status='UNKNOWN', reason=reason, filled=None))
        self.halted = dict(order_id=pending['order_id'], reason=reason)

    def expire(self, ns):
        pending = self.store.pending_order(self.bot)
        if pending and ns >= self.context[pending['order_id']]['submitted'] + ORDER_LIFETIME_NS:
            self.unknown(pending, 'OBSERVATION_LIFETIME_NO_EVIDENCE')

    def request(self, pending, cancel=None):
        ctx = self.context[pending['order_id']]
        return Order(self.session, pending['market_id'], ctx['condition'], pending['token_id'],
                     ctx['submitted'], ctx['submitted'] + ORDER_LIFETIME_NS, pending['side'],
                     pending['quantity'], Decimal('.99') if pending['side'] == 'BUY' else Decimal('.01'),
                     cancel_requested_ns=cancel)

    def cancel(self, pending):
        result = Estimator().execute(self.request(pending, self.current['decision_mono_ns']), [],
                                     self.scenario, cash=self.store.cash(self.bot),
                                     held=self.store.position(self.bot, pending['market_id'], pending['outcome'])['quantity'])
        self.apply(pending, result, None, self.current['decision_wall_us'])

    def receipt(self, snapshot, obs):
        pending = self.store.pending_order(self.bot)
        if not pending or pending['token_id'] != obs.book.token_id:
            return
        ctx = self.context[pending['order_id']]
        ns = obs.timing.finished_mono_ns
        ready = ctx['submitted'] + self.scenario.local_delay_ns + self.scenario.exchange_delay_ns
        if ns < ready:
            return
        self.expire(ns)
        if self.stopped():
            return
        try:
            if (obs.book.condition_id != snapshot.market.condition_id
                    or snapshot.market.condition_id != ctx['condition']
                    or snapshot.market.market_id != pending['market_id']):
                raise ValueError('execution identity conflict')
            raw = obs.timing.payload
            minimum, tick = (Decimal(str(raw[k])) for k in ('min_order_size', 'tick_size'))
            if isinstance(raw['min_order_size'], bool) or isinstance(raw['tick_size'], bool):
                raise ValueError('boolean metadata')
            fees = fee_evidence(snapshot.market, observed_us=obs.timing.finished_wall_us)
            if not same_schedule(ctx['fees'], fees):
                self.unknown(pending, 'FEE_SCHEDULE_CHANGED_OR_UNVERIFIED')
                return
            book = Book(self.session, snapshot.market.market_id, snapshot.market.condition_id,
                        obs.book.token_id, ns,
                        tuple(Level(x.price, x.size) for x in obs.book.bids),
                        tuple(Level(x.price, x.size) for x in obs.book.asks), minimum, tick,
                        fees, ns, obs.timing.finished_wall_us)
            adjusted = self.ledger.adapt(book, self.scenario.depth_fraction)
            # Single Estimator instance for identity reuse protection; lifetime ledger owns cross-order consumption.
            result = self.estimator.execute(self.request(pending), [adjusted],
                                        replace(self.scenario, depth_fraction=Decimal(1)),
                                        cash=self.store.cash(self.bot), held=self.store.position(
                                            self.bot, pending['market_id'], pending['outcome'])['quantity'])
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.unknown(pending, 'INVALID_OR_MISSING_EXECUTION_EVIDENCE')
            return
        self.apply(pending, result, book, obs.timing.finished_wall_us)

    def apply(self, pending, estimate, book, at):
        if estimate['filled'] is None:
            self.unknown(pending, estimate['reason'])
            return
        log = dict(estimate, order_id=pending['order_id'], side=pending['side'],
                   market_id=pending['market_id'], outcome=pending['outcome'])
        if not estimate['filled']:
            self.store.reject_order(pending['order_id'], estimate['reason'], at_us=at)
        else:
            levels = tuple(Level(p, q) for p, q in estimate['fills'])
            adjusted = replace(book, asks=levels) if pending['side'] == 'BUY' else replace(book, bids=levels)
            result = self.store.execute_pending(self.bot, {pending['outcome']: adjusted}, book.fees, at_us=at)
            if any(result[k] != estimate[k] for k in ('filled', 'notional', 'fee')):
                raise RuntimeError('store/estimator accounting mismatch')
            extra = estimate['extra_cost']
            if extra:
                with self.store.transaction() as conn:
                    order = self.store.order(pending['order_id'])
                    conn.execute('UPDATE orders SET fee=?,realized=? WHERE order_id=?',
                                 (str(order['fee'] + extra), str(order['realized'] - extra
                                  if pending['side'] == 'SELL' else order['realized']), pending['order_id']))
                    if pending['side'] == 'BUY':
                        pos = self.store.position(self.bot, pending['market_id'], pending['outcome'])
                        conn.execute('UPDATE positions SET cost_basis=? WHERE bot_id=? AND market_id=? AND outcome=?',
                                     (str(pos['cost_basis'] + extra), self.bot, pending['market_id'], pending['outcome']))
                    self.store.reconcile(self.bot)
            self.ledger.charge(pending['token_id'], estimate['fills'])
        self.loss_stop(at)
        ctx = self.context[pending['order_id']]
        direction = Decimal(1) if pending['side'] == 'BUY' else Decimal(-1)
        for key in ('mid', 'top'):
            log['slippage_vs_' + key] = sum((direction * (p - ctx[key]) * q
                                           for p, q in estimate['fills']), ZERO) if ctx[key] is not None else None
        log['decision_spread_cost'] = (log['slippage_vs_mid'] - log['slippage_vs_top']
                                       if log['slippage_vs_mid'] is not None else None)
        self.executions.append(log)

    def resolve(self, row):
        market = row['window']['market_id']
        window = self.store.window(self.bot, market)
        if window is None:
            raise ValueError('resolution for unknown window')
        if row['received_wall_us'] >= window['end_us'] and window['status'] == 'ACTIVE':
            pending = self.store.pending_order(self.bot)
            if pending and pending['market_id'] == market:
                self.unknown(pending, 'WINDOW_ENDED_WITH_UNKNOWN_EXECUTION')
                return
            self.store.close_window(self.bot, market, 'UNKNOWN_SETTLEMENT',
                                    at_us=row['received_wall_us'], note='recorded resolution receipt')
        self.store.settle_window(self.bot, market, evidence=row['evidence'], at_us=row['received_wall_us'])
        self.loss_stop(row['received_wall_us'])

    def report(self):
        orders = self.store.orders(self.bot)
        fills = [r for r in self.executions if r.get('filled')]
        fees = sum((r['fee'] for r in fills), ZERO)
        extra = sum((r['extra_cost'] for r in fills), ZERO)
        positions = self.store.open_positions(self.bot)
        marks = self.store.equity_rows(self.bot, limit=100000)
        complete = [r for r in marks if not r['missing_marks'] and r['unrealized'] is not None]
        peak = self.settings.initial_cash
        drawdown = ZERO
        for row in complete:
            equity = Decimal(row['cash']) + Decimal(row['exposure']) + Decimal(row['unrealized'])
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        unrealized = Decimal(marks[-1]['unrealized']) if marks and marks[-1]['unrealized'] is not None else None
        realized = self.store.realized(self.bot)
        decisions = [r for m in sorted(self.markets) for r in self.store.decisions_for_market(self.bot, m)]
        trips = [r for r in orders if r['side'] == 'SELL' and r['filled']]
        # Allocate entry costs to closed quantities; remaining costs stay in basis.
        open_costs = sum((p['cost_basis'] for p in positions), ZERO)
        # Track cost allocation with the same average-basis fractions as MvpStore.
        costs, quantities, realized_costs = {}, {}, ZERO
        events = [(o['updated_us'], 'order', o) for o in orders if o['filled']]
        events += [(s['settled_us'], 'settlement', s) for s in self.store.settlements(self.bot)]
        for _, kind, event in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == 'settlement':
                for key in list(costs):
                    if key[0] == event['market_id']:
                        realized_costs += costs.pop(key)
                        quantities.pop(key)
                continue
            key = event['market_id'], event['outcome']
            if event['side'] == 'BUY':
                costs[key] = costs.get(key, ZERO) + event['fee']
                quantities[key] = quantities.get(key, ZERO) + event['filled']
            else:
                allocated = costs[key] * event['filled'] / quantities[key]
                realized_costs += allocated + event['fee']
                costs[key] -= allocated
                quantities[key] -= event['filled']
        gross_realized = realized + realized_costs
        status = self.store.safety_state(self.bot)['status']
        settings = self.settings.as_dict()
        settings.pop('db_path')
        settings.pop('state_dir')
        report = dict(
            mode='PAPER_ONLY_CAUSAL_REPLAY', certified=False, upstream_freshness='UNKNOWN',
            session=self.session, scenario=asdict(self.scenario), settings=settings,
            order_observation_lifetime_ns=ORDER_LIFETIME_NS, limits=dict(BUY='.99', SELL='.01'),
            halted=self.halted, terminal_status=status,
            strict_certified_results=dict(executions=[], note='Strict execution not run or replaced; estimates separate.'),
            strategy_decisions=decisions, estimated_executions=self.executions,
            trade_counts=dict(buys=sum(r['side'] == 'BUY' for r in fills),
                              sells=sum(r['side'] == 'SELL' for r in fills),
                              partials=sum(r.get('status') == 'PARTIAL' for r in self.executions),
                              no_fills=sum(r.get('status') == 'NO_FILL' for r in self.executions),
                              cancels=sum(r.get('status') == 'CANCELLED' for r in self.executions),
                              unknown=sum(r.get('status') == 'UNKNOWN' for r in self.executions)),
            account=dict(cash=self.store.cash(self.bot), exposure=open_costs,
                         realized=realized, gross_realized=gross_realized,
                         realized_costs=realized_costs, unallocated_entry_costs=sum(costs.values(), ZERO),
                         gross_unrealized=None if self.halted or unrealized is None else
                             unrealized + sum(costs.values(), ZERO),
                         unrealized=None if self.halted else unrealized,
                         net_pnl=None if self.halted or unrealized is None else realized + unrealized,
                         performance='UNKNOWN' if self.halted else 'DETERMINISTIC'),
            fees=dict(market_fees=fees, extra_cost=extra, total=fees + extra),
            round_trips=trips, unresolved=positions, settlements=self.store.settlements(self.bot),
            observed_drawdown=dict(max_observed_drawdown=drawdown, complete_bid_marks=len(complete),
                                   total_marks=len(marks), note='Complete bid marks only; gaps not interpolated.'),
            coverage=dict(cycles_replayed=self.cycles, statuses=self.statuses,
                          stale_or_missing_decisions=sum(d['reason'] in
                              ('STALE_PAIR', 'ONE_SIDED_BOOK') for d in decisions),
                          timing_or_missing_cycles=sum(n for s, n in self.statuses.items()
                              if s.startswith(('TIMING_INVALID:', 'SLOT_UNCERTAIN:', 'ERROR:')))),
            extra_cost_ledger=[dict(order_id=r['order_id'], side=r['side'],
                                   extra_cost=r['extra_cost'], market_fee=r['fee']) for r in fills],
            slippage_note='slippage_vs_mid = decision spread plus slippage_vs_top; informational, not deducted again.',
            integrity=dict(reconciled=True, own_depth='Lifetime token/price debit; never replenished.'))
        return json.loads(json.dumps(report, default=str, sort_keys=True, allow_nan=False))


def replay(records, *, scenario, db_dir=None):
    return CausalReplay(records, scenario=scenario, db_dir=db_dir).run()


def run_capture(path, *, scenario, db_dir=None):
    from .depth_capture import read_records
    return replay(read_records(Path(path)), scenario=scenario, db_dir=db_dir)
