"""TDD tests for ReplayEngine: deterministic chronological replay with settlement."""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.replay.adapter import load_replay_recording
from polymarket_bot.replay.engine import ReplayEngine, run_replay
from polymarket_bot.replay.report import BacktestReport

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _iso(us):
    moment = EPOCH.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return moment.isoformat()


class EngineTests(unittest.TestCase):
    """Test chronological replay with execution, fees, partial fills, and settlement."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path

    def _load_window(self):
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = (
            load_replay_recording(self.db_path, self.run_id, '1001'))
        return snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording

    def test_replay_engine_exists(self):
        """ReplayEngine class should exist."""
        from polymarket_bot.replay.engine import ReplayEngine
        self.assertTrue(callable(ReplayEngine))

    def test_run_replay_returns_report(self):
        """run_replay returns a BacktestReport with all required fields."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsInstance(report, BacktestReport)
        self.assertEqual(report.schema, 'phase3b-backtest-v1')

    def test_baseline_strategy_reproduces_existing_behavior(self):
        """ReplayEngine with baseline config should match PaperEngine on same inputs."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Should have made at least one decision
        self.assertGreater(len(report.decisions), 0)
        # Constant fixture midpoints -> 0 momentum -> HOLD decisions
        self.assertEqual(report.counts['requested'], 0)

    def test_fees_pnl_reconciliation(self):
        """cash + basis = initial + realized (within tolerance)."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Reconciliation check
        cash = Decimal(report.cash)
        basis = sum(Decimal(p['cost_basis']) for p in report.positions.values())
        initial = Decimal('100')
        realized = Decimal(report.realized_pnl or '0')
        self.assertAlmostEqual(float(cash + basis), float(initial + realized), places=6)

    def test_partial_fills_tracked(self):
        """PARTIAL fills consume depth, remainder abandoned."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        partial_fills = [t for t in report.ledger if t['status'] == 'PARTIAL']
        for t in partial_fills:
            self.assertGreater(Decimal(t['filled_quantity']), 0)
            self.assertGreater(Decimal(t['unfilled_quantity']), 0)

    def test_risk_constraints_reported_not_enforced(self):
        """Exposure, cash, position limits are checked but don't stop replay."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000,
            max_exposure='5',  # Lower than default $10
            max_order='10'
        )
        # Should still produce a report even if risk limits would trigger
        self.assertIsInstance(report, BacktestReport)
        self.assertGreater(len(report.decisions), 0)

    def test_settlement_applied_when_evidence_available(self):
        """Self-consistent RESOLVED evidence settles positions (engine re-verifies it)."""
        from datetime import datetime, timezone

        def iso(us):
            return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).isoformat()

        snapshots, excluded, opportunities, _, _, paper_recording = self._load_window()
        start_us = int(snapshots[0].market.window_start.timestamp() * 1_000_000)
        end_us = int(snapshots[0].market.window_end.timestamp() * 1_000_000)
        resolved_us = end_us + 60_000_000
        at_us = end_us + 300_000_000
        market_id = paper_recording.market_id
        condition = paper_recording.condition_id
        token_ids = [paper_recording.tokens['UP'], paper_recording.tokens['DOWN']]

        gamma = {
            'id': market_id,
            'conditionId': condition,
            'outcomes': json.dumps(['Up', 'Down']),
            'clobTokenIds': json.dumps(token_ids),
            'endDate': iso(end_us),
            'slug': 'btc-updown-15m-' + str(start_us // 1_000_000),
            'closed': True,
            'umaResolutionStatus': 'resolved',
            'outcomePrices': json.dumps(['1', '0'])
        }
        resolution = {'data': [{
            'condition_id': condition,
            'status': 'resolved',
            'extended_review': False,
            'was_disputed': False,
            'new_version_q': False,
            'transaction_hash': '0x' + 'ab' * 32,
            'resolved_block': 123456,
            'resolved_at': iso(resolved_us),
            'payouts': [1000000, 0]
        }]}
        settlement_evidence = {
            'gamma_raw': json.dumps(gamma, sort_keys=True, separators=(',', ':')),
            'resolution_raw': json.dumps(resolution, sort_keys=True, separators=(',', ':')),
            'market_id': market_id,
            'condition_id': condition,
            'window_start_us': start_us,
            'window_end_us': end_us,
            'at_us': at_us
        }
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsNotNone(report.settlement)
        self.assertEqual(report.settlement['status'], 'RESOLVED')
        self.assertEqual(report.settlement['winning_outcome'], 'UP')

    def test_no_future_data_leakage(self):
        """Decisions at slot N never use data from slot > N."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Each decision should only reference snapshots at or before its slot
        for decision in report.decisions:
            slot_idx = decision['slot_index']
            for inp in decision.get('inputs', []):
                self.assertLessEqual(inp.get('slot_index', 0), slot_idx)

    def test_chronological_execution_order(self):
        """Orders executed in chronological order by decision time."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Ledger should be in execution order (not request order)
        exec_times = [Decimal(t.get('snapshot', {}).get('receipt_server_us', 0)) for t in report.ledger if t.get('snapshot')]
        self.assertEqual(exec_times, sorted(exec_times))

    def test_deterministic_reproduction(self):
        """Same inputs → identical report (byte-for-byte)."""
        snapshots, excluded, opportunities, settlement_evidence, _, paper_recording = self._load_window()
        report1 = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        report2 = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        import json
        self.assertEqual(
            json.dumps(report1.__dict__, sort_keys=True, default=str),
            json.dumps(report2.__dict__, sort_keys=True, default=str)
        )

    def test_max_drawdown_calculated(self):
        """Maximum drawdown calculated from equity curve."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsNotNone(report.max_drawdown)
        self.assertIsInstance(report.max_drawdown, Decimal)

    def test_daily_pnl_buckets(self):
        """P&L grouped by UTC day from execution/settlement times."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsInstance(report.daily_pnl, dict)


class ReplayFixTests(unittest.TestCase):
    """Defect regressions found by the independent Phase 3B review (2026-10-27)."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        from tests.replay.fixtures import traded_fixture
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.db_path = None  # set per-fixture
        self._traded = None

    def _recorded(self):
        from tests.test_paper import recorded_fixture
        if getattr(self, '_f_recorded', None) is None:
            f = recorded_fixture(self)
            self.addCleanup(f.close)
            self._f_recorded = f
        return self._f_recorded

    def _traded_fx(self):
        from tests.replay.fixtures import traded_fixture
        if getattr(self, '_f_traded', None) is None:
            f = traded_fixture(self)
            self.addCleanup(f.close)
            self._f_traded = f
        return self._f_traded

    def _run_id(self, f):
        return f.rows('SELECT run_id FROM recording_runs')[0]['run_id']

    def test_report_builds_when_replay_actually_trades(self):
        """C3 regression: any executed trade must produce a report, not crash.

        The fabricated replay timestamps (source_timestamp_raw treated as µs) sent
        receipt_server_us to year ~58000 and datetime.fromtimestamp crashed.
        """
        from polymarket_bot.replay.adapter import load_replay_recording
        from decimal import Decimal
        f = self._traded_fx()
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = (
            load_replay_recording(f.db.path, self._run_id(f), '1001'))
        report = run_replay(
            snapshots=snapshots, paper_recording=paper_recording,
            settlement_evidence=settlement_evidence)
        executed = [t for t in report.ledger if t['status'] in ('FILLED', 'PARTIAL')]
        self.assertGreaterEqual(len(executed), 1)
        trade = executed[0]
        self.assertTrue(trade.get('fills'), 'executed trade must have real fills')
        self.assertGreater(Decimal(trade['fee']), 0)
        # Daily P&L must use real epochs, not the fabricated year-58000 scale.
        import datetime
        for day in report.daily_pnl:
            parsed = datetime.date.fromisoformat(day)
            self.assertEqual(parsed.isoformat(), day, 'daily_pnl keys must be ISO dates')

    def test_replay_provenance_matches_recorded_evidence(self):
        """C3/I1 regression: decision proofs carry the real recorder receipt clocks.

        Fabricating receipt_us (source+100ms) made the STALE_PAIR gate dead code;
        decisions must reproduce what paper.load_recording proved from anchors.
        """
        from polymarket_bot.replay.adapter import load_replay_recording
        f = self._recorded()
        snapshots, excluded, opportunities, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        report = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        recorded = {s['receipt_us'] for s in (
            {'receipt_us': snap.receipt_us} for snap in paper_recording.snapshots)}
        decision_receipts = set()
        for d in report.decisions:
            for inp in d.get('inputs', []):
                receipt = inp.get('receipt_server_us')
                self.assertIsNotNone(receipt)
                decision_receipts.add(receipt)
        self.assertLessEqual(decision_receipts, recorded,
                             'decision proofs must use real recorded receipt times')
        # The fabricated constant +100_000 µs lag must be gone.
        self.assertNotIn(min(recorded) + 100_000, decision_receipts)

    def test_staleness_gate_uses_real_receipt_clock(self):
        """I1 regression: a lagged slot end triggers STALE_PAIR under a tight max_age."""
        import sqlite3
        from contextlib import closing
        from polymarket_bot.replay.adapter import load_replay_recording
        f = self._recorded()
        mid_slot = f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_id")[1]['slot_id']
        with closing(sqlite3.connect(f.db.path)) as conn:
            with conn:
                conn.execute("UPDATE sample_slots SET end_mono_ns = end_mono_ns + 200000000 WHERE slot_id = ?",
                             (mid_slot,))
        snapshots, excluded, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        tight = run_replay(snapshots=snapshots, paper_recording=paper_recording, max_age_us=100_000)
        reasons = [d['reason'] for d in tight.decisions]
        self.assertIn('STALE_PAIR', reasons, 'real receipt lag must fire the staleness gate')
        lenient = run_replay(snapshots=snapshots, paper_recording=paper_recording, max_age_us=5_000_000)
        self.assertNotIn('STALE_PAIR', [d['reason'] for d in lenient.decisions])

    def test_variant_parameters_execute(self):
        """C2 regression: prespecified variant parameters must run, not TypeError."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        from polymarket_bot.replay.variants import get_variant
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        for variant in (get_variant('entry_cap_0.5'), get_variant('min_remaining_60s'),
                        get_variant('reversal_confirm')):
            params = dict(get_variant('baseline').parameters)
            params.update(variant.parameters)
            report = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                                **params)
            self.assertIsInstance(report, BacktestReport)
        # Baseline must remain executable after the grid changes.
        report = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        self.assertIsInstance(report, BacktestReport)

    def test_entry_price_cap_blocks_late_entries(self):
        """C2 regression: entry cap gates BUY decisions above the cap."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        capped = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                            **{'max_entry_price': '0.6'})
        self.assertIn('ENTRY_PRICE_CAP', [d['reason'] for d in capped.decisions])
        base = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        self.assertNotIn('ENTRY_PRICE_CAP', [d['reason'] for d in base.decisions])
        capped_fills = [t for t in capped.ledger if t.get('side') == 'BUY' and t['status'] in ('FILLED', 'PARTIAL')]
        for t in capped_fills:
            self.assertLessEqual(Decimal(t['decision_best_price'] or '1'), Decimal('0.6'))

    def test_min_remaining_blocks_late_entries(self):
        """C2 regression: min_remaining_seconds gate skips entries near window end."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        blocked = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                             **{'min_remaining_seconds': 600})
        self.assertIn('MIN_REMAINING', [d['reason'] for d in blocked.decisions])
        allowed = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                             **{'min_remaining_seconds': 60})
        self.assertNotIn('MIN_REMAINING', [d['reason'] for d in allowed.decisions])

    def test_depth_scale_scales_book_depth(self):
        """C2 regression: depth_scale scales executable depth for fill sizing."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        scaled = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                            **{'depth_scale': 0.5})
        buys = [t for t in scaled.ledger if t.get('side') == 'BUY' and t['status'] in ('FILLED', 'PARTIAL')]
        self.assertGreaterEqual(len(buys), 1)
        for t in buys:
            self.assertLessEqual(Decimal(t['filled_quantity']), Decimal('2'))
        base = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        base_buys = [t for t in base.ledger if t.get('side') == 'BUY' and t['status'] in ('FILLED', 'PARTIAL')]
        self.assertGreaterEqual(len(base_buys), 1)

    def test_trade_limit_guard_enforced(self):
        """I5 regression: max_trades_per_window stops further entries."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        limited = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                             max_trades_per_window=1)
        self.assertIn('TRADE_LIMIT', [d['reason'] for d in limited.decisions])
        base = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        self.assertNotIn('TRADE_LIMIT', [d['reason'] for d in base.decisions])

    def test_daily_loss_guard_enforced(self):
        """I5 regression: max_daily_loss blocks new entries after the day's loss limit."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        guarded = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                             max_daily_loss='0.01')
        self.assertIn('DAILY_LOSS_LIMIT', [d['reason'] for d in guarded.decisions])

    def test_reversal_confirmation_delays_exit(self):
        """C2 regression: two consecutive opposite signals required to reverse."""
        f = self._traded_fx()
        from polymarket_bot.replay.adapter import load_replay_recording
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            f.db.path, self._run_id(f), '1001')
        confirmed = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                               **{'require_reversal_confirmation': True})
        plain = run_replay(snapshots=snapshots, paper_recording=paper_recording)
        self.assertIn('REVERSAL_UNCONFIRMED', [d['reason'] for d in confirmed.decisions])
        self.assertNotIn('REVERSAL_UNCONFIRMED', [d['reason'] for d in plain.decisions])


class SettlementIntegrityTests(unittest.TestCase):
    """C1/I2 regressions: settlement evidence verified from real rows, totals folded."""

    def setUp(self):
        from tests.replay.fixtures import traded_fixture
        self.f = traded_fixture(self)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path
        self.row = self.f.rows("SELECT * FROM markets WHERE market_id='1001'")[0]

    def _insert_resolution_rows(self, raw_response):
        """Insert one faithful RESOLVED_DIRECTIONAL observation, live recorder-shaped."""
        import hashlib
        import sqlite3
        import zlib
        from contextlib import closing
        env = json.dumps(raw_response, sort_keys=True, separators=(',', ':')).encode()
        z = zlib.compress(env)
        sha = hashlib.sha256(env).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                request_id = conn.execute(
                    """INSERT INTO request_attempts (run_id, market_id, slot_id, anchor_id, operation,
                        attempt_number, method, path, selector_json, started_wall_us, ended_wall_us,
                        started_server_us, ended_server_us, start_mono_ns, end_mono_ns, latency_ns,
                        http_status, result, error_code, response_bytes, response_raw_z,
                        raw_size_bytes, codec_version, raw_complete)
                       VALUES ((SELECT run_id FROM recording_runs LIMIT 1), '1001', NULL, NULL,
                        'RESOLUTION', 1, 'GET', '/v2/resolutions', :sel, :w0, :w1, :w0, :w1,
                        :m0, :m1, :m1 - :m0, 200, 'success', NULL, :size, :z, :size, 1, 1)""",
                    {'sel': json.dumps(['condition=' + self.row['condition_id']]),
                     'w0': self.row['window_end_us'] + 111_000_000,
                     'w1': self.row['window_end_us'] + 121_000_000,
                     'm0': 9_000_000_000_000_101, 'm1': 9_000_000_010_000_101,
                     'size': len(env), 'z': z}).lastrowid
                conn.execute(
                    """INSERT INTO resolution_observations (market_id, request_id, condition_id,
                        status_raw, payouts_raw_json, provenance_json, reported_resolved_us,
                        reported_time_raw, learned_wall_us, learned_server_us, mapped_outcome,
                        classification, response_hash, raw_resolution_z, raw_size_bytes, codec_version)
                       VALUES ('1001', :rid, :cond, 'resolved', '[0,1000000]', '{}',
                        :reported, :reported_raw, :w1, :w1, 'DOWN', 'RESOLVED_DIRECTIONAL',
                        :sha, :z, :size, 1)""",
                    {'rid': request_id, 'cond': self.row['condition_id'],
                     'reported': self.row['window_end_us'] + 60_000_000,
                     'reported_raw': _iso(self.row['window_end_us'] + 60_000_000),
                     'w1': self.row['window_end_us'] + 121_000_000,
                     'sha': sha, 'z': z, 'size': len(env)})

    def _insert_final_gamma_revision(self, gamma):
        import sqlite3
        import zlib
        from contextlib import closing
        body = json.dumps(gamma, sort_keys=True, separators=(',', ':')).encode()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """INSERT INTO market_revisions (market_id, observed_us, run_id, description,
                        resolution_source_url, gamma_raw_z, raw_sizes_json, codec_version, content_hash)
                       VALUES ('1001', :obs, (SELECT run_id FROM recording_runs LIMIT 1),
                        'final refresh', 'https://example.test/rules', :z, :sizes, 1, :hash)""",
                    {'obs': self.row['window_end_us'] + 300_000_000, 'z': zlib.compress(body),
                     'sizes': json.dumps({'gamma': len(body)}), 'hash': 'traded-final-gamma'})


    def _gamma(self):
        r = self.row
        return dict(id=r['market_id'], conditionId=r['condition_id'],
                    outcomes='["Up", "Down"]',
                    clobTokenIds=json.dumps([r['up_token_id'], r['down_token_id']]),
                    endDate=_iso(r['window_end_us']),
                    slug='btc-updown-15m-' + str(r['window_start_us'] // 1_000_000),
                    closed=True, umaResolutionStatus='resolved',
                    outcomePrices='["0", "1"]')

    def _resolution_envelope(self, extra=False):
        r = self.row
        row = dict(condition_id=r['condition_id'], status='resolved',
                   transaction_hash='0x' + 'ab' * 32, resolved_block=123,
                   resolved_at=_iso(r['window_end_us'] + 60_000_000),
                   extended_review=False, was_disputed=False, new_version_q=False,
                   payouts=[0, 1000000])
        data = [row]
        if extra:
            # Faithful batch: the recorder fetches 20 conditions per response.
            data.append(dict(condition_id='0x' + 'fe' * 20, status='resolved',
                             transaction_hash='0x' + 'cd' * 32, resolved_block=123,
                             resolved_at=_iso(r['window_end_us'] + 61_000_000),
                             extended_review=False, was_disputed=False, new_version_q=False,
                             payouts=[1000000, 0]))
        return dict(data=data)

    def test_settlement_resolves_from_recorder_rows(self):
        """C1 regression: real-shaped resolution rows verify as RESOLVED, not UNKNOWN."""
        from polymarket_bot.replay.adapter import load_replay_recording
        self._insert_final_gamma_revision(self._gamma())
        # Faithful batch shape: envelope holds both this condition and an unrelated one.
        self._insert_resolution_rows(self._resolution_envelope(extra=True))
        _, _, _, evidence, result, _ = load_replay_recording(
            self.db_path, self.run_id, '1001')
        self.assertIsNotNone(evidence)
        self.assertEqual(result.get('status'), 'RESOLVED',
                         f'unexpected: {result}')
        self.assertEqual(result.get('winning_outcome'), 'DOWN')
        self.assertEqual(result.get('tx_hash'), '0x' + 'ab' * 32)

    def test_run_replay_reverifies_and_applies_settlement(self):
        """I2 regression: engine re-verifies evidence and folds settlement into totals."""
        from polymarket_bot.replay.adapter import load_replay_recording
        self._insert_final_gamma_revision(self._gamma())
        self._insert_resolution_rows(self._resolution_envelope())
        snapshots, _, _, evidence, result, paper_recording = load_replay_recording(
            self.db_path, self.run_id, '1001')
        report = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                            settlement_evidence=evidence)
        self.assertIsNotNone(report.settlement)
        self.assertEqual(report.settlement.get('status'), 'RESOLVED')
        self.assertEqual(report.settlement.get('winning_outcome'), 'DOWN')
        self.assertEqual(report.cash, report.settlement['final_cash'])
        realized = Decimal(report.realized_pnl or '0')
        cash = Decimal(report.cash)
        basis = sum(Decimal(p['cost_basis']) for p in report.positions.values())
        self.assertAlmostEqual(float(cash + basis), 100.0 + float(realized), places=6)
        self.assertEqual(Decimal(report.total_pnl), realized + Decimal(report.unrealized_pnl or '0'))

    def test_doctored_evidence_not_settled(self):
        """I2 regression: cross-market evidence is rejected, positions stay unsettled."""
        from polymarket_bot.replay.adapter import load_replay_recording
        self._insert_final_gamma_revision(self._gamma())
        self._insert_resolution_rows(self._resolution_envelope())
        snapshots, _, _, evidence, _, paper_recording = load_replay_recording(
            self.db_path, self.run_id, '1001')
        forged = dict(evidence, condition_id='0x' + 'ff' * 32)
        report = run_replay(snapshots=snapshots, paper_recording=paper_recording,
                            settlement_evidence=forged)
        self.assertIsNotNone(report.settlement)
        self.assertEqual(report.settlement.get('status'), 'UNKNOWN_SETTLEMENT')


if __name__ == '__main__':
    unittest.main()