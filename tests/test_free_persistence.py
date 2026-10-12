"""Persistence tests for approximate evidence (Task 6)."""
import unittest
import tempfile
import shutil
from pathlib import Path
from decimal import Decimal

from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    ApproximateReferenceReport,
)
from polymarket_bot.recorder.models import RunConfig
from tests.recorder_support import DatabaseFixture, NOW_US


class FreePersistenceTests(unittest.TestCase):
    def setUp(self):
        # Create temp directory manually for better Windows cleanup control
        self.temp_dir = Path(tempfile.mkdtemp(prefix="free-persistence-test-"))
        self.db_path = self.temp_dir / "recorder.sqlite3"
        self.store = RecorderStore.open_approximate(self.db_path)
        self.addCleanup(self._cleanup)
        self.run_id = self.store.start_run(
            RunConfig(mode="APPROXIMATE_REFERENCE"),
            wall_us=NOW_US,
            app_revision="test",
            offline_reference=None
        )

    def _cleanup(self):
        """Close store and remove temp directory with retry for Windows locking."""
        if hasattr(self, 'store') and self.store:
            self.store.close()
        if hasattr(self, 'temp_dir') and self.temp_dir.exists():
            import time
            for _ in range(5):
                try:
                    shutil.rmtree(self.temp_dir)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def test_save_approximate_observation(self):
        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        obs_id = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id > 0
        row = self.store._one("SELECT * FROM approximate_observations WHERE observation_id=?", (obs_id,))
        assert row['provider'] == 'coinbase'
        assert row['price_text'] == '50000'
        assert row['bid_price_text'] == '49999'
        assert row['ask_price_text'] == '50001'
        assert row['source_timestamp_us'] == 123456789
        assert row['receipt_wall_us'] == 999
        assert row['receipt_server_us'] == 999
        assert row['raw_payload_hash'] == 'abc'
        assert row['raw_payload_size'] == 100
        assert row['transport_type'] == 'rest'
        assert row['parser_revision'] == 'v1'
        assert row['http_status'] == 200
        assert row['result'] == 'success'
        assert row['error_code'] is None

    def test_save_approximate_observation_with_none_bid_ask(self):
        obs = ApproximateObservation(
            provider='kraken',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=None,
            receipt_wall_us=1000,
            receipt_server_us=1000,
            raw_payload_hash='def',
            raw_payload_size=200,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        obs_id = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id > 0
        row = self.store._one("SELECT * FROM approximate_observations WHERE observation_id=?", (obs_id,))
        assert row['provider'] == 'kraken'
        assert row['price_text'] == '50000'
        assert row['bid_price_text'] is None
        assert row['ask_price_text'] is None
        assert row['source_timestamp_us'] is None

    def test_save_approximate_reference(self):
        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=Decimal('50000'),
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=30,
            source_provider='coinbase',
            was_fallback=False,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000
        )
        ref_id = self.store.save_approximate_reference(self.run_id, report)
        assert ref_id > 0
        row = self.store._one("SELECT * FROM approximate_references WHERE reference_id=?", (ref_id,))
        assert row['mode'] == 'APPROXIMATE'
        assert row['verification_status'] == 'UNVERIFIED'
        assert row['reference_kind'] == 'APPROXIMATE_SAMPLE_MEAN_60S'
        assert row['value_text'] == '50000'
        assert row['window_start_us'] == 0
        assert row['window_end_us'] == 60_000_000
        assert row['observation_count'] == 30
        assert row['source_provider'] == 'coinbase'
        assert row['was_fallback'] == 0
        assert row['receipt_wall_us'] == 60_000_000
        assert row['receipt_server_us'] == 60_000_000
        assert row['diagnostics_json'] == '[]'

    def test_save_approximate_reference_with_none_value(self):
        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=None,
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=0,
            source_provider='',
            was_fallback=True,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000,
            diagnostics=('MISSING_OBSERVATIONS',)
        )
        ref_id = self.store.save_approximate_reference(self.run_id, report)
        assert ref_id > 0
        row = self.store._one("SELECT * FROM approximate_references WHERE reference_id=?", (ref_id,))
        assert row['value_text'] is None
        assert row['observation_count'] == 0
        assert row['was_fallback'] == 1
        assert row['diagnostics_json'] == '["MISSING_OBSERVATIONS"]'

    def test_v1_database_rejects_approximate_save(self):
        # Create v1 database
        import tempfile
        from pathlib import Path
        temp_dir = Path(tempfile.mkdtemp(prefix="recorder-v1-test-"))
        v1_path = temp_dir / "recorder.sqlite3"

        store_v1 = RecorderStore.open(v1_path)
        store_v1.close()

        # Now try to open v1 for approximate - should fail
        with self.assertRaises(RecorderStoreError) as raised:
            RecorderStore.open_approximate(v1_path)
        self.assertEqual(raised.exception.code, "schema_version_mismatch")

        # Cleanup
        import shutil
        for _ in range(5):
            try:
                shutil.rmtree(temp_dir)
                break
            except PermissionError:
                import time
                time.sleep(0.1)

    def test_approximate_mode_start_run(self):
        # Verify APPROXIMATE_REFERENCE mode works with v2 database
        run_id = self.store.start_run(
            RunConfig(mode="APPROXIMATE_REFERENCE"),
            wall_us=NOW_US + 1000,
            app_revision="test2",
            offline_reference=None
        )
        assert run_id is not None
        run = self.store._one("SELECT * FROM recording_runs WHERE run_id=?", (run_id,))
        assert run['mode'] == 'APPROXIMATE_REFERENCE'
        assert run['reference_activation'] == 'DISABLED'
        assert run['state'] == 'RUNNING'

    def test_save_approximate_observation_requires_writer(self):
        # Read-only store should reject writes
        read_only_store = RecorderStore.read_only(self.db_path)
        self.addCleanup(read_only_store.close)

        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        with self.assertRaises(RecorderStoreError) as raised:
            read_only_store.save_approximate_observation(self.run_id, obs)
        self.assertEqual(raised.exception.code, "read_only")

    def test_save_approximate_reference_requires_writer(self):
        read_only_store = RecorderStore.read_only(self.db_path)
        self.addCleanup(read_only_store.close)

        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=Decimal('50000'),
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=30,
            source_provider='coinbase',
            was_fallback=False,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000
        )
        with self.assertRaises(RecorderStoreError) as raised:
            read_only_store.save_approximate_reference(self.run_id, report)
        self.assertEqual(raised.exception.code, "read_only")

    def test_save_approximate_observation_unique_constraint(self):
        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        # First save succeeds
        obs_id1 = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id1 > 0

        # Second save with same run_id, provider, receipt_wall_us should fail (unique constraint)
        with self.assertRaises(RecorderStoreError) as raised:
            self.store.save_approximate_observation(self.run_id, obs)
        # SQLite constraint error wraps in storage_failure
        self.assertEqual(raised.exception.code, "storage_failure")


if __name__ == "__main__":
    unittest.main()