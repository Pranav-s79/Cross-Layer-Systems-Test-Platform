from __future__ import annotations

import pytest

from crosslayer.core.contracts.expressions import Expression, ExpressionRuntimeError
from crosslayer.core.errors import ExpressionError, MissingEvidence
from tests.conftest import ev


def test_pc_plus_four_with_32_bit_wrap():
    expr = Expression(
        'curr.attributes["rtl.pc"] == (prev.attributes["rtl.pc"] + 4) & 0xFFFFFFFF',
        names={"prev", "curr"},
    )
    prev = ev("rtl.fetch", 1, attrs={"rtl.pc": 0xFFFFFFFC})
    curr = ev("rtl.fetch", 2, attrs={"rtl.pc": 0x0})
    assert expr.evaluate(prev=prev, curr=curr) is True


def test_boolean_short_circuit_and_functions():
    expr = Expression('e.severity == "error" or max(1, len(e.attributes)) > 0', names={"e"})
    assert expr.evaluate(e=ev("x.y", 0))


def test_conditional_and_membership():
    expr = Expression('"ok" if e.event_type in ("a.b", "c.d") else "no"', names={"e"})
    assert expr.evaluate(e=ev("a.b", 0)) == "ok"


def test_missing_attribute_is_missing_evidence():
    expr = Expression('e.attributes["rtl.pc"] == 4', names={"e"})
    with pytest.raises(MissingEvidence):
        expr.evaluate(e=ev("x.y", 0))


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('echo hi')",  # call to non-whitelisted name
        "e.__class__",  # dunder attribute
        "e.attributes.get('x')",  # method call
        "[x for x in e.attributes]",  # comprehension
        "lambda: 1",
        "e.attributes['a'] ** 2",  # power is not allowed
        "undefined_name > 1",
        "max(*e.attributes)",
        "e.event_type = 'x'",  # assignment is a syntax error in eval mode
    ],
)
def test_static_validation_rejects_unsafe_or_unknown(source):
    with pytest.raises(ExpressionError):
        Expression(source, names={"e"})


def test_runtime_type_error_is_reported():
    expr = Expression('e.attributes["a.b"] + 1', names={"e"})
    with pytest.raises(ExpressionRuntimeError):
        expr.evaluate(e=ev("x.y", 0, attrs={"a.b": "text"}))


def test_resource_guards():
    with pytest.raises(ExpressionRuntimeError, match="shift"):
        Expression("1 << 100000", names=set()).evaluate()
    with pytest.raises(ExpressionRuntimeError, match="repetition"):
        Expression("'a' * 1000000000", names=set()).evaluate()


def test_attribute_access_only_on_events():
    expr = Expression("e.event_type", names={"e"})
    with pytest.raises(ExpressionRuntimeError):
        expr.evaluate(e={"event_type": "x"})


def test_unbound_name_at_evaluation():
    expr = Expression("e.event_type", names={"e"})
    with pytest.raises(ExpressionRuntimeError):
        expr.evaluate()


# -- randomized checks (seeded, no extra dependency) -------------------------

_RANDOM_NAMES = ("e", "prev")
_ALLOWED_LEAVES = ('1', '2.5', '"a"', 'True', 'None', 'e.severity', 'e.attributes["x.k"]', "e.timestamp_ns")
_ALLOWED_OPS = ("+", "-", "*", "/", "//", "%", "&", "|", "^", "<<", ">>", "==", "<", ">=", "in", "and", "or")
_DISALLOWED = (
    "lambda: 1", "[x for x in (1, 2)]", "{1: 2}", "{1, 2}", "f'{e}'", "(y := 1)", "e.__class__",
    "__import__('os')", "open('f')", "eval('1')", "abs(x=1)", "max(*[1, 2])", "e.nope", "unknown_name",
    "1 if e else (lambda: 2)", "(1, 2)[1:2]", "await e", "1 @ 2", "b'x'", "...", "1j",
)


def _random_expr(rng, depth=0):
    if depth >= 3 or rng.random() < 0.3:
        return rng.choice(_ALLOWED_LEAVES)
    kind = rng.choice(("bin", "not", "call", "ifexp", "tuple"))
    sub = lambda: _random_expr(rng, depth + 1)  # noqa: E731
    if kind == "bin":
        return f"({sub()} {rng.choice(_ALLOWED_OPS)} {sub()})"
    if kind == "not":
        return f"(not {sub()})"
    if kind == "call":
        return f"{rng.choice(('abs', 'min', 'max', 'len', 'int', 'float', 'round'))}({sub()})"
    if kind == "ifexp":
        return f"({sub()} if {sub()} else {sub()})"
    return f"({sub()}, {sub()})"


def test_random_expressions_never_raise_undocumented_errors():
    import random

    rng = random.Random(20240601)
    event = ev("a.b", 5, attrs={"x.k": 3})
    for _ in range(1500):
        source = _random_expr(rng)
        expr = Expression(source, names=set(_RANDOM_NAMES))
        try:
            expr.evaluate(e=event, prev=event)
        except (ExpressionRuntimeError, MissingEvidence):
            pass


def test_random_disallowed_syntax_is_rejected_at_load():
    import random

    rng = random.Random(7)
    for _ in range(300):
        bad = rng.choice(_DISALLOWED)
        source = _random_expr(rng, depth=2) + rng.choice((" + ", " and ", " == ")) + f"({bad})"
        with pytest.raises(ExpressionError):
            Expression(source, names=set(_RANDOM_NAMES))
