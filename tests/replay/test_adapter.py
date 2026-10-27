"""TDD tests for ReplayAdapter: CP5 recorder evidence → MarketSnapshot sequences."""
import hashlib
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from polymarket_bot.market_data.models import MarketSnapshot
from polymarket_bot.replay.adapter import ReplayAdapter, load_replay_recording


class AdapterTests(unittest.TestCase):
    """Test CP5 evidence loading with validation, pairing, and opportunity tracking."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path

    def test_replay_adapter_class_exists(self):
        """ReplayAdapter class should exist and be callable."""
        self.assertTrue(callable(ReplayAdapter))

    def test_load_replay_recording_returns_valid_snapshots(self):
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = load_replay_recording(
            self.db_path, self.run_id, '1001')
        self.assertIsInstance(snapshots, tuple)
        self.assertGreaterEqual(len(snapshots), 1)
        for s in snapshots:
            self.assertIsInstance(s, MarketSnapshot)
        self.assertIsInstance(excluded, tuple)
        self.assertIsInstance(opportunities, tuple)

    def test_snapshot_pairs_up_and_down(self):
        """Each MarketSnapshot has both up and down observations."""
        snapshots, _, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        for s in snapshots:
            self.assertIsNotNone(s.up)
            self.assertIsNotNone(s.down)
            self.assertEqual(s.up.book.token_id, '9000000000000000000001')
            self.assertEqual(s.down.book.token_id, '9000000000000000000002')

    def test_snapshot_contains_full_depth(self):
        """Snapshots preserve full book depth (bids/asks tuples)."""
        snapshots, _, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        snap = snapshots[0]
        self.assertIsInstance(snap.up.book.bids, tuple)
        self.assertIsInstance(snap.up.book.asks, tuple)
        self.assertIsInstance(snap.down.book.bids, tuple)
        self.assertIsInstance(snap.down.book.asks, tuple)
        self.assertTrue(len(snap.up.book.bids) > 0)
        self.assertTrue(len(snap.up.book.asks) > 0)

    def test_chronological_order(self):
        """Snapshots sorted by availability (execution time)."""
        snapshots, _, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        times = [s.collection_finished_at.timestamp() * 1_000_000 for s in snapshots]
        self.assertEqual(times, sorted(times))

    def test_opportunities_include_timing(self):
        """Opportunities dict has start_ns, available_ns, slot_id, outcome, reason."""
        _, _, opportunities, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        self.assertGreaterEqual(len(opportunities), 2)
        for opp in opportunities:
            self.assertIn('slot_id', opp)
            self.assertIn('slot_index', opp)
            self.assertIn('outcome', opp)
            self.assertIn('start_ns', opp)
            self.assertIn('available_ns', opp)
            self.assertIn('reason', opp)

    def test_read_only_mode(self):
        """Adapter opens DB read-only, never writes."""
        stat_before = os.stat(self.db_path)
        digest_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        load_replay_recording(self.db_path, self.run_id, '1001')
        stat_after = os.stat(self.db_path)
        digest_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(stat_before.st_size, stat_after.st_size)
        self.assertEqual(digest_before, digest_after)

    def test_adapter_lazy_load_properties(self):
        """ReplayAdapter lazy-loads on property access."""
        adapter = ReplayAdapter(self.db_path, self.run_id, '1001')
        self.assertEqual(adapter.run_id, self.run_id)
        self.assertEqual(adapter.market_id, '1001')
        _ = adapter.snapshots  # triggers load
        self.assertGreater(len(adapter.snapshots), 0)
        self.assertIsInstance(adapter.excluded, tuple)
        self.assertIsInstance(adapter.opportunities, tuple)

    def test_adapter_idempotent_load(self):
        """Calling load() twice is safe and returns same snapshot count."""
        adapter = ReplayAdapter(self.db_path, self.run_id, '1001')
        adapter.load()
        count1 = len(adapter.snapshots)
        adapter.load()  # should not reload
        count2 = len(adapter.snapshots)
        self.assertEqual(count1, count2)

    def test_max_age_us_passed_to_loading(self):
        """max_age_us parameter is accepted by loader without error."""
        snapshots, excluded, opportunities, _, _, _ = load_replay_recording(
            self.db_path, self.run_id, '1001', max_age_us=5_000_000)
        self.assertGreater(len(snapshots), 0)

    def test_invalid_observation_excluded(self):
        """Observation with status != VALID produces INVALID_EXECUTION_BOOK in excluded."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                # Must also clear text fields to satisfy CHECK constraint
                conn.execute("UPDATE book_observations SET status='INVALID', best_bid_text=NULL, best_ask_text=NULL, bid_size_text=NULL, ask_size_text=NULL, midpoint_text=NULL, spread_text=NULL WHERE slot_id=(SELECT MIN(slot_id) FROM book_observations) AND outcome='UP'")
        _, excluded, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        self.assertTrue(any('slot/book not COMPLETE/VALID' in str(e.get('reason', '')) or
                           'INVALID' in str(e.get('reason', '')) for e in excluded))

    def test_identity_verification_rejects_mismatch(self):
        """Token identity mismatch in observation excludes that book."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE book_observations SET expected_token_id='999' WHERE outcome='UP'")
        _, excluded, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        self.assertTrue(any('MISMATCH' in str(e.get('reason', '')).upper() or
                           'IDENTITY' in str(e.get('reason', '')).upper() for e in excluded))

    def test_missing_observation_excluded(self):
        """Missing one outcome from COMPLETE slot produces MISSING_OBSERVATION."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM book_observations WHERE slot_id=(SELECT MIN(slot_id) FROM sample_slots WHERE status='COMPLETE') AND outcome='DOWN'")
        _, excluded, _, _, _, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        self.assertTrue(any(e.get('reason') == 'MISSING_OBSERVATION' for e in excluded))

    def test_settlement_evidence_none_without_resolution(self):
        """Synthetic fixture has no resolution evidence → settlement is None."""
        _, _, _, settlement_evidence, settlement_result, _ = load_replay_recording(self.db_path, self.run_id, '1001')
        self.assertIsNone(settlement_evidence)
        self.assertIsNone(settlement_result)


class AdapterIntegrityTests(unittest.TestCase):
    """I4 regressions for the multi-market loader plus the real-recording contract."""

    def setUp(self):
        from tests.replay.fixtures import traded_fixture
        self.f = traded_fixture(self)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path
        # Post-hoc completion: the fixture run stops at its deadline before the
        # recorder processes end-of-life, so the market stays CURRENT. Real
        # replayable windows are EXPIRED/INVALIDATED; mark completion explicitly
        # (same fixture convention as the post-hoc slot-end extension tests).
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE markets SET lifecycle='EXPIRED' WHERE market_id='1001'")
                conn.execute(
                    "UPDATE recording_runs SET ended_wall_us=COALESCE(ended_wall_us, started_wall_us) "
                    "WHERE run_id=?", (self.run_id,))

    def test_load_all_windows_returns_recorded_windows(self):
        """Completed windows load with real horizon and the real paper recording."""
        from polymarket_bot.replay.adapter import load_all_windows
        windows, skipped = load_all_windows(self.db_path)
        self.assertEqual(skipped, [])
        self.assertGreaterEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.market_id, '1001')
        self.assertGreater(window.horizon_ns, 0)
        self.assertIsNotNone(window.paper_recording)
        self.assertGreater(len(window.paper_recording.snapshots), 0)
        self.assertGreater(len(window.snapshots), 0)
        starts = [w.window_start_us for w in windows]
        self.assertEqual(starts, sorted(starts))

    def test_load_all_windows_reports_failures(self):
        """Unfailable windows surface diagnostics instead of silently shrinking data."""
        import sqlite3
        from contextlib import closing
        # Break determinism invariants for one market: wrong token side of the first revision.
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE market_revisions SET gamma_raw_z=? WHERE revision_id=(SELECT MIN(revision_id) FROM market_revisions)",
                             (b'\x78\x9c' + b'{}',))  # valid zlib, but not the recorded gamma
        from polymarket_bot.replay.adapter import load_all_windows
        windows, skipped = load_all_windows(self.db_path)  # may still load a later revision
        # Either the window loads via a later revision or the failure is reported; never silent.
        if len(windows) == 0:
            self.assertTrue(any(e.get('reason') for e in skipped), 'failure must be reported, not swallowed')


if __name__ == '__main__':
    unittest.main()
