"""Time-unit helpers. All internal times are integer nanoseconds."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def _to_ns(value: int | float | str, scale: int, unit: str) -> int:
    try:
        ns = Decimal(str(value)) * scale
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not ns.is_finite() or ns != ns.to_integral_value():
        raise ValueError(f"{value} {unit} is not a whole number of nanoseconds")
    return int(ns)


def ms_to_ns(value: int | float | str) -> int:
    """Convert milliseconds to integer nanoseconds without float rounding.

    ``int(1.001 * 1_000_000)`` is 1000999, one nanosecond short, because
    1.001 has no exact binary representation. Going through
    ``Decimal(str(x))`` gives exactly 1_001_000.
    """
    return _to_ns(value, NS_PER_MS, "ms")


def us_to_ns(value: int | float | str) -> int:
    """Convert microseconds to integer nanoseconds without float rounding."""
    return _to_ns(value, NS_PER_US, "us")


def decimal_seconds_to_ns(text: str) -> int:
    """Parse a log timestamp like ``"12.000450"`` (seconds) into exact ns."""
    whole, _, frac = text.strip().partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()) or len(frac) > 9:
        raise ValueError(f"not a decimal seconds timestamp: {text!r}")
    return int(whole) * NS_PER_S + int(frac.ljust(9, "0") or "0")


def format_ns(ns: int | float) -> str:
    """Human-readable duration: 1500 -> '1.5 us', 2_500_000 -> '2.5 ms'."""
    magnitude = abs(ns)
    if magnitude >= NS_PER_S:
        return f"{ns / NS_PER_S:.3g} s"
    if magnitude >= NS_PER_MS:
        return f"{ns / NS_PER_MS:.3g} ms"
    if magnitude >= NS_PER_US:
        return f"{ns / NS_PER_US:.3g} us"
    return f"{ns:g} ns"
