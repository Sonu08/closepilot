# Security policy

ClosePilot is a personal, open-source hackathon project that runs locally on synthetic data. It is not a production system and holds no real financial or personal data.

## What ClosePilot protects

The core guarantee is that **only the human Finance Reviewer can approve or reject a case**, and that the audit record shows who did what. The design, the threats considered, the mitigations, their tests and the accepted residual risks are in [docs/threat-model.md](docs/threat-model.md).

## Reporting a vulnerability

If you find a way for an agent, or anyone without the reviewer secret, to produce an `APPROVED` or `REJECTED` case, forge a human event, or bypass the audit checks:

- Report it privately through the repository's private vulnerability reporting (**Security → Report a vulnerability** on the repository page), or open an issue *without* exploit details and ask for a private channel.
- Include the ClosePilot commit, the steps to reproduce and the observed result. Please don't include real credentials or real company data.

There is no bug bounty. Reports are handled on a best-effort basis.

## Handling secrets

- The reviewer secret belongs only in the reviewer's shell (`CLOSEPILOT_REVIEWER_SECRET`). Never write it to a file, commit it or paste it into a Switch room. ClosePilot stores only a salted scrypt hash, in `.closepilot/reviewer.json`.
- Switch agent credentials are written by Switch to `.switch/` folders. These, `.closepilot/`, `out/` and `switch/local/` are git-ignored.
- ClosePilot itself needs no API keys, tokens or `.env` file (see [.env.example](.env.example)).

## Before publishing a fork

Run the release checks listed in [docs/threat-model.md → Release checks](docs/threat-model.md#release-checks):
- secret and company-reference scans across the full git history;
- a clean-clone install and test;
- screenshot hygiene.
