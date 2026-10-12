"""Tests for approximate reference metrics separation from exact Chainlink coverage."""
import importlib.util
import unittest
import zlib
from pathlib import Path

from polymarket_bot.recorder.models import RunConfig, CHAINLINK_FEED_ID
from polymarket_bot.recorder.store import RecorderStore
from tests.recorder_support import DatabaseFixture, NOW_US, WINDOW_START_US


class FreeMetricsTests(unittest.TestCase):
    def fixture_v1(self):
        fixture = DatabaseFixture()
        self.addCleanup(fixture.close)
        store = RecorderStore.open(fixture.path)
        self.addCleanup(store.close)
        run_id = store.start_run(RunConfig(), wall_us=NOW_US, app_revision="fixture-v1")
        self._seed_market(store, run_id)
        return store, run_id

    def fixture_v2(self):
        fixture = DatabaseFixture()
        self.addCleanup(fixture.close)
        store = RecorderStore.open_approximate(fixture.path)
        self.addCleanup(store.close)
        run_id = store.start_run(RunConfig(), wall_us=NOW_US, app_revision="fixture-v2")
        self._seed_market(store, run_id)
        return store, run_id

    def _seed_market(self, store, run_id):
        from tests.support import MARKET_ID, TOKEN_UP, TOKEN_DOWN, CONDITION
        with store._connection:
            store._connection.execute("""INSERT INTO markets
                (market_id,event_id,condition_id,slug,question,window_start_us,window_end_us,
                 first_seen_us,up_token_id,down_token_id,up_outcome_index,down_outcome_index,
                 lifecycle,resolution_state,resolution_attempts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (MARKET_ID, "2001", CONDITION, "known-market", "Bitcoin Up or Down?",
                 WINDOW_START_US, WINDOW_START_US + 900_000_000, NOW_US,
                 TOKEN_UP, TOKEN_DOWN, 1, 0, "CURRENT", "PENDING", 0))
            store._connection.execute("""INSERT INTO market_revisions
                (revision_id,market_id,observed_us,run_id,description,resolution_source_url,
                 gamma_raw_z,raw_sizes_json,codec_version,content_hash)
                VALUES (1,?,?,?,?,?,?,?,?,?)""",
                (MARKET_ID, NOW_US, run_id, "known rules", "https://example.test/rules",
                 zlib.compress(b"{}"), '{"gamma":2}', 1, "fixture-revision"))
            store._connection.execute("""INSERT INTO clock_anchors
                (anchor_id,run_id,server_seconds,request_start_mono_ns,request_end_mono_ns,
                 request_start_wall_us,request_end_wall_us,anchor_mono_ns,anchor_server_us,
                 uncertainty_us,status) VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, NOW_US // 1_000_000, 0, 0, NOW_US, NOW_US,
                 0, NOW_US, 1_000_000, "VALID"))

    def metrics(self):
        self.assertIsNotNone(importlib.util.find_spec("polymarket_bot.recorder.metrics"),
                             "The measurement implementation is missing")
        from polymarket_bot.recorder import metrics
        return metrics

    def _seed_slot(self, store, run_id, index, status, committed=None):
        from tests.support import MARKET_ID
        with store._connection:
            cursor = store._connection.execute("""INSERT INTO sample_slots
                (market_id,slot_index,scheduled_us,run_id,metadata_revision_id,status,
                 committed_wall_us,reason,quality_json,reference_status)
                VALUES (?,?,?,?,1,?,?, 'FIXTURE','{}','DISABLED')""",
                (MARKET_ID, index, WINDOW_START_US+index*2_000_000, run_id, status,
                 None if status == "IN_PROGRESS" else (NOW_US if committed is None else committed)))
        return cursor.lastrowid

    def _seed_chainlink_reference(self, store, run_id, slot_id, verified="VERIFIED", learned=None):
        from tests.support import MARKET_ID
        learned = learned or NOW_US
        with store._connection:
            request_id = store._connection.execute("""INSERT INTO request_attempts
                (run_id,operation,attempt_number,method,path,selector_json,started_wall_us,
                 ended_wall_us,start_mono_ns,end_mono_ns,latency_ns,result,response_bytes,
                 response_raw_z,raw_size_bytes,codec_version,raw_complete)
                VALUES (?,?,1,'GET','/ws/v1','[]',?,?,0,?,?,?,100,?,100,1,1)""",
                (run_id, "REFERENCE_LATEST", NOW_US, learned, learned, learned, "SUCCESS",
                 zlib.compress(b'x'*100))).lastrowid
            store._connection.execute("UPDATE request_attempts SET started_wall_us=?,ended_wall_us=?,ended_server_us=? WHERE request_id=?",
                                      (min(NOW_US,learned),learned,learned,request_id))
            fetch = store._connection.execute("""INSERT INTO reference_fetches
                (request_id,feed_id,purpose,origin,target_us,status) VALUES (?,?,?,?,?,'AVAILABLE')""",
                (request_id, CHAINLINK_FEED_ID, "SLOT_CURRENT", "LIVE_LATEST", learned)).lastrowid
            report = store._connection.execute("""INSERT INTO reference_reports
                (feed_id,report_hash,first_fetch_id,valid_from_seconds,observations_seconds,
                 expires_at_seconds,decoded_price_text,decoder_revision,decode_status,
                 verification_status,first_received_wall_us,first_received_server_us,
                 raw_report_z,raw_size_bytes,codec_version)
                VALUES (?,?,?,?,?,?,'60000','v1','DECODED',?,?,?,?,1,1)""",
                (CHAINLINK_FEED_ID, str(fetch), fetch, learned//1_000_000, learned//1_000_000,
                 learned//1_000_000+60, verified, learned, learned, zlib.compress(b'x'))).lastrowid
            store._connection.execute("""INSERT INTO reference_associations
                (market_id,feed_id,role,slot_id,target_us,status,fetch_id,report_id,origin,
                 learned_wall_us,learned_server_us,available_live,selection_policy_revision,attempt_count)
                VALUES (?,?,?,?,?,'AVAILABLE',?,?,?,?,?,?, 'policy-v1',1)""",
                (MARKET_ID, CHAINLINK_FEED_ID, "SLOT_CURRENT", slot_id,
                 learned, fetch, report, "LIVE_LATEST", learned, learned, 1))

    def _seed_approximate_observation(self, store, run_id, provider, receipt_wall_us, result="success"):
        with store._connection:
            store._connection.execute("""INSERT INTO approximate_observations
                (run_id,provider,pair,quote_currency,price_text,bid_price_text,ask_price_text,
                 source_timestamp_us,receipt_wall_us,receipt_server_us,raw_payload_hash,
                 raw_payload_size,transport_type,parser_revision,http_status,result,error_code)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, provider, "BTC/USD", "USD", "50000.00", "49999.00", "50001.00",
                 None if provider == "kraken" else receipt_wall_us - 1_000_000,
                 receipt_wall_us, receipt_wall_us // 1000, "abc123", 100,
                 "rest", f"{provider}-ticker-v1", 200 if result == "success" else 500,
                 result, None if result == "success" else "HTTP_ERROR"))

    def _seed_approximate_reference(self, store, run_id, receipt_wall_us, source_provider, was_fallback=False):
        with store._connection:
            store._connection.execute("""INSERT INTO approximate_references
                (run_id,mode,reference_kind,value_text,window_start_us,window_end_us,
                 observation_count,source_provider,was_fallback,verification_status,
                 receipt_wall_us,receipt_server_us,diagnostics_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, "APPROXIMATE", "APPROXIMATE_SAMPLE_MEAN_60S", "50000.00",
                 receipt_wall_us - 60_000_000, receipt_wall_us,
                 1, source_provider, int(was_fallback), "UNVERIFIED",
                 receipt_wall_us, receipt_wall_us // 1000, "{}"))

    def test_approximate_coverage_separate_from_exact(self):
        """Insert approximate data, verify it doesn't affect Chainlink coverage count."""
        metrics = self.metrics()
        store, run_id = self.fixture_v2()
        slot_id = self._seed_slot(store, run_id, 150, "COMPLETE")
        with store._connection:
            store._connection.execute("UPDATE recording_runs SET mode='REFERENCE_REQUIRED',reference_activation='VERIFIED',activation_evidence_json='{}'")
        self._seed_chainlink_reference(store, run_id, slot_id, verified="VERIFIED", learned=NOW_US)
        self._seed_approximate_reference(store, run_id, NOW_US + 1_000_000, "coinbase", was_fallback=False)
        self._seed_approximate_reference(store, run_id, NOW_US + 2_000_000, "kraken", was_fallback=True)
        self._seed_approximate_observation(store, run_id, "coinbase", NOW_US + 1_000_000)
        self._seed_approximate_observation(store, run_id, "kraken", NOW_US + 2_000_000)

        report = metrics.measure(store, start_us=NOW_US, end_us=NOW_US + 5_000_000)

        self.assertEqual(report.reference_coverage["live_slots"], 1)
        self.assertEqual(report.reference_coverage["paired_slots"], 1)
        self.assertEqual(report.approximate_references_count, 2)
        self.assertEqual(report.approximate_fallback_count, 1)
        self.assertEqual(report.approximate_observations_by_provider["coinbase"], 1)
        self.assertEqual(report.approximate_observations_by_provider["kraken"], 1)
        self.assertEqual(report.approximate_coverage["coinbase"]["references"], 1)
        self.assertEqual(report.approximate_coverage["kraken"]["references"], 1)
        self.assertEqual(report.approximate_coverage["kraken"]["fallbacks"], 1)

    def test_approximate_observations_by_provider(self):
        """Test that approximate observations are grouped by provider."""
        metrics = self.metrics()
        store, run_id = self.fixture_v2()

        for i in range(3):
            self._seed_approximate_observation(store, run_id, "coinbase", NOW_US + i * 1_000_000)
        for i in range(2):
            self._seed_approximate_observation(store, run_id, "kraken", NOW_US + i * 1_000_000)
        self._seed_approximate_observation(store, run_id, "coinbase", NOW_US + 10_000_000, result="error")

        report = metrics.measure(store, start_us=NOW_US, end_us=NOW_US + 20_000_000)

        self.assertEqual(report.approximate_observations_by_provider["coinbase"], 4)
        self.assertEqual(report.approximate_observations_by_provider["kraken"], 2)
        self.assertEqual(sum(report.approximate_observations_by_provider.values()), 6)

    def test_approximate_fallback_count(self):
        """Test that fallback count tracks was_fallback in approximate_references."""
        metrics = self.metrics()
        store, run_id = self.fixture_v2()

        self._seed_approximate_reference(store, run_id, NOW_US + 1_000_000, "coinbase", was_fallback=False)
        self._seed_approximate_reference(store, run_id, NOW_US + 2_000_000, "coinbase", was_fallback=False)
        self._seed_approximate_reference(store, run_id, NOW_US + 3_000_000, "kraken", was_fallback=True)
        self._seed_approximate_reference(store, run_id, NOW_US + 4_000_000, "coinbase,kraken", was_fallback=True)

        report = metrics.measure(store, start_us=NOW_US, end_us=NOW_US + 10_000_000)

        self.assertEqual(report.approximate_references_count, 4)
        self.assertEqual(report.approximate_fallback_count, 2)
        self.assertEqual(report.approximate_coverage["coinbase"]["fallbacks"], 1)
        self.assertEqual(report.approximate_coverage["kraken"]["fallbacks"], 2)

    def test_interval_counts_and_unavailable_slot_coverage(self):
        store, run = self.fixture_v2()
        self._seed_approximate_observation(store, run, "coinbase", NOW_US - 1)
        self._seed_approximate_observation(store, run, "coinbase", NOW_US + 1)
        self._seed_approximate_observation(store, run, "coinbase", NOW_US + 2, result="error")
        self._seed_approximate_reference(store, run, NOW_US + 1, "coinbase")
        self._seed_approximate_reference(store, run, NOW_US + 2, "kraken", True)
        store._connection.execute("UPDATE approximate_references SET value_text=NULL WHERE was_fallback=1")
        store._connection.commit()
        report = self.metrics().measure(store, start_us=NOW_US, end_us=NOW_US + 5)
        self.assertEqual(report.approximate_observations_by_provider["coinbase"], 2)
        self.assertEqual(report.approximate_coverage["coinbase"]["successes"], 1)
        self.assertEqual(report.approximate_fallback_count, 0)
        self.assertIsNone(report.approximate_coverage["coinbase"]["slot_coverage_percent"])
        self.assertEqual(report.approximate_coverage["coinbase"]["coverage_unavailable_reason"],
                         "MISSING_SLOT_LINKAGE_AND_DURABLE_AS_OF_EVIDENCE")

    def test_v1_database_approximate_metrics_none(self):
        """v1 DB has no approximate tables → metrics return None or zero."""
        metrics = self.metrics()
        store, run_id = self.fixture_v1()
        slot_id = self._seed_slot(store, run_id, 150, "COMPLETE")
        self._seed_chainlink_reference(store, run_id, slot_id, verified="VERIFIED", learned=NOW_US)

        report = metrics.measure(store, start_us=NOW_US, end_us=NOW_US + 2_000_000)

        self.assertEqual(report.approximate_references_count, None)
        self.assertEqual(report.approximate_fallback_count, None)
        self.assertEqual(report.approximate_observations_by_provider, {})
        self.assertEqual(report.approximate_coverage, {})


if __name__ == "__main__":
    unittest.main()