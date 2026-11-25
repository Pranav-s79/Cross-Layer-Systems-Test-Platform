"""JSON Schema for ``core.yaml`` core profiles.

Kept no stricter than ``profile.py``, which stays authoritative: it also checks
that files exist, that mutant edits name real sources, and that mutant targets
match the adapter. ``tests/integrations/test_rv32i_profile_schema.py`` checks
the accepted keys and every built-in profile against it.
"""

from __future__ import annotations

from typing import Any

from crosslayer.core.schemas import BASE_URL, DIALECT
from crosslayer.integrations.rv32i import profile
from crosslayer.integrations.rv32i.traces import RETIRE_FIELDS, TRACE_FORMATS

_STR = {"type": "string", "minLength": 1}
_SIGNAL = {"type": "string", "pattern": profile._SIGNAL_NAME_RE.pattern}
_DEFINES = {"type": "array", "items": {"type": "string", "pattern": profile._DEFINE_RE.pattern},
            "description": "NAME or NAME=VALUE, passed to the simulator compile"}


def core_profile_schema() -> dict[str, Any]:
    trace = {"enum": sorted(TRACE_FORMATS)}
    edit = {
        "type": "object", "additionalProperties": False, "required": ["file", "find", "replace"],
        "properties": {
            "file": {**_STR, "description": "One of the profile's sources"},
            "find": {**_STR, "description": "Text that must match exactly once"},
            "replace": {"type": "string", "description": "Replacement; must differ from find"},
        },
    }
    mutant = {
        "type": "object", "additionalProperties": False, "required": ["target_subsystem"],
        "anyOf": [{"required": ["edits"], "properties": {"edits": {"minItems": 1}}},
                  {"required": ["defines"], "properties": {"defines": {"minItems": 1}}}],
        "properties": {
            "description": {"type": "string"},
            "target_subsystem": _STR,
            "target_interface": {"type": ["string", "null"]},
            "model_fault": {"type": ["string", "null"], "description": "Reference-model fault matching this mutant"},
            "edits": {"type": "array", "items": edit},
            "defines": _DEFINES,
        },
    }
    signals = {
        "type": "object", "additionalProperties": False, "required": ["clock", "valid", "fields"],
        "description": "Waveform signals for trace: vcd-signals, sampled before each rising clock edge where valid is 1",
        "properties": {
            "clock": _SIGNAL, "valid": _SIGNAL,
            "fields": {
                "type": "object", "minProperties": 1, "required": ["pc", "insn"], "additionalProperties": False,
                "properties": {name: _SIGNAL for name in RETIRE_FIELDS},
            },
        },
    }
    return {
        "$schema": DIALECT,
        "$id": f"{BASE_URL}/rv32i-core-profile.schema.json",
        "title": "CrossLayer RV32I core profile",
        "type": "object",
        "additionalProperties": False,
        "required": list(profile._REQUIRED),
        "allOf": [{"if": {"properties": {"trace": {"anyOf": [{"const": "vcd-signals"},
                                                           {"type": "array", "contains": {"const": "vcd-signals"}}]}},
                          "required": ["trace"]},
                   "then": {"required": ["signals"]}}],
        "properties": {
            "schema_version": {"const": profile.SCHEMA_VERSION},
            "name": {"type": "string", "pattern": profile._NAME_RE.pattern},
            "description": {"type": "string"},
            "isa": {"enum": sorted(profile.ISAS)},
            "trace": {"oneOf": [trace, {"type": "array", "items": trace, "minItems": 1, "uniqueItems": True}],
                      "description": "Trace formats the testbench produces; the first is the default"},
            "testbench": {**_STR, "description": "Relative path, separate from the sources"},
            "sources": {"type": "array", "items": _STR, "minItems": 1, "uniqueItems": True},
            "includes": {"type": "array", "items": _STR, "uniqueItems": True,
                         "description": "Files the sources include: copied next to them, not compiled alone"},
            "top": {"type": "string", "pattern": profile._MODULE_RE.pattern,
                    "description": "Root module to elaborate (iverilog -s)"},
            "memory_images": {
                "type": "object",
                "propertyNames": {"pattern": profile._IMAGE_NAME_RE.pattern},
                "additionalProperties": {"type": "integer", "minimum": 1, "maximum": profile.MAX_IMAGE_WORDS},
                "description": "File name -> depth in words; the program image is written under each name, zero-padded",
            },
            "defines": _DEFINES,
            "clock_period_ns": {"type": "integer", "minimum": 1},
            "pipeline": {
                "type": "object", "additionalProperties": False, "required": ["stages", "bypass_window"],
                "properties": {"stages": {"type": "integer", "minimum": 2},
                               "bypass_window": {"type": "integer", "minimum": 0}},
                "description": "Pipelined cores: stage count and how many previous results reach execute by forwarding",
            },
            "provenance": {"type": "object", "description": "Upstream URL, commit, license, modifications"},
            "signals": signals,
            "mutants": {"type": ["object", "null"],
                        "propertyNames": {"pattern": profile._NAME_RE.pattern},
                        "additionalProperties": mutant},
        },
    }
