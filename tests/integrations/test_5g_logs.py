"""Phase 4.2: upstream-derived Open5GS and OCUDU log profiles and fixtures."""

from __future__ import annotations

from pathlib import Path

from crosslayer.integrations.textlog.profile import ParsedLine, load_profile

EXPERIMENT = Path(__file__).resolve().parents[2] / "experiments" / "5g-logs"


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

