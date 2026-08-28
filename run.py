r"""
Ceded engine — one-click folder runner.
========================================

1. Put your THREE input files in the  demo_input\  folder — one each for the
   gross / QS, the FAC, and the EB feed. Name them so the type is in the file
   name (e.g.  QS.xlsx  /  gross.xlsx ,  FAC.xlsx ,  EB.xlsx ). .xlsx or .csv.
2. Run:   python run.py
3. Get:   ONE Excel with every output on its own sheet, in  demo_output\

The lookup rules (reference data) are bundled with the code as CSV files in
reference_data/, so there is NOTHING to configure — no database, no network. The
engine only READS them; it never changes them. (If reference_data/ is missing,
it falls back to the workbook named by the HADRON_WORKBOOK env var.)
"""
from __future__ import annotations

import glob
import logging
import os
import sys
from pathlib import Path

# Windows consoles default to cp1252 and crash on unicode (e.g. the ✓). Print
# everything as UTF-8 and never let an unencodable character stop the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import pandas as pd

from ceded_platform import PipelineConfig, load_reference_data, run_cycle
from ceded_platform.reference import load_reference_data_local
from ceded_platform.report import build_report

IN_DIR = HERE / "demo_input"
OUT_DIR = HERE / "demo_output"
OUT_FILE = OUT_DIR / "ceded_output.xlsx"
REF_DIR = HERE / "reference_data"       # bundled local reference (CSV) — default
# Fallback reference workbook, used only if reference_data/ is missing. Override
# with the HADRON_WORKBOOK env var.
WORKBOOK_REF = os.environ.get(
    "HADRON_WORKBOOK",
    "As-Is and To-Be Process with sample calculations - Hadron X Donyati 2.xlsx")

FAC_MAP = {"Policy Eff Date": "Policy Effective Date",
           "Policy Exp Date": "Policy Expiration Date",
           "Fac Re Premium": "Premium", "Location State": "Risk State"}
EB_MAP = {"Current Policy Number": "Policy Number",
          "EB Premium": "Premium", "Principal State": "Risk State"}


def _classify(fname: str) -> str | None:
    """Which feed a file is, from its name (gross/QS, FAC, or EB)."""
    n = Path(fname).stem.lower()
    if "gross" in n or "subledger" in n or "quota" in n or "qs" in n:
        return "qs"
    if "fac" in n:
        return "fac"
    if "eb" in n:
        return "eb"
    return None


def _read_file(path: str) -> pd.DataFrame:
    p = Path(path)
    return pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_excel(p)


def _find_inputs() -> dict:
    """The three feed files in demo_input\\, keyed 'qs' / 'fac' / 'eb'."""
    IN_DIR.mkdir(exist_ok=True)
    files = [f for f in glob.glob(str(IN_DIR / "*.*"))
             if not Path(f).name.startswith("~")
             and Path(f).suffix.lower() in (".xlsx", ".xls", ".csv")]
    found: dict = {}
    for f in files:
        kind = _classify(f)
        if kind and kind not in found:
            found[kind] = f
    return found


def _remap(df: pd.DataFrame, mapping: dict, ptype: str) -> pd.DataFrame:
    if df.empty:
        return df
    ren = {s: d for s, d in mapping.items()
           if s in df.columns and d not in df.columns}
    df = df.rename(columns=ren)
    if "Premium Type" not in df.columns:
        df["Premium Type"] = ptype
    if "Trading Partner" not in df.columns:
        df["Trading Partner"] = "COR"        # default when the feed omits it
    return df


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    found = _find_inputs()
    if "qs" not in found:
        sys.exit(f"No gross/QS file found in {IN_DIR}\n"
                 "  (name your three files so the type is in the name, e.g. "
                 "QS.xlsx, FAC.xlsx, EB.xlsx)")
    qs = _read_file(found["qs"])
    fac = _remap(_read_file(found["fac"]) if "fac" in found else pd.DataFrame(),
                 FAC_MAP, "FAC")
    eb = _remap(_read_file(found["eb"]) if "eb" in found else pd.DataFrame(),
                EB_MAP, "EB")
    print("Input files:")
    for k in ("qs", "fac", "eb"):
        print(f"  {k.upper():<4} <- {Path(found[k]).name}" if k in found
              else f"  {k.upper():<4} <- (none found)")
    print(f"  rows: QS={len(qs):,}  FAC={len(fac):,}  EB={len(eb):,}")

    # reference tables: bundled local CSVs (default), else the workbook fallback
    if REF_DIR.exists() and any(REF_DIR.glob("*.csv")):
        ref = load_reference_data_local(REF_DIR)
        print("  reference: read from bundled local files (reference_data/)")
    else:
        ref = load_reference_data(WORKBOOK_REF)
        print("  reference: read from the workbook")

    cfg = PipelineConfig(for_month_ending="2026-05-31",
                         current_period="May'26", prior_period="April'26")
    res = run_cycle(eb, fac, qs,
                    pd.DataFrame(columns=["Ceded ID", "Accounting Period"]),
                    ref, cfg)

    # ONE presentation-ready Excel: Summary sheet + formatted detail sheets --- #
    OUT_DIR.mkdir(exist_ok=True)
    build_report(res, {"qs": qs, "fac": fac, "eb": eb}, OUT_FILE, cfg)
    print(f"\n  -> wrote the report to  {OUT_FILE}")
    print("     tabs: Summary, Movement, Journal Entry, Allocation, "
          "Reconciliations, ITD Pivot, Detail, Input QS/FAC/EB")

    print("\n" + ("ALL RECONCILIATIONS PASS ✓" if res.all_recons_pass
                  else "*** SOME RECONCILIATIONS FAILED ***"))


if __name__ == "__main__":
    main()
