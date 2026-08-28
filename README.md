# Ceded Reinsurance — Calculation Engine

A Python engine that implements the **TO-BE ceded (seeded) reinsurance process**.
It takes the monthly MGA premium (quota-share / FAC / EB), works out exactly how
much of each premium dollar is ceded to each reinsurer, produces the summarized
journal entry, and proves every step ties with built-in reconciliation controls.

---

## What's in the box

```
ceded_platform/          THE ENGINE
    config.py        tunables & constants (calc paths, group keys, AUM buckets, GL accounts)
    reference.py     load the reference tables (contracts, rates, panel) from the workbook
    steps.py         Steps 1-12: ingest, dates, grouping, THE OFFSET, enrich, merge
    engine_calc.py   Step 13 (assign contract + exclusion rules) + Step 14 (the calculation)
    closing.py       Steps 15-19: ITD pivot, prior month, movement, persist, reinsurer split
    journal.py       Step 20: the summarized journal entry (debits = credits)
    recon.py         the 9 reconciliation checks
    sqlstore.py      OPTIONAL: read reference from / write output to Supabase
    report.py        build the presentation Excel
    pipeline.py      run_cycle() — the 20-step orchestrator

run.py               THE RUNNER — 3 files in demo_input/  ->  one Excel in demo_output/
reference_data/      the reference tables, bundled as CSVs (the engine reads these)
demo_input/          sample QS / FAC / EB files so you can run it immediately
.env.example         optional: copy to .env for Supabase output-saving
requirements.txt     dependencies
```

---

## Quick start

```bash
pip install -r requirements.txt
python run.py
```

`run.py` reads the three files in `demo_input/`, runs all 20 steps, and writes
**one Excel** (`demo_output/ceded_output.xlsx`) with a Summary tab plus every
detail. If it finishes with `ALL RECONCILIATIONS PASS ✓`, the run is internally
consistent.

### Running with your own data
Put **three files** in `demo_input/`, named so the type is in the file name
(e.g. `QS.xlsx`, `FAC.xlsx`, `EB.xlsx`) — `.xlsx` or `.csv`. Then `python run.py`.

> The three feeds must be the **same program for the same month** — they are the
> same premium seen three ways (the gross book, and the FAC and EB pieces carved
> out of it). "Reconciliations pass" proves the maths is consistent, **not** that
> the three files are a matched month.

---

## Reference data — bundled locally (no setup needed)

The engine needs the reference tables (which contract cedes what, at what %, to
which reinsurer). These ship **with the code**, as CSV files in `reference_data/`.
The engine reads them straight from disk — **no database, no workbook, no network,
nothing to configure.** Clone and run.

The engine reads these files but never writes them, so a teammate can't
accidentally corrupt the reference. To update the reference after a rule change,
regenerate the CSVs from the source (see below) and commit them.

### Priority order
`run.py` picks the reference source in this order:
1. **`reference_data/` local CSVs** — the default (fast, always present)
2. **Supabase** — used only if you *delete* `reference_data/` and set `SUPABASE_DB_URL`
3. **The workbook** — used only if neither of the above is available (set `HADRON_WORKBOOK`)

### Optional: Supabase for saving outputs
Everything above is local. If you *also* want to write a run's **output** tables to
a Supabase database, copy `.env.example` to `.env`, paste your connection string,
and run with `--save-to-db`:
```bash
python run.py --save-to-db
```
The `.env` file is gitignored, and this only writes output tables — never the
reference.

### Regenerating `reference_data/` from the workbook
```bash
python -c "from ceded_platform import load_reference_data; \
from ceded_platform.reference import save_reference_data_local; \
save_reference_data_local(load_reference_data(r'PATH\to\workbook.xlsx'), 'reference_data')"
```

---

## Input columns each feed needs

**QS / subledger** (the gross book) — one row per ledger line:
`UMR, Trading Partner, Accounting Period, MGA Alias, Entry Type, accident_year,
Product Name, Line of Business, Class of Business, ASLOB Code, Risk Effective Date,
Risk State, UW year, Written, Commission, Receivable, Earned Premium,
Unearned Premium, DAC, Deferred Receivable, Direct Indemnity/AO/DCC/ULAE IBNR,
Direct ULAE Reserves`

**FAC / EB bordereaux** — one row per premium line:

| Column | Notes |
|---|---|
| Policy Effective Date, Policy Expiration Date | Excel serial, real date, or ISO text — all accepted |
| Premium | the premium amount |
| Accounting Period | e.g. `06, 2026` |
| Trading Partner | e.g. `COR` (defaults to `COR` if the feed omits it) |
| Risk State | mapped automatically from common variants |
| Premium Type | optional — defaults to `FAC` / `EB` by file |

MGA-specific column-name variants are mapped in `config.BDX_COLUMN_MAP`.

---

## What comes out — the Excel tabs

| Tab | What it is |
|---|---|
| **Summary** | the answer at a glance: ceded by program, split by reinsurer, and the 9-check validation |
| Movement | this month's change by program (Ceded ID) — what you book |
| Journal Entry | the summarized, GL-coded entry (debits = credits), NetSuite-shaped |
| Allocation | per program: seeded vs allocated across the reinsurer panel, with flags |
| Reconciliations | the 9 checks, expected vs actual — a human can verify the run |
| Detail | every row with all 15 ceded measures (the grain) |
| ITD Pivot | inception-to-date totals by program |
| Input QS / FAC / EB | the exact inputs used |

---

## The two things that make this hard

### 1. The offset (Step 5b)
FAC and EB premium **already sit inside the QS book**. For every FAC/EB row the
engine emits a mirror row with the premium **negated** and re-tagged to QS. The
negative rows remove that money from the quota-share cession; the positive rows
re-add it through the FAC/EB path at the right contract and %. Total premium is
unchanged, routing is corrected. The engine raises if the offsets don't net to
zero, and Recon 2 asserts it again.

### 2. Two calculation paths (Step 14)
`Calc Path` (QS vs FAC/EB) selects the formula set — the two use opposite sign
conventions. A single boolean mask picks every formula, so a row can never mix
conventions. Core measures:

| Measure | QS | FAC / EB |
|---|---|---|
| Ceded Written Premium | `Written × Ceded%` | `FAC Prem × Ceded%` |
| Ceded Commission | `CWP × CC% × −1` | `CWP × CC%` |
| Ceded Reinsurance Payable | `−(CWP + CC + CFC)` | `CWP − CC − CFC` |
| Ceded IBNR (Indemnity/A&O/DCC) | `−Direct × Ceded%` | `factor × earned_base × Ceded%` |

`earned_base = Days Earned / Policy Days × FAC Prem`. IBNR factors come from the
reference tables keyed on Trading Partner + Calc Path.

---

## The reconciliations — your month-end control panel

All nine must say **PASS**:

| Recon | Proves |
|---|---|
| 1 | merged premium = EB + FAC — no bordereau row lost in the merge |
| 2 | offsets net to zero — quota share correctly derived from gross |
| 3 | grouped premium ties back to the raw feeds |
| 4 / 6 | the FAC/EB carve-out is reversible (`gross-ex-FAC + FAC = gross`) |
| 5 | FAC premium survived the merge |
| 7 | Step-14 cession %s are internally consistent |
| **8** | by-reinsurer allocations tie to the program total; no reinsurer silently dropped |
| **9** | the journal entry balances — total debits = total credits, every program |

The engine reproduces the workbook sample to the penny (`CX0COR = 47,620.9802`).

---

## Open items for the business/actuarial team

These are kept **workbook-faithful and flagged** — not silently decided:

1. **Exclusion routing for excluded rows** — where an excluded line re-cedes (a
   same-partner sibling contract) needs confirmation once the bolt-on feed exists.
2. **`TERM_LIMIT` rules need a `Policy Limit` column** on the feeds to bind.
3. **XOL / surplus-share calc paths** are future — the `Calc Path` switch is built
   to take them, but their formulas aren't defined yet.
4. **Journal Dr/Cr convention & GAAP reclass** — mapped from the workbook's account
   tables but not yet tied to a real Hadron NetSuite journal; validate on one real month.
5. **Settlement / collateral matching** by reinsurer + period needs the T10 master
   to bridge panel names to collateral names.

---

## A note on naming

The code calls the gross book the **"subledger"** feed; the business calls it the
**"gross"** feed. They are the same thing — only the label differs.
