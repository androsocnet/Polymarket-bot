import unittest
from tests.support import *
from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.clob import ServerClock
from polymarket_bot.market_data.discovery import discover_current
from polymarket_bot.market_data.models import MarketDataError

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.time = FakeClock()
        self.clock = ServerClock(JsonRead(int(NOW.timestamp()),0,0),monotonic=self.time.monotonic)
    def discover(self, gamma, fast=True):
        return discover_current(gamma, clock=self.clock, deadline=120, use_fast_path=fast)
    def test_list_discovery_survives_slug_rename_and_disabled_fast_path(self):
        gamma = FakeGamma(fast=AssertionError("Fast path must be disabled"))
        result, source = self.discover(gamma, fast=False)
        self.assertEqual(source,"filtered_list")
        self.assertEqual(result.market_id,MARKET_ID)
        self.assertEqual(gamma.slug_calls,[])

    def error(self, gamma, code, fast=False):
        with self.assertRaises(MarketDataError) as caught:
            self.discover(gamma, fast=fast)
        self.assertEqual(caught.exception.code,code)

    def test_fast_path_validates_all_children(self):
        gamma = FakeGamma(fast=make_event(markets=[make_market(id="9",closed=True),make_market()]))
        result, source = self.discover(gamma)
        self.assertEqual((result.market_id,source),(MARKET_ID,"fast_path"))
        self.assertEqual(gamma.list_calls,[])
        self.assertEqual(gamma.slug_calls[0][0],"btc-updown-15m-1791593100")

    def test_fast_404_falls_back(self):
        gamma = FakeGamma(fast=MarketDataError("transport_error","missing",http_status=404))
        self.assertEqual(self.discover(gamma)[1],"filtered_list")
        self.assertEqual(gamma.refresh_calls,[(MARKET_ID,120)])

    def test_wrong_or_malformed_fast_payload_falls_back(self):
        for fast in (make_event(markets=[]),make_event(markets="bad"),
                     make_event(markets=[make_market(clobTokenIds=None)]),
                     make_event(markets=[make_market(),make_market(id="1002")]),
                     MarketDataError("invalid_metadata","bad JSON")):
            with self.subTest(fast=fast):
                self.assertEqual(self.discover(FakeGamma(fast=fast))[1],"filtered_list")

    def test_fast_transport_failures_are_preserved(self):
        for status in (403,503):
            self.error(FakeGamma(fast=MarketDataError("transport_error","failed",http_status=status)), "transport_error", fast=True)

    def test_future_open_market_not_selected(self):
        future = make_market(eventStartTime=(NOW+timedelta(minutes=10)).isoformat(),
                             endDate=(NOW+timedelta(minutes=25)).isoformat())
        self.error(FakeGamma(events=[make_event(markets=[future])]),"no_current_market")

    def test_fixed_bounds_across_pages(self):
        unrelated = [make_event(id=str(i),seriesSlug="other",title="Other",
                                 markets=[make_market(question="Other")]) for i in range(100)]
        gamma = FakeGamma(events=unrelated+[make_event()])
        self.assertEqual(self.discover(gamma,fast=False)[0].market_id,MARKET_ID)
        self.assertEqual([c[2] for c in gamma.list_calls],[0,100])
        self.assertEqual({c[0] for c in gamma.list_calls},{NOW})
        self.assertEqual({c[1] for c in gamma.list_calls},{NOW+timedelta(minutes=30)})

    def test_filters_do_not_send_recurrence(self):
        import httpx
        from polymarket_bot.market_data.http import PublicGet
        from polymarket_bot.market_data.gamma import GammaClient
        requests = []
        def handle(r):
            requests.append(r)
            return httpx.Response(200, stream=httpx.ByteStream(b"[]"))
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            gamma = GammaClient(PublicGet(client,monotonic=self.time.monotonic,sleep=self.time.sleep,utcnow=self.time.utcnow))
            gamma.list_events(end_min=NOW,end_max=NOW+timedelta(minutes=30),offset=100,deadline=120)
        query = requests[0].url.params
        self.assertNotIn("recurrence",query)
        self.assertEqual(dict(query),{"active":"true","closed":"false","end_date_min":NOW.isoformat(),
            "end_date_max":(NOW+timedelta(minutes=30)).isoformat(),"limit":"100","offset":"100","order":"endDate","ascending":"true"})

    def test_ambiguous_distinct_market_ids_fail(self):
        self.error(FakeGamma(events=[make_event(markets=[make_market(),make_market(id="1002")])]),"ambiguous_market")

    def test_zero_candidates_fail(self):
        self.error(FakeGamma(events=[]),"no_current_market")

    def test_current_target_missing_ids_is_explicit_error(self):
        self.error(FakeGamma(events=[make_event(markets=[make_market(clobTokenIds=None)])]),"unsupported_identifier_shape")

    def test_repeated_full_pages_remain_incomplete(self):
        gamma = FakeGamma(events=[make_event()]*500)
        self.error(gamma,"discovery_incomplete")
        self.assertEqual([c[2] for c in gamma.list_calls],[0,100,200,300,400])

    def test_refreshed_identity_change_is_rejected(self):
        for patch in ({"id":"changed"},{"conditionId":"0x"+"cd"*32},
                      {"clobTokenIds":'["3","4"]'},{"endDate":NOW.isoformat()}):
            with self.subTest(patch=patch):
                self.error(FakeGamma(refresh=make_market(**patch)),"invalid_metadata")

    def test_duplicate_candidates_deduplicate_but_conflicts_fail(self):
        self.assertEqual(self.discover(FakeGamma(events=[make_event(),make_event()]),fast=False)[0].market_id,MARKET_ID)
        self.error(FakeGamma(events=[make_event(),make_event(markets=[make_market(clobTokenIds='["3","4"]')])]),"invalid_metadata")

    def test_bch_only_fallback_returns_no_current_btc(self):
        bch = make_event(id="2002",seriesSlug="bch-up-or-down-15m",title="Bitcoin Cash Up or Down?",
                         markets=[make_market(id="1002",question="Bitcoin Cash Up or Down?")])
        self.error(FakeGamma(events=[bch]),"no_current_market")

    def test_btc_plus_bch_fallback_is_not_ambiguous(self):
        bch = make_event(id="2002",seriesSlug="bch-up-or-down-15m",title="Bitcoin Cash Up or Down?",
                         markets=[make_market(id="1002",question="Bitcoin Cash Up or Down?")])
        result,source = self.discover(FakeGamma(events=[bch,make_event()]),fast=False)
        self.assertEqual((result.market_id,source),(MARKET_ID,"filtered_list"))

    def test_fractional_start_conflict_rejects_fast_path(self):
        start = NOW-timedelta(minutes=5)+timedelta(microseconds=500000)
        fast = make_event(slug="btc-updown-15m-1791593100",markets=[make_market(
            eventStartTime=start.isoformat(),endDate=(start+timedelta(minutes=15)).isoformat())])
        result,source = self.discover(FakeGamma(fast=fast))
        self.assertEqual((result.market_id,source),(MARKET_ID,"filtered_list"))
