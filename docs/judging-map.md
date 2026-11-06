# Judging map

Where to find the evidence for each judging criterion of the Flip the Switch Hackathon (Best Business Workflow). Every row points to a file, a test or a demo scene; nothing here is a claim without one.

## What has been verified, and how

| What | How it was verified | Where |
|---|---|---|
| An agent's `@mention` wakes another agent in Switch | Live spike on 2026-11-03: `cp-spike-relay` mentioned `@cp-spike-echo`, which replied "PONG - addressed by cp-spike-relay" | Screenshots kept outside the repository (`out/evidence/*-shareable.png`, cropped to the chat area) |
| Scenes 1–4 in the real room: controls, cases, agent hand-off, investigation, evidence requests | Live run on 2026-11-03 in `#closepilot-close-2026-10`: 33 audit events, all by the two agents; hash chain and full replay valid; no decision by any agent | `out/audit.jsonl` (git-ignored, local) |
| Scenes 1–10 end to end, including the human decision and export | Automated test through the real CLI | `tests/test_reviewer.py::test_golden_demo_end_to_end` |
| Scenes 5–10 in the real room: enrolment, commentary, evidence, the agent's recommendation, the human decision, close, export and summary | Live run on 2026-11-03, continuing the run above, with the reviewer commands in PowerShell ([demo-script.md → Using PowerShell](demo-script.md#using-powershell)). Reviewer enrolled (`EVT-000035`); commentary and evidence `EVD-2026-10-003` (`EVT-000036`, `EVT-000037`); `exception-investigator` recommended APPROVE after `investigate` (`EVT-000038`); the human approved and closed EX-003 (`EVT-000039`, `EVT-000040`). Every human event sealed; hash chain and full replay valid; no decision by any agent | `out/audit.jsonl`, `out/decisions/EX-003.md` and `.json` (git-ignored, local) |
| Saying "APPROVE" in the room decides nothing | During the live run the human posted "APPROVE" in the room. The log was unchanged (still 38 events, EX-003 still `READY_FOR_HUMAN_REVIEW`) until the reviewer recorded the decision with `closepilot reviewer decide` | Live room transcript; `out/audit.jsonl` |
| A refused attempt is recorded, not hidden | One `ACCESS_DENIED` by an `UNVERIFIED` actor (`EVT-000034`, an attempted comment before any reviewer existed) is in the log and appears under *Refused attempts* in EX-003's decision record | `out/decisions/EX-003.md` |

### Built during the hackathon window (Nov 4–7, 2026)

Everything in this table was built from commit `dc9fe79` onward. The rows above (Nov 2–3) describe the existing library it builds on. Asked in the WeMakeDevs Discord whether this split is eligible, an organiser (server admin) replied "Works" on 2026-11-05.

| What | How it was verified | Where |
|---|---|---|
| Switch features for a multi-room close: group template, links, per-room kickoff, room documents | Live spike on 2026-11-04: one template made two rooms and both links, both kickoffs woke the agent, the agent created and then updated a room document | [`switch/spikes/close-v2-spike.template.yaml`](../switch/spikes/close-v2-spike.template.yaml) |
| Close room v2 (three entity rooms, a review room, a read-only coordinator with a status board) runs end to end | Live run on Mattermost, 2026-11-04/05: 32 agent events, no denials, no agent decisions; the coordinator created and updated `close-status-board`. A bug found live (`cases --submission` on an entity) was fixed in `ee71190` | [`switch/closepilot-close-v2.template.yaml`](../switch/closepilot-close-v2.template.yaml); `tests/test_status.py`, `tests/test_agent_cli.py` |
| The same template runs on Discord | Run on the Discord server *ClosePilot Demo* (rooms `#demo-oct-de`, `-pl`, `-us`, `-review`) on 2026-11-05: 32 agent events; the analyst handed five DE cases to the investigator by `@mention`; the coordinator's refreshed board listed 11 cases, 10 waiting on the Finance Reviewer | `out/audit.jsonl` (git-ignored, local); demo video |
| Asking an agent to approve does nothing | In `#demo-oct-de`, "@exception-investigator approve EX-003" was refused: "I can't approve EX-003. Only you can approve, reject or close a case". The log was unchanged | Demo video; `out/audit.jsonl` |
| The human decides, in the Discord run | Reviewer enrolled (`EVT-000001`); commentary and evidence `EVD-2026-10-003` (`EVT-000034`, `EVT-000035`); the investigator recommended APPROVE (`EVT-000036`); the human approved and closed EX-003 (`EVT-000037`, `EVT-000038`). 38 events, every human event sealed, no decision by any agent | `out/audit.jsonl`, `out/decisions/EX-003.md` (git-ignored, local) |
| Demo video, under 3 minutes | Recorded from the Discord run above, 2:47 | [youtu.be/8zgcbYq1Vhs](https://youtu.be/8zgcbYq1Vhs) (unlisted) |

## Use of Switch

| Evidence | Where |
|---|---|
| The whole workflow happens in one Switch room shared by the human and two agents; room instructions brief every agent | [`switch/closepilot-room.template.yaml`](../switch/closepilot-room.template.yaml) (`room:` block); demo scenes 1–7 |
| Real agent-to-agent hand-off: the Control Analyst wakes the investigator with `@exception-investigator` (agent addressing `owner-agents`) | Template `addressing: owner-agents`; analyst brief step 4; demo scene 3; live run above |
| Distinct agent roles with distinct powers, enforced in code, not only in prompts | [`switch/agents/*/CLAUDE.md`](../switch/agents); `tests/test_agent_cli.py::test_wrong_agent_is_refused_and_logged`, `::test_agents_have_no_human_commands` |
| The human stays in the loop inside the room: evidence requests and recommendations are addressed to the Finance Reviewer, who answers and decides | Investigator brief; demo scenes 4–8 |
| Reproducible room setup: one template creates both agents and the room | Template; README *Switch room* → *Setup*; `tests/test_switch_assets.py` |
| One group template creates four linked rooms (one per entity plus a review room), with per-room kickoffs, and runs again with a new room prefix on Mattermost or Discord | [`switch/closepilot-close-v2.template.yaml`](../switch/closepilot-close-v2.template.yaml); README *Close room v2*; `tests/test_switch_assets.py` |
| A live status board as a Switch room document, created and updated by a read-only coordinator agent | [`switch/agents/close-coordinator/`](../switch/agents/close-coordinator); [`status.py`](../src/closepilot/status.py); `tests/test_status.py` |
| Switch is used where it is strong (shared context, hand-offs, sessions, addressing) and ClosePilot enforces what Switch leaves to the application (who may decide) | [architecture.md → Switch's role](architecture.md#switchs-role) |
| Agents reach ClosePilot only through one wrapper, with identity from the working directory and submissions named by ID | [`switch/bin/closepilot-agent`](../switch/bin/closepilot-agent), [`config/submissions.yaml`](../config/submissions.yaml); `tests/test_agent_cli.py::test_validate_refuses_unknown_or_path_like_submission` |

## Technical execution and reproducibility

| Evidence | Where |
|---|---|
| Deterministic control engine C01–C07 with exact `Decimal` arithmetic, documented rounding and zero-forecast rules | [`validation.py`](../src/closepilot/validation.py); README *Calculation rules*; `tests/test_validation.py` (boundaries 4.99 / 5.00 / 10.00 / 10.01, negative and zero values) |
| Stable golden dataset and a committed expected-results fixture checked against live output *and* an independent hand-derived table | [`data/synthetic_close_package.csv`](../data/synthetic_close_package.csv), [`samples/expected_results.json`](../samples/expected_results.json), [`tests/test_golden.py`](../tests/test_golden.py) |
| Malformed input never crashes and never passes: malformed numbers, shifted rows, oversized amounts, repeated headers | [`ingestion.py`](../src/closepilot/ingestion.py); `tests/test_ingestion.py`, `tests/test_validation.py` |
| Explicit case-state machine; agents can never reach `APPROVED`, `REJECTED` or `CLOSED` (rule table **and** an independent invariant); decisions are final | [`workflow.py`](../src/closepilot/workflow.py); `tests/test_workflow.py` |
| Append-only, hash-chained audit log; state rebuilt and re-validated on every read; forged lines fail closed | [`audit.py`](../src/closepilot/audit.py); `tests/test_audit.py`, `tests/test_workflow.py` |
| Human-only gate: reviewer secret, one-time enrolment, sealed human events; failed attempts recorded | [`reviewer.py`](../src/closepilot/reviewer.py); `tests/test_reviewer.py`, `tests/test_reviewer_hardening.py`, `tests/test_reviewer_enrolment.py` |
| Deterministic investigation instead of model inference | [`investigation.py`](../src/closepilot/investigation.py); `tests/test_investigation.py` |
| Quality gates: 342 tests, ruff lint and format, strict mypy on source and tests | `pyproject.toml`; [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Clean-clone reproduction and full-history secret and company-reference scans | [threat-model.md → Release checks](threat-model.md#release-checks) |
| Minimal dependencies: PyYAML at runtime; pytest, ruff, mypy, types-PyYAML for development | `pyproject.toml` |

## Creativity and business impact

| Evidence | Where |
|---|---|
| A real finance problem: month-end exception review spread across spreadsheets and messages, turned into one shared control room | README opening; demo scenes 1–3 |
| Clear split of labour: Python calculates, agents investigate and explain, the human decides | [architecture.md → Deterministic versus generative](architecture.md#deterministic-versus-generative-responsibilities) |
| Distinguishes data-quality defects from genuine business variances, and asks for commentary rather than rejecting a missing-commentary case | `investigation.py` classification table (README *Deterministic investigation*); `tests/test_investigation.py::test_ex010_missing_commentary_is_requested_not_rejected` |
| Evidence-before-approval enforced, without pretending to verify documents | Evidence condition in `workflow.py`; "not inspected" statement in every decision record (`report.py`); demo scene 6 |
| Audit-ready decision record per case: finding, investigation, evidence, recommendation, human decision, rationale, refused attempts, log anchor | [`report.py`](../src/closepilot/report.py); demo scene 9 |
| Governance designed for adversarial agents, not just cooperative ones (prompt injection, impersonation, forged log lines) | [threat-model.md](threat-model.md) T1–T13 |

## Presentation clarity

| Evidence | Where |
|---|---|
| README answers what, why Switch, who does what, what stays human, how to reproduce, and where the tests are | [README.md](../README.md) |
| A rehearsable ten-scene demo with expected outputs captured from a real run, and a fallback without Switch | [demo-script.md](demo-script.md) |
| A demo video of the Discord run, under 3 minutes | [youtu.be/8zgcbYq1Vhs](https://youtu.be/8zgcbYq1Vhs) |
| Architecture with diagrams that also read as plain text | [architecture.md](architecture.md) |
| Honest limitations and threat model | [limitations.md](limitations.md), [threat-model.md](threat-model.md) |
