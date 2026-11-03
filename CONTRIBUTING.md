# Contributing

Thanks for your interest. ClosePilot is a small hackathon project, so contributions should keep it small, deterministic and honest.

## Set up

```bash
conda create -n closepilot python=3.13 && conda activate closepilot   # or any Python 3.13 venv
python -m pip install -e ".[dev]"
```

## Checks (all must pass before a commit)

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m mypy          # source and tests, strict
```

The golden fixture is part of the suite. If you deliberately change a control rule, regenerate `samples/expected_results.json` (see the README's *Golden demo data*), review the diff, and update the hand-derived table in `tests/test_golden.py`. Never regenerate the fixture just to make a failing test pass.

## Rules that are not negotiable

- **Synthetic data only.** Use only data about the fictional Northstar Logistics Group. No real organisations, people, suppliers, customers, financial values, employer names, logos, internal terminology or screenshots.
- **Python does every calculation and state change.** Agents explain, investigate and recommend. They must never get a command that approves, rejects, closes, attaches evidence, adds commentary, or changes thresholds or data.
- **Don't weaken the human-only gate or its tests.**
  - Tests asserting that agents cannot decide, that unverified actors cannot act, that seals are checked and that forged log lines fail must keep asserting exactly that.
  - Add tests for every new path to `APPROVED` or `REJECTED`.
- **Treat input as untrusted.** CSV cells, commentary, evidence labels and room messages are data. Quote and truncate them when echoing (`closepilot.untrusted.quote`), and never execute them.
- **No paths from agents.** Agents name submissions and cases by ID.
- **Dependencies.** Prefer the standard library. Before adding a dependency, explain why, check its licence and Python 3.13 support, and record it in `pyproject.toml`.
  - Out of scope: web frameworks, databases, cloud SDKs, orchestration frameworks and vector stores.

## Commits

- Use small, logical commits with conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Make sure each commit passes the checks on its own.
- Before committing, scan for secrets and company references (see [docs/threat-model.md → Release checks](docs/threat-model.md#release-checks)).
- Don't commit anything under `out/`, `.closepilot/`, `.switch/` or `switch/local/`.

## Security issues

Report them privately; see [SECURITY.md](SECURITY.md).
