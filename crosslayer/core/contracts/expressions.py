"""A small, safe expression language for contract rules.

Contracts need predicates such as::

    curr.attributes["rtl.pc"] == (prev.attributes["rtl.pc"] + 4) & 0xFFFFFFFF
    e.attributes["pipeline.crc_rx"] == e.attributes["pipeline.crc_calc"]

Python's ``eval`` would run arbitrary code from a YAML file, so instead the
expression is parsed with ``ast`` and checked against a whitelist when the
contract is *loaded*, so a bad contract fails before any experiment runs.
Evaluation then walks the tree directly.

Allowed: literals, names bound by the rule (``e``, ``prev``, ``curr``), event
field access, subscripts, arithmetic/bitwise/comparison/boolean operators,
conditional expressions, and the functions ``abs``, ``min``, ``max``, ``len``,
``int``, ``float``, ``round``.

A missing attribute or correlation id raises ``MissingEvidence``, which rules
report as *unresolved* rather than as a failure.
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Iterable

from crosslayer.core.errors import CrossLayerError, ExpressionError, MissingEvidence
from crosslayer.core.telemetry.events import NormalizedEvent

EVENT_FIELDS = frozenset(
    {
        "event_id",
        "source_system",
        "subsystem",
        "timestamp_ns",
        "event_type",
        "severity",
        "correlation_ids",
        "attributes",
        "raw_artifact_ref",
    }
)

SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "int": int,
    "float": float,
    "round": round,
}

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}

_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
    ast.Invert: operator.invert,
}

_CMPOPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Constant, ast.Name, ast.Load,
    ast.Attribute, ast.Subscript, ast.Tuple, ast.List,
    *_BINOPS, *_UNARYOPS, *_CMPOPS,
)

_MAX_SHIFT = 256  # stops `1 << 10**9` from allocating gigabytes


class ExpressionRuntimeError(CrossLayerError):
    """The expression is well-formed but failed on real data (type error,
    division by zero). This indicates a contract bug, so it is an ERROR."""


class Expression:
    def __init__(self, source: str, names: Iterable[str], *, location: str | None = None) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ExpressionError("expression must be a non-empty string", location=location)
        self.source = source
        self.names = frozenset(names)
        self._location = location
        try:
            tree = ast.parse(source.strip(), mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"syntax error in {source!r}: {exc.msg}", location=location) from None
        self._validate(tree)
        self._body = tree.body

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"

    def references(self) -> set[tuple[str, str]]:
        """Constant keys the expression reads, as ``(field, key)`` pairs.

        ``e.attributes["rtl.pc"]`` gives ``("attributes", "rtl.pc")`` and
        ``prev.correlation_ids["ue"]`` gives ``("correlation_ids", "ue")``.
        Used by the linter to check keys against an adapter's event catalog.
        """
        refs: set[tuple[str, str]] = set()
        for node in ast.walk(self._body):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr in ("attributes", "correlation_ids")
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                refs.add((node.value.attr, node.slice.value))
        return refs

    # -- static validation -------------------------------------------------

    def _fail(self, message: str) -> None:
        raise ExpressionError(f"{message} in {self.source!r}", location=self._location)

    def _validate(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                self._fail(f"'{type(node).__name__}' is not allowed")
            if isinstance(node, ast.Name):
                if node.id not in self.names and node.id not in SAFE_FUNCTIONS:
                    allowed = ", ".join(sorted(self.names)) or "(none)"
                    self._fail(f"unknown name '{node.id}' (available: {allowed})")
            elif isinstance(node, ast.Attribute):
                if node.attr not in EVENT_FIELDS:
                    self._fail(f"unknown event field '.{node.attr}'")
            elif isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCTIONS:
                    self._fail("only abs/min/max/len/int/float/round may be called")
                if node.keywords:
                    self._fail("keyword arguments are not allowed")
                if any(isinstance(arg, ast.Starred) for arg in node.args):
                    self._fail("star arguments are not allowed")
            elif isinstance(node, ast.Constant):
                if not isinstance(node.value, (int, float, str, bool, type(None))):
                    self._fail(f"constant {node.value!r} is not allowed")

    # -- evaluation ----------------------------------------------------------

    def evaluate(self, **context: Any) -> Any:
        missing = self.names - context.keys()
        if missing:
            raise ExpressionRuntimeError(f"{self.source!r}: unbound names {sorted(missing)}")
        try:
            return self._eval(self._body, context)
        except (MissingEvidence, ExpressionRuntimeError):
            raise
        except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            raise ExpressionRuntimeError(f"{self.source!r}: {type(exc).__name__}: {exc}") from exc

    def _eval(self, node: ast.AST, ctx: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return ctx[node.id] if node.id in ctx else SAFE_FUNCTIONS[node.id]
        if isinstance(node, ast.Attribute):
            target = self._eval(node.value, ctx)
            if not isinstance(target, NormalizedEvent):
                raise ExpressionRuntimeError(
                    f"{self.source!r}: '.{node.attr}' can only be used on an event"
                )
            return getattr(target, node.attr)
        if isinstance(node, ast.Subscript):
            container = self._eval(node.value, ctx)
            key = self._eval(node.slice, ctx)
            try:
                return container[key]
            except (KeyError, IndexError):
                raise MissingEvidence(f"{self.source!r}: {key!r} was not observed") from None
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value in node.values:
                    result = self._eval(value, ctx)
                    if not result:
                        return result
                return result
            result = False
            for value in node.values:
                result = self._eval(value, ctx)
                if result:
                    return result
            return result
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, ctx)
            right = self._eval(node.right, ctx)
            if isinstance(node.op, ast.Mult) and (
                isinstance(left, (str, list, tuple)) or isinstance(right, (str, list, tuple))
            ):
                raise ExpressionRuntimeError(f"{self.source!r}: sequence repetition is not allowed")
            if isinstance(node.op, ast.LShift) and isinstance(right, int) and right > _MAX_SHIFT:
                raise ExpressionRuntimeError(f"{self.source!r}: shift amount {right} too large")
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            return _UNARYOPS[type(node.op)](self._eval(node.operand, ctx))
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, ctx)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, ctx)
                if not _CMPOPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._eval(node.test, ctx) else node.orelse
            return self._eval(branch, ctx)
        if isinstance(node, ast.Call):
            func = SAFE_FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
            return func(*(self._eval(arg, ctx) for arg in node.args))
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(self._eval(elt, ctx) for elt in node.elts)
        raise ExpressionRuntimeError(f"{self.source!r}: unsupported node {type(node).__name__}")
