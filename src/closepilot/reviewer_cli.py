"""``closepilot reviewer``: the human Finance Reviewer's commands.

Commands that change a case (comment, attach-evidence, request-evidence, rework, decide,
close) pass the reviewer check first: the secret in ``CLOSEPILOT_REVIEWER_SECRET`` must
match the hash stored by ``reviewer init``. A failed check on an existing case is recorded
as an ACCESS_DENIED event by an UNVERIFIED actor, and nothing else happens. Read-only
commands (cases, case, export, summary) need no secret.

Exit codes: 0 success, 2 bad input or configuration, 3 refused by the workflow,
4 audit log locked, unreadable or failed integrity checks, 5 reviewer check failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from closepilot.agent_cli import (
    AUDIT_PATH,
    CONFIG_PATH,
    case_to_dict,
    load_results,
)
from closepilot.audit import AuditError, AuditLog, last_event_hash
from closepilot.config import ConfigError, load_config
from closepilot.ingestion import IngestionError
from closepilot.investigation import InvestigationError, investigate, investigation_to_dict
from closepilot.models import Action, Actor, ActorType, Case, Decision
from closepilot.report import decision_record, decision_record_markdown, package_summary
from closepilot.reviewer import (
    REVIEWER_FILE,
    SECRET_ENV,
    ReviewerError,
    init_reviewer,
    verify_reviewer,
)
from closepilot.submissions import SubmissionError
from closepilot.workflow import CaseNotFound, CaseWorkflow, WorkflowError

EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_REFUSED = 3
EXIT_AUDIT_ERROR = 4
EXIT_REVIEWER_CHECK_FAILED = 5

DECISIONS_DIR = Path("out/decisions")
UNVERIFIED = Actor(ActorType.UNVERIFIED, "unverified-reviewer")

Mutation = Callable[[CaseWorkflow, Actor, argparse.Namespace], Case]


def _workflow(home: Path) -> CaseWorkflow:
    return CaseWorkflow(AuditLog(home / AUDIT_PATH), load_config(home / CONFIG_PATH))


def _print(payload: object) -> int:
    print(json.dumps(payload, indent=2))
    return EXIT_OK


# --- read-only --------------------------------------------------------------------------


def _init(args: argparse.Namespace) -> int:
    reviewer = init_reviewer(args.home / REVIEWER_FILE, args.name, os.environ.get(SECRET_ENV))
    return _print({"reviewer": reviewer.name, "file": str(REVIEWER_FILE)})


def _cases(args: argparse.Namespace) -> int:
    cases = list(_workflow(args.home).cases().values())
    if args.submission:
        cases = [c for c in cases if c.submission_id == args.submission]
    return _print({"cases": [case_to_dict(c) for c in cases]})


def _case(args: argparse.Namespace) -> int:
    return _print(case_to_dict(_workflow(args.home).get(args.case_id)))


def _export(args: argparse.Namespace) -> int:
    wf = _workflow(args.home)
    events = wf.log.read()
    case = wf.get(args.case_id)
    results = load_results(args.home, case.submission_id, wf.config)
    record = decision_record(
        case.case_id,
        events,
        log_location=AUDIT_PATH.as_posix(),
        log_anchor=last_event_hash(events),
        investigation=investigation_to_dict(investigate(case, results)),
    )
    out_dir = args.home / DECISIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{case.case_id}.json"
    md_path = out_dir / f"{case.case_id}.md"
    json_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(decision_record_markdown(record), encoding="utf-8")
    return _print(
        {
            "case_id": case.case_id,
            "final_state": record["final_state"],
            "json": (DECISIONS_DIR / json_path.name).as_posix(),
            "markdown": (DECISIONS_DIR / md_path.name).as_posix(),
        }
    )


def _summary(args: argparse.Namespace) -> int:
    from closepilot.cli import build_report  # cli imports this module

    wf = _workflow(args.home)
    results = load_results(args.home, args.submission, wf.config)
    summary = package_summary(
        args.submission,
        build_report(results)["summary"],
        wf.cases(),
        log_location=AUDIT_PATH.as_posix(),
    )
    return _print(summary)


# --- case-changing (reviewer check required) -------------------------------------------


def _mutating(action: Action, run: Mutation) -> Callable[[argparse.Namespace], int]:
    def handler(args: argparse.Namespace) -> int:
        wf = _workflow(args.home)
        try:
            reviewer = verify_reviewer(args.home / REVIEWER_FILE, os.environ.get(SECRET_ENV))
        except ReviewerError as exc:
            try:
                wf.record_denied(UNVERIFIED, args.case_id, action, f"reviewer check failed: {exc}")
                recorded = " (recorded in the audit log)"
            except CaseNotFound:
                recorded = ""
            print(f"refused: reviewer check failed: {exc}{recorded}", file=sys.stderr)
            return EXIT_REVIEWER_CHECK_FAILED
        case = run(wf, Actor(ActorType.HUMAN, reviewer.name), args)
        return _print(case_to_dict(case))

    return handler


def _comment(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    return wf.add_commentary(actor, args.case_id, args.text)


def _attach(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    return wf.attach_evidence(actor, args.case_id, args.reference, args.note)


def _request(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    return wf.request_evidence(actor, args.case_id, args.request)


def _rework(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    return wf.return_for_rework(actor, args.case_id, args.reason)


def _decide(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    decision = Decision.APPROVED if args.decision == "approve" else Decision.REJECTED
    return wf.decide(actor, args.case_id, decision, args.rationale)


def _close(wf: CaseWorkflow, actor: Actor, args: argparse.Namespace) -> Case:
    return wf.close(actor, args.case_id, args.note)


def add_reviewer_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reviewer = sub.add_parser("reviewer", help="the human Finance Reviewer's commands")
    reviewer.add_argument("--home", type=Path, default=Path("."), help="ClosePilot home directory")
    cmds = reviewer.add_subparsers(dest="reviewer_command", required=True)

    p = cmds.add_parser("init", help=f"set up the reviewer (secret read from {SECRET_ENV})")
    p.add_argument("--name", required=True, help="your name as it should appear in the audit log")
    p.set_defaults(reviewer_handler=_init)

    p = cmds.add_parser("cases", help="list cases")
    p.add_argument("--submission")
    p.set_defaults(reviewer_handler=_cases)

    p = cmds.add_parser("case", help="show one case")
    p.add_argument("case_id")
    p.set_defaults(reviewer_handler=_case)

    p = cmds.add_parser("comment", help="add commentary to a case")
    p.add_argument("case_id")
    p.add_argument("--text", required=True)
    p.set_defaults(reviewer_handler=_mutating(Action.ADD_COMMENTARY, _comment))

    p = cmds.add_parser("attach-evidence", help="attach an evidence reference (a label)")
    p.add_argument("case_id")
    p.add_argument("reference")
    p.add_argument("--note")
    p.set_defaults(reviewer_handler=_mutating(Action.ATTACH_EVIDENCE, _attach))

    p = cmds.add_parser("request-evidence", help="ask for more evidence")
    p.add_argument("case_id")
    p.add_argument("--request", required=True)
    p.set_defaults(reviewer_handler=_mutating(Action.REQUEST_EVIDENCE, _request))

    p = cmds.add_parser("rework", help="return a case to the investigator")
    p.add_argument("case_id")
    p.add_argument("--reason", required=True)
    p.set_defaults(reviewer_handler=_mutating(Action.RETURN_FOR_REWORK, _rework))

    p = cmds.add_parser("decide", help="approve or reject a case (final)")
    p.add_argument("case_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--rationale", required=True)
    p.set_defaults(reviewer_handler=_mutating(Action.DECIDE, _decide))

    p = cmds.add_parser("close", help="close a decided case")
    p.add_argument("case_id")
    p.add_argument("--note")
    p.set_defaults(reviewer_handler=_mutating(Action.CLOSE, _close))

    p = cmds.add_parser("export", help="write the decision record to out/decisions/")
    p.add_argument("case_id")
    p.set_defaults(reviewer_handler=_export)

    p = cmds.add_parser("summary", help="final package summary for a submission")
    p.add_argument("submission")
    p.set_defaults(reviewer_handler=_summary)

    reviewer.set_defaults(handler=run_reviewer_command)


def run_reviewer_command(args: argparse.Namespace) -> int:
    try:
        result: int = args.reviewer_handler(args)
    except ReviewerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REVIEWER_CHECK_FAILED
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
