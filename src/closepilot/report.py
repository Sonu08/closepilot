"""Decision records and package summaries, built only from audit events.

Everything here is derived from the append-only audit log (plus, optionally, the
deterministic investigation, which is recomputed and labelled as such), so a record can
always be traced back to the events that produced it. Nothing here writes to the log.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from closepilot.models import Action, AuditEvent, Case, CaseState, Decision
from closepilot.workflow import replay

EVIDENCE_STATEMENT = (
    "ClosePilot records the evidence references a human attached. It has not inspected "
    "the documents behind them."
)


def _actor(event: AuditEvent) -> str:
    return f"{event.actor_name} ({event.actor_type.value})"


def decision_record(
    case_id: str,
    events: Sequence[AuditEvent],
    *,
    log_location: str,
    log_anchor: str | None = None,
    investigation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the decision record for one case from the full audit log."""
    case = replay(events).get(case_id)
    if case is None:
        raise KeyError(f"case {case_id!r} does not exist")
    own = [e for e in events if e.case_id == case_id]
    by_action: dict[Action, list[AuditEvent]] = {a: [] for a in Action}
    for event in own:
        by_action[event.action].append(event)

    opened = by_action[Action.OPEN_CASE][0]
    decide = by_action[Action.DECIDE]
    close = by_action[Action.CLOSE]
    decision = decide[-1] if decide else None
    return {
        "case_id": case_id,
        "submission_id": case.submission_id,
        "final_state": case.state.value,
        "control_ids": list(case.control_ids),
        "automated_finding": {
            "summary": opened.rationale,
            "opened_by": _actor(opened),
            "at": opened.timestamp_utc,
            "evidence_required_by_configuration": case.evidence_required,
        },
        "handoffs": [
            {"by": _actor(e), "to": e.details.get("to"), "note": e.rationale, "at": e.timestamp_utc}
            for e in by_action[Action.HANDOFF]
        ],
        "evidence_requests": [
            {"by": _actor(e), "request": e.rationale, "at": e.timestamp_utc}
            for e in by_action[Action.REQUEST_EVIDENCE]
        ],
        "commentary": [
            {"by": _actor(e), "text": e.rationale, "at": e.timestamp_utc}
            for e in by_action[Action.ADD_COMMENTARY]
        ],
        "evidence": [
            {
                "reference": e.evidence_references[0],
                "attached_by": _actor(e),
                "note": e.rationale,
                "at": e.timestamp_utc,
            }
            for e in by_action[Action.ATTACH_EVIDENCE]
        ],
        "evidence_statement": EVIDENCE_STATEMENT,
        "recommendations": [
            {
                "by": _actor(e),
                "recommendation": None if e.recommendation is None else e.recommendation.value,
                "rationale": e.rationale,
                "at": e.timestamp_utc,
            }
            for e in by_action[Action.RECOMMEND]
        ],
        "returned_for_rework": [
            {"by": _actor(e), "reason": e.rationale, "at": e.timestamp_utc}
            for e in by_action[Action.RETURN_FOR_REWORK]
        ],
        "human_decision": None
        if decision is None
        else {
            "decision": None if decision.human_decision is None else decision.human_decision.value,
            "decided_by": decision.actor_name,
            "actor_type": decision.actor_type.value,
            "rationale": decision.rationale,
            "recommendation_at_decision": (
                None if decision.recommendation is None else decision.recommendation.value
            ),
            "evidence_at_decision": list(decision.evidence_references),
            "at": decision.timestamp_utc,
            "event_id": decision.event_id,
        },
        "closed": None
        if not close
        else {"by": _actor(close[-1]), "at": close[-1].timestamp_utc, "note": close[-1].rationale},
        "refused_attempts": [
            {
                "actor": _actor(e),
                "attempted_action": e.details.get("attempted_action"),
                "reason": e.rationale,
                "at": e.timestamp_utc,
            }
            for e in by_action[Action.ACCESS_DENIED]
        ],
        "investigation": None
        if investigation is None
        else {
            "note": "Recomputed deterministically from the package when this record was made.",
            **investigation,
        },
        "audit": {
            "log": log_location,
            "case_events": [e.event_id for e in own],
            "log_events_total": len(events),
            "log_last_event_hash": log_anchor,
        },
    }


def _quote(text: object) -> str:
    """Render untrusted text as an inert Markdown block quote."""
    lines = str(text if text is not None else "").splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


def inline_code(text: object) -> str:
    """Render untrusted text inside a line as one inert code span: backticks replaced
    and line breaks collapsed, so it can neither close the span nor start a block."""
    flat = " ".join(str(text if text is not None else "").replace("`", "'").split())
    return f"`{flat}`"


def decision_record_markdown(record: Mapping[str, Any]) -> str:
    out: list[str] = [
        f"# Decision record: {record['case_id']}",
        "",
        f"- **Submission:** {inline_code(record['submission_id'])}",
        f"- **Final state:** {record['final_state']}",
        f"- **Controls:** {', '.join(record['control_ids'])}",
        "",
        "## Automated finding",
        "",
        _quote(record["automated_finding"]["summary"]),
        "",
        f"Opened by {inline_code(record['automated_finding']['opened_by'])} at "
        f"{record['automated_finding']['at']}. Evidence required by configuration: "
        f"{'yes' if record['automated_finding']['evidence_required_by_configuration'] else 'no'}.",
    ]
    inv = record.get("investigation")
    if inv:
        out += ["", "## Investigation (deterministic)", ""]
        out.append(f"- **Classification:** {inv['classification']}")
        out += [f"- {reason}" for reason in inv["reasons"]]
        conc = inv.get("cost_centre_concentration")
        if conc and conc.get("record_share_pct") is not None:
            out.append(
                f"- Share of the {inline_code(conc['entity'])} / "
                f"{inline_code(conc['cost_centre'])} / {inline_code(conc['currency'])} "
                f"variance: {conc['record_share_pct']}% "
                f"({conc['record_variance']} of {conc['group_variance']})"
            )
    for title, key, field in (
        ("Hand-offs", "handoffs", "note"),
        ("Evidence requests", "evidence_requests", "request"),
        ("Commentary", "commentary", "text"),
    ):
        if record[key]:
            out += ["", f"## {title}", ""]
            for item in record[key]:
                out += [f"{inline_code(item['by'])} at {item['at']}:", "", _quote(item[field]), ""]
    out += ["", "## Evidence", ""]
    if record["evidence"]:
        for item in record["evidence"]:
            out.append(
                f"- {inline_code(item['reference'])}, attached by "
                f"{inline_code(item['attached_by'])} at {item['at']}"
            )
    else:
        out.append("- None attached.")
    out += ["", f"*{record['evidence_statement']}*"]
    if record["recommendations"]:
        out += ["", "## Agent recommendations", ""]
        for item in record["recommendations"]:
            out += [
                f"**{item['recommendation']}** by {inline_code(item['by'])} at {item['at']}:",
                "",
                _quote(item["rationale"]),
                "",
            ]
    out += ["", "## Human decision", ""]
    decision = record["human_decision"]
    if decision is None:
        out.append("No decision has been recorded.")
    else:
        out += [
            f"- **Decision:** {decision['decision']}",
            f"- **Decided by:** {inline_code(decision['decided_by'])} ({decision['actor_type']})",
            f"- **At:** {decision['at']} (event {decision['event_id']})",
            f"- **Recommendation at the time:** {decision['recommendation_at_decision']}",
            f"- **Evidence at the time:** {', '.join(decision['evidence_at_decision']) or 'none'}",
            "",
            "Rationale:",
            "",
            _quote(decision["rationale"]),
        ]
    if record["closed"]:
        out += ["", f"Closed by {inline_code(record['closed']['by'])} at {record['closed']['at']}."]
    if record["refused_attempts"]:
        out += ["", "## Refused attempts", ""]
        for item in record["refused_attempts"]:
            out.append(
                f"- {inline_code(item['actor'])} tried {item['attempted_action']} "
                f"at {item['at']}: refused"
            )
    audit = record["audit"]
    out += [
        "",
        "## Audit trail",
        "",
        f"- **Log:** `{audit['log']}` ({audit['log_events_total']} events)",
        f"- **This case's events:** {', '.join(audit['case_events'])}",
    ]
    if audit["log_last_event_hash"]:
        out.append(f"- **Hash of the log's last event:** `{audit['log_last_event_hash']}`")
    return "\n".join(out) + "\n"


def package_summary(
    submission_id: str,
    validation_summary: Mapping[str, Any],
    cases: Mapping[str, Case],
    *,
    log_location: str,
) -> dict[str, Any]:
    """Final package summary (demo scene 10) for one submission."""
    own = [c for c in cases.values() if c.submission_id == submission_id]
    decided = [c for c in own if c.decision is not None]
    states = Counter(c.state.value for c in own)
    outstanding = [c.case_id for c in own if c.decision is None]
    return {
        "submission_id": submission_id,
        "package_status": "ALL_EXCEPTIONS_DECIDED"
        if own and not outstanding
        else ("EXCEPTIONS_OUTSTANDING" if outstanding else "NO_EXCEPTIONS"),
        "records": validation_summary["total_records"],
        "controls": dict(validation_summary["status_counts"]),
        "cases": len(own),
        "cases_by_state": {s.value: states.get(s.value, 0) for s in CaseState},
        "human_approved": sorted(c.case_id for c in decided if c.decision is Decision.APPROVED),
        "human_rejected": sorted(c.case_id for c in decided if c.decision is Decision.REJECTED),
        "outstanding": sorted(outstanding),
        "decision_log": log_location,
    }
