"""Persistence contracts tested against real local SQLite and OS locks."""
import importlib
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
from threading import Thread
import unittest
import zlib

from tests.recorder_support import DatabaseFixture


# Literal column contract transcribed from design section 7; T/I/B are SQL types.
COLUMNS = {
    "recording_runs": "run_id:T started_wall_us:I ended_wall_us:I mode:T reference_activation:T state:T app_revision:T config_json:T activation_evidence_json:T last_committed_wall_us:I exit_reason:T",
    "clock_anchors": "anchor_id:I run_id:T server_seconds:I request_start_mono_ns:I request_end_mono_ns:I request_start_wall_us:I request_end_wall_us:I anchor_mono_ns:I anchor_server_us:I uncertainty_us:I status:T diagnostic_json:T",
    "markets": "market_id:T event_id:T condition_id:T slug:T question:T window_start_us:I window_end_us:I first_seen_us:I up_token_id:T down_token_id:T up_outcome_index:I down_outcome_index:I lifecycle:T resolution_state:T resolution_next_due_us:I resolution_attempts:I selected_resolution_id:I",
    "market_revisions": "revision_id:I market_id:T observed_us:I run_id:T description:T resolution_source_url:T active:I closed:I accepting_orders:I enable_order_book:I fee_config_json:T tick_size_text:T min_order_size_text:T delay_config_json:T gamma_raw_z:B clob_config_raw_z:B raw_sizes_json:T codec_version:I content_hash:T",
    "sample_slots": "slot_id:I market_id:T slot_index:I scheduled_us:I run_id:T anchor_id:I metadata_revision_id:I status:T started_server_us:I finished_server_us:I committed_wall_us:I start_mono_ns:I end_mono_ns:I delay_us:I reason:T quality_json:T reference_status:T",
    "request_attempts": "request_id:I run_id:T market_id:T slot_id:I anchor_id:I operation:T attempt_number:I method:T path:T selector_json:T started_wall_us:I ended_wall_us:I started_server_us:I ended_server_us:I start_mono_ns:I end_mono_ns:I latency_ns:I http_status:I result:T error_code:T response_bytes:I response_raw_z:B raw_size_bytes:I codec_version:I raw_complete:I",
    "book_observations": "slot_id:I outcome:T expected_token_id:T expected_condition_id:T request_id:I status:T error_code:T quality_json:T source_timestamp_raw:T source_us:I book_hash:T best_bid_text:T best_ask_text:T bid_size_text:T ask_size_text:T midpoint_text:T spread_text:T tick_size_text:T min_order_size_text:T bid_level_count:I ask_level_count:I",
    "reference_feeds": "feed_id:T name:T environment:T source_url:T catalog_metadata_json:T lookback_seconds:I report_schema_version:I price_scale:I decoder_revision:T verified_us:I verification_evidence_json:T",
    "reference_reports": "report_id:I feed_id:T report_hash:T first_fetch_id:I valid_from_seconds:I observations_seconds:I expires_at_seconds:I decoded_price_text:T decoder_revision:T decode_status:T verification_status:T first_received_wall_us:I first_received_server_us:I raw_report_z:B raw_size_bytes:I codec_version:I",
    "reference_fetches": "fetch_id:I request_id:I feed_id:T purpose:T origin:T target_us:I status:T error_code:T response_raw_z:B raw_size_bytes:I codec_version:I",
    "reference_associations": "association_id:I market_id:T feed_id:T role:T slot_id:I target_us:I status:T reason:T fetch_id:I report_id:I origin:T learned_wall_us:I learned_server_us:I available_live:I selection_policy_revision:T next_due_us:I attempt_count:I",
    "resolution_observations": "resolution_id:I market_id:T request_id:I condition_id:T status_raw:T payouts_raw_json:T provenance_json:T reported_resolved_us:I reported_time_raw:T learned_wall_us:I learned_server_us:I mapped_outcome:T classification:T response_hash:T raw_resolution_z:B raw_size_bytes:I codec_version:I",
    "recorder_events": "event_id:I run_id:T market_id:T kind:T started_us:I ended_us:I detail_json:T",
    "storage_checkpoints": "checkpoint_id:I run_id:T observed_wall_us:I db_file_bytes:I page_size:I page_count:I freelist_count:I journal_bytes:I committed_slot_count:I reference_report_count:I book_raw_bytes:I book_compressed_bytes:I reference_raw_bytes:I reference_compressed_bytes:I",
}
FEED = "0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f"
RAW = zlib.compress(b"{}")
ROWS = {
    "recording_runs": dict(run_id="run", started_wall_us=1, mode="BOOK_ONLY", reference_activation="DISABLED", state="RUNNING", app_revision="test", config_json="{}"),
    "reference_feeds": dict(feed_id=FEED, name="BTC/USD-Streams-TWAP-60s-mainnet-production", environment="mainnet", source_url="https://example.test/feed", catalog_metadata_json="{}", lookback_seconds=60, report_schema_version=2),
    "markets": dict(market_id="market", event_id="event", condition_id="condition", slug="slug", question="question", window_start_us=0, window_end_us=900_000_000, first_seen_us=1, up_token_id="11", down_token_id="22", up_outcome_index=1, down_outcome_index=0, lifecycle="CURRENT", resolution_state="PENDING", resolution_attempts=0),
    "clock_anchors": dict(anchor_id=1, run_id="run", server_seconds=1, request_start_mono_ns=0, request_end_mono_ns=100, request_start_wall_us=1, request_end_wall_us=2, anchor_mono_ns=50, anchor_server_us=1_000_000, uncertainty_us=1_000_000, status="VALID"),
    "market_revisions": dict(revision_id=1, market_id="market", observed_us=1, run_id="run", description="rules", resolution_source_url="https://example.test/rules", gamma_raw_z=RAW, raw_sizes_json='{"gamma":2}', codec_version=1, content_hash="hash"),
    "sample_slots": dict(slot_id=1, market_id="market", slot_index=0, scheduled_us=0, run_id="run", anchor_id=1, metadata_revision_id=1, status="IN_PROGRESS", reason="", quality_json="{}", reference_status="REFERENCE_INCOMPLETE"),
    "request_attempts": dict(request_id=1, run_id="run", market_id="market", slot_id=1, anchor_id=1, operation="BOOK_BATCH", attempt_number=1, method="POST", path="/books", selector_json='["11","22"]', started_wall_us=1, ended_wall_us=2, start_mono_ns=0, end_mono_ns=100, latency_ns=100, result="SUCCESS", raw_complete=1),
    "reference_fetches": dict(fetch_id=1, request_id=1, feed_id=FEED, purpose="SLOT_CURRENT", origin="LIVE_LATEST", status="UNAVAILABLE"),
    "reference_reports": dict(report_id=1, feed_id=FEED, report_hash="report", first_fetch_id=1, decode_status="UNVERIFIED", verification_status="UNVERIFIED", first_received_wall_us=2, raw_report_z=RAW, raw_size_bytes=2, codec_version=1),
    "reference_associations": dict(association_id=1, market_id="market", feed_id=FEED, role="SLOT_CURRENT", slot_id=1, target_us=0, status="DISABLED", origin="NOT_FETCHED", learned_wall_us=2, available_live=0, attempt_count=0),
    "resolution_observations": dict(resolution_id=1, market_id="market", request_id=1, condition_id="condition", provenance_json="{}", learned_wall_us=2, classification="PENDING", response_hash="hash", raw_resolution_z=RAW, raw_size_bytes=2, codec_version=1),
    "book_observations": dict(slot_id=1, outcome="UP", expected_token_id="11", expected_condition_id="condition", request_id=1, status="UNAVAILABLE", quality_json="{}"),
    "recorder_events": dict(event_id=1, run_id="run", kind="STARTUP", started_us=1, detail_json="{}"),
    "storage_checkpoints": dict(checkpoint_id=1, run_id="run", observed_wall_us=1, db_file_bytes=4096, page_size=4096, page_count=1, freelist_count=0, journal_bytes=0, committed_slot_count=0, reference_report_count=0, book_raw_bytes=0, book_compressed_bytes=0, reference_raw_bytes=0, reference_compressed_bytes=0),
}


def insert(connection, table, values):
    keys = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({keys}) VALUES ({placeholders})", tuple(values.values()))


class SchemaTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("polymarket_bot.recorder.store")
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, "RecorderStore schema and ownership foundation is missing")
        self.fixture = DatabaseFixture()
        self.addCleanup(self.fixture.close)

    def open(self, path=None):
        store = self.module.RecorderStore.open(path or self.fixture.path)
        self.addCleanup(store.close)
        return store

    def reader(self):
        reader = sqlite3.connect(self.fixture.path)
        reader.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(reader.close)
        return reader

    def seed(self, connection):
        for table, row in ROWS.items():
            insert(connection, table, row)
        connection.commit()

    def test_exact_schema_constraints(self):
        store = self.open()
        connection = store._connection
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(COLUMNS))
        for name, contract in COLUMNS.items():
            types = {"T": "TEXT", "I": "INTEGER", "B": "BLOB"}
            expected = [(col.split(":")[0], types[col.split(":")[1]]) for col in contract.split()]
            info = connection.execute(f"PRAGMA table_info({name})").fetchall()
            self.assertEqual([(row[1], row[2]) for row in info], expected, name)
            expected_pk = ["slot_id", "outcome"] if name == "book_observations" else [expected[0][0]]
            self.assertEqual([row[1] for row in info if row[5]], expected_pk, name)
        for pragma, expected in (("user_version", 1), ("foreign_keys", 1), ("journal_mode", "delete"), ("synchronous", 2)):
            self.assertEqual(connection.execute(f"PRAGMA {pragma}").fetchone()[0], expected)
        self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(book_observations)") if row[5]], ["slot_id", "outcome"])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_foreign_keys_and_required_indexes(self):
        self.open()
        connection = self.reader()
        expected_fks = {
            "clock_anchors": {("run_id", "recording_runs", "run_id")},
            "markets": {("selected_resolution_id", "resolution_observations", "resolution_id")},
            "market_revisions": {("market_id", "markets", "market_id"), ("run_id", "recording_runs", "run_id")},
            "sample_slots": {("market_id", "markets", "market_id"), ("run_id", "recording_runs", "run_id"), ("anchor_id", "clock_anchors", "anchor_id"), ("metadata_revision_id", "market_revisions", "revision_id")},
            "request_attempts": {("run_id", "recording_runs", "run_id"), ("market_id", "markets", "market_id"), ("slot_id", "sample_slots", "slot_id"), ("anchor_id", "clock_anchors", "anchor_id")},
            "book_observations": {("slot_id", "sample_slots", "slot_id"), ("request_id", "request_attempts", "request_id")},
            "reference_reports": {("feed_id", "reference_feeds", "feed_id"), ("first_fetch_id", "reference_fetches", "fetch_id")},
            "reference_fetches": {("request_id", "request_attempts", "request_id"), ("feed_id", "reference_feeds", "feed_id")},
            "reference_associations": {("market_id", "markets", "market_id"), ("feed_id", "reference_feeds", "feed_id"), ("slot_id", "sample_slots", "slot_id"), ("fetch_id", "reference_fetches", "fetch_id"), ("report_id", "reference_reports", "report_id")},
            "resolution_observations": {("market_id", "markets", "market_id"), ("request_id", "request_attempts", "request_id")},
            "recorder_events": {("run_id", "recording_runs", "run_id"), ("market_id", "markets", "market_id")},
            "storage_checkpoints": {("run_id", "recording_runs", "run_id")},
        }
        for table in COLUMNS:
            actual = {(row[3], row[2], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
            self.assertEqual(actual, expected_fks.get(table, set()), table)
        required = {
            "clock_anchors": [("run_id", "anchor_mono_ns")],
            "markets": [("window_start_us",), ("resolution_state", "resolution_next_due_us"), ("condition_id",)],
            "market_revisions": [("market_id", "observed_us")],
            "sample_slots": [("market_id", "slot_index"), ("market_id", "status"), ("scheduled_us",)],
            "request_attempts": [("operation", "started_wall_us"), ("slot_id",)],
            "book_observations": [("expected_token_id", "source_us")],
            "reference_reports": [("feed_id", "report_hash"), ("feed_id", "observations_seconds")],
            "reference_fetches": [("request_id",)],
            "reference_associations": [("slot_id", "learned_wall_us"), ("market_id", "role", "learned_wall_us")],
            "resolution_observations": [("request_id", "market_id"), ("market_id", "learned_wall_us")],
            "recorder_events": [("kind", "started_us")],
            "storage_checkpoints": [("observed_wall_us",)],
        }
        for table, expected in required.items():
            indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
            columns = {tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")): index for index in indexes}
            for key in expected:
                self.assertIn(key, columns, (table, key))
            if table == "reference_associations":
                for key in expected:
                    self.assertEqual((columns[key][2], columns[key][4]), (1, 1))
        self.seed(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE clock_anchors SET run_id='absent'")

    def test_checks_reject_invalid_enums_booleans_and_counts(self):
        self.open()
        connection = self.reader()
        self.seed(connection)
        bad = {
            "recording_runs": dict(mode="TRADING", reference_activation="UNKNOWN", state="UNKNOWN"),
            "clock_anchors": dict(status="UNKNOWN", uncertainty_us=-1),
            "markets": dict(lifecycle="UNKNOWN", resolution_state="UNKNOWN", resolution_attempts=-1, up_outcome_index=2, down_outcome_index=1, down_token_id="11", window_end_us=899_999_999),
            "market_revisions": dict(active=2, closed=-1, accepting_orders=2, enable_order_book=3),
            "sample_slots": dict(slot_index=-1, status="UNKNOWN", delay_us=-1),
            "request_attempts": dict(raw_complete=2, attempt_number=-1, latency_ns=-1, response_bytes=-1, raw_size_bytes=-1),
            "book_observations": dict(outcome="YES", status="UNKNOWN", bid_level_count=-1, ask_level_count=-1),
            "reference_feeds": dict(lookback_seconds=30, report_schema_version=3),
            "reference_reports": dict(decode_status="UNKNOWN", raw_size_bytes=-1),
            "reference_fetches": dict(purpose="UNKNOWN", origin="UNKNOWN", raw_size_bytes=-1),
            "reference_associations": dict(role="UNKNOWN", status="UNKNOWN", origin="UNKNOWN", available_live=2, attempt_count=-1),
            "resolution_observations": dict(mapped_outcome="YES", raw_size_bytes=-1),
            "recorder_events": dict(kind="TRADE"),
            "storage_checkpoints": {key: -1 for key in ROWS["storage_checkpoints"] if key not in ("checkpoint_id", "run_id", "observed_wall_us")},
        }
        for table, changes in bad.items():
            for key, value in changes.items():
                with self.subTest(table=table, column=key), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"UPDATE {table} SET {key}=?", (value,))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE sample_slots SET slot_index=450")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE recording_runs SET mode='REFERENCE_REQUIRED'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE recording_runs SET reference_activation='VERIFIED'")

    def test_uniqueness_and_partial_association_indexes(self):
        self.open()
        connection = self.reader()
        self.seed(connection)
        conflicts = (
            ("markets", dict(market_id="other")),
            ("sample_slots", dict(slot_id=2)),
            ("reference_reports", dict(report_id=2)),
            ("reference_fetches", dict(fetch_id=2)),
            ("reference_associations", dict(association_id=2)),
            ("resolution_observations", dict(resolution_id=2)),
            ("book_observations", {}),
        )
        for table, changes in conflicts:
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                insert(connection, table, ROWS[table] | changes)
        insert(connection, "reference_associations", ROWS["reference_associations"] | dict(association_id=3, learned_wall_us=3))
        boundary = ROWS["reference_associations"] | dict(association_id=4, role="OPENING", slot_id=None)
        insert(connection, "reference_associations", boundary)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(connection, "reference_associations", boundary | dict(association_id=5))
        insert(connection, "reference_associations", boundary | dict(association_id=6, role="CLOSING", target_us=900_000_000))

    def test_unknown_schema_preserves_file(self):
        with closing(sqlite3.connect(self.fixture.path)) as connection:
            connection.execute("PRAGMA user_version=3")
            connection.execute("CREATE TABLE evidence (body TEXT)")
            connection.execute("INSERT INTO evidence VALUES ('original')")
            connection.commit()
        original = self.fixture.path.read_bytes()
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "unknown_schema")
        self.assertEqual(self.fixture.path.read_bytes(), original)

    def test_corruption_and_existing_empty_file_are_preserved(self):
        for original in (b"", b"not a SQLite database", b"SQLite format 3\0" + b"broken" * 100):
            with self.subTest(original_length=len(original)):
                self.fixture.path.write_bytes(original)
                with self.assertRaises(self.module.RecorderStoreError):
                    self.open()
                self.assertEqual(self.fixture.path.read_bytes(), original)

    def test_existing_version_one_requires_exact_schema(self):
        self.open().close()
        with closing(sqlite3.connect(self.fixture.path)) as connection:
            connection.execute("DROP INDEX sample_slots_scheduled")
            connection.commit()
        original = self.fixture.path.read_bytes()
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "invalid_schema")
        self.assertEqual(self.fixture.path.read_bytes(), original)

    def test_startup_checks_foreign_key_integrity(self):
        self.open().close()
        with closing(sqlite3.connect(self.fixture.path)) as connection:
            insert(connection, "clock_anchors", ROWS["clock_anchors"])
            connection.commit()
        original = self.fixture.path.read_bytes()
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "database_integrity")
        self.assertEqual(self.fixture.path.read_bytes(), original)

    def test_writer_lock_survives_transaction_gaps(self):
        store = self.open()
        store._connection.commit()
        alias = self.fixture.path.parent / "." / self.fixture.path.name
        for path in (self.fixture.path, alias):
            with self.subTest(path=path), self.assertRaises(self.module.RecorderStoreError) as raised:
                self.open(path)
            self.assertEqual(raised.exception.code, "writer_locked")
        store.close()
        self.open()

    def test_failed_initialization_releases_ownership(self):
        self.fixture.path.write_bytes(b"existing evidence")
        for _ in range(2):
            with self.assertRaises(self.module.RecorderStoreError) as raised:
                self.open()
            self.assertNotEqual(raised.exception.code, "writer_locked")
        self.assertEqual(self.fixture.path.read_bytes(), b"existing evidence")

    def test_failed_close_cannot_release_writer_ownership(self):
        store = self.open()
        self.addCleanup(store._connection.close)
        errors = []

        def close_from_other_thread():
            try:
                store.close()
            except sqlite3.ProgrammingError as exc:
                errors.append(exc)

        thread = Thread(target=close_from_other_thread)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(store._connection.execute("SELECT 1").fetchone(), (1,))
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "writer_locked")
        store.close()
        self.open()

    def codec(self):
        self.assertTrue(callable(getattr(self.module, "encode_evidence", None)), "Bounded evidence encoder is missing")
        self.assertTrue(callable(getattr(self.module, "decode_evidence", None)), "Bounded evidence decoder is missing")
        return self.module.encode_evidence, self.module.decode_evidence

    def test_codec_is_bounded_and_byte_exact(self):
        encode, decode = self.codec()
        originals = (b"", bytes(range(256)), b' {"size":"1e+99999999999999999999","price":1e-99999999999999999999} \r\n', b"A" * 10_485_760)
        for original in originals:
            with self.subTest(size=len(original)):
                encoded = encode(original)
                self.assertEqual(encoded[1:], (len(original), 1))
                self.assertEqual(zlib.decompress(encoded[0]), original)
                self.assertEqual(decode(*encoded), original)
        with self.assertRaises(self.module.EvidenceError) as raised:
            encode(b"A" * 10_485_761)
        self.assertEqual(raised.exception.code, "invalid_evidence")

    def test_codec_rejects_invalid_sizes_versions_and_types(self):
        encode, decode = self.codec()
        blob = zlib.compress(b"abc")
        for size, version in ((-1, 1), (2, 1), (4, 1), (10_485_761, 1), (3, 0), (3, 2), (3, True), (True, 1), ("3", 1), (3, 1.0)):
            with self.subTest(size=size, version=version), self.assertRaises(self.module.EvidenceError):
                decode(blob, size, version)
        for value in ("abc", bytearray(b"abc"), None):
            with self.subTest(value_type=type(value)), self.assertRaises(self.module.EvidenceError):
                encode(value)
            with self.assertRaises(self.module.EvidenceError):
                decode(value, 3, 1)

    def test_codec_rejects_invalid_truncated_trailing_and_bomb_streams(self):
        _, decode = self.codec()
        blob = zlib.compress(b"abc")
        cases = ((b"", 0), (b"invalid", 7), (blob[:-1], 3), (blob + b"trailing", 3), (blob + blob, 3), (zlib.compress(b"A" * 10_485_761), 10_485_760), (zlib.compress(b"A" * 1_000_000), 1))
        for encoded, size in cases:
            with self.subTest(blob_size=len(encoded), size=size), self.assertRaises(self.module.EvidenceError) as raised:
                decode(encoded, size, 1)
            self.assertEqual(raised.exception.code, "invalid_evidence")

    def child_owner(self):
        """Use explicit src-layout imports, with no HTTP or production sampling."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        script = """
import os
from pathlib import Path
import sys
sys.path[:0] = [str(Path(sys.argv[2]) / 'src'), sys.argv[2]]
import tests
from polymarket_bot.recorder.store import RecorderStore
store = RecorderStore.open(Path(sys.argv[1]))
store._connection.commit()
print('OWNER_READY', flush=True)
sys.stdin.readline()
os._exit(0)
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.fixture.path), str(root)],
                                 cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

        self.addCleanup(cleanup)
        # Bound the real pipe readiness signal so a broken child cannot hang tests.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready = executor.submit(child.stdout.readline)
            try:
                self.assertEqual(ready.result(timeout=10).strip(), "OWNER_READY")
            except Exception as exc:
                if child.poll() is None:
                    child.kill()
                _, stderr = child.communicate(timeout=10)
                self.fail(f'Child owner readiness failed: executable={sys.executable!r}, cwd={str(root)!r}, '
                          f'PYTHONPATH={os.environ.get("PYTHONPATH")!r}, exit={child.returncode}, '
                          f'stderr={stderr!r}, readiness={exc!r}')
        return child

    @unittest.skipUnless(os.name == "nt", "Windows process ownership contract")
    def test_live_process_owner_rejects_another_store(self):
        child = self.child_owner()
        self.assertIsNone(child.poll())
        with self.assertRaises(self.module.RecorderStoreError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "writer_locked")
        self.assertIsNone(child.poll())

    @unittest.skipUnless(os.name == "nt", "Windows process ownership contract")
    def test_abrupt_process_exit_releases_writer_lock(self):
        child = self.child_owner()
        with self.assertRaises(self.module.RecorderStoreError):
            self.open()
        child.kill()
        child.communicate(timeout=10)
        self.assertIsNotNone(child.returncode)
        # The lock file remains; its existence does not imply ownership.
        self.assertTrue(self.fixture.path.with_name("recorder.sqlite3.writer.lock").exists())
        self.open().close()
        self.open()


if __name__ == "__main__":
    unittest.main()
