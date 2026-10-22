"""Two full 15-minute windows at 2-second cadence, end to end, fake market only."""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.report import build_report
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import START, Collector, descriptor, snapshot, us

BOT = 'default'


def market_for(window: int):
    start = START + timedelta(minutes=15 * window)
    prefix = 'ab' if window == 0 else 'cd'
    return descriptor(market_id=str(1001 + window), start=start, condition='0x' + prefix * 32,
                      event=str(2001 + window), up=str(111111 + window), down=str(222222 + window))


def samples():
    out = []
    for window in range(2):
        market = market_for(window)
        for slot in range(450):
            mid = Decimal('0.40') + Decimal('0.02') * ((slot // 5) % 3)
            out.append(snapshot(slot=slot, start=market.window_start, market=market,
                                up_bid=str(mid), up_ask=str(mid + Decimal('0.02')),
                                down_bid=str(Decimal(1) - mid - Decimal('0.02')),
                                down_ask=str(Decimal(1) - mid)))
    return out


class SoakTests(unittest.TestCase):
    def test_two_windows_450_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MvpStore.open(base / 'paper.sqlite')
            try:
                settings = Settings.from_env({}, base=base)
                store.start_run('soak', BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
                engine = PaperEngine(store, Collector(samples()), settings, run_id='soak',
                                     now_us=lambda: us(START))
                statuses = [engine.step().status for _ in range(900)]
                self.assertEqual(set(statuses), {'OK'})
                self.assertEqual(store.window(BOT, '1001')['status'], 'UNKNOWN_SETTLEMENT')
                self.assertEqual(store.active_window(BOT)['market_id'], '1002')
                self.assertEqual(len(store.observations_for_market(BOT, '1001')), 900)
                self.assertEqual(len(store.observations_for_market(BOT, '1002')), 900)
                self.assertEqual(len(store.decisions_for_market(BOT, '1001')), 450)
                self.assertEqual(len(store.decisions_for_market(BOT, '1002')), 450)
                self.assertGreater(len(store.orders(BOT)), 0)
                self.assertEqual(store.heartbeat_row(BOT)['cycles'], 900)
                report = build_report(base / 'paper.sqlite', now_us=1)
                self.assertEqual(report['decisions']['total'], 900)
                self.assertEqual(report['windows']['total'], 2)
                self.assertEqual(report['windows']['by_status'],
                                 {'UNKNOWN_SETTLEMENT': 1, 'ACTIVE': 1})
                self.assertGreater(report['orders']['total'], 0)
                self.assertTrue(any(status in report['orders']['by_status']
                                    for status in ('FILLED', 'PARTIAL')))
                self.assertEqual(report['settlement'], 'UNKNOWN_NOT_INFERRED')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
