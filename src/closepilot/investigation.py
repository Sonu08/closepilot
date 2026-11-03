"""Deterministic investigation of one case.

The Exception Investigator agent explains and recommends; this module supplies the facts
it explains and a rule-based suggested next step, so the agent never has to infer a fact
or a number. No I/O, no AI: the same case and package always give the same result.

Classification
--------------
* ``DATA_QUALITY``: the case has a C03 (mapping), C04 (duplicate), C06 (currency or
  period) or C07 (required field) finding. The record cannot be relied on as submitted,
  so the suggested recommendation is REJECT, meaning "correct and resubmit".
* ``VARIANCE``: only variance-related findings (C01 material, C02 moderate, C05 missing
  commentary). This is treated as a possibly genuine business variance that needs
  explanation and, where configured, evidence before it can be approved.

Suggested next step (for a case in INVESTIGATING)
-------------------------------------------------
1. DATA_QUALITY                                      -> RECOMMEND_REJECT
2. VARIANCE, evidence needed and none attached       -> REQUEST_EVIDENCE (with ``missing``)
3. VARIANCE, commentary required and none provided   -> REQUEST_COMMENTARY
4. VARIANCE, explanation and evidence satisfied      -> RECOMMEND_APPROVE

"Commentary provided" means the source record had commentary, or the Finance Reviewer
added commentary to the case. "Evidence attached" means a human attached an evidence
reference; ClosePilot records references and never inspects the documents behind them.

Other states map to waiting steps: OPEN -> HANDOFF, AWAITING_EVIDENCE -> WAIT_FOR_REVIEWER,
READY_FOR_HUMAN_REVIEW -> WAIT_FOR_DECISION, and decided states -> NONE.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from closepilot.models import Case, CaseState, RecordResult

DATA_QUALITY_CONTROLS: frozenset[str] = frozenset({"C03", "C04", "C06", "C07"})
MAX_COMMENTARY_ECHO = 200
_TWO_DP = Decimal("0.01")


class Classification(Enum):
    DATA_QUALITY = "DATA_QUALITY"
    VARIANCE = "VARIANCE"


class NextStep(Enum):
    HANDOFF = "HANDOFF"
    RECOMMEND_REJECT = "RECOMMEND_REJECT"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    REQUEST_COMMENTARY = "REQUEST_COMMENTARY"
    RECOMMEND_APPROVE = "RECOMMEND_APPROVE"
    WAIT_FOR_REVIEWER = "WAIT_FOR_REVIEWER"
    WAIT_FOR_DECISION = "WAIT_FOR_DECISION"
    NONE = "NONE"


class InvestigationError(ValueError):
    """The case's records cannot be found in the package it refers to."""


@dataclass(frozen=True)
class Concentration:
    """Share of the variance in the record's entity / cost centre / currency group."""

    entity: str
    cost_centre: str
    currency: str
    records: int
    group_variance: Decimal
    record_variance: Decimal
    share_pct: Decimal | None  # None when the group's variance nets to zero


@dataclass(frozen=True)
class Investigation:
    case_id: str
    state: CaseState
    classification: Classification
    next_step: NextStep
    missing: tuple[str, ...]
    reasons: tuple[str, ...]
    records: tuple[RecordResult, ...]
    mapping_valid: bool
    duplicate_rows: int
    currency_and_period_valid: bool
    integrity_errors: tuple[str, ...]
    source_commentary: str
    commentary_required: bool
    commentary_provided: bool
    evidence_required: bool
    evidence_attached: tuple[str, ...]
    concentration: Concentration | None


def _case_records(case: Case, results: Sequence[RecordResult]) -> list[RecordResult]:
    if case.case_id.startswith("LINE-") and case.case_id[5:].isdigit():
        line = int(case.case_id[5:])
        return [r for r in results if r.record.line_number == line]
    return [r for r in results if r.record.record_id == case.case_id]


def _concentration(primary: RecordResult, results: Sequence[RecordResult]) -> Concentration | None:
    if primary.variance is None:
        return None
    raw = primary.record.raw
    key = (raw.get("entity", ""), raw.get("cost_centre", ""), raw.get("currency", ""))
    if not all(key):
        return None
    group = [
        r
        for r in results
        if r.variance is not None
        and (
            r.record.raw.get("entity"),
            r.record.raw.get("cost_centre"),
            r.record.raw.get("currency"),
        )
        == key
    ]
    total = sum((r.variance.amount for r in group if r.variance is not None), Decimal(0))
    share = None
    if total != 0:
        share = (primary.variance.amount / total * 100).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    return Concentration(
        entity=key[0],
        cost_centre=key[1],
        currency=key[2],
        records=len(group),
        group_variance=total,
        record_variance=primary.variance.amount,
        share_pct=share,
    )


def investigate(case: Case, results: Sequence[RecordResult]) -> Investigation:
    records = _case_records(case, results)
    if not records:
        raise InvestigationError(
            f"no record for case {case.case_id!r} in submission {case.submission_id!r}"
        )
    findings = [f for r in records for f in r.findings]
    controls = {f.control_id for f in findings}
    primary = records[0]

    classification = (
        Classification.DATA_QUALITY if controls & DATA_QUALITY_CONTROLS else Classification.VARIANCE
    )
    source_commentary = primary.record.raw.get("commentary", "")
    commentary_required = "C05" in controls
    commentary_provided = bool(source_commentary) or bool(case.commentary)
    evidence_needed = case.evidence_required or case.evidence_requested
    evidence_ok = not evidence_needed or bool(case.evidence_references)

    reasons: list[str] = []
    if "C03" in controls:
        reasons.append("master-data mapping is invalid (C03)")
    if "C04" in controls:
        reasons.append(f"record_id occurs {len(records)} times in the package (C04)")
    if "C06" in controls:
        reasons.append("currency or reporting period is not allowed (C06)")
    if "C07" in controls:
        reasons.append("a required field is missing or unusable (C07)")
    if classification is Classification.VARIANCE:
        if "C01" in controls:
            reasons.append("variance exceeds the review threshold or is undefined (C01)")
        if "C02" in controls:
            reasons.append("variance is within the warning band (C02)")
        reasons.append("no data-quality finding: treated as a possible genuine business variance")
        if commentary_required:
            reasons.append(
                "commentary was required but blank in the source record"
                + ("; the Finance Reviewer has added commentary" if case.commentary else "")
            )
        if evidence_needed:
            reasons.append(
                f"evidence is required ({len(case.evidence_references)} reference(s) attached)"
            )

    missing: list[str] = []
    if case.state is CaseState.OPEN:
        step = NextStep.HANDOFF
    elif case.state is CaseState.AWAITING_EVIDENCE:
        step = NextStep.WAIT_FOR_REVIEWER
    elif case.state is CaseState.READY_FOR_HUMAN_REVIEW:
        step = NextStep.WAIT_FOR_DECISION
    elif case.state is not CaseState.INVESTIGATING:
        step = NextStep.NONE
    elif classification is Classification.DATA_QUALITY:
        step = NextStep.RECOMMEND_REJECT
    elif not evidence_ok:
        step = NextStep.REQUEST_EVIDENCE
        if commentary_required and not commentary_provided:
            missing.append("commentary")
        missing.append("evidence")
    elif commentary_required and not commentary_provided:
        step = NextStep.REQUEST_COMMENTARY
        missing.append("commentary")
    else:
        step = NextStep.RECOMMEND_APPROVE

    return Investigation(
        case_id=case.case_id,
        state=case.state,
        classification=classification,
        next_step=step,
        missing=tuple(missing),
        reasons=tuple(reasons),
        records=tuple(records),
        mapping_valid="C03" not in controls,
        duplicate_rows=len(records),
        currency_and_period_valid="C06" not in controls,
        integrity_errors=tuple(f.message for f in findings if f.control_id == "C07"),
        source_commentary=source_commentary[:MAX_COMMENTARY_ECHO],
        commentary_required=commentary_required,
        commentary_provided=commentary_provided,
        evidence_required=evidence_needed,
        evidence_attached=case.evidence_references,
        concentration=_concentration(primary, results),
    )


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def investigation_to_dict(inv: Investigation) -> dict[str, Any]:
    c = inv.concentration
    return {
        "case_id": inv.case_id,
        "state": inv.state.value,
        "classification": inv.classification.value,
        "next_step": inv.next_step.value,
        "missing": list(inv.missing),
        "reasons": list(inv.reasons),
        "facts": {
            "mapping_valid": inv.mapping_valid,
            "duplicate_rows": inv.duplicate_rows,
            "currency_and_period_valid": inv.currency_and_period_valid,
            "integrity_errors": list(inv.integrity_errors),
            "source_commentary": inv.source_commentary,
            "commentary_required": inv.commentary_required,
            "commentary_provided": inv.commentary_provided,
            "evidence_required": inv.evidence_required,
            "evidence_attached": list(inv.evidence_attached),
            "evidence_note": (
                "ClosePilot records evidence references; it has not inspected the "
                "documents behind them."
            ),
        },
        "records": [
            {
                "record_id": r.record.record_id,
                "line": r.record.line_number,
                "entity": r.record.raw.get("entity", ""),
                "cost_centre": r.record.raw.get("cost_centre", ""),
                "account": r.record.raw.get("account", ""),
                "currency": r.record.raw.get("currency", ""),
                "actual": None if r.record.actual is None else _money(r.record.actual),
                "forecast": None if r.record.forecast is None else _money(r.record.forecast),
                "variance_amount": None if r.variance is None else _money(r.variance.amount),
                "variance_pct": (
                    None if r.variance is None or r.variance.pct is None else str(r.variance.pct)
                ),
                "status": r.status.value,
                "findings": [
                    {"control_id": f.control_id, "status": f.status.value, "message": f.message}
                    for f in r.findings
                ],
            }
            for r in inv.records
        ],
        "cost_centre_concentration": None
        if c is None
        else {
            "entity": c.entity,
            "cost_centre": c.cost_centre,
            "currency": c.currency,
            "records_in_group": c.records,
            "group_variance": _money(c.group_variance),
            "record_variance": _money(c.record_variance),
            "record_share_pct": None if c.share_pct is None else str(c.share_pct),
        },
    }
