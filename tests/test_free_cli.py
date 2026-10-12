"""CLI tests for approximate-reference mode."""
import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from polymarket_bot.recorder.cli import _parser, _record, main


class FreeCliTests(unittest.TestCase):
    def test_exchange_cookie_jar_isolated_during_book_collection(self):
        import threading
        import httpx
        from unittest.mock import Mock, patch
        from polymarket_bot.recorder import cli
        clients = []
        original = httpx.Client
        def client_factory(**kwargs):
            client = original(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)
            clients.append(client)
            return client
        def recorder_factory(store, public, **kwargs):
            session = kwargs['approximate_reference']
            exchange = session._providers['coinbase']._client
            self.assertIs(exchange, session._providers['kraken']._client)
            entered, release = threading.Event(), threading.Event()
            def receive_cookie():
                exchange.cookies.set('public', 'tracking')
                entered.set()
                release.wait(1)
            thread = threading.Thread(target=receive_cookie)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNot(public.client, exchange)
                self.assertFalse(public.client.cookies)
                self.assertIsNone(public.client.auth)
                self.assertFalse(public.client.trust_env)
                self.assertFalse(exchange.trust_env)
                self.assertFalse(public.client.follow_redirects)
                self.assertFalse(exchange.follow_redirects)
            finally:
                release.set()
                thread.join(1)
            recorder = Mock()
            recorder.approximate_worker = None
            recorder.run.return_value = Mock(run_id='run', state='STOPPED', slots_finalized=0,
                                             reason='STOP_REQUESTED', exit_code=0)
            return recorder
        with patch.object(cli.RecorderStore, 'open_approximate', return_value=Mock()), patch.object(
                cli.httpx, 'Client', side_effect=client_factory), patch.object(cli, 'Recorder', side_effect=recorder_factory):
            try:
                self.assertEqual(cli._record(Path('unused'), False, 'approximate-reference'), 0)
                self.assertEqual(len(clients), 2)
                self.assertTrue(all(client.is_closed for client in clients))
            finally:
                for client in clients:
                    client.close()

    def test_active_worker_retains_client_store_and_owner(self):
        from unittest.mock import Mock, MagicMock, patch
        from polymarket_bot.recorder import cli
        store, client, recorder = Mock(), MagicMock(), Mock()
        client.__enter__.return_value = client
        recorder.approximate_worker._thread.is_alive.return_value = True
        recorder.run.return_value = Mock(run_id='run', state='FAILED', slots_finalized=0,
                                         reason='original_failure', exit_code=2)
        with patch.object(cli.RecorderStore, 'open_approximate', return_value=store), patch.object(
                cli.httpx, 'Client', return_value=client), patch.object(cli, 'Recorder', return_value=recorder):
            self.assertEqual(cli._record(Path('unused'), False, 'approximate-reference'), 2)
        store.close.assert_not_called()
        client.close.assert_not_called()
        self.assertEqual(cli._retained_approximate_runs[-1], (recorder, client, client, store))
        cli._retained_approximate_runs.pop()

    def test_duration_is_positive_bounded_and_passes_monotonic_deadline(self):
        from unittest.mock import MagicMock, Mock, patch
        from polymarket_bot.recorder import cli
        parser = cli._parser()
        for duration in ('0', '-1', '121', 'nan', 'inf'):
            with self.assertRaises(SystemExit):
                parser.parse_args(['record', '--mode', 'approximate-reference', '--db', 'unused',
                                   '--duration-seconds', duration])
        args = parser.parse_args(['record', '--mode', 'approximate-reference', '--db', 'unused',
                                  '--duration-seconds', '110'])
        self.assertEqual(args.duration_seconds, 110)
        recorder = Mock()
        recorder.approximate_worker = None
        recorder.run.return_value = Mock(run_id='run', state='STOPPED', slots_finalized=0,
                                         reason='STOP_REQUESTED', exit_code=0)
        with patch.object(cli.RecorderStore, 'open_approximate'), patch.object(cli.httpx, 'Client',
                return_value=MagicMock()), patch.object(cli, 'Recorder', return_value=recorder), patch.object(
                cli.time, 'monotonic_ns', return_value=1000000000):
            cli._record(Path('unused'), False, 'approximate-reference', duration_seconds=110)
        self.assertEqual(recorder.run.call_args.kwargs['deadline_ns'], 111000000000)

    def test_approximate_mode_choice(self):
        """Parser accepts approximate-reference as a mode choice."""
        parser = _parser()
        args = parser.parse_args(["record", "--mode", "approximate-reference", "--db", "/tmp/test.db"])
        self.assertEqual(args.mode, "approximate-reference")
        self.assertEqual(args.command, "record")

    def test_approximate_mode_requires_v2(self):
        """Approximate mode on v1 DB raises schema_version_mismatch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "v1.db"
            # Create a v1 database
            store = RecorderStore.open(db_path)
            store.close()

            # Try to open with approximate mode - should fail
            with self.assertRaises(RecorderStoreError) as cm:
                RecorderStore.open_approximate(db_path)
            self.assertEqual(cm.exception.code, "schema_version_mismatch")

    def test_book_only_cli_unchanged(self):
        """BOOK_ONLY mode still works with existing behavior."""
        parser = _parser()
        args = parser.parse_args(["record", "--mode", "book-only", "--db", "/tmp/test.db"])
        self.assertEqual(args.mode, "book-only")
        self.assertEqual(args.command, "record")

    def test_reference_required_still_refused(self):
        """REFERENCE_REQUIRED still prints activation_required and returns 2."""
        # This test checks the main() function behavior for reference-required
        # We need to capture stderr
        import io
        from contextlib import redirect_stderr

        f = io.StringIO()
        with redirect_stderr(f):
            exit_code = main(["record", "--mode", "reference-required", "--db", "/tmp/test.db"])
        
        output = f.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("activation_required", output)
        self.assertIn("verified Chainlink reference runtime is unavailable", output)

    def test_approximate_mode_parser_requires_db(self):
        """Approximate-reference mode requires --db argument."""
        parser = _parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["record", "--mode", "approximate-reference"])

    def test_approximate_mode_parser_rejects_unknown_mode(self):
        """Unknown mode values are rejected."""
        parser = _parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["record", "--mode", "invalid-mode", "--db", "/tmp/test.db"])


if __name__ == "__main__":
    unittest.main()