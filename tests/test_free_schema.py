"""Tests for FREE_REFERENCE v2 schema with approximate tables."""
import importlib
import unittest
from contextlib import closing
import sqlite3

from tests.recorder_support import DatabaseFixture


class FreeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("polymarket_bot.recorder.store")
        self.assertIsNotNone(self.module, "RecorderStore module is missing")
        self.fixture = DatabaseFixture()
        self.addCleanup(self.fixture.close)

    def open(self, path=None):
        store = self.module.RecorderStore.open(path or self.fixture.path)
        self.addCleanup(store.close)
        return store

    def reader(self, path=None):
        p = path or self.fixture.path
        reader = sqlite3.connect(p)
        reader.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(reader.close)
        return reader

    def test_trusted_v1_schema_opens_without_changes(self):
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        trusted = subprocess.check_output(
            ["git", "show", "e115b2f:src/polymarket_bot/recorder/schema.sql"],
            cwd=root,
        ).decode("utf-8")
        with closing(sqlite3.connect(self.fixture.path)) as connection:
            connection.executescript(trusted + "\nPRAGMA user_version=1;")
        before = self.fixture.path.read_bytes()
        self.open().close()
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(self.module._load_v1_schema(), trusted)

    def test_failed_observations_persist_without_price(self):
        import hashlib
        from polymarket_bot.recorder.free_reference import ApproximateObservation
        from polymarket_bot.recorder.models import RunConfig
        store = self.module.RecorderStore.open_approximate(self.fixture.path)
        self.addCleanup(store.close)
        run = store.start_run(RunConfig(mode="APPROXIMATE_REFERENCE"), wall_us=100000000,
                              app_revision="regression")
        obs = ApproximateObservation("coinbase", "BTC/USD", "USD", None, None, None,
            None, 100000000, None, hashlib.sha256(b"").hexdigest(), 0, "rest",
            "coinbase-ticker-v1", None, "error", "TRANSPORT_TIMEOUT", 1000)
        for _ in range(2):
            store.save_approximate_observation(run, obs)
        rows = store._rows("SELECT * FROM approximate_observations")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["price_text"])
        self.assertEqual(rows[0]["receipt_mono_ns"], 1000)

    def test_v1_database_rejects_approximate_mode(self):
        """Create v1 database (BOOK_ONLY), then try to open for approximate -> expect schema_version_mismatch."""
        # Create v1 database by opening normally (BOOK_ONLY mode)
        self.open().close()
        
        # Verify it's v1
        conn = self.reader()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        conn.close()
        
        # Try to open for approximate mode - should fail
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.module.RecorderStore.open_approximate(self.fixture.path)
        self.assertEqual(raised.exception.code, "schema_version_mismatch")

    def test_new_v2_database_creates_approximate_tables(self):
        """Open new database in approximate mode, verify approximate tables exist."""
        store = self.module.RecorderStore.open_approximate(self.fixture.path)
        self.addCleanup(store.close)
        
        conn = self.reader()
        
        # Check user_version is 2
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)
        
        # Check all v1 tables exist
        v1_tables = {
            "recording_runs", "clock_anchors", "markets", "market_revisions",
            "sample_slots", "request_attempts", "book_observations",
            "reference_feeds", "reference_reports", "reference_fetches",
            "reference_associations", "resolution_observations",
            "recorder_events", "storage_checkpoints"
        }
        actual_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(v1_tables.issubset(actual_tables), 
            f"Missing v1 tables: {v1_tables - actual_tables}")
        
        # Check new approximate tables exist
        approximate_tables = {"approximate_feeds", "approximate_observations", "approximate_references"}
        for table in approximate_tables:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNotNone(row, f"Missing approximate table: {table}")
        
        # Verify approximate_feeds schema
        info = conn.execute("PRAGMA table_info(approximate_feeds)").fetchall()
        columns = {row[1]: row[2] for row in info}
        self.assertIn("provider", columns)
        self.assertIn("pair", columns)
        self.assertIn("quote_currency", columns)
        
        # Verify approximate_observations schema
        info = conn.execute("PRAGMA table_info(approximate_observations)").fetchall()
        columns = {row[1]: row[2] for row in info}
        required = {"observation_id", "run_id", "provider", "pair", "quote_currency",
                    "price_text", "bid_price_text", "ask_price_text",
                    "source_timestamp_us", "receipt_wall_us", "receipt_server_us",
                    "raw_payload_hash", "raw_payload_size", "transport_type",
                    "parser_revision", "http_status", "result", "error_code"}
        for col in required:
            self.assertIn(col, columns, f"Missing column in approximate_observations: {col}")
        
        # Verify approximate_references schema
        info = conn.execute("PRAGMA table_info(approximate_references)").fetchall()
        columns = {row[1]: row[2] for row in info}
        required = {"reference_id", "run_id", "mode", "reference_kind", "value_text",
                    "window_start_us", "window_end_us", "observation_count",
                    "source_provider", "was_fallback", "verification_status",
                    "receipt_wall_us", "receipt_server_us", "diagnostics_json"}
        for col in required:
            self.assertIn(col, columns, f"Missing column in approximate_references: {col}")
        
        conn.close()

    def test_v2_database_supports_book_only(self):
        """v2 database must also support BOOK_ONLY mode (includes all v1 tables)."""
        # Create v2 database via approximate mode
        self.module.RecorderStore.open_approximate(self.fixture.path).close()
        
        # Now open for BOOK_ONLY - should work
        store = self.module.RecorderStore.open(self.fixture.path)
        self.addCleanup(store.close)
        
        # Verify we can start a BOOK_ONLY run
        from polymarket_bot.recorder.models import RunConfig
        config = RunConfig(mode="BOOK_ONLY", use_fast_path=False)
        run_id = store.start_run(config, wall_us=1_000_000, app_revision="test")
        self.assertIsNotNone(run_id)
        
        # Verify v1 tables work
        conn = self.reader()
        row = conn.execute("SELECT mode FROM recording_runs WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row[0], "BOOK_ONLY")
        conn.close()

    def test_existing_v1_book_only_unaffected(self):
        """Open v1 database, verify it still works for BOOK_ONLY, user_version remains 1."""
        # Create v1 database
        self.open().close()
        
        # Open again for BOOK_ONLY - should work
        store = self.open()
        
        # Verify user_version is still 1
        conn = self.reader()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        
        # Can start a BOOK_ONLY run
        from polymarket_bot.recorder.models import RunConfig
        config = RunConfig(mode="BOOK_ONLY", use_fast_path=False)
        run_id = store.start_run(config, wall_us=1_000_000, app_revision="test")
        self.assertIsNotNone(run_id)
        
        conn.close()

    def test_open_approximate_on_new_database_sets_v2(self):
        """Fresh database opened with open_approximate gets v2 schema."""
        store = self.module.RecorderStore.open_approximate(self.fixture.path)
        self.addCleanup(store.close)
        
        conn = self.reader()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()