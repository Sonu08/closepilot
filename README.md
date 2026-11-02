# ClosePilot: AI Finance Control Room

ClosePilot is a collaborative AI finance-control room where agents validate and investigate synthetic close exceptions, while humans retain final approval authority.

> **ClosePilot does not allow an AI agent to approve material finance exceptions.** Agents calculate, investigate and recommend; a human reviewer makes and records the final decision.

**Status:** work in progress (Phase 2 — full deterministic control engine C01–C07). All data is synthetic and describes the fictional *Northstar Logistics Group*.

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

## Licence

MIT — see [LICENSE](LICENSE).
