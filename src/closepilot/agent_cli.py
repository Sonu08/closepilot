"""``closepilot agent``: the commands the Switch agents run.

Each command acts as one named agent (``--as``) and prints JSON. Agents name submissions
and cases by ID only; files are found through ``config/submissions.yaml`` under the
ClosePilot home (``--home``). There is deliberately no command that decides, closes,
attaches evidence or adds commentary: those are human actions, and the workflow would
refuse them from an agent anyway.

Exit codes: 0 success, 2 bad input or configuration, 3 refused by the workflow (a
refusal on an existing case is recorded in the audit log as ACCESS_DENIED), 4 the audit
log is locked, unreadable or failed integrity checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from closepilot.audit import AuditError, AuditLog
from closepilot.config import ConfigError, ControlConfig, load_config
from closepilot.ingestion import IngestionError, load_package
from closepilot.investigation import InvestigationError, investigate, investigation_to_dict
from closepilot.models import (
    EXCEPTION_INVESTIGATOR,
    KNOWN_AGENTS,
    Actor,
    ActorType,
    Case,
    Recommendation,
    RecordResult,
)
from closepilot.submissions import SubmissionError, load_registry, resolve
from closepilot.validation import validate_package
from closepilot.workflow import CaseWorkflow, WorkflowError

EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_REFUSED = 3
EXIT_AUDIT_ERROR = 4

CONFIG_PATH = Path("config/controls.yaml")
REGISTRY_PATH = Path("config/submissions.yaml")
AUDIT_PATH = Path("out/audit.jsonl")


def case_to_dict(case: Case) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "submission_id": case.submission_id,
        "state": case.state.value,
        "control_ids": list(case.control_ids),
        "owner": case.owner,
        "evidence_required": case.evidence_required,
        "evidence_requested": case.evidence_requested,
        "evidence_references": list(case.evidence_references),
        "commentary": list(case.commentary),
        "recommendation": None if case.recommendation is None else case.recommendation.value,
        "recommendation_rationale": case.recommendation_rationale,
        "decision": None if case.decision is None else case.decision.value,
        "decision_rationale": case.decision_rationale,
        "decided_by": case.decided_by,
    }


def _emit(payload: object) -> int:
    print(json.dumps(payload, indent=2))
    return EXIT_OK


def _workflow(home: Path) -> CaseWorkflow:
    return CaseWorkflow(AuditLog(home / AUDIT_PATH), load_config(home / CONFIG_PATH))


def load_results(home: Path, submission_id: str, config: ControlConfig) -> list[RecordResult]:
    """Validate a registered submission's package (looked up by ID, never by path)."""
    submission = resolve(load_registry(home / REGISTRY_PATH, home), submission_id)
    return validate_package(load_package(submission.package), config)


def _submissions(args: argparse.Namespace, actor: Actor) -> int:
    registry = load_registry(args.home / REGISTRY_PATH, args.home)
    return _emit({"submissions": sorted(registry)})


def _validate(args: argparse.Namespace, actor: Actor) -> int:
    # Imported here: cli.py imports this module to register the subcommand.
    from closepilot.cli import build_report

    submission = resolve(load_registry(args.home / REGISTRY_PATH, args.home), args.submission)
    results = validate_package(
        load_package(submission.package), load_config(args.home / CONFIG_PATH)
    )
    return _emit({"submission_id": submission.submission_id, **build_report(results)})


def _open_cases(args: argparse.Namespace, actor: Actor) -> int:
    submission = resolve(load_registry(args.home / REGISTRY_PATH, args.home), args.submission)
    wf = _workflow(args.home)
    results = validate_package(load_package(submission.package), wf.config)
    opened, already = wf.open_cases_for_package(actor, results)
    return _emit(
        {"submission_id": submission.submission_id, "opened": opened, "already_open": already}
    )


def _cases(args: argparse.Namespace, actor: Actor) -> int:
    cases = list(_workflow(args.home).cases().values())
    if args.submission:
        cases = [c for c in cases if c.submission_id == args.submission]
    return _emit({"cases": [case_to_dict(c) for c in cases]})


def _case(args: argparse.Namespace, actor: Actor) -> int:
    return _emit(case_to_dict(_workflow(args.home).get(args.case_id)))


def _investigate(args: argparse.Namespace, actor: Actor) -> int:
    wf = _workflow(args.home)
    case = wf.get(args.case_id)
    results = load_results(args.home, case.submission_id, wf.config)
    return _emit(investigation_to_dict(investigate(case, results)))


def _handoff(args: argparse.Namespace, actor: Actor) -> int:
    case = _workflow(args.home).handoff(actor, args.case_id, args.to, args.note)
    return _emit(case_to_dict(case))


def _request_evidence(args: argparse.Namespace, actor: Actor) -> int:
    case = _workflow(args.home).request_evidence(actor, args.case_id, args.request)
    return _emit(case_to_dict(case))


def _recommend(args: argparse.Namespace, actor: Actor) -> int:
    recommendation = Recommendation(args.recommendation)
    case = _workflow(args.home).recommend(actor, args.case_id, recommendation, args.rationale)
    return _emit(case_to_dict(case))


def add_agent_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    agent = sub.add_parser("agent", help="commands for the Switch agents (JSON output)")
    agent.add_argument("--as", dest="agent", required=True, choices=sorted(KNOWN_AGENTS))
    agent.add_argument("--home", type=Path, default=Path("."), help="ClosePilot home directory")
    cmds = agent.add_subparsers(dest="agent_command", required=True)

    cmds.add_parser("submissions", help="list registered submission IDs").set_defaults(
        agent_handler=_submissions
    )

    p = cmds.add_parser("validate", help="run the controls on a registered submission")
    p.add_argument("submission")
    p.set_defaults(agent_handler=_validate)

    p = cmds.add_parser("open-cases", help="open cases for REVIEW_REQUIRED and ERROR records")
    p.add_argument("submission")
    p.set_defaults(agent_handler=_open_cases)

    p = cmds.add_parser("cases", help="list cases")
    p.add_argument("--submission")
    p.set_defaults(agent_handler=_cases)

    p = cmds.add_parser("case", help="show one case")
    p.add_argument("case_id")
    p.set_defaults(agent_handler=_case)

    p = cmds.add_parser("investigate", help="deterministic facts and next step for a case")
    p.add_argument("case_id")
    p.set_defaults(agent_handler=_investigate)

    p = cmds.add_parser("handoff", help="hand an OPEN case to an investigator")
    p.add_argument("case_id")
    p.add_argument("--to", default=EXCEPTION_INVESTIGATOR.name, choices=sorted(KNOWN_AGENTS))
    p.add_argument("--note", required=True)
    p.set_defaults(agent_handler=_handoff)

    p = cmds.add_parser("request-evidence", help="ask the Finance Reviewer for evidence")
    p.add_argument("case_id")
    p.add_argument("--request", required=True)
    p.set_defaults(agent_handler=_request_evidence)

    p = cmds.add_parser("recommend", help="recommend APPROVE or REJECT to the Finance Reviewer")
    p.add_argument("case_id")
    p.add_argument("recommendation", choices=[r.value for r in Recommendation])
    p.add_argument("--rationale", required=True)
    p.set_defaults(agent_handler=_recommend)

    agent.set_defaults(handler=run_agent_command)


def run_agent_command(args: argparse.Namespace) -> int:
    actor = Actor(ActorType.AGENT, args.agent)
    try:
        result: int = args.agent_handler(args, actor)
    except (ConfigError, IngestionError, SubmissionError, InvestigationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except WorkflowError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AuditError as exc:
        print(f"audit log error: {exc}", file=sys.stderr)
        return EXIT_AUDIT_ERROR
    return result
