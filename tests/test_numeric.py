import unittest
from decimal import Decimal, getcontext, setcontext, InvalidOperation

from polymarket_bot.market_data import numeric
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.clob import parse_book
from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.parsing import parse_market
from tests.support import make_book, make_event, make_market, TOKEN_UP, CONDITION, NOW


class NumericTests(unittest.TestCase):
    def invalid(self, value):
        with self.assertRaises(MarketDataError) as caught:
            numeric.parse_decimal(value, field="test field")
        self.assertEqual(caught.exception.code, "invalid_numeric")
        self.assertLess(len(str(caught.exception)), 100)

    def test_numeric_input_budgets(self):
        for value in ("1e129", "1e-129", "1e999999999999999999999", "1" * 129,
                      " " * 256 + "1", "1." + "0" * 255 + "1"):
            with self.subTest(value=value[:20]):
                self.invalid(value)
        self.assertEqual(numeric.parse_decimal("1e128", field="x"), Decimal("1e128"))
        self.assertEqual(numeric.parse_decimal("1e-128", field="x"), Decimal("1e-128"))
        self.assertEqual(numeric.parse_decimal("0.00000000000000000000000000001", field="x"),
                         Decimal("0.00000000000000000000000000001"))

    def test_zero_padded_exponent_within_value_budget(self):
        self.assertEqual(numeric.parse_decimal("1e0128", field="probe"), Decimal("1e128"))
        self.assertEqual(numeric.parse_decimal("1e" + "0" * 250 + "128", field="probe"),
                         Decimal("1e128"))
        self.assertEqual(numeric.parse_decimal("1e-" + "0" * 249 + "128", field="probe"),
                         Decimal("1e-128"))

    def test_oversized_python_integer_for_decimal_helper_is_structured_error(self):
        huge = 10 ** 5000
        self.invalid(huge)

    def test_oversized_python_integer_for_integer_helper_is_structured_error(self):
        huge = 10 ** 5000
        with self.assertRaises(MarketDataError) as caught:
            numeric.parse_integer(huge, field="probe", minimum=0, maximum=2**63-1)
        self.assertEqual(caught.exception.code, "invalid_numeric")
        self.assertLess(len(str(caught.exception)), 100)

    def test_nonfinite_and_wrong_types(self):
        for value in ("NaN", "sNaN", "Infinity", True, False, 0.2, Decimal("NaN")):
            with self.subTest(value=repr(value)):
                self.invalid(value)

    def test_bounded_integer_and_json_tokens(self):
        for value in (True, "1" * 257, "9999999999999999999999999", -1, 101):
            with self.subTest(value=repr(value)[:20]):
                with self.assertRaises(MarketDataError) as caught:
                    numeric.parse_integer(value, field="count", minimum=0, maximum=100)
                self.assertEqual(caught.exception.code, "invalid_numeric")
        self.assertEqual(numeric.parse_integer("100", field="count", minimum=0, maximum=100), 100)
        for token in (b'{"x":1e999999999999999999999999}', b'{"x":' + b'1' * 257 + b'}',
                      b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(token=token[:30]):
                with self.assertRaises(MarketDataError) as caught:
                    numeric.load_numeric_json(token)
                self.assertEqual(caught.exception.code, "invalid_numeric")
        self.assertEqual(numeric.load_numeric_json(b'{"x":0.00000000000000000000000000001}')["x"],
                         Decimal("0.00000000000000000000000000001"))

    def test_decimal_text_exact_and_context_independent(self):
        before = getcontext().copy()
        try:
            getcontext().prec = 2
            getcontext().Emax = 2
            getcontext().Emin = -2
            self.assertEqual(numeric.decimal_text(Decimal("1e-128")), "0." + "0" * 127 + "1")
            self.assertEqual(numeric.decimal_text(Decimal("1e128")), "1" + "0" * 128)
            derived = Decimal("1." + "0" * 255 + "1e128")
            self.assertEqual(numeric.decimal_text(derived), "1" + "0" * 128 + "." + "0" * 127 + "1")
            with self.assertRaises(MarketDataError):
                numeric.decimal_text(Decimal("1e1000000"))
        finally:
            getcontext().prec = before.prec
            getcontext().Emax = before.Emax
            getcontext().Emin = before.Emin

    def test_ambient_context_does_not_change_results(self):
        before = getcontext().copy()
        try:
            getcontext().prec = 2
            getcontext().Emax = 2
            getcontext().Emin = -2
            getcontext().traps[InvalidOperation] = True
            ambient = getcontext().copy()
            data = make_book(bids=[{"price":"0.60","size":"1e128"},
                                   {"price":"0.60","size":"1e-128"}],
                             asks=[{"price":"0.61","size":"1"}])
            book = parse_book(JsonRead(data, 0, 1), token_id=TOKEN_UP, condition_id=CONDITION)
            self.assertEqual(book.midpoint, Decimal("0.605"))
            self.assertEqual(book.bid_size, Decimal("1." + "0" * 255 + "1e128"))
            self.assertEqual((getcontext().prec, getcontext().Emax, getcontext().Emin,
                              getcontext().flags, getcontext().traps),
                             (ambient.prec, ambient.Emax, ambient.Emin,
                              ambient.flags, ambient.traps))
            self.assertEqual(getcontext().prec, 2)
            self.assertEqual(getcontext().Emax, 2)
            self.assertEqual(getcontext().Emin, -2)
        finally:
            setcontext(before)

    def test_numeric_json_and_metadata_containment(self):
        for field, value, code in (("id", "1" * 500, "invalid_metadata"),
                                   ("slug", "x" * 500, "invalid_metadata"),
                                   ("clobTokenIds", '["' + '1' * 500 + '","2"]',
                                    "unsupported_identifier_shape")):
            with self.subTest(field=field):
                with self.assertRaises(MarketDataError) as caught:
                    parse_market(make_event(), make_market(**{field:value}), now=NOW)
                self.assertEqual(caught.exception.code, code)
        result = parse_market(make_event(), make_market(outcomePrices='["1e-1000000","0.2"]'), now=NOW)
        self.assertIsNone(result.up.metadata_price)
        self.assertTrue(result.warnings)
