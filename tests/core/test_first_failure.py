"""First failing event view: attribute pairing and bundle rendering."""

import json

from crosslayer.core.artifacts.first_failure import first_failure, pair_attributes, render_first_failure


def test_pairs_prefix_and_suffix_references_mismatches_first():
    pairs = pair_attributes({
        "rv.pc": 4, "rv.ref_pc": 4, "rv.rd_val": 41, "rv.ref_rd_val": 42,
        "x.gain": 1, "x.gain_ref": 2, "x.lonely": 3, "rv.ref_orphan": 9,
    })
    assert [(p["field"], p["match"]) for p in pairs] == [
        ("rv.rd_val", False), ("x.gain", False), ("rv.pc", True)]
    assert pairs[0]["observed"] == 41 and pairs[0]["reference"] == 42


def _bundle(tmp_path, violations):
    (tmp_path / "raw_artifacts").mkdir()
    (tmp_path / "raw_artifacts" / "trace.log").write_text("line one\nretire pc=8\n", encoding="utf-8")
    events = [
        {"event_id": "trace.log:2", "attributes": {"rv.rd_val": 41, "rv.ref_rd_val": 42}},
        {"event_id": "trace.log:1", "attributes": {}},
    ]
    (tmp_path / "normalized_events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    findings = [{"kind": "violation", "message": m, "timestamp_ns": t, "evidence": ev} for m, t, ev in violations]
    doc = {"contracts": [{"result": {"contract_id": "c.one", "verdict": "fail", "findings": findings}}]}
    (tmp_path / "contracts.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


def test_first_failure_picks_earliest_violation_with_raw_line(tmp_path):
    ev = lambda i: [{"event_id": i, "event_type": "cpu.retire", "subsystem": "core",
                     "timestamp_ns": 1, "raw_artifact_ref": "raw_artifacts/trace.log#L2"}]
    bundle = _bundle(tmp_path, [("late", 50, ev("trace.log:1")), ("early", 10, ev("trace.log:2"))])
    failure = first_failure(bundle)
    assert failure["message"] == "early" and failure["raw_line"] == "retire pc=8"
    assert failure["pairs"][0]["field"] == "rv.rd_val"
    text = render_first_failure(failure)
    assert "retire pc=8" in text and "rv.rd_val" in text and "<-- differs" in text


def test_first_failure_is_none_without_evidence(tmp_path):
    bundle = _bundle(tmp_path, [("no evidence", 5, [])])
    assert first_failure(bundle) is None


def test_missing_event_or_raw_file_still_reports(tmp_path):
    ev = [{"event_id": "gone:1", "event_type": "t", "subsystem": "s", "timestamp_ns": 0,
           "raw_artifact_ref": "raw_artifacts/missing.log#L3"}]
    failure = first_failure(_bundle(tmp_path, [("m", 1, ev)]))
    assert failure["pairs"] == [] and failure["raw_line"] is None
