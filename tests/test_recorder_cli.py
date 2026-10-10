"""Recorder command boundary: explicit mode and offline reporting."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.recorder_support import DatabaseFixture
from polymarket_bot.recorder.store import RecorderStore


class RecorderCliTests(unittest.TestCase):
    def invoke(self, arguments):
        from polymarket_bot.recorder.cli import main
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_help_is_entirely_offline_and_credential_free(self):
        with patch("httpx.Client", side_effect=AssertionError("HTTP constructed")), \
             patch.object(RecorderStore, "open", side_effect=AssertionError("writer opened")), \
             patch.object(RecorderStore, "read_only", side_effect=AssertionError("reader opened")), \
             patch("os.getenv", side_effect=AssertionError("credential environment read")), \
             patch("builtins.open", side_effect=AssertionError("file opened")):
            for arguments in (["--help"], ["record", "--help"], ["report", "--help"]):
                with self.subTest(arguments=arguments):
                    code, out, err = self.invoke(arguments)
                    self.assertEqual((code, err), (0, ""))
                    self.assertIn("usage:", out)

    def test_record_requires_explicit_incomplete_mode(self):
        with patch.object(RecorderStore, "open", side_effect=AssertionError("writer opened")), \
             patch("httpx.Client", side_effect=AssertionError("HTTP constructed")):
            code, out, err = self.invoke(["record", "--db", "unused.sqlite3"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("--mode", err)

        from polymarket_bot.recorder.models import RunResult
        with patch.object(RecorderStore, "open") as open_store, \
             patch("polymarket_bot.recorder.cli.Recorder") as recorder, \
             patch("polymarket_bot.recorder.cli.httpx.Client") as client, \
             patch("os.getenv", side_effect=AssertionError("credential environment read")), \
             patch("builtins.open", side_effect=AssertionError("file opened")):
            recorder.return_value.run.return_value = RunResult("run", "STOPPED", 0, 0, "STOP_REQUESTED")
            code, out, err = self.invoke(["record", "--mode", "book-only", "--db", "unused.sqlite3"])
        self.assertEqual(code, 0)
        self.assertIn("BOOK_ONLY", out + err)
        self.assertIn("REFERENCE_INCOMPLETE", out + err)
        self.assertEqual(open_store.call_count, 1)
        self.assertEqual(client.call_args.kwargs, {"trust_env": False, "follow_redirects": False})
        self.assertEqual(recorder.call_count, 1)
        self.assertEqual(recorder.return_value.run.call_args.args[0].mode, "BOOK_ONLY")

    def test_reference_required_cannot_activate_from_cli(self):
        with patch.object(RecorderStore, "open", side_effect=AssertionError("writer opened")), \
             patch("httpx.Client", side_effect=AssertionError("HTTP constructed")), \
             patch("os.getenv", side_effect=AssertionError("credential environment read")), \
             patch("builtins.open", side_effect=AssertionError("file opened")):
            code, out, err = self.invoke(["record", "--mode", "reference-required", "--db", "unused.sqlite3"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("activation_required", err)

    def test_report_reads_existing_db_with_labelled_units_and_denominators(self):
        fixture = DatabaseFixture()
        try:
            writer = RecorderStore.open(fixture.path)
            writer.close()
            lock_bytes = Path(str(fixture.path) + ".writer.lock").read_bytes()
            original_open = Path.open
            def only_schema_file(path, *args, **kwargs):
                if path.name != "schema.sql":
                    raise AssertionError(f"Unexpected file read: {path.name}")
                return original_open(path, *args, **kwargs)
            with patch.object(RecorderStore, "open", side_effect=AssertionError("writer opened")), \
                 patch("httpx.Client", side_effect=AssertionError("HTTP constructed")), \
                 patch("os.getenv", side_effect=AssertionError("credential environment read")), \
                 patch.object(Path, "open", only_schema_file):
                code, out, err = self.invoke(["report", "--db", str(fixture.path),
                    "--start", "2026-10-10T00:00:00Z", "--end", "2026-10-10T00:01:00Z"])
            self.assertEqual((code, err), (0, ""))
            report = json.loads(out)
            self.assertEqual(report["interval_utc"], {"start": "2026-10-10T00:00:00Z", "end": "2026-10-10T00:01:00Z"})
            self.assertEqual(report["units"]["physical_growth_bytes"], "bytes")
            self.assertEqual(report["units"]["mb_per_day"], "decimal MB/day")
            self.assertEqual(report["units"]["status_percentages"], "percent of eligible scheduled slots")
            self.assertEqual(report["units"]["rollover_gaps"], "seconds in calibrated server time")
            self.assertIn("eligible_scheduled_slots", report["denominators"])
            self.assertIsNone(report["mb_per_day"])
            self.assertEqual(Path(str(fixture.path) + ".writer.lock").read_bytes(), lock_bytes)
        finally:
            fixture.close()

    def test_report_missing_database_does_not_create_it(self):
        fixture = DatabaseFixture()
        try:
            code, out, err = self.invoke(["report", "--db", str(fixture.path),
                "--start", "2026-10-10T00:00:00Z", "--end", "2026-10-10T00:01:00Z"])
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertFalse(fixture.path.exists())
            self.assertFalse(Path(str(fixture.path) + ".writer.lock").exists())
        finally:
            fixture.close()
