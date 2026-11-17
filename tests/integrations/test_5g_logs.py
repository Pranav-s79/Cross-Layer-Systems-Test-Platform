"""Phase 4.2: upstream-derived Open5GS and OCUDU log profiles and fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crosslayer.core.adapters import RunContext
from crosslayer.integrations.textlog import TextLogAdapter
from crosslayer.integrations.textlog.profile import ParsedLine, load_profile

EXPERIMENT = Path(__file__).resolve().parents[2] / "experiments" / "5g-logs"
SOURCES = [
    {"input": "logs/amf.log", "profile": "open5gs_amf", "year": 2026, "utc_offset": "+00:00"},
    {"input": "logs/smf.log", "profile": "open5gs_smf", "year": 2026, "utc_offset": "+00:00"},
    {"input": "logs/gnb.log", "profile": "ocudu_gnb", "utc_offset": "+00:00"},
]
JOIN = {"name": "ngap_ue", "keys": ["ran_ue_ngap_id", "amf_ue_ngap_id"],
        "fallback_window_ms": 250}


def _parsed(profile_name: str, log_name: str) -> list[ParsedLine]:
    profile = load_profile(profile_name)
    parsed: list[ParsedLine] = []
    for line in (EXPERIMENT / "logs" / log_name).read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        match = profile.match(line)
        assert isinstance(match, ParsedLine), line
        parsed.append(match)
    return parsed


def _normalize(tmp_path: Path, sources: list[dict[str, Any]] = SOURCES, joins: list[dict[str, Any]] | None = None):
    adapter = TextLogAdapter()
    parameters = {"sources": sources, "joins": joins if joins is not None else [JOIN]}
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    context = RunContext(
        run_id="5g", seed=0, parameters=parameters, faults=(), raw_dir=raw, work_dir=tmp_path,
        base_dir=EXPERIMENT, timeout_s=10,
        inputs=tuple((EXPERIMENT / source["input"]).resolve() for source in sources),
    )
    adapter.validate_parameters(parameters)
    assert adapter.faults_for(parameters, EXPERIMENT) == []
    return adapter.normalize(context, adapter.collect(context, None))


def test_open5gs_amf_profile_parses_registration_and_ngap_ids() -> None:
    events = _parsed("open5gs_amf", "amf.log")
    assert [event.rule.event_type for event in events] == [
        "ngap.initial_ue_message",
        "ngap.ue_ids_assigned",
        "registration.requested",
        "registration.complete",
    ]
    assert events[1].correlation == {"ran_ue_ngap_id": "11", "amf_ue_ngap_id": "42"}
    assert events[1].attributes == {"ran.tac": 7, "ran.cell_id": 0x19B0}
    assert events[3].correlation == {"supi": "imsi-001010000000001"}


def test_open5gs_smf_profile_parses_allocated_session() -> None:
    [event] = _parsed("open5gs_smf", "smf.log")
    assert event.rule.event_type == "pdu_session.allocated"
    assert event.correlation == {"supi": "imsi-001010000000001"}
    assert event.attributes == {"core.dnn": "internet", "core.ipv4": "10.45.0.2", "core.ipv6": ""}


def test_ocudu_profile_parses_ngap_and_rrc_procedures() -> None:
    events = _parsed("ocudu_gnb", "gnb.log")
    assert [event.rule.event_type for event in events] == [
        "rrc.setup_started",
        "rrc.connected",
        "ngap.initial_ue_message",
        "ngap.downlink_nas_transport",
        "pdu_session.setup_requested",
        "pdu_session.setup_started",
        "rrc.reconfiguration_started",
        "rrc.connected",
        "pdu_session.setup_complete",
    ]
    assert events[3].correlation == {
        "ue": "7",
        "ran_ue_ngap_id": "11",
        "amf_ue_ngap_id": "42",
    }
    assert events[0].correlation == {"ue": "7", "rnti": str(0x4601)}


def test_ngap_pair_join_records_exact_and_time_window_confidence(tmp_path: Path) -> None:
    result = _normalize(tmp_path)
    assert result.diagnostics == []
    by_id = {event.event_id: event for event in result.events}
    expected = '["11","42"]'

    exact = by_id["amf.log:2"]
    assert exact.correlation_ids["ngap_ue"] == expected
    assert exact.confidence_metadata["correlation_joins"]["ngap_ue"] == {
        "method": "id_pair",
        "confidence": "high",
        "ids": {"ran_ue_ngap_id": "11", "amf_ue_ngap_id": "42"},
    }

    fallback = by_id["smf.log:1"]
    assert fallback.correlation_ids["ngap_ue"] == expected
    metadata = fallback.confidence_metadata["correlation_joins"]["ngap_ue"]
    assert metadata["method"] == "time_window" and metadata["confidence"] == "low"
    assert metadata["window_ms"] == 250
    assert metadata["anchor_event_id"] == "gnb.log:5"
    assert metadata["delta_ms"] == 5

    [stats] = result.stats["joins"]
    assert stats == {
        "name": "ngap_ue",
        "keys": ["ran_ue_ngap_id", "amf_ue_ngap_id"],
        "fallback_window_ms": 250,
        "exact": 5,
        "fallback": 9,
        "ambiguous": 0,
        "unmatched": 0,
    }


def test_time_fallback_refuses_an_ambiguous_pair(tmp_path: Path) -> None:
    source = {"input": "amf.log", "profile": "open5gs_amf", "year": 2026, "utc_offset": "+00:00"}
    log = tmp_path / "fixture" / "amf.log"
    log.parent.mkdir()
    log.write_text("\n".join([
        "11/17 09:00:00.100: [amf] INFO:     RAN_UE_NGAP_ID[1] AMF_UE_NGAP_ID[10] TAC[7] CellID[0x1]",
        "11/17 09:00:00.150: [gmm] INFO: Registration request",
        "11/17 09:00:00.200: [amf] INFO:     RAN_UE_NGAP_ID[2] AMF_UE_NGAP_ID[20] TAC[7] CellID[0x2]",
    ]) + "\n", encoding="utf-8")
    sources = [{**source, "input": str(log)}]
    result = _normalize(tmp_path / "run", sources=sources, joins=[{**JOIN, "fallback_window_ms": 100}])
    request = next(event for event in result.events if event.event_type == "registration.requested")
    assert "ngap_ue" not in request.correlation_ids
    assert result.stats["joins"][0]["ambiguous"] == 1


def test_5g_catalog_declares_the_derived_join() -> None:
    catalog = TextLogAdapter().event_catalog()
    relevant = [spec for spec in catalog if spec.event_type in {"registration.requested", "pdu_session.allocated"}]
    assert relevant and all("ngap_ue" in spec.correlation_ids for spec in relevant)

