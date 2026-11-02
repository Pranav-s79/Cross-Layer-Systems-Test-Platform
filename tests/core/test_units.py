from __future__ import annotations

import pytest

from crosslayer.core.units import decimal_seconds_to_ns, format_ns, ms_to_ns


def test_ms_to_ns_is_exact():
    assert int(1.001 * 1_000_000) == 1_000_999  # the float trap this helper avoids
    assert ms_to_ns(1.001) == 1_001_000
    assert ms_to_ns(0.3) == 300_000
    assert ms_to_ns(5) == 5_000_000
    with pytest.raises(ValueError):
        ms_to_ns(0.0000001)  # 0.1 ns is not representable


def test_decimal_seconds_to_ns():
    assert decimal_seconds_to_ns("0.000001") == 1_000
    assert decimal_seconds_to_ns("12.5") == 12_500_000_000
    assert decimal_seconds_to_ns("1") == 1_000_000_000
    for bad in ("-1.0", "1.2.3", "abc", "1.0000000001"):
        with pytest.raises(ValueError):
            decimal_seconds_to_ns(bad)


def test_format_ns():
    assert format_ns(500) == "500 ns"
    assert format_ns(1_500) == "1.5 us"
    assert format_ns(5_000_000) == "5 ms"
    assert format_ns(2_000_000_000) == "2 s"
