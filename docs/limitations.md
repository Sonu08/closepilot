# Limitations

ClosePilot is a hackathon demonstration. These limits are deliberate or accepted; security-specific residual risks are listed in [threat-model.md → Residual risks](threat-model.md#residual-risks-accepted-stated-plainly).

## Scope

- **Synthetic data only.** The package describes the fictional Northstar Logistics Group. No real organisation, person, supplier, customer or financial value is used.
- **No production finance integration.** No general ledger, ERP, consolidation, invoice or forecasting system is connected. Packages are CSV files registered in `config/submissions.yaml`.
- **One package, one period.** The configuration allows the period `2026-10` and the currencies EUR, USD and PLN. Totals are reported per currency; there is no currency conversion.
- **Illustrative thresholds.** The 5% and 10% thresholds and the evidence-required controls in `config/controls.yaml` are project settings, not anyone's policy. Changing them is a human edit to a file, and the control logic stays the same.

## Trust and identity

- **No production identity or access management.** The human is identified by a reviewer secret held in their own shell. Everything runs as one operating-system user on one machine. See the threat model for what this does and does not protect.
- **No autonomous approval, by design.** Agents never approve, reject or close. A material exception waits for a human however long that takes.
- **Agents' permission files are a second layer, not a sandbox.** With Switch's Auto mode, Claude Code's own review decides about commands outside the allow and deny lists. The human-only guarantee rests on the workflow, the reviewer check and the seals, not on agent behaviour.
- **Switch tools cannot be removed from an agent.** Switch's docs say disallowed tools never remove the Switch tools. Agent creation stays off because "can manage agents" is off in the template.

## Evidence

- **Evidence is a reference, not a document.** The human attaches a label (for example `EVD-2026-10-003`). ClosePilot records who attached it and when. It does not open, store, hash or verify any document, and every decision record says so.
- **Commentary is free text** from the source record or the reviewer. ClosePilot checks only that it is present, never what it says.

## Audit

- **Application-level append-only log, not immutable storage.** The log is a hash-chained JSON Lines file with sealed human events. Edits are detected, not prevented. Someone with file access can delete it, and starting over from nothing is not detectable from inside; the hash in an exported decision record is the outside anchor.
- **One writer at a time.** A lock directory serialises writes. A process killed mid-write leaves a stale lock that a human removes.
- **Some views are unchecked.** `closepilot agent …`, `reviewer cases` and `reviewer case` do not verify seals. Only `export`, `summary` and the case-changing reviewer commands are authoritative.

## Deployment

- **Local demonstration architecture.** One Windows machine runs Switch Console, its local Switch server, Mattermost in Docker, Claude Code and Python. Nothing is deployed to a cloud or shared server.
- **Machine-specific setup.** `switch/local/python` (git-ignored) must point at the interpreter, because the agents' Git Bash shell does not see the conda environment.
- **Switch Console quirks seen during the build** (Switch Console 0.40.1):
  - **Template runs:** a run can report a timeout and "Nothing was created" while the server has in fact created the agents and room. Check before retrying, because agent names cannot be reused.
  - **After a server Reset:** this computer had to be enrolled as a machine again before managed agents could be created.

## Agents' wording

- The agents' sentences in the room vary between runs. The figures, states and suggested next steps they report come from ClosePilot and do not.
- The Control Analyst hands off every case it opens, so the investigator works through all of them, not just the primary exception. The demo narrates EX-003.
