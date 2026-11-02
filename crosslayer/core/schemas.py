"""JSON Schemas for CrossLayer's YAML files, for editor validation and autocomplete.

The Python loaders remain authoritative: they also check things a schema
cannot express (expression safety, ``min <= max``, cross-references between
contracts and cases). The schemas are kept *no stricter* than the loaders, so
an editor never flags a file that CrossLayer would accept;
``tests/core/test_schemas.py`` enforces this on every YAML file in the repository.

Regenerate the checked-in copies with ``crosslayer schema --write schemas/``.
"""

from __future__ import annotations

from typing import Any, Callable

BASE_URL = "https://raw.githubusercontent.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/main/schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"

_STR = {"type": "string", "minLength": 1}
_STR_OR_LIST = {"oneOf": [_STR, {"type": "array", "items": _STR, "minItems": 1}]}
_NON_NEG_INT = {"type": "integer", "minimum": 0}
_ID = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*$"}
_OPT_TEXT = {"type": ["string", "null"]}

_SELECTOR = {
    "type": "object",
    "description": "Conjunction of filters over normalized events.",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "event_type": {**_STR_OR_LIST, "description": "Event type or shell pattern, e.g. packet.*"},
        "subsystem": {**_STR_OR_LIST, "description": "Subsystem name or shell pattern"},
        "source_system": _STR_OR_LIST,
        "severity": _STR_OR_LIST,
        "attributes": {"type": "object", "minProperties": 1, "description": "Exact-match attribute filters"},
        "where": {**_STR, "description": "Expression over the event `e`"},
    },
}
_SELECTORS = {"oneOf": [{"$ref": "#/$defs/selector"},
                        {"type": "array", "items": {"$ref": "#/$defs/selector"}, "minItems": 1}]}


def _rule(kind: str, required: list[str], properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", *required],
        "properties": {"kind": {"const": kind}, **properties},
        **extra,
    }


_RULES = [
    _rule("count", ["select"],
          {"select": {"$ref": "#/$defs/selector"}, "min": _NON_NEG_INT, "max": _NON_NEG_INT},
          anyOf=[{"required": ["min"]}, {"required": ["max"]}]),
    _rule("response", ["trigger", "response"], {
        "trigger": {"$ref": "#/$defs/selector"},
        "response": _SELECTORS,
        "correlate_by": {**_STR, "description": "Correlation-id key shared by trigger and response"},
        "within_ms": {"type": "number", "exclusiveMinimum": 0, "description": "Deadline in milliseconds"},
        "min_triggers": _NON_NEG_INT,
    }),
    _rule("transitions", ["select", "state", "allowed"], {
        "select": {"$ref": "#/$defs/selector"},
        "state": {"oneOf": [{"const": "event_type"},
                            {"type": "object", "additionalProperties": False, "required": ["attribute"],
                             "properties": {"attribute": _STR}}]},
        "group_by": _STR,
        "initial": _STR_OR_LIST,
        "allowed": {"type": "object", "minProperties": 1,
                    "additionalProperties": {"oneOf": [{"type": "null"}, {"type": "array", "items": _STR}]}},
        "allow_repeat": {"type": "boolean"},
        "min_events": _NON_NEG_INT,
    }),
    _rule("threshold", ["select", "aggregate", "op", "value"], {
        "select": {"$ref": "#/$defs/selector"},
        "attribute": _STR,
        "aggregate": {"enum": ["max", "min", "mean", "sum", "count", "p50", "p90", "p95", "p99"]},
        "op": {"enum": ["<", "<=", ">", ">=", "==", "!="]},
        "value": {"type": "number"},
        "min_samples": _NON_NEG_INT,
    }, **{"if": {"properties": {"aggregate": {"not": {"const": "count"}}}},
          "then": {"required": ["attribute"]}}),
    _rule("invariant", ["select", "expect"], {
        "select": {"$ref": "#/$defs/selector"},
        "expect": {**_STR, "description": "Expression over the event `e` that must be true"},
        "min_matches": _NON_NEG_INT,
    }),
    _rule("successive", ["select", "expect"], {
        "select": {"$ref": "#/$defs/selector"},
        "expect": {**_STR, "description": "Expression over `prev` and `curr`"},
        "when": {**_STR, "description": "Only check pairs where this is true"},
        "group_by": _STR,
        "min_pairs": _NON_NEG_INT,
    }),
]

_CONTRACT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "title", "owner", "source", "destination", "rule"],
    "properties": {
        "id": _ID,
        "title": _STR,
        "owner": {**_STR, "description": "Team or subsystem accountable for this boundary"},
        "source": _STR,
        "destination": _STR,
        "description": _OPT_TEXT,
        "severity": {"enum": ["critical", "major", "minor"]},
        "on_fail": {"enum": ["fail_run", "warn"]},
        "precondition": {"$ref": "#/$defs/selector"},
        "evidence": _SELECTORS,
        "tags": {"type": "array", "items": {"type": "string"}},
        "rule": {"oneOf": _RULES},
    },
}


def _defs() -> dict[str, Any]:
    return {"selector": _SELECTOR, "contract": _CONTRACT}


def _document(name: str, title: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": DIALECT,
        "$id": f"{BASE_URL}/{name}.schema.json",
        "title": title,
        **body,
        "$defs": _defs(),
    }


def contracts_schema() -> dict[str, Any]:
    return _document("contracts", "CrossLayer contracts", {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version"],
        "properties": {
            "schema_version": {"const": 1},
            "contracts": {"type": "array", "items": {"$ref": "#/$defs/contract"}},
        },
    })


def experiment_schema() -> dict[str, Any]:
    fault = {"oneOf": [_STR, {"type": "object", "additionalProperties": False, "required": ["name"],
                              "properties": {"name": _STR, "params": {"type": "object"}}}]}
    return _document("experiment", "CrossLayer experiment", {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "name", "adapter", "seed"],
        "properties": {
            "schema_version": {"const": 1},
            "name": _STR,
            "description": _OPT_TEXT,
            "adapter": {**_STR, "description": "Registered adapter name or package.module:Class"},
            "seed": _NON_NEG_INT,
            "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            "parameters": {"type": ["object", "null"], "description": "Adapter-specific parameters"},
            "faults": {"type": ["array", "null"], "items": fault},
            "contracts": {"type": ["array", "null"],
                          "items": {"oneOf": [_STR, {"$ref": "#/$defs/contract"}]}},
            "include_adapter_contracts": {"type": "boolean"},
            "inputs": {"type": ["array", "null"], "items": {"type": "string"}},
            "base_dir": {"type": "string"},
        },
    })


def contract_tests_schema() -> dict[str, Any]:
    t = {"type": "number"}
    event = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type"],
        "oneOf": [{"required": ["t_ns"]}, {"required": ["t_us"]}, {"required": ["t_ms"]}],
        "properties": {
            "t_ns": {"type": "integer"}, "t_us": t, "t_ms": t,
            "type": _STR, "subsystem": _STR, "source": _STR, "severity": _STR,
            "ids": {"type": "object"}, "attrs": {"type": "object"},
        },
    }
    verdict = {"enum": ["pass", "fail", "inconclusive", "skipped", "error"]}
    case = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "contract", "expect"],
        "oneOf": [{"required": ["events"]}, {"required": ["trace"]}],
        "properties": {
            "name": _STR, "contract": _STR, "expect": verdict, "description": _OPT_TEXT,
            "events": {"type": "array", "items": event},
            "trace": {**_STR, "description": "Path to a normalized_events.jsonl file"},
            "expect_violations": _NON_NEG_INT, "expect_unresolved": _NON_NEG_INT,
        },
    }
    return _document("contract-tests", "CrossLayer contract tests", {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "cases"],
        "properties": {
            "schema_version": {"const": 1},
            "adapter": _STR,
            "contracts": {"type": "array", "items": _STR},
            "cases": {"type": "array", "items": case, "minItems": 1},
        },
    })


def suite_schema() -> dict[str, Any]:
    entry = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "path"],
        "properties": {
            "id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]*$"},
            "path": {**_STR, "description": "Experiment file, relative to the suite"},
            "expect": {"enum": ["pass", "fail", "inconclusive", "skipped", "error"],
                       "description": "Verdict this experiment should produce (default pass)"},
            "depends_on": _STR_OR_LIST,
        },
    }
    return _document("suite", "CrossLayer experiment suite", {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "name", "experiments"],
        "properties": {
            "schema_version": {"const": 1},
            "name": _STR,
            "description": _OPT_TEXT,
            "experiments": {"type": "array", "items": entry, "minItems": 1},
        },
    })


SCHEMAS: dict[str, Callable[[], dict[str, Any]]] = {
    "contracts": contracts_schema,
    "experiment": experiment_schema,
    "contract-tests": contract_tests_schema,
    "suite": suite_schema,
}
