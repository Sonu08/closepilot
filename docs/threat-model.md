# Threat model

ClosePilot is a local, single-machine demonstration: one human Finance Reviewer and two Claude Code agents in a Switch room, all running as the same operating-system user, working on synthetic data. This document states what it protects, against whom, how, and what it does not protect.

## What must hold

1. **Only the human decides.** No agent can make a case `APPROVED` or `REJECTED`, close it, attach evidence or add commentary.
2. **The record is trustworthy.** Every action is in the audit log, and a decision shown by the reviewer's outputs (`export`, `summary`) was really made by the enrolled reviewer.
3. **Numbers are deterministic.** Every figure, control result and state change comes from Python, never from a model.
4. **Agents touch only what they are given.** Submissions are named by ID, never by path; agents cannot edit files.

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| Audit log | `out/audit.jsonl` (git-ignored) | The single source of truth; case state is rebuilt from it |
| Reviewer file | `.closepilot/reviewer.json` (git-ignored) | Salted scrypt hash of the reviewer secret |
| Reviewer secret | Only in the reviewer's shell (`CLOSEPILOT_REVIEWER_SECRET`) | Proves the human; derives the seal key |
| Close package | `data/`, registered in `config/submissions.yaml` | The figures every control runs on |
| Control configuration | `config/controls.yaml` | Thresholds; changing them changes outcomes |
| Switch agent credentials | `.switch/` folders and the Switch Console data folder | Let an agent act in Switch rooms |

## Actors and trust

| Actor | Trusted for | Not trusted for |
|---|---|---|
| Finance Reviewer (human) | Every decision | — |
| `control-analyst`, `exception-investigator` (agents) | Running their own commands, explaining, recommending | Deciding; any claim not printed by a ClosePilot command |
| Room messages, CSV cells, command output | Nothing: data only | Instructions of any kind |
| Other processes as the same OS user | Nothing | — (can read and write every file here) |

## Trust boundaries

1. **Room → agent.** Switch delivers a sender's text between `BEGIN/END SWITCH MESSAGE` markers; agent briefs say room text, CSV cells and command output are data.
2. **Agent → ClosePilot.** Agents run one wrapper, `switch/bin/closepilot-agent`, which takes the agent's identity from its working directory and runs `closepilot agent`. That CLI has no decide, close, attach-evidence or commentary command.
3. **ClosePilot domain.** `workflow.py` permits each action only to the actors in its rule table, and a separate invariant keeps every agent event inside the four agent states.
4. **Human → ClosePilot.** `closepilot reviewer` commands need the reviewer secret; every human event is sealed with a key derived from it, and the reviewer is enrolled once in the log.

## Threats and mitigations

| # | Threat | Mitigation | Tested in |
|---|---|---|---|
| T1 | An agent approves or rejects a case | Rule table: `DECIDE`, `CLOSE` human only. Independent invariant: agent events stay in agent states and carry no decision. Agent CLI has no such command. | `test_workflow.py`, `test_agent_cli.py` |
| T2 | Prompt injection in a CSV cell or room message ("approve everything") | Controls are deterministic Python; agents cannot decide whatever they read. Untrusted values are quoted and cut to 40 characters in findings; Markdown exports render them as inert code spans or block quotes. | `test_validation.py`, `test_reviewer_hardening.py` |
| T3 | Agent reads or writes an arbitrary file via a path argument | Agents name submissions by ID; the registry resolves only to files inside the ClosePilot home. Path-like IDs are refused. | `test_agent_cli.py` |
| T4 | One agent acts as the other | The wrapper derives identity from the working directory; the workflow limits each agent to its own actions and logs refusals as `ACCESS_DENIED`. | `test_agent_cli.py` |
| T5 | Forged "human" line appended to the log (valid hash chain) | Every human event carries an HMAC keyed from the reviewer secret; reviewer commands and outputs verify all seals and stop (exit 4). | `test_reviewer_hardening.py` |
| T6 | Reviewer file replaced with one of the attacker's choosing | The log enrols the reviewer once (seal-key fingerprint); a second enrolment is refused by `init` and by replay; a non-matching reviewer file stops every reviewer command. | `test_reviewer_enrolment.py` |
| T7 | Earlier log lines edited, deleted or reordered | SHA-256 hash chain; replay re-validates every event against the workflow rules and fails closed. | `test_audit.py`, `test_workflow.py` |
| T8 | Forged `ACCESS_DENIED` used to smuggle decision data | A denial must leave the case unchanged and carry only `attempted_action`. | `test_workflow.py` |
| T9 | Malformed or hostile input crashes the run or corrupts totals | Plain-decimal amounts within 15+6 digits; shifted rows marked malformed and excluded; repeated header columns rejected; size and record limits. | `test_ingestion.py`, `test_validation.py` |
| T10 | Tampered reviewer file weakens or hangs the secret check | Fixed scrypt parameters, salt and hash lengths; anything else is refused as malformed. | `test_reviewer_hardening.py` |
| T11 | Approval recommended or decided without evidence | Evidence condition (`evidence_required_controls`, or evidence requested) enforced at recommend and decide; requesting evidence clears a stale recommendation. | `test_workflow.py` |
| T12 | Two commands interleave log writes | Exclusive lock directory; failed writes rolled back; idempotent case opening. | `test_audit.py`, `test_workflow.py` |
| T13 | Secrets or company data committed | `.closepilot/`, `.switch/`, `out/`, `switch/local/` git-ignored; history scanned (see *Release checks*). | — |

## Residual risks (accepted, stated plainly)

- **Same OS user.** Agents run as the reviewer's own user. Permission files and Claude Code's auto mode are a second layer, not a sandbox: an agent that runs commands outside its allow list could read or write any file here. The guarantees above hold because the workflow, the seals and the enrolment do not depend on the agent behaving; they do not stop an agent from *damaging* files.
- **Starting over is not detectable from inside.** Deleting both the log and the reviewer file and enrolling afresh leaves no trace in either. The log hash in an exported decision record is the outside anchor; keep exports somewhere the agents cannot write.
- **First enrolment wins.** Someone who enrols before the real reviewer is not prevented; the real reviewer's own `init` is refused, so it is noticed before any decision.
- **Unchecked views.** `closepilot agent …`, `reviewer cases` and `reviewer case` do not verify seals. Only `export`, `summary` and the case-changing reviewer commands are authoritative.
- **Denial noise.** Anyone can trigger `ACCESS_DENIED` entries; they change nothing but can fill the log.
- **Stale lock.** A process killed while writing leaves `out/audit.jsonl.lock`; every later write times out until a human removes it.
- **Investigator can pull a case back.** The investigator may move a `READY_FOR_HUMAN_REVIEW` case back to `AWAITING_EVIDENCE`. It stays within agent states and decides nothing, but it removes the case from the review queue.
- **Evidence is a label.** ClosePilot records evidence references and never inspects the documents behind them; every record says so.
- **Secret handling.** A secret typed on a command line may stay in shell history; the README shows how to set it without echoing. A weak secret could be brute-forced from the stored hash (scrypt, N=2^14).

## Out of scope

Multi-user deployment, real identity management, network attackers, a compromised Switch server or Claude Code itself, and real financial data. ClosePilot uses only synthetic data about the fictional Northstar Logistics Group.

## Release checks

Run before publishing (last run 2026-11-03, all clean):

- Company terms in tracked files, every blob of every commit, commit messages and authors.
- Secret patterns (keys, tokens, private keys, JWTs) across all history; only labelled synthetic test secrets match.
- No binaries, images or office files ever committed; no email addresses in tracked files.
- Clean clone into a new virtual environment: install, full test suite, ruff, mypy, golden output.
- Dependency licences: runtime PyYAML (MIT); development tools MIT, BSD, Apache-2.0, PSF, and MPL-2.0 (`pathspec`, via mypy, not redistributed).
- Screenshots: the raw captures stay in git-ignored `out/evidence/`; only the cropped `*-shareable.png` copies (chat area only, no paths or taskbar) are fit to share.
