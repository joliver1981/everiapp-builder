"""Regression: dataset rows must serialize numeric DB types as JSON NUMBERS.

A DECIMAL/NUMERIC column coming back as a string silently breaks generated apps
(Recharts renders no bars, totals become NaN). `_coerce_for_json` must turn
Decimal into a real number and dates into ISO strings.
"""
from datetime import date, datetime

from decimal import Decimal

from src.datasets.service import _coerce_for_json


def test_decimal_becomes_number():
    whole = _coerce_for_json(Decimal("28476314.00"))
    assert whole == 28476314 and isinstance(whole, int)
    frac = _coerce_for_json(Decimal("76079623.29"))
    assert isinstance(frac, float) and abs(frac - 76079623.29) < 1e-6


def test_dates_iso():
    assert _coerce_for_json(date(2021, 5, 26)) == "2021-05-26"
    assert _coerce_for_json(datetime(2021, 5, 26, 9, 30, 0)) == "2021-05-26T09:30:00"


def test_passthrough_and_nested():
    assert _coerce_for_json(None) is None
    assert _coerce_for_json(True) is True
    assert _coerce_for_json("x") == "x"
    assert _coerce_for_json(3) == 3
    assert _coerce_for_json(2.5) == 2.5
    assert _coerce_for_json([Decimal("1.0"), Decimal("2.5")]) == [1, 2.5]
    assert _coerce_for_json({"a": Decimal("3.00")}) == {"a": 3}
    assert _coerce_for_json(b"hi") == "hi"
