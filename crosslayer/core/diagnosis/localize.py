"""Rank where a fault most likely started, from contract results and the system graph.

A deterministic, explainable baseline. Each failing contract is evidence
against the subsystems on its boundary and, more weakly, against what feeds
its source. Each passing contract is counter-evidence for the subsystems it
touches. Nothing here reads fault labels; scoring against them is separate
(:mod:`crosslayer.core.diagnosis.scoring`).

Weights (documented in ``docs/fault-localization.md``):

* a failing contract weighs ``severity x 0.5**r``, where ``r`` counts the
  distinct first-violation times earlier than its own (later failures are
  often consequences of earlier ones);
* its source subsystem receives the full weight, its destination
  ``DESTINATION``, and a subsystem ``k`` edges upstream of the source
  ``UPSTREAM**k / 2``: components that log nothing are reached this way;
* each passing contract multiplies the score of its source by
  ``PASS_AT_SOURCE`` and of its destination by ``PASS_AT_DESTINATION``,
  down to ``FLOOR``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from crosslayer.core.adapters.base import AdapterDescription
from crosslayer.core.contracts.model import ContractResult
from crosslayer.core.verdict import Verdict

METHOD = "boundary-evidence-v1"
SEVERITY = {"critical": 1.0, "major": 0.7, "minor": 0.4}
DESTINATION = 0.6
UPSTREAM = 0.5
PASS_AT_SOURCE = 0.85
PASS_AT_DESTINATION = 0.9
FLOOR = 0.25
CONFIDENT_MARGIN = 0.25


@dataclass(frozen=True)
class Edge:
    source: str
    destination: str
    name: str
    """An interface name, or ``contract:<id>`` for a boundary with no interface."""
    interface: bool


@dataclass
class Hypothesis:
    subsystem: str
    score: float = 0.0
    probability: float = 0.0
    interface: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = field(default_factory=list)
    interface_weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subsystem": self.subsystem,
            "interface": self.interface,
            "probability": round(self.probability, 4),
            "score": round(self.score, 6),
            "evidence": self.evidence,
            "counter_evidence": self.counter_evidence,
        }


@dataclass
class Localization:
    method: str
    status: str
    """``no_failures``, ``confident``, or ``ambiguous``."""
    hypotheses: list[Hypothesis]
    margin: float | None
    unresolved: list[str]
    """Contracts that were INCONCLUSIVE: evidence that could not be weighed."""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "margin": None if self.margin is None else round(self.margin, 4),
            "reason": self.reason,
            "unresolved_contracts": self.unresolved,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
        }


class SystemGraph:
    """Subsystems as nodes; interfaces and contract boundaries as directed edges."""

    def __init__(self, description: AdapterDescription, results: Sequence[ContractResult]) -> None:
        self.nodes = list(description.subsystems)
        self.edges: list[Edge] = [Edge(i.source, i.destination, i.name, True) for i in description.interfaces]
        pairs = {(e.source, e.destination) for e in self.edges}
        for r in results:
            c = r.contract
            if c.source != c.destination and (c.source, c.destination) not in pairs:
                self.edges.append(Edge(c.source, c.destination, f"contract:{c.id}", False))
                pairs.add((c.source, c.destination))
        self._into: dict[str, list[Edge]] = {}
        for e in self.edges:
            self._into.setdefault(e.destination, []).append(e)

    def upstream(self, node: str) -> list[tuple[str, int, Edge]]:
        """Subsystems that feed ``node``: (subsystem, distance, first edge on the way to ``node``)."""
        seen = {node}
        out: list[tuple[str, int, Edge]] = []
        queue: deque[tuple[str, int]] = deque([(node, 0)])
        while queue:
            current, dist = queue.popleft()
            for edge in sorted(self._into.get(current, []), key=lambda e: (e.source, e.name)):
                if edge.source in seen:
                    continue
                seen.add(edge.source)
                out.append((edge.source, dist + 1, edge))
                queue.append((edge.source, dist + 1))
        return out

    def interfaces_between(self, source: str, destination: str) -> list[str]:
        """Interfaces carrying data from ``source`` to ``destination``; if there are
        none, those in the opposite direction (a contract may observe a reply)."""
        forward = sorted(e.name for e in self.edges if e.interface and (e.source, e.destination) == (source, destination))
        return forward or sorted(
            e.name for e in self.edges if e.interface and (e.source, e.destination) == (destination, source))


def localize(description: AdapterDescription, results: Sequence[ContractResult]) -> Localization:
    graph = SystemGraph(description, results)
    failing = [r for r in results if r.verdict is Verdict.FAIL]
    passing = [r for r in results if r.verdict is Verdict.PASS]
    unresolved = sorted(r.contract.id for r in results if r.verdict is Verdict.INCONCLUSIVE)
    if not failing:
        return Localization(METHOD, "no_failures", [], None, unresolved, "no contract failed")

    times = sorted({r.first_violation_ns for r in failing if r.first_violation_ns is not None})
    hyps: dict[str, Hypothesis] = {}

    def hyp(name: str) -> Hypothesis:
        return hyps.setdefault(name, Hypothesis(name))

    for r in failing:
        c = r.contract
        rank = times.index(r.first_violation_ns) if r.first_violation_ns is not None else len(times)
        weight = SEVERITY.get(str(c.severity), 0.7) * 0.5 ** rank
        base = {"contract": c.id, "boundary": f"{c.source} -> {c.destination}",
                "first_violation_ns": r.first_violation_ns, "weight": round(weight, 4)}
        roles = [(c.source, 1.0, "source")]
        if c.destination != c.source:
            roles.append((c.destination, DESTINATION, "destination"))
        for name, affinity, role in roles:
            h = hyp(name)
            h.score += weight * affinity
            h.evidence.append({**base, "role": role})
            for iface in graph.interfaces_between(c.source, c.destination):
                h.interface_weights[iface] = h.interface_weights.get(iface, 0.0) + weight * affinity
        for name, dist, edge in graph.upstream(c.source):
            if name == c.destination:
                continue
            affinity = UPSTREAM ** dist / 2
            h = hyp(name)
            h.score += weight * affinity
            h.evidence.append({**base, "role": f"upstream ({dist} hop{'s' if dist > 1 else ''})"})
            if edge.interface:
                h.interface_weights[edge.name] = h.interface_weights.get(edge.name, 0.0) + weight * affinity

    for h in hyps.values():
        factor = 1.0
        for r in passing:
            c = r.contract
            if h.subsystem == c.source:
                factor *= PASS_AT_SOURCE
                role = "source"
            elif h.subsystem == c.destination:
                factor *= PASS_AT_DESTINATION
                role = "destination"
            else:
                continue
            h.counter_evidence.append({"contract": c.id, "boundary": f"{c.source} -> {c.destination}",
                                       "verdict": "pass", "role": role})
            for iface in graph.interfaces_between(c.source, c.destination):
                if iface in h.interface_weights:
                    h.interface_weights[iface] *= 0.5
        h.score *= max(factor, FLOOR)
        if h.interface_weights:
            h.interface = max(sorted(h.interface_weights), key=lambda i: h.interface_weights[i])

    ranked = sorted(hyps.values(), key=lambda h: (-h.score, h.subsystem))
    total = sum(h.score for h in ranked) or 1.0
    for h in ranked:
        h.probability = h.score / total
    margin = ranked[0].probability - (ranked[1].probability if len(ranked) > 1 else 0.0)
    status, reason = _status(ranked, margin)
    return Localization(METHOD, status, ranked, margin, unresolved, reason)


def _direct(h: Hypothesis) -> set[str]:
    """Failing contracts with this subsystem on their boundary (not just upstream)."""
    return {e["contract"] for e in h.evidence if not e["role"].startswith("upstream")}


def _status(ranked: Sequence[Hypothesis], margin: float) -> tuple[str, str]:
    """Confident only when the evidence, not just the priors, separates the top two."""
    if len(ranked) > 1:
        first, second = ranked[0], ranked[1]
        if _direct(first) == _direct(second):
            return ("ambiguous", f"the same failing contracts implicate `{first.subsystem}` and "
                    f"`{second.subsystem}` directly; only priors and counter-evidence order them")
    if margin < CONFIDENT_MARGIN:
        return "ambiguous", f"top-two margin {margin:.2f} is below {CONFIDENT_MARGIN}"
    return "confident", f"top-two margin {margin:.2f}"

