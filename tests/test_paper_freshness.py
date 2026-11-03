"""Synthetic clock brackets only; no network or stored evidence."""
from dataclasses import replace
from datetime import timedelta
import unittest

from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data import freshness
from tests.linux_mvp_support import START, snapshot, us

NS = 1_000_000_000
BASE = 100 * NS


def read(payload, start, finish, *, wall_offset=0):
    wall = us(START) + wall_offset
    return JsonRead(payload, start / NS, finish / NS, b'', start, finish,
                    wall + start // 1000, wall + finish // 1000)


def timed_snapshot(*, source_age=0, down_age=None, wall_offset=0):
    """Two integer samples one second apart narrow symmetric quantization bounds."""
    s = snapshot(slot=2)
    epoch = us(START) // 1_000_000
    first = read(epoch + 4, BASE, BASE + NS // 10, wall_offset=wall_offset)
    final = read(epoch + 5, BASE + NS, BASE + 11 * NS // 10, wall_offset=wall_offset)
    observations = []
    for index, (obs, age) in enumerate(((s.up, source_age), (s.down, source_age if down_age is None else down_age))):
        timing = read({}, BASE + (5 + index) * NS // 10, BASE + (6 + index) * NS // 10,
                      wall_offset=wall_offset)
        source = START + timedelta(seconds=4.5 - age)
        book = replace(obs.book, source_timestamp_raw=str(us(source) // 1000), source_at=source)
        observations.append(replace(obs, book=book, timing=timing))
    return replace(s, up=observations[0], down=observations[1], initial_time=first,
                   final_time=final, validated_mono_ns=final.finished_mono_ns,
                   validated_at=START + timedelta(seconds=5.05))


class FreshnessTests(unittest.TestCase):
    def check(self, s=None, *, now=BASE + 11 * NS // 10):
        return freshness.evaluate(s or timed_snapshot(), decision_mono_ns=now,
                                  max_age_seconds=5, sample_seconds=2)

    def test_integer_seconds_keep_signed_age_and_actual_wall_receipt(self):
        result = self.check()
        self.assertTrue(result['valid'])
        up = result['outcomes']['UP']
        self.assertTrue(up['fresh'])
        self.assertEqual(up['receipt_wall_us'], us(START) + 100_600_000)
        self.assertLess(up['receipt_source_age_lower_us'], 0)
        self.assertGreater(up['receipt_source_age_upper_us'], 0)
        self.assertEqual(result['quantization_uncertainty_us'], 1_000_000)

    def test_local_clock_offset_does_not_change_source_freshness(self):
        a, b = self.check(), self.check(timed_snapshot(wall_offset=86_400_000_000))
        self.assertEqual(a['outcomes']['UP']['source_age_upper_us'],
                         b['outcomes']['UP']['source_age_upper_us'])
        self.assertTrue(b['outcomes']['UP']['fresh'])

    def test_independent_old_source_rejected_even_with_fresh_receipt(self):
        result = self.check(timed_snapshot(down_age=8))
        self.assertTrue(result['outcomes']['UP']['fresh'])
        self.assertFalse(result['outcomes']['DOWN']['fresh'])
        self.assertEqual(result['outcomes']['DOWN']['reason'], 'STALE_SOURCE')

    def test_delay_to_decision_counts_for_local_and_source_age(self):
        result = self.check(now=BASE + 7 * NS)
        self.assertFalse(result['outcomes']['UP']['fresh'])
        self.assertGreater(result['outcomes']['UP']['local_receipt_age_us'], 5_000_000)
        self.assertGreater(result['outcomes']['UP']['source_age_upper_us'], 5_000_000)

    def test_provably_future_source_rejected_not_abs_or_clamped(self):
        result = self.check(timed_snapshot(source_age=-4))
        self.assertFalse(result['outcomes']['UP']['fresh'])
        self.assertEqual(result['outcomes']['UP']['reason'], 'FUTURE_SOURCE')
        self.assertLess(result['outcomes']['UP']['source_age_upper_us'], 0)

    def test_upper_bound_not_point_estimate_controls_five_second_limit(self):
        result = self.check(timed_snapshot(source_age=4.5))
        up = result['outcomes']['UP']
        self.assertGreater(up['source_age_upper_us'], 5_000_000)
        self.assertFalse(up['fresh'])

    def test_missing_malformed_reversed_and_conflicting_timing_fail_closed(self):
        s = timed_snapshot()
        bad_reads = [None, replace(s.final_time, finished_mono_ns=True),
                     replace(s.final_time, finished_mono_ns=BASE),
                     replace(s.final_time, started_wall_us=1),
                     replace(s.final_time, started_mono=1),
                     replace(s.final_time, payload=s.final_time.payload + 20)]
        for bad in bad_reads:
            with self.subTest(read=bad):
                result = self.check(replace(s, final_time=bad))
                self.assertFalse(result['valid'])
                self.assertEqual(result['reason'], 'TIMING_INVALID')

    def test_conflicting_validation_clock_and_book_order_rejected(self):
        s = timed_snapshot()
        self.assertFalse(self.check(replace(s, validated_at=s.validated_at + timedelta(seconds=20)))['valid'])
        backwards = replace(s.down, timing=read({}, BASE + NS // 5, BASE + 3 * NS // 10))
        self.assertFalse(self.check(replace(s, down=backwards))['outcomes']['DOWN']['fresh'])

    def test_invalid_evidence_keeps_raw_fields_and_specific_error_json_safe(self):
        import json
        s = timed_snapshot()
        bad = replace(s, final_time=replace(s.final_time, payload=float('nan')),
                      up=replace(s.up, book=replace(s.up.book, source_timestamp_raw=object())))
        result = self.check(bad)
        json.dumps(result, allow_nan=False)
        self.assertIn('integer server seconds', result['error'])
        self.assertEqual(result['captured']['final_time']['payload'], {'invalid_type': 'float', 'value': 'nan'})
        self.assertEqual(result['outcomes']['UP']['source_timestamp_raw'], {'invalid_type': 'object'})
        self.assertEqual(result['captured']['validated_mono_ns'], s.validated_mono_ns)
        mismatch = replace(s, up=replace(s.up, book=replace(s.up.book, source_timestamp_raw='bad')))
        evidence = self.check(mismatch)['outcomes']['UP']
        self.assertEqual(evidence['source_timestamp_raw'], 'bad')
        self.assertIn('Invalid raw source timestamp', evidence['error'])

    def test_raw_timestamp_mismatch_and_missing_receipt_rejected(self):
        s = timed_snapshot()
        for up in (replace(s.up, timing=None), replace(s.up, book=replace(s.up.book,
                    source_at=s.up.book.source_at + timedelta(milliseconds=1))),
                   replace(s.up, book=replace(s.up.book, source_timestamp_raw='bad'))):
            with self.subTest(up=up):
                self.assertFalse(self.check(replace(s, up=up))['outcomes']['UP']['fresh'])

    def test_network_delay_and_reversed_causal_order_rejected(self):
        s = timed_snapshot()
        late = replace(s.up, timing=read({}, BASE + NS // 5, BASE + 7 * NS))
        final = read(s.final_time.payload + 7, BASE + 8 * NS, BASE + 81 * NS // 10)
        delayed = replace(s, up=late, final_time=final, validated_mono_ns=final.finished_mono_ns,
                          validated_at=s.validated_at + timedelta(seconds=7))
        result = self.check(delayed, now=final.finished_mono_ns)
        self.assertFalse(result['outcomes']['UP']['fresh'])
        self.assertEqual(result['outcomes']['UP']['reason'], 'STALE_RESPONSE')
        future = replace(s.up, timing=read({}, BASE + 2 * NS, BASE + 3 * NS))
        self.assertFalse(self.check(replace(s, up=future))['outcomes']['UP']['fresh'])

    def test_window_and_slot_boundary_uncertainty_never_certifies_active_slot(self):
        s = timed_snapshot()
        result = self.check(replace(s, market=replace(s.market, window_start=START + timedelta(seconds=5))))
        self.assertFalse(result['window_valid'])
        self.assertFalse(self.check(now=BASE + 21 * NS // 10)['slot_valid'])


if __name__ == '__main__':
    unittest.main()
