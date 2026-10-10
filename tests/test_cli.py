import io
import os
from pathlib import Path
import tempfile
import unittest
from tests.support import *
from polymarket_bot.market_data.snapshot import collect_snapshot
from polymarket_bot.cli import render_snapshot, run_snapshot

WARNING = "The Up and Down books were fetched separately; this REST snapshot is not atomic."

class CliTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gamma, self.clob = FakeGamma(), FakeClob(self.clock)
        self.out, self.err = io.StringIO(), io.StringIO()
    def run_cli(self, **options):
        flags = dict(use_fast_path=True,jakarta=False,debug=False)
        flags.update(options)
        return run_snapshot(self.gamma,self.clob,monotonic=self.clock.monotonic,
                            stdout=self.out,stderr=self.err,**flags)
    def test_non_atomic_warning_and_reference_price_labels(self):
        snapshot = collect_snapshot(self.gamma,self.clob,monotonic=self.clock.monotonic)
        output = render_snapshot(snapshot)
        self.assertIn(WARNING,output)
        self.assertIn("midpoint reference price",output)
        self.assertIn("best bid: 0.60",output)
        self.assertIn("best ask: 0.61",output)
        self.assertIn(snapshot.up.book.source_at.isoformat(),output)
        self.assertIn(snapshot.down.book.source_at.isoformat(),output)
    def test_one_run_exits_and_never_persists(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            try:
                os.chdir(folder)
                before = list(Path(folder).iterdir())
                self.assertEqual(self.run_cli(),0)
                self.assertEqual(list(Path(folder).iterdir()),before)
            finally:
                os.chdir(original)
        self.assertEqual(len(self.clob.book_calls),2)

    def test_empty_books_exit_zero_with_unavailable_quotes(self):
        self.clob.books = {TOKEN_UP:make_book(bids=[],asks=[]),
                           TOKEN_DOWN:make_book(asset_id=TOKEN_DOWN,bids=[],asks=[])}
        self.assertEqual(self.run_cli(),0)
        self.assertIn("best bid: unavailable; best ask: unavailable",self.out.getvalue())
        self.assertIn("midpoint reference price: unavailable",self.out.getvalue())

    def test_failure_exit_one_not_success(self):
        from polymarket_bot.market_data.models import MarketDataError
        self.gamma.fast = MarketDataError("transport_error","Network unavailable.")
        self.assertEqual(self.run_cli(),1)
        self.assertEqual(self.out.getvalue(),"")
        self.assertIn("transport_error",self.err.getvalue())

    def test_unready_market_prints_incomplete_metadata(self):
        self.gamma.fast = make_event(markets=[make_market(acceptingOrders=False)])
        self.assertEqual(self.run_cli(),1)
        self.assertIn("incomplete",self.out.getvalue())
        self.assertIn(MARKET_ID,self.out.getvalue())
        self.assertNotIn("best bid",self.out.getvalue())

    def test_interrupt_exits_130_without_retry(self):
        self.clob.times = [KeyboardInterrupt()]
        self.assertEqual(self.run_cli(),130)
        self.assertEqual(len(self.clob.time_calls),1)
        self.assertEqual(self.out.getvalue(),"")

    def test_jakarta_conversion(self):
        self.assertEqual(self.run_cli(jakarta=True),0)
        output = self.out.getvalue()
        self.assertIn("07:50",output)
        self.assertIn("+07:00",output)
        self.assertIn("UTC window:",output)

    def test_debug_prices_do_not_replace_books(self):
        self.gamma.fast = make_event(markets=[make_market(outcomePrices='["0.99","0.01"]',version="v2",positionIds='["1","2"]')])
        self.assertEqual(self.run_cli(debug=True),0)
        self.assertIn("best bid: 0.60",self.out.getvalue())
        self.assertIn("Gamma metadata",self.out.getvalue())
        self.assertIn("0.99",self.out.getvalue())

    def test_no_fast_path_flag_selects_list_flow(self):
        self.assertEqual(self.run_cli(use_fast_path=False),0)
        self.assertEqual(self.gamma.slug_calls,[])
        self.assertIn("Discovery: filtered_list",self.out.getvalue())
