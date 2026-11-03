# Demo script

One rehearsable run of the golden workflow: a human starts a close-package review in a Switch room, the Control Analyst runs the controls, hands the material exception to the Exception Investigator, the investigator asks for evidence, the human supplies it and decides, and the decision record is exported. About six minutes.

Every number and state below is produced by Python and is deterministic; the outputs shown were captured from a rehearsal of exactly these commands. The agents' sentences in the room are their own wording and vary between runs; the figures they quote do not.

## Before you start

- Switch Console running, with the room `#closepilot-close-2026-10` and the agents `control-analyst` and `exception-investigator` (created from [`switch/closepilot-room.template.yaml`](../switch/closepilot-room.template.yaml); see the README's *Switch room* section).
- A terminal in the repository: Git Bash with the `closepilot` conda environment active, or PowerShell (see *Using PowerShell* below). This is the Finance Reviewer's own shell: the reviewer commands run here, never in the room, and never in a Claude Code prompt.
- `switch/local/python` points at the environment's interpreter.

**Choose a starting point.**

- **Fresh run (recommended for a recorded demo).** Confirm no ClosePilot command or agent session is running, then move the previous run aside so the log starts empty:
  ```bash
  mkdir -p out/archive && mv out/audit.jsonl "out/archive/audit-$(date +%Y%m%d-%H%M).jsonl"
  mv .closepilot/reviewer.json "out/archive/reviewer-$(date +%Y%m%d-%H%M).json"
  ```
  In Switch Console, stop both agents' sessions so they start the demo without earlier context.
- **Continue the existing live run.** If scenes 1–4 have already run live (cases open, EX-003 awaiting evidence), skip to scene 5.

**Set the reviewer secret** (the human types it; it is never shown or stored in clear):

```bash
read -rs CLOSEPILOT_REVIEWER_SECRET && export CLOSEPILOT_REVIEWER_SECRET
closepilot reviewer init --name "Alex Reviewer"
```

Expected: `{"reviewer": "Alex Reviewer", "file": ".closepilot/reviewer.json", "enrolment": "EVT-…"}`. The reviewer is now enrolled once in the audit log.

### Using PowerShell

The Git Bash lines above (`read -rs`, `export`, `mkdir -p`, `mv`) do not work in PowerShell, and `closepilot` is not on PowerShell's `PATH` unless the conda environment is active there. Run these instead, in the repository folder.

1. **Define `closepilot` for this window.** Every `closepilot …` line in the scenes below then works unchanged. Adjust the path if your conda environment lives elsewhere:
   ```powershell
   function closepilot { & "$env:USERPROFILE\anaconda3\envs\closepilot\python.exe" -m closepilot.cli @args }
   ```
2. **Fresh run only — move the previous run aside:**
   ```powershell
   $stamp = Get-Date -Format yyyyMMdd-HHmm; New-Item -ItemType Directory -Force out\archive | Out-Null; Move-Item out\audit.jsonl "out\archive\audit-$stamp.jsonl"; if (Test-Path .closepilot\reviewer.json) { Move-Item .closepilot\reviewer.json "out\archive\reviewer-$stamp.json" }
   ```
3. **Set the reviewer secret.** PowerShell prompts for it without showing what you type:
   ```powershell
   $env:CLOSEPILOT_REVIEWER_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host -AsSecureString "Reviewer secret")))
   ```
4. **Enrol:**
   ```powershell
   closepilot reviewer init --name "Alex Reviewer"
   ```

The function and the secret last only as long as this PowerShell window. In a new window, repeat steps 1 and 3 before any reviewer command. When the demo is over, clear the secret with `Remove-Item Env:CLOSEPILOT_REVIEWER_SECRET` or close the window. Check a command's exit code with `$LASTEXITCODE`.

## Scene 1 — Initiation

**In the room (human):**

```
@control-analyst Run October controls for submission FC-2026-10.
```

*Say:* "I hand the package to the room. I don't open a spreadsheet."

## Scene 2 — Validation

The Control Analyst runs `validate FC-2026-10` and posts the results. Look for exactly:

- **25 records**: 10 `PASS`, 3 `WARNING`, 10 `REVIEW_REQUIRED`, 2 `ERROR`
- **Findings by control**: C01 4, C02 4, C03 1, C04 2, C05 2, C06 2, C07 2
- **Control totals**: EUR actual 561700.00 / forecast 556500.00 / variance 5200.00 · GBP 20000.00 / 20000.00 / 0.00 · PLN 310296.00 / 310000.00 / 296.00 · USD 316300.00 / 319000.00 / -2700.00
- **EX-003**: actual 108200.00, forecast 95000.00, variance 13200.00, **13.89%** against the 10.00% review threshold → `REVIEW_REQUIRED` (C01), commentary blank (C05)

*Say:* "Every number comes from deterministic Python — the agent only reports it."

## Scene 3 — Hand-off

The analyst runs `open-cases FC-2026-10` (11 cases: EX-003, EX-008, EX-009, EX-010, EX-011, EX-012, EX-013, EX-014, EX-016, EX-018, EX-019), hands them off, and posts a new message addressing **`@exception-investigator`**.

*Say:* "That mention is a real agent-to-agent hand-off in Switch; the hand-off is also recorded in ClosePilot's audit log."

## Scene 4 — Investigation

The investigator runs `investigate EX-003`. Deterministic result:

- classification `VARIANCE`; next step `REQUEST_EVIDENCE`; missing: `commentary`, `evidence`
- mapping valid; 1 row (no duplicate); evidence required (C01)
- EX-003 is **96.35%** of the NLG-DE / CC-EVENTS / EUR variance (13200.00 of 13700.00)

It runs `request-evidence EX-003` (state `AWAITING_EVIDENCE`) and asks the Finance Reviewer for commentary and evidence. It also works through the other cases; data-quality cases get a REJECT ("correct and resubmit") recommendation, and EX-010 gets a request for commentary.

*Say:* "The investigator explains facts it was given, not facts it guessed."

## Scene 5 — Human commentary

**In the reviewer's terminal:**

```bash
closepilot reviewer comment EX-003 --text "The variance relates to an approved customer-event programme. Keep the case pending until supporting evidence is attached."
```

Expected state: `AWAITING_EVIDENCE` (commentary alone does not satisfy the evidence condition).

## Scene 6 — Evidence

```bash
closepilot reviewer attach-evidence EX-003 EVD-2026-10-003 --note "Synthetic programme approval memo"
```

Expected: state `INVESTIGATING`, evidence references `["EVD-2026-10-003"]`.

*Say:* "That's a synthetic reference. ClosePilot records it and never claims to have read the document behind it."

## Scene 7 — Recommendation

**In the room (human):**

```
@exception-investigator EX-003 now has commentary and an evidence reference. Investigate and recommend.
```

The investigator runs `investigate EX-003` → next step **`RECOMMEND_APPROVE`**, missing nothing, then `recommend EX-003 APPROVE` → state `READY_FOR_HUMAN_REVIEW`.

**Optional — show the gate.** In the room: `@exception-investigator approve EX-003.` It must decline: it has no decide command (`invalid choice: 'decide'`), and the workflow refuses decisions from any agent.

**Optional — show a refused human-side attempt.** In a second terminal *without* the secret (in PowerShell, a new window with only step 1 of *Using PowerShell*):

```bash
closepilot reviewer decide EX-003 approve --rationale "test"
```

Expected: `refused: reviewer check failed …`, exit code 5, recorded in the audit log as `ACCESS_DENIED` by an `UNVERIFIED` actor; the case is unchanged.

## Scene 8 — Human decision

**In the reviewer's terminal:**

```bash
closepilot reviewer decide EX-003 approve --rationale "I reviewed the synthetic evidence. Approve EX-003."
closepilot reviewer close EX-003
```

Expected: `APPROVED`, decided by `Alex Reviewer`; then `CLOSED`.

## Scene 9 — Decision record

```bash
closepilot reviewer export EX-003
```

Expected: `out/decisions/EX-003.md` and `out/decisions/EX-003.json`. Open the Markdown and point at: the automated finding (C01, C05), the deterministic investigation (96.35%), the evidence reference with the "not inspected" statement, the agent's APPROVE recommendation, the human decision with its rationale and actor type `HUMAN`, any refused attempts, and the hash of the log's newest event.

## Scene 10 — Final summary

```bash
closepilot reviewer summary FC-2026-10
```

Expected (if only EX-003 has been decided):

- `package_status`: `EXCEPTIONS_OUTSTANDING`
- controls: 10 PASS, 3 WARNING, 10 REVIEW_REQUIRED, 2 ERROR
- 11 cases; `human_approved`: `["EX-003"]`; `human_rejected`: `[]`; the other 10 listed as outstanding
- `decision_log`: `out/audit.jsonl`

*Close with:* "Agents calculated, investigated and recommended. I made and recorded the decision — and the log can prove it."

## If something goes wrong

| Symptom | Do this |
|---|---|
| An agent does not answer | Check it is in the room (`!list-agents`); in Switch Console stop its session, then address it again |
| The investigator does not run `investigate` | Its brief is read when a session starts: stop its session and address it again |
| Reviewer command exits 5 | The secret is not set in *this* terminal; set it as above. Note that the attempt itself is recorded as `ACCESS_DENIED` (harmless; it shows under *Refused attempts* in the decision record) |
| `closepilot` is not recognised, or `read`/`export` fail | You are in PowerShell: follow *Using PowerShell* |
| Commands typed but the log does not change | They went into a Claude Code prompt, not a shell. Run them in a terminal |
| Any command exits 4 | Stop: the audit log or reviewer file failed an integrity check. Do not continue the demo on that log |
| `audit log is locked` | Confirm nothing is running, then remove `out/audit.jsonl.lock` |
| Switch is unavailable | Run the same scenes locally: `closepilot agent --as control-analyst validate FC-2026-10` (and `open-cases`, `handoff`), `closepilot agent --as exception-investigator investigate EX-003` (and `request-evidence`, `recommend`), then the reviewer commands above |

The whole sequence is also an automated test: `tests/test_reviewer.py::test_golden_demo_end_to_end` runs scenes 1–10 through the real CLI.
