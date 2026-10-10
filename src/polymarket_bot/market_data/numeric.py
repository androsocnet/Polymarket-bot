"""Bounded conversion and exact arithmetic for untrusted numeric evidence."""
import re
import json
from contextlib import contextmanager
from decimal import (Context, Decimal, DecimalException, Inexact, Rounded,
                     Overflow, Underflow, InvalidOperation, localcontext)

from .models import MarketDataError

_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?\Z")


def _invalid(field: str) -> MarketDataError:
    return MarketDataError("invalid_numeric", f"Invalid or unsupported numeric {field}.")


def parse_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, Decimal):
        raw = str(value)
    elif type(value) is int:
        if value.bit_length() > 426:
            raise _invalid(field)
        raw = str(value)
    elif type(value) is str:
        raw = str(value)
    else:
        raise _invalid(field)
    if len(raw) > 256:
        raise _invalid(field)
    match = _DECIMAL.fullmatch(raw.strip())
    if match is None:
        raise _invalid(field)
    coefficient = raw.strip().split("e")[0].split("E")[0]
    if sum(c.isdigit() for c in coefficient) > 128:
        raise _invalid(field)
    exponent = match[1]
    if exponent is not None:
        significant = exponent.lstrip("+-").lstrip("0")
        if len(significant) > 3 or int(significant or "0") > 128:
            raise _invalid(field)
    try:
        result = Decimal(raw.strip())
        if not result.is_finite() or (result and abs(result.adjusted()) > 128):
            raise _invalid(field)
        return result
    except (DecimalException, ValueError, OverflowError) as exc:
        raise _invalid(field) from exc


def parse_integer(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) not in (str, int):
        raise _invalid(field)
    if type(value) is int and value.bit_length() > 67:
        raise _invalid(field)
    raw = str(value)
    if len(raw) > 20 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", raw):
        raise _invalid(field)
    try:
        number = int(raw)
    except (ValueError, OverflowError) as exc:
        raise _invalid(field) from exc
    if number < minimum or number > maximum:
        raise _invalid(field)
    return number


def load_numeric_json(raw: bytes | str) -> object:
    try:
        return json.loads(raw,
                          parse_int=lambda x: parse_integer(x, field="JSON integer",
                                                              minimum=-(2**63), maximum=2**63-1),
                          parse_float=lambda x: parse_decimal(x, field="JSON decimal"),
                          parse_constant=lambda x: (_ for _ in ()).throw(_invalid("JSON constant")))
    except MarketDataError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _invalid("JSON payload") from exc


def decimal_text(value: Decimal) -> str:
    if (not isinstance(value, Decimal) or not value.is_finite() or
            len(value.as_tuple().digits) > 512 or abs(value.as_tuple().exponent) > 1024 or
            (value and abs(value.adjusted()) > 1024)):
        raise _invalid("Decimal text")
    try:
        result = format(value, "f")
        if len(result) > 2048:
            raise _invalid("Decimal text")
        return result
    except (DecimalException, ValueError, OverflowError) as exc:
        raise _invalid("Decimal text") from exc


@contextmanager
def exact_decimal_context():
    context = Context(prec=512, Emin=-1024, Emax=1024)
    for signal in (Inexact, Rounded, Overflow, Underflow, InvalidOperation):
        context.traps[signal] = True
    with localcontext(context) as active:
        yield active
