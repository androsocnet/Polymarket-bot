import unittest
from polymarket_bot.market_data.parsing import parse_market
from tests.support import *
from polymarket_bot.market_data.models import MarketDataError
from decimal import Decimal

class ParsingTests(unittest.TestCase):
    def test_explicit_future_start_preserves_actual_now_validation(self):
        import inspect
        from typing import Any, cast
        parse_market = cast(Any, __import__('polymarket_bot.market_data.parsing', fromlist=['parse_market']).parse_market)
        self.assertIn('expected_start', inspect.signature(parse_market).parameters)
        start = NOW + timedelta(minutes=10)
        market = make_market(eventStartTime=start.isoformat(),
                             endDate=(start+timedelta(seconds=900)).isoformat())
        self.assertIsNone(parse_market(make_event(), market, now=NOW))
        self.assertIsNotNone(parse_market(make_event(), market, now=NOW, expected_start=start))
        for expected, actual in ((start, start), (start, start+timedelta(seconds=1)),
                                 (start.replace(tzinfo=None), NOW),
                                 (start+timedelta(seconds=1), NOW)):
            with self.subTest(expected=expected, actual=actual):
                self.assertIsNone(parse_market(make_event(), market, now=actual, expected_start=expected))
        for patch in ({'endDate': (start+timedelta(seconds=901)).isoformat()},
                      {'slug': 'btc-updown-15m-1'}):
            self.assertIsNone(parse_market(make_event(), dict(market, **patch), now=NOW, expected_start=start))
        for patch in ({'outcomes': '["Up","Up"]'}, {'clobTokenIds': '["1","1"]'},
                      {'eventStartTime': start.replace(tzinfo=None).isoformat()}):
            with self.assertRaises(MarketDataError):
                parse_market(make_event(), dict(market, **patch), now=NOW, expected_start=start)

    def test_reversed_outcomes_preserve_exact_token_mapping(self):
        market = make_market(outcomes='["Down", "Up"]')
        result = parse_market(make_event(), market, now=NOW)
        self.assertEqual(result.up.token_id, TOKEN_DOWN)
        self.assertEqual(result.down.token_id, TOKEN_UP)

    def parse(self, **overrides):
        return parse_market(make_event(), make_market(**overrides), now=NOW)

    def error(self, code, **overrides):
        with self.assertRaises(MarketDataError) as caught:
            self.parse(**overrides)
        self.assertEqual(caught.exception.code, code)

    def test_window_start_inclusive_end_exclusive(self):
        market = make_market()
        self.assertIsNotNone(parse_market(make_event(), market, now=NOW-timedelta(minutes=5)))
        self.assertIsNone(parse_market(make_event(), market, now=NOW+timedelta(minutes=10)))

    def test_creation_date_is_not_window_start(self):
        self.assertEqual(self.parse().window_start, NOW-timedelta(minutes=5))

    def test_missing_window_is_invalid_metadata(self):
        for field in ("eventStartTime", "endDate"):
            with self.subTest(field=field):
                self.error("invalid_metadata", **{field: None})

    def test_rejects_wrong_asset_duration_or_status(self):
        for patch in ({"active":False}, {"closed":True}, {"archived":True},
                      {"eventStartTime":(NOW-timedelta(hours=1)).isoformat()},
                      {"endDate":(NOW+timedelta(minutes=55)).isoformat()},
                      {"eventStartTime":(NOW+timedelta(minutes=10)).isoformat()},
                      {"endDate":NOW.isoformat()}):
            with self.subTest(patch=patch):
                self.assertIsNone(self.parse(**patch))
        self.assertIsNone(parse_market(make_event(seriesSlug="eth-up-or-down-15m"),
                                       make_market(question="Ethereum Up or Down?"), now=NOW))
        for patch in ({"active":False}, {"closed":True}, {"archived":True}):
            self.assertIsNone(parse_market(make_event(**patch), make_market(), now=NOW))

    def test_series_rename_can_use_btc_question(self):
        result = parse_market(make_event(seriesSlug="new-name"), make_market(), now=NOW)
        self.assertEqual(result.up.token_id, TOKEN_UP)

    def test_conflicting_asset_metadata_is_rejected(self):
        self.assertIsNone(self.parse(question="Ethereum Up or Down?"))
        self.assertIsNone(parse_market(make_event(seriesSlug="eth-up-or-down-15m"), make_market(), now=NOW))

    def test_missing_token_ids_does_not_use_position_ids(self):
        self.error("unsupported_identifier_shape", clobTokenIds=None, positionIds='["1","2"]')

    def test_extra_version_metadata_does_not_select_ids(self):
        result = self.parse(version="v2", positionIds='["1","2"]')
        self.assertEqual(result.up.token_id, TOKEN_UP)
        self.assertEqual(result.metadata["version"], "v2")

    def test_booleans_and_duplicate_ids_are_rejected(self):
        for tokens in ('[true,"2"]', '["1","1"]', '[]', '[""]', '[1,2]', 'bad'):
            with self.subTest(tokens=tokens):
                self.error("unsupported_identifier_shape", clobTokenIds=tokens)
        for patch in ({"id":True}, {"conditionId":"bad"}, {"outcomes":'["Up","Up"]'},
                      {"outcomes":'["Yes","No"]'}, {"outcomes":'[]'}, {"outcomes":'bad'},
                      {"active":1}, {"closed":"false"}, {"eventStartTime":"2026-10-10T00:45:00"}):
            with self.subTest(patch=patch):
                self.error("invalid_metadata", **patch)

    def test_malformed_optional_prices_warn_without_changing_tokens(self):
        for prices in (None, "bad", '["NaN","0.2"]', '[true,"0.2"]', '["0.2"]', '["2","0"]'):
            with self.subTest(prices=prices):
                result = self.parse(outcomePrices=prices)
                self.assertTrue(result.warnings)
                self.assertIsNone(result.up.metadata_price)
                self.assertEqual(result.up.token_id, TOKEN_UP)
        self.assertEqual(self.parse().up.metadata_price, Decimal("0.4"))

    def test_unknown_readiness_is_not_pricing_ready(self):
        for value in (None, False, 1, "true"):
            self.assertFalse(self.parse(acceptingOrders=value).pricing_ready)

    def test_recognized_slug_conflict_is_rejected(self):
        wrong = "btc-updown-15m-1"
        self.assertIsNone(self.parse(slug=wrong))
        self.assertIsNone(parse_market(make_event(slug=wrong), make_market(), now=NOW))

    def test_numeric_slug_out_of_range_is_metadata_error(self):
        self.error("invalid_metadata", slug="btc-updown-15m-" + "9" * 40)

    def test_bitcoin_cash_is_not_btc_even_with_renamed_series(self):
        for series in ("bch-up-or-down-15m","renamed-family","btc-up-or-down-15m"):
            with self.subTest(series=series):
                event = make_event(seriesSlug=series,title="Bitcoin Cash Up or Down?")
                market = make_market(question="Bitcoin Cash Up or Down?")
                self.assertIsNone(parse_market(event,market,now=NOW))

    def test_other_asset_series_contradicts_btc_question(self):
        event = make_event(seriesSlug="bch-up-or-down-15m")
        self.assertIsNone(parse_market(event,make_market(),now=NOW))

    def test_recognized_slug_requires_exact_start_instant(self):
        start = NOW-timedelta(minutes=5)
        slug = f"btc-updown-15m-{int(start.timestamp())}"
        for field in ("event", "market"):
            with self.subTest(field=field):
                event = make_event(slug=slug) if field == "event" else make_event()
                market = make_market(slug=slug) if field == "market" else make_market()
                self.assertIsNotNone(parse_market(event,market,now=NOW))
                fractional = start+timedelta(microseconds=500000)
                market.update(eventStartTime=fractional.isoformat(),
                              endDate=(fractional+timedelta(minutes=15)).isoformat())
                self.assertIsNone(parse_market(event,market,now=NOW))

    def test_deeply_nested_optional_prices_warn(self):
        result = self.parse(outcomePrices="["*5000+"0"+"]"*5000)
        self.assertTrue(result.warnings)
        self.assertIsNone(result.up.metadata_price)
        self.assertEqual(result.up.token_id,TOKEN_UP)

    def test_deeply_nested_required_arrays_have_explicit_errors(self):
        nested = "["*5000+"0"+"]"*5000
        self.error("invalid_metadata",outcomes=nested)
        self.error("unsupported_identifier_shape",clobTokenIds=nested)
