"""Package status board: where every case stands and whom it is waiting on.

Built only from the replayed cases and the validated package, so it is deterministic and
never claims more than the audit log shows. The close coordinator agent posts the
Markdown form as a room document in the review room; the JSON form is for anything else.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from closepilot.models import (
    CONTROL_ANALYST,
    EXCEPTION_INVESTIGATOR,
    HUMAN_ROLE,
    Case,
    CaseState,
    RecordResult,
)
from closepilot.report import inline_code
from closepilot.workflow import case_id_for

#: Who must act next for a case in each state, and what they must do.
WAITING_ON: dict[CaseState, tuple[str | None, str]] = {
    CaseState.OPEN: (CONTROL_ANALYST.name, "hand off to the investigator"),
    CaseState.INVESTIGATING: (EXCEPTION_INVESTIGATOR.name, "investigate and recommend"),
    CaseState.AWAITING_EVIDENCE: (HUMAN_ROLE, "add the requested commentary or evidence"),
    CaseState.READY_FOR_HUMAN_REVIEW: (HUMAN_ROLE, "decide"),
    CaseState.APPROVED: (HUMAN_ROLE, "close"),
    CaseState.REJECTED: (HUMAN_ROLE, "close"),
    CaseState.CLOSED: (None, "nothing: closed"),
}

_UNKNOWN_ENTITY = "(no entity)"


def package_status(
    submission_id: str,
    results: Sequence[RecordResult],
    cases: Mapping[str, Case],
    label: str | None = None,
) -> dict[str, Any]:
    """Status of one submission: per entity, the records and every case's next step.

    ``submission_id`` is the package's own submission ID, which its cases carry;
    ``label`` names the board, for example an entity submission such as FC-2026-10-DE."""
    entity_of: dict[str, str] = {}
    records_by_entity: Counter[str] = Counter()
    for result in results:
        entity = result.record.raw.get("entity", "") or _UNKNOWN_ENTITY
        records_by_entity[entity] += 1
        entity_of.setdefault(case_id_for(result), entity)

    # Only cases opened from these records: an entity's board never shows another entity's
    # cases, even though they share the package's submission_id.
    own = sorted(
        (c for c in cases.values() if c.submission_id == submission_id and c.case_id in entity_of),
        key=lambda c: c.case_id,
    )
    rows = []
    for case in own:
        who, what = WAITING_ON[case.state]
        rows.append(
            {
                "case_id": case.case_id,
                "entity": entity_of.get(case.case_id, _UNKNOWN_ENTITY),
                "state": case.state.value,
                "controls": list(case.control_ids),
                "recommendation": None
                if case.recommendation is None
                else case.recommendation.value,
                "decision": None if case.decision is None else case.decision.value,
                "waiting_on": who,
                "next_step": what,
            }
        )

    entities = []
    for entity in sorted(records_by_entity):
        mine = [r for r in rows if r["entity"] == entity]
        entities.append(
            {
                "entity": entity,
                "records": records_by_entity[entity],
                "cases": len(mine),
                "cases_by_state": dict(Counter(r["state"] for r in mine)),
            }
        )
    waiting = Counter(r["waiting_on"] for r in rows if r["waiting_on"])
    return {
        "submission_id": label or submission_id,
        "records": len(results),
        "cases": len(rows),
        "waiting_on": dict(sorted(waiting.items())),
        "entities": entities,
        "case_list": rows,
    }


def _cell(text: object) -> str:
    """An untrusted value in a table cell: an inert code span whose pipes cannot split the
    cell (a pipe inside a code span still ends a GFM table cell unless escaped)."""
    return inline_code(text).replace("|", "\\|")


def status_markdown(status: Mapping[str, Any]) -> str:
    """The status board as Markdown. Untrusted values are rendered as inert code spans."""
    out = [
        f"# Close status: {inline_code(status['submission_id'])}",
        "",
        f"{status['records']} records, {status['cases']} cases.",
        "",
        "## Waiting on",
        "",
    ]
    if status["waiting_on"]:
        out += [f"- {inline_code(who)}: {n}" for who, n in status["waiting_on"].items()]
    else:
        out.append("- Nobody: every case is closed.")
    out += ["", "## By entity", "", "| Entity | Records | Cases | States |", "|---|---|---|---|"]
    for e in status["entities"]:
        states = ", ".join(f"{s} {n}" for s, n in sorted(e["cases_by_state"].items())) or "-"
        out.append(f"| {_cell(e['entity'])} | {e['records']} | {e['cases']} | {states} |")
    out += [
        "",
        "## Cases",
        "",
        "| Case | Entity | State | Recommendation | Waiting on | Next step |",
        "|---|---|---|---|---|---|",
    ]
    for r in status["case_list"]:
        out.append(
            f"| {_cell(r['case_id'])} | {_cell(r['entity'])} | {r['state']} "
            f"| {r['recommendation'] or '-'} | {r['waiting_on'] or '-'} | {r['next_step']} |"
        )
    out += [
        "",
        "*Built by ClosePilot from the audit log. Only the Finance Reviewer approves or rejects.*",
    ]
    return "\n".join(out) + "\n"
