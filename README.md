# ClosePilot: AI Finance Control Room

ClosePilot is a collaborative AI finance-control room where agents validate and investigate synthetic close exceptions, while humans retain final approval authority.

> **ClosePilot does not allow an AI agent to approve material finance exceptions.** Agents calculate, investigate and recommend; a human reviewer makes and records the final decision.

**Status:** work in progress (Phase 1 — deterministic controls C01 and the numeric part of C07). All data is synthetic and describes the fictional *Northstar Logistics Group*.

## Quick start (development)

```bash
conda activate closepilot          # Python 3.13
python -m pip install -e ".[dev]"
python -m pytest
closepilot validate tests/fixtures/c01_package.csv
```

## Calculation rules (implemented so far)

- `variance = actual - forecast` (exact `Decimal` arithmetic)
- `variance % = variance / |forecast| × 100`, rounded half-up to 2 dp; thresholds compare the rounded value
- Zero forecast: both zero → `0.00%`; non-zero actual → percentage undefined (`null`) and `REVIEW_REQUIRED`
- **C01 Material variance:** `|variance %| > variance_review_pct` (config: `config/controls.yaml`) → `REVIEW_REQUIRED`
- **C07 Required-field integrity (numeric part):** a missing or malformed `actual` / `forecast` (e.g. `1,000.00`, `1e5`, `NaN`, `=1+1`), or a row whose field count does not match the header → `ERROR`. Such a record is never `PASS`. Checks for missing text fields follow in Phase 2.
- **Severity precedence:** `ERROR` > `REVIEW_REQUIRED` > `WARNING` > `PASS`; every individual finding stays visible.

## Licence

MIT — see [LICENSE](LICENSE).
