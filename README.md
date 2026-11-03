# ClosePilot: AI Finance Control Room

ClosePilot is a collaborative AI finance-control room where agents validate and investigate synthetic close exceptions, while humans retain final approval authority.

> **ClosePilot does not allow an AI agent to approve material finance exceptions.** Agents calculate, investigate and recommend; a human reviewer makes and records the final decision.

**Status:** working end to end — deterministic controls, a Switch room with two agents, deterministic investigation, human-only approval behind a reviewer check, an append-only audit log and decision-record export. All data is synthetic and describes the fictional *Northstar Logistics Group*.

## Quick start (development)

```bash
conda activate closepilot          # Python 3.13
python -m pip install -e ".[dev]"
python -m pytest
closepilot validate data/synthetic_close_package.csv          # human-readable
closepilot validate data/synthetic_close_package.csv --json   # structured
```

## Calculation rules

- `variance = actual - forecast` (exact `Decimal` arithmetic)
- `variance % = variance / |forecast| × 100`, rounded half-up to 2 dp; thresholds compare the rounded value
- Zero forecast: both zero → `0.00%`; non-zero actual → percentage undefined (`null`). An undefined percentage is treated as exceeding every threshold: C01 requires review and C05 requires commentary.
- Amounts must be plain decimals (`-1234.56`) with at most 15 integer and 6 decimal digits. Thousands separators, exponents, `NaN`, currency symbols, formula-like values and out-of-range amounts are rejected as `ERROR` (C07). These limits keep all arithmetic exact; no input can crash the run.

## Control catalogue

Thresholds live in [`config/controls.yaml`](config/controls.yaml) (illustrative project settings, not company policy).

| ID | Control | Rule | Result |
|----|---------|------|--------|
| C01 | Material variance | `\|variance %\| > variance_review_pct` (10), or % undefined | `REVIEW_REQUIRED` |
| C02 | Moderate variance | `variance_warning_pct ≤ \|variance %\| ≤ variance_review_pct` (5–10, inclusive) | `WARNING` |
| C03 | Master-data mapping | `mapping_status` not in `mapping_valid_values` (exact match) | `REVIEW_REQUIRED` |
| C04 | Duplicate record | `record_id` occurs more than once in the package (every occurrence flagged) | `REVIEW_REQUIRED` |
| C05 | Required commentary | `\|variance %\| ≥ commentary_required_pct` (5), or % undefined, and commentary blank | `REVIEW_REQUIRED` |
| C06 | Currency and period | `currency` / `period` not in the allowed lists (exact match) | `REVIEW_REQUIRED` |
| C07 | Required-field integrity | required field blank, amount missing/malformed, or row field count ≠ header | `ERROR` |

- **Severity precedence:** `ERROR` > `REVIEW_REQUIRED` > `WARNING` > `PASS`. A record's status is its highest-severity finding; every finding stays visible, listed in catalogue order.
- Blank values are reported once, by C07. C03, C04 and C06 judge only non-blank values.
- **Matching is exact and case-sensitive** for `record_id`, `currency`, `period` and `mapping_status`: `eur` is not `EUR`, and `EX-011` / `ex-011` are *not* treated as duplicates by C04. Values are only trimmed of surrounding whitespace.
- `commentary` is the only optional column; whether it is required is decided by C05.
- A row whose field count does not match the header (for example an unquoted `1,000.00`) is **structurally malformed**: its values may sit in the wrong columns, so it receives only the C07 field-count `ERROR`. No other control runs on it, it is not counted for C04 and it is excluded from control totals.
- A package missing a required **column**, or whose header repeats a column name, is rejected as a whole (exit code 2); per-row problems never stop the package.
- **Control totals** are reported per currency (never summed across currencies) and include every record with usable amounts, flagged or not. Two consequences are deliberate and visible: **duplicate records (C04) are counted once per occurrence**, so the golden package's EX-011 contributes twice to the USD total; and records with other findings (for example a missing `cost_centre`) are still included. Records with unusable amounts or a malformed row are listed as `records_excluded_from_totals`.

## Golden demo data

[`data/synthetic_close_package.csv`](data/synthetic_close_package.csv) holds 25 hand-written, stable records (no random generation). Each one exercises a specific scenario: thresholds just below, at and above each boundary, negative values, zero actual or forecast, invalid mapping, a duplicate ID, an unsupported currency, an invalid period, a missing field and a malformed number. EX-003 is the primary exception in the demo.

Expected result: 25 records — 10 `PASS`, 3 `WARNING`, 10 `REVIEW_REQUIRED`, 2 `ERROR`.

[`samples/expected_results.json`](samples/expected_results.json) is the committed output of `closepilot validate … --json`. [`tests/test_golden.py`](tests/test_golden.py) checks it against both the live output and a hand-derived table, so the fixture cannot drift. If you deliberately change the rules, regenerate it (in Git Bash, so the file stays UTF-8) and review the diff:

```bash
python -m closepilot.cli validate data/synthetic_close_package.csv --json > samples/expected_results.json
```

## Case workflow and human-only approval

Records that are `REVIEW_REQUIRED` or `ERROR` become cases (one per `record_id`, so duplicate rows share a case). [`src/closepilot/workflow.py`](src/closepilot/workflow.py) enforces the state machine and role permissions:

| Action | From → to | Who may do it |
|---|---|---|
| Open case | – → `OPEN` | Control Analyst, human |
| Hand off | `OPEN` → `INVESTIGATING` | Control Analyst, human |
| Request evidence | `INVESTIGATING` / `READY_FOR_HUMAN_REVIEW` → `AWAITING_EVIDENCE` | Exception Investigator, human |
| Attach evidence | `AWAITING_EVIDENCE` → `INVESTIGATING` | **human only** |
| Add commentary | any undecided state (unchanged) | **human only** |
| Recommend | `INVESTIGATING` → `READY_FOR_HUMAN_REVIEW` | Exception Investigator |
| Return for rework | `READY_FOR_HUMAN_REVIEW` → `INVESTIGATING` | **human only** |
| Decide | `READY_FOR_HUMAN_REVIEW` → `APPROVED` / `REJECTED` | **human only** |
| Close | `APPROVED` / `REJECTED` → `CLOSED` | **human only** |

- **Agents can never reach `APPROVED`, `REJECTED` or `CLOSED`.** Besides the table, a separate invariant blocks any agent event outside the four agent states, or any agent event carrying a decision.
- **A decision is final.** `APPROVED` and `REJECTED` lead only to `CLOSED`; nothing overrides or reopens them.
- **Evidence condition.** For controls in `evidence_required_controls` (C01 by default), or after evidence was requested, approval can be neither recommended nor decided until the human has attached an evidence reference. References are labels the human supplies; ClosePilot records them and **does not claim to have inspected the documents behind them**.
- **Requesting evidence clears the current recommendation**, so a recommendation made before the new evidence existed is never shown as current; the investigator must recommend again.
- **Unauthorised attempts on an existing case are refused and logged** as `ACCESS_DENIED` events; the case is unchanged. An `ACCESS_DENIED` event cannot carry a decision, recommendation or evidence, so it cannot be used to slip agent-attributed decision data into the log. An unauthorised attempt to *open* a case is refused but **not** logged, because no case exists to attach the event to. Invalid transitions and invalid input are refused without an event.
- **Human actor names** must be non-blank, at most 64 characters, free of control characters and different from every agent name, so the audit trail never shows an agent name on a human action.
- **Trust boundary:** the domain layer guarantees that an *agent* actor cannot decide. Proving that a *human* actor really is the human is done by the interface layer (planned: a reviewer secret held only in the human's shell, plus `decide` disallowed as a tool for agents).

## Audit log

[`src/closepilot/audit.py`](src/closepilot/audit.py) writes one JSON object per line (all spec fields plus `details` and `prev_event_hash`). The log is the single source of truth: case state is rebuilt by replaying events, and every replayed event is re-checked against the workflow rules.

- **Append-only API, hash-chained.** Each event stores the SHA-256 of the previous line, so editing, deleting, reordering or truncating earlier lines is detected and reading fails closed.
- **Locked writes.** An exclusive lock (a `<log name>.lock` directory next to the log) prevents concurrent commands from interleaving lines. A command waits up to 5 seconds for the lock, then fails without writing anything.
- **Stale lock recovery.** If a ClosePilot process is killed while holding the lock, the lock directory remains and every later command fails with `audit log is locked (...)`. ClosePilot never removes a lock it did not create, because it cannot tell a crashed process from a slow one. Recovery is a human action: confirm that no ClosePilot command is running, then delete the `.lock` directory named in the error. The log itself stays consistent, because a write either completes or is rolled back.
- **Durable or not at all.** Writes are fsynced. A failed write is rolled back and raised, so an action is never reported as recorded when it was not.
- **Honest limits.** This is application-level tamper *evidence*, not immutable or cryptographically secured storage: anyone with write access can replace the whole file, and a rule-respecting edit of the *newest* line is not detectable by the chain alone (rule-breaking edits, such as a forged agent approval, are still rejected on replay).

## Switch room

ClosePilot runs as a Switch room with two Claude Code agents and one human:

| Participant | Can | Cannot |
|---|---|---|
| `control-analyst` (agent) | validate a submission, open cases, hand them off | investigate, recommend, decide |
| `exception-investigator` (agent) | read cases, request evidence, recommend APPROVE / REJECT | hand off, attach evidence, decide |
| Finance Reviewer (human) | attach evidence, add commentary, return for rework, **approve / reject**, close | — |

The control analyst hands work to the investigator with a real `@exception-investigator` mention. This was verified live: an agent's mention wakes another agent when its addressing is `owner-agents`.

**Setup (once per machine)**

1. Install ClosePilot into a Python 3.13 environment (see Quick start).
2. Write that interpreter's path to `switch/local/python` (git-ignored), for example `C:/Users/you/anaconda3/envs/closepilot/python.exe`. The agents' shell is Git Bash, where `python` may be a Windows Store stub and `closepilot` is not on `PATH`.
3. In Switch Console: **Templates → Import template**, choose [`switch/closepilot-room.template.yaml`](switch/closepilot-room.template.yaml), set **ClosePilot checkout** to this repository's path, and select **Create all**.
4. In the room: `@control-analyst check FC-2026-10`.

**How agents reach ClosePilot**

- Each agent works in its own folder, [`switch/agents/<name>/`](switch/agents), with a `CLAUDE.md` brief and a `.claude/settings.json` that allows exactly one command, `../../bin/closepilot-agent`, and denies file edits, web access and common shell escapes.
- [`switch/bin/closepilot-agent`](switch/bin/closepilot-agent) takes the agent's identity from its working directory, not from an argument, and runs `closepilot agent --as <name> …` ([`agent_cli.py`](src/closepilot/agent_cli.py)), which prints JSON.
- Agents name submissions by ID. [`config/submissions.yaml`](config/submissions.yaml) maps IDs to packages inside the repository; a path-like or unknown ID is refused, so a room message cannot point ClosePilot at an arbitrary file.
- The agent CLI has no decide, close, attach-evidence or commentary command. Even if an agent ran Python directly, the workflow refuses those actions from any agent actor.
- Agent actions are written to `out/audit.jsonl` (git-ignored). Exit code `3` means the workflow refused the action; a refusal on an existing case is logged as `ACCESS_DENIED`.

**Limits, stated plainly.** The per-agent permission files are a second layer, not a sandbox: with Auto mode on, Claude Code's own review decides about commands outside the allow and deny lists. The guarantee that no agent can approve or reject comes from the workflow layer. Switch credentials live in `.switch/` folders, which are git-ignored.

## Deterministic investigation

`closepilot agent investigate <CASE_ID>` ([`investigation.py`](src/closepilot/investigation.py)) gives the Exception Investigator verified facts instead of leaving it to infer them: mapping validity, duplicate rows, currency and period, field integrity, source commentary, evidence status, and the record's share of its entity / cost-centre / currency variance (EX-003: 96.35% of the NLG-DE / CC-EVENTS / EUR variance).

It classifies the case and suggests a next step by fixed rules:

| Case | Classification | Suggested next step |
|---|---|---|
| Any C03 / C04 / C06 / C07 finding | `DATA_QUALITY` | `RECOMMEND_REJECT` (correct and resubmit) |
| Variance, evidence required but not attached | `VARIANCE` | `REQUEST_EVIDENCE` (lists what is `missing`) |
| Variance, commentary required but not provided | `VARIANCE` | `REQUEST_COMMENTARY` |
| Variance, commentary and evidence satisfied | `VARIANCE` | `RECOMMEND_APPROVE` |

Commentary counts as provided when the source record had it or the Finance Reviewer added it to the case. The agent explains and recommends; the facts and the rule come from Python.

## Human reviewer commands

The Finance Reviewer works from their own shell with `closepilot reviewer …` ([`reviewer_cli.py`](src/closepilot/reviewer_cli.py)).

```bash
# Once: choose a secret of at least 12 characters and keep it only in your shell.
read -rs CLOSEPILOT_REVIEWER_SECRET && export CLOSEPILOT_REVIEWER_SECRET   # Git Bash
closepilot reviewer init --name "Alex Reviewer"

closepilot reviewer comment EX-003 --text "Approved customer-event programme."
closepilot reviewer attach-evidence EX-003 EVD-2026-10-003 --note "Programme approval memo"
closepilot reviewer decide EX-003 approve --rationale "I reviewed the synthetic evidence."
closepilot reviewer close EX-003
closepilot reviewer export EX-003          # writes out/decisions/EX-003.{md,json}
closepilot reviewer summary FC-2026-10     # final package summary
```

In PowerShell, set the secret without echoing it: `$env:CLOSEPILOT_REVIEWER_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host -AsSecureString)))`.

- **Reviewer check.** `init` stores your name and a salted scrypt hash of the secret in `.closepilot/reviewer.json` (git-ignored); the secret itself is never written. Every command that changes a case (`comment`, `attach-evidence`, `request-evidence`, `rework`, `decide`, `close`) needs the same secret in `CLOSEPILOT_REVIEWER_SECRET`. Read-only commands (`cases`, `case`, `export`, `summary`) do not.
- **Why agents cannot pass it.** Switch starts agent sessions with a fixed set of environment variables, so the secret in your shell never reaches them; their permission files allow only the agent wrapper, which always runs `closepilot agent`; and the stored hash cannot be reversed.
- **Failed checks are recorded.** A wrong or missing secret is refused (exit code `5`) and logged as `ACCESS_DENIED` by an `UNVERIFIED` actor, which no workflow rule permits to do anything.
- **Decision record.** `export` writes the automated finding, the deterministic investigation (recomputed), hand-offs, evidence requests, commentary, evidence references, recommendations, the human decision with its rationale, refused attempts and the audit trail, including the hash of the log's newest event. Recording that hash elsewhere anchors the log against a later edit of its newest line.
- **Limits.** This is a local demonstration control, not identity management: anyone who runs commands as the same operating-system user *and* knows the secret is treated as the reviewer. Shell history may keep a secret typed on the command line, so set it with the prompts above.

## Licence

MIT — see [LICENSE](LICENSE).
