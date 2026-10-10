"""Hand-counted measurement fixtures; all SQLite files are disposable and offline."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from tests.recorder_support import DatabaseFixture, seed_writer_market, NOW_US


class RecorderMetricsTests(unittest.TestCase):
    def fixture(self):
        fixture = DatabaseFixture()
        self.addCleanup(fixture.close)
        store = RecorderStore.open(fixture.path)
        self.addCleanup(store.close)
        run_id = store.start_run(RunConfig(), wall_us=NOW_US, app_revision="fixture-v1")
        seed_writer_market(store, run_id)
        return store, run_id

    def metrics(self):
        self.assertIsNotNone(importlib.util.find_spec("polymarket_bot.recorder.metrics"),
                             "The measurement implementation is missing")
        from polymarket_bot.recorder import metrics
        return metrics

    def checkpoint(self, store, run_id, wall_us, size, *, terminals=0):
        with store._connection:
            store._connection.execute("""INSERT INTO storage_checkpoints
                (run_id,observed_wall_us,db_file_bytes,page_size,page_count,freelist_count,
                 journal_bytes,committed_slot_count,reference_report_count,book_raw_bytes,
                 book_compressed_bytes,reference_raw_bytes,reference_compressed_bytes)
                VALUES (?,?,?,4096,10,2,0,?,0,0,0,0,0)""", (run_id, wall_us, size, terminals))

    def slot(self, store, run_id, index, status, *, committed=None):
        from tests.support import MARKET_ID
        from tests.recorder_support import WINDOW_START_US
        with store._connection:
            cursor = store._connection.execute("""INSERT INTO sample_slots
                (market_id,slot_index,scheduled_us,run_id,metadata_revision_id,status,
                 committed_wall_us,reason,quality_json,reference_status)
                VALUES (?,?,?,?,1,?,?, 'FIXTURE','{}','DISABLED')""",
                (MARKET_ID, index, WINDOW_START_US+index*2_000_000, run_id, status,
                 None if status == "IN_PROGRESS" else (NOW_US if committed is None else committed)))
        return cursor.lastrowid

    def request(self, store, run_id, *, latency=1, operation="BOOK_BATCH", result="SUCCESS"):
        import zlib
        with store._connection:
            cursor = store._connection.execute("""INSERT INTO request_attempts
                (run_id,operation,attempt_number,method,path,selector_json,started_wall_us,
                 ended_wall_us,start_mono_ns,end_mono_ns,latency_ns,result,response_bytes,
                 response_raw_z,raw_size_bytes,codec_version,raw_complete)
                VALUES (?,?,1,'GET','/book','[]',?,?,0,?,?,?,100,?,100,1,1)""",
                (run_id, operation, NOW_US, NOW_US, latency, latency, result, zlib.compress(b'x'*100)))
        return cursor.lastrowid

    def test_growth_formula_and_shared_batch_bytes(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        slot_id = self.slot(store, run_id, 150, "COMPLETE", committed=NOW_US+1)
        request_id = self.request(store, run_id)
        with store._connection:
            for outcome, depth in (("UP", 4), ("DOWN", 8)):
                store._connection.execute("""INSERT INTO book_observations
                    (slot_id,outcome,expected_token_id,expected_condition_id,request_id,status,
                     quality_json,bid_level_count,ask_level_count) VALUES (?,?, '1','c',?,'VALID','{}',?,0)""",
                    (slot_id, outcome, request_id, depth))
        self.checkpoint(store, run_id, NOW_US, 40960)
        self.checkpoint(store, run_id, NOW_US+86_400_000_000, 1_000_040_960, terminals=1)
        report = metrics.measure(store, start_us=NOW_US, end_us=NOW_US+86_400_000_000)
        self.assertEqual((report.elapsed_seconds, report.physical_growth_bytes,
                          report.mb_per_day, report.gb_per_30_days), (86400, 1_000_000_000, 1000, 30))
        self.assertEqual(report.average_depth, 6)
        self.assertEqual(report.evidence_bytes["book_raw_bytes"], 100)
        self.assertEqual(report.bytes_per_terminal_slot, 1_000_000_000)

    def test_checkpoint_uses_actual_physical_size_and_unique_requests(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        self.request(store, run_id)
        physical = store.path.stat().st_size
        metrics.take_checkpoint(store, run_id=run_id, wall_us=NOW_US)
        row = store._rows("SELECT * FROM storage_checkpoints")[0]
        self.assertEqual(row["db_file_bytes"], physical)
        self.assertEqual(row["book_raw_bytes"], 100)
        self.assertEqual(row["page_size"], 4096)
        self.assertEqual(row["journal_bytes"], 0)
        self.assertFalse(store._connection.in_transaction)

    def test_checkpoint_refuses_open_transaction(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        store._connection.execute("BEGIN")
        try:
            with self.assertRaises(RecorderStoreError):
                metrics.take_checkpoint(store, run_id=run_id, wall_us=NOW_US)
        finally:
            store._connection.rollback()

    def test_read_only_report_never_creates_or_writes_db(self):
        self.assertTrue(hasattr(RecorderStore, "read_only"), "Read-only measurement store is missing")
        metrics = self.metrics()
        store, run_id = self.fixture()
        missing = store.path.parent / "missing" / "absent.sqlite3"
        with self.assertRaises(RecorderStoreError):
            RecorderStore.read_only(missing)
        self.assertFalse(missing.parent.exists())
        before = store.path.read_bytes()
        with patch("builtins.open", side_effect=AssertionError("No writer lock")):
            reader = RecorderStore.read_only(store.path)
        try:
            metrics.measure(reader, start_us=NOW_US, end_us=NOW_US+2_000_000)
            for mutation in (lambda: reader.start_run(RunConfig(), wall_us=NOW_US, app_revision="bad"),
                             lambda: reader.finish_run(run_id, state="STOPPED", wall_us=NOW_US, reason="bad"),
                             lambda: reader.record_event(run_id, "STARTUP", started_us=NOW_US),
                             lambda: metrics.take_checkpoint(reader, run_id=run_id, wall_us=NOW_US)):
                with self.assertRaises(RecorderStoreError):
                    mutation()
        finally:
            reader.close()
        self.assertEqual(store.path.read_bytes(), before)

    def test_denominators_exclude_prefirst_and_inprogress_include_restart(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        for index, status in enumerate(("COMPLETE", "PARTIAL", "MISSED", "FAILED", "INTERRUPTED", "IN_PROGRESS"),150):
            self.slot(store, run_id, index, status)
        self.slot(store, run_id, 149, "MISSED")
        store.finish_run(run_id, state="STOPPED", wall_us=NOW_US+1_000_000, reason="pause")
        store.start_run(RunConfig(), wall_us=NOW_US+10_000_000, app_revision="fixture-v2")
        report = metrics.measure(store, start_us=NOW_US-2_000_000, end_us=NOW_US+12_000_000)
        self.assertEqual(report.denominators["eligible_scheduled_slots"], 5)
        self.assertEqual(report.denominators["in_progress_slots"], 1)
        self.assertEqual(report.denominators["pre_first_start_slots"], 1)
        self.assertEqual(report.denominators["terminal_slots"], 5)
        self.assertEqual(report.status_percentages, dict.fromkeys(("COMPLETE","PARTIAL","MISSED","FAILED","INTERRUPTED"),20))
        self.assertEqual(report.diagnostics["restart_downtime_seconds"], 9)

    def test_unknown_windows_have_disclosed_gap_duration(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        self.slot(store, run_id, 449, "COMPLETE")
        report = metrics.measure(store, start_us=NOW_US+598_000_000, end_us=NOW_US+610_000_000)
        self.assertEqual(report.denominators["eligible_scheduled_slots"],1)
        self.assertEqual(report.status_percentages["COMPLETE"],100)
        self.assertEqual(report.denominators["unassigned_gap_seconds"],10)
        self.assertFalse(report.diagnostics["whole_period_coverage_established"])

    def test_zero_and_missing_checkpoints_do_not_fabricate_rates(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        self.checkpoint(store,run_id,NOW_US,40000)
        for end in (NOW_US,NOW_US+2_000_000):
            report = metrics.measure(store,start_us=NOW_US,end_us=end)
            self.assertIsNone(report.mb_per_day)
            self.assertIsNone(report.bytes_per_terminal_slot)
            self.assertTrue(report.diagnostics["missing_checkpoints"])
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US)
        self.assertTrue(all(value is None for value in report.status_percentages.values()))

    def test_percentiles_and_disabled_reference(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        for latency in (1,2,3,4):
            self.request(store,run_id,latency=latency)
        self.request(store,run_id,latency=9,operation="CLOCK",result="ERROR")
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+2_000_000)
        self.assertEqual(report.latency_percentiles["BOOK_BATCH"]["SUCCESS"],dict(count=4,p50=2,p95=4,p99=4,max=4))
        self.assertEqual(report.latency_percentiles["CLOCK"]["ERROR"],dict(count=1,p50=9,p95=9,p99=9,max=9))
        self.assertEqual(report.reference_coverage["live_percent"],0)
        self.assertEqual(metrics.nearest_rank_percentiles([]),dict(count=0,p50=None,p95=None,p99=None,max=None))

    def reference(self,store,run_id,slot_id,*,role="SLOT_CURRENT",origin="LIVE_LATEST",learned=NOW_US,verified="VERIFIED"):
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from tests.support import MARKET_ID
        import zlib
        request_id = self.request(store,run_id,operation="REFERENCE")
        target = NOW_US if role=="SLOT_CURRENT" else NOW_US+(-300_000_000 if role=="OPENING" else 600_000_000)
        with store._connection:
            fetch = store._connection.execute("""INSERT INTO reference_fetches
                (request_id,feed_id,purpose,origin,target_us,status) VALUES (?,?,?,?,?,'AVAILABLE')""",
                (request_id,CHAINLINK_FEED_ID,role,origin,target)).lastrowid
            report = store._connection.execute("""INSERT INTO reference_reports
                (feed_id,report_hash,first_fetch_id,valid_from_seconds,observations_seconds,
                 expires_at_seconds,decoded_price_text,decoder_revision,decode_status,
                 verification_status,first_received_wall_us,first_received_server_us,
                 raw_report_z,raw_size_bytes,codec_version)
                VALUES (?,?,?,?,?,?,'60000','v1','DECODED',?,?,?,?,1,1)""",
                (CHAINLINK_FEED_ID,str(fetch),fetch,target//1_000_000,target//1_000_000,
                 target//1_000_000+60,verified,learned,learned,zlib.compress(b'x'))).lastrowid
            store._connection.execute("""INSERT INTO reference_associations
                (market_id,feed_id,role,slot_id,target_us,status,fetch_id,report_id,origin,
                 learned_wall_us,learned_server_us,available_live,selection_policy_revision,attempt_count)
                VALUES (?,?,?,?,?,'AVAILABLE',?,?,?,?,?,?, 'policy-v1',1)""",
                (MARKET_ID,CHAINLINK_FEED_ID,role,slot_id if role=="SLOT_CURRENT" else None,
                 target,fetch,report,origin,learned,learned,int(origin=="LIVE_LATEST")))

    def test_live_reference_requires_verified_timely_original_provenance(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        slot = self.slot(store,run_id,150,"COMPLETE")
        with store._connection:
            store._connection.execute("UPDATE recording_runs SET mode='REFERENCE_REQUIRED',reference_activation='VERIFIED',activation_evidence_json='{}'")
        self.reference(store,run_id,slot,origin="HISTORICAL_LOOKUP",learned=NOW_US+1_000_000)
        self.reference(store,run_id,slot,learned=NOW_US+2_000_000) # Receipt at the deadline cannot be original live coverage.
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+4_000_000)
        self.assertEqual(report.reference_coverage["live_slots"],0)
        self.reference(store,run_id,slot,learned=NOW_US,verified="UNVERIFIED")
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+4_000_000)
        self.assertEqual(report.reference_coverage["live_slots"],0)
        # Offline fixture mode activation: no credential loading or runtime activation.
        with store._connection:
            store._connection.execute("UPDATE recording_runs SET mode='REFERENCE_REQUIRED',reference_activation='VERIFIED',activation_evidence_json='{}'")
            store._connection.execute("DELETE FROM reference_associations WHERE learned_wall_us=?",(NOW_US,))
        self.reference(store,run_id,slot,learned=NOW_US)
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+4_000_000)
        self.assertEqual(report.reference_coverage["live_percent"],50)
        self.assertEqual(report.reference_coverage["paired_percent"],100)
        self.assertEqual(report.reference_coverage["complete_market_windows"],0)
        self.assertEqual(report.reference_coverage["opening_count"],0)

    def test_timely_receipt_and_boundaries_have_separate_counts(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        with store._connection:
            store._connection.execute("UPDATE recording_runs SET mode='REFERENCE_REQUIRED',reference_activation='VERIFIED',activation_evidence_json='{}'")
        slot = self.slot(store,run_id,150,"COMPLETE")
        self.reference(store,run_id,slot,learned=NOW_US+500_000)
        self.reference(store,run_id,slot,role="OPENING",origin="HISTORICAL_LOOKUP",learned=NOW_US+1_000_000)
        self.reference(store,run_id,slot,role="CLOSING",origin="HISTORICAL_LOOKUP",learned=NOW_US+601_000_000)
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+610_000_000)
        self.assertEqual(report.reference_coverage["live_slots"],1)
        self.assertEqual(report.reference_coverage["opening_count"],1)
        self.assertEqual(report.reference_coverage["closing_count"],1)
        self.assertEqual(report.reference_coverage["complete_market_windows"],0)

    def test_resolution_counts_are_as_of_learning_and_delays_use_server_time(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        from tests.support import MARKET_ID
        import zlib
        request = self.request(store,run_id,operation="RESOLUTION")
        with store._connection:
            store._connection.execute("""INSERT INTO resolution_observations
                (market_id,request_id,condition_id,provenance_json,reported_resolved_us,
                 learned_wall_us,learned_server_us,mapped_outcome,classification,response_hash,
                 raw_resolution_z,raw_size_bytes,codec_version,payouts_raw_json)
                VALUES (?,?,'c','{}',?,?,?,'UP','RESOLVED_DIRECTIONAL','h',?,1,1,'[1,0]')""",
                (MARKET_ID,request,NOW_US+600_000_000,NOW_US+605_000_000,NOW_US+604_000_000,zlib.compress(b'x')))
        early = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+602_000_000)
        self.assertEqual((early.resolution_summary["tracked_expired_markets"],early.resolution_summary["pending"]),(1,1))
        later = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+610_000_000)
        self.assertEqual(later.resolution_summary["directional"],1)
        self.assertEqual(later.resolution_summary["success_percent"],100)
        self.assertEqual(later.resolution_summary["learning_delay_seconds"]["p50"],4)
        with store._connection:
            store._connection.execute("""INSERT INTO resolution_observations
                (market_id,request_id,condition_id,provenance_json,learned_wall_us,
                 mapped_outcome,classification,response_hash,raw_resolution_z,raw_size_bytes,
                 codec_version,payouts_raw_json) VALUES (?,?,'c','{}',?,'DOWN',
                 'RESOLVED_DIRECTIONAL','conflict',?,1,1,'[0,1]')""",
                (MARKET_ID,self.request(store,run_id,operation="RESOLUTION"),NOW_US+611_000_000,zlib.compress(b'x')))
        conflict = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+620_000_000)
        self.assertEqual((conflict.resolution_summary["conflict"],conflict.resolution_summary["directional"]),(1,0))
        self.assertEqual(conflict.resolution_summary["unknown_learning_delay_count"],1)

    def test_depth_and_terminal_observations_after_end_are_excluded(self):
        metrics = self.metrics()
        store, run_id = self.fixture()
        slot = self.slot(store,run_id,150,"COMPLETE",committed=NOW_US+10_000_000)
        with store._connection:
            store._connection.execute("""INSERT INTO book_observations
                (slot_id,outcome,expected_token_id,expected_condition_id,status,quality_json,
                 bid_level_count,ask_level_count) VALUES (?,'UP','1','c','VALID','{}',10,10)""",(slot,))
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+2_000_000)
        self.assertIsNone(report.average_depth)
        self.assertEqual(report.denominators["terminal_slots"],0)

    def test_checkpoint_interval_and_diagnostics_are_explicit(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        self.checkpoint(store,run_id,NOW_US+1_000_000,40000)
        self.checkpoint(store,run_id,NOW_US+3_000_000,44096)
        store.record_event(run_id,"DISCOVERY_GAP",started_us=NOW_US+600_000_000,
                           ended_us=NOW_US+604_000_000,detail={"unknown_market":True})
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+610_000_000)
        self.assertEqual(report.elapsed_seconds,2)
        self.assertEqual(report.diagnostics["physical_interval_us"],(NOW_US+1_000_000,NOW_US+3_000_000))
        self.assertEqual(report.diagnostics["startup_baselines"][0]["db_file_bytes"],40000)
        self.assertEqual(report.diagnostics["schema_version"],1)
        self.assertEqual(report.diagnostics["clock_uncertainty_us"]["max"],1_000_000)
        self.assertEqual(report.diagnostics["app_revisions"],("fixture-v1",))
        self.assertEqual(report.rollover_gaps["count"],1)
        self.assertEqual(report.rollover_gaps["duration_seconds"],4)

    def test_physical_bytes_denominator_uses_checkpoint_interval_not_quality_interval(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        self.slot(store,run_id,150,"COMPLETE",committed=NOW_US)
        self.slot(store,run_id,151,"COMPLETE",committed=NOW_US+2_000_000)
        self.checkpoint(store,run_id,NOW_US+1_000_000,40000,terminals=1)
        self.checkpoint(store,run_id,NOW_US+3_000_000,41000,terminals=2)
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+4_000_000)
        self.assertEqual(report.denominators["terminal_slots"],2)
        self.assertEqual(report.bytes_per_terminal_slot,1000)
        self.assertEqual(report.bytes_per_complete_slot,1000)
        self.assertEqual(report.denominators["physical_terminal_slots"],1)

    def test_new_terminal_count_uses_checkpoint_counters_at_shared_wall_time(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        self.slot(store,run_id,150,"COMPLETE",committed=NOW_US)
        self.slot(store,run_id,151,"MISSED",committed=NOW_US)
        self.checkpoint(store,run_id,NOW_US,40000,terminals=1)
        self.checkpoint(store,run_id,NOW_US+3_000_000,41000,terminals=2)
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+3_000_000)
        self.assertEqual(report.bytes_per_terminal_slot,1000)
        self.assertEqual(report.denominators["physical_terminal_slots"],1)

    def test_reader_refuses_idempotent_mutation_helpers(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        reader = RecorderStore.read_only(store.path)
        self.addCleanup(reader.close)
        with self.assertRaises(RecorderStoreError) as raised:
            reader.save_market_revision(run_id,None)
        self.assertEqual(raised.exception.code,"read_only")
        with self.assertRaises(RecorderStoreError) as raised:
            reader._append_reference_association({})
        self.assertEqual(raised.exception.code,"read_only")
        for mutation in (
                lambda:reader.save_anchor(run_id,None),
                lambda:reader.expire_market("unknown",observed_us=NOW_US),
                lambda:reader.save_resolutions((),requests=()),
                lambda:reader.save_reference_evidence(None,(),()),
                lambda:reader.save_reference_plan(None),
                lambda:reader.reserve_slot("unknown",None,run_id=run_id,anchor_id=1,
                    metadata_revision_id=1,started_mono_ns=0,started_server_us=NOW_US),
                lambda:reader.finalize_slot(1,None,committed_wall_us=NOW_US),
                lambda:reader.mark_missed("unknown",None,run_id=run_id,reason="test",wall_us=NOW_US),
                lambda:reader.save_control_attempts(run_id,()),
                lambda:reader.recover_interrupted(wall_us=NOW_US),
                lambda:reader.save_checkpoint(run_id=run_id,wall_us=NOW_US,values={}),
                lambda:reader.update_checkpoint_physical(1,{})):
            with self.assertRaises(RecorderStoreError) as raised:
                mutation()
            self.assertEqual(raised.exception.code,"read_only")

    def test_depth_breakdown_has_mean_side_depth_and_excludes_invalid(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        slot = self.slot(store,run_id,150,"PARTIAL")
        with store._connection:
            for outcome,status,bids,asks in (("UP","VALID",2,4),("DOWN","INVALID",90,90)):
                store._connection.execute("""INSERT INTO book_observations
                    (slot_id,outcome,expected_token_id,expected_condition_id,status,quality_json,
                     bid_level_count,ask_level_count) VALUES (?,?,'1','c',?,'{}',?,?)""",
                    (slot,outcome,status,bids,asks))
        report = metrics.measure(store,start_us=NOW_US,end_us=NOW_US+2_000_000)
        self.assertEqual(report.average_depth,6)
        self.assertEqual(report.depth_breakdown["UP"]["mean_bid_levels"],2)
        self.assertEqual(report.depth_breakdown["UP"]["mean_ask_levels"],4)
        self.assertEqual(report.depth_breakdown["excluded_invalid_books"],1)

    def test_checkpoint_includes_its_own_page_overhead(self):
        metrics = self.metrics()
        store,run_id = self.fixture()
        # Exhaust a checkpoint leaf page; this forces checkpoint storage growth.
        for number in range(100):
            metrics.take_checkpoint(store,run_id=run_id,wall_us=NOW_US+number)
            row = store._rows("SELECT * FROM storage_checkpoints ORDER BY checkpoint_id DESC LIMIT 1")[0]
            self.assertEqual(row["db_file_bytes"],store.path.stat().st_size)
            self.assertEqual(row["page_count"]*row["page_size"],row["db_file_bytes"])


if __name__ == "__main__":
    unittest.main()
