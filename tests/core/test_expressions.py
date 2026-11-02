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
