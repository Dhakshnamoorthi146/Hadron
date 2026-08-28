r"""
Ceded engine — one-click folder runner.
========================================

1. Put your THREE input files in the  demo_input\  folder — one each for the
   gross / QS, the FAC, and the EB feed. Name them so the type is in the file
   name (e.g.  QS.xlsx  /  gross.xlsx ,  FAC.xlsx ,  EB.xlsx ). .xlsx or .csv.
2. Run:   python run.py
3. Get:   ONE Excel with every output on its own sheet, in  demo_output\

Reference data (the lookup rules) is READ from Supabase when SUPABASE_DB_URL is
set, otherwise from the workbook. Either way the engine only READS the reference
— it can never change it. Put your Supabase URL in a  .env  file next to this
script (see .env.example) and it's picked up automatically.

By default the run writes ONLY the local Excel. Add  --save-to-db  if you also
want to write the run's OUTPUT tables back to Supabase (off by default so people
sharing one database don't overwrite each other).
"""
from __future__ import annotations

import argparse
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
from ceded_platform.sqlstore import (load_reference_data_sql, make_engine,
                                     save_outputs)

IN_DIR = HERE / "demo_input"
OUT_DIR = HERE / "demo_output"
OUT_FILE = OUT_DIR / "ceded_output.xlsx"
REF_DIR = HERE / "reference_data"       # bundled local reference (CSV) — default
# Reference workbook used only when SUPABASE_DB_URL is not set. Override with the
# HADRON_WORKBOOK env var; defaults to a copy sitting next to this script.
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


def _excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Excel can't write list/dict cells (e.g. missing_reinsurers) — stringify."""
    if df.empty:
        return df
    out = df.copy()
    for c in out.columns:
        if out[c].map(lambda v: isinstance(v, (list, dict))).any():
            out[c] = out[c].astype(str)
    return out


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (no dependency): set KEY=VALUE lines into the
    environment if not already set. Lets a teammate paste the Supabase URL into a
    .env file instead of fiddling with shell environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(
        description="Run one ceded cycle from the three files in demo_input/.")
    ap.add_argument("--save-to-db", action="store_true",
                    help="also write this run's OUTPUT tables back to Supabase "
                         "(off by default so shared-DB users don't overwrite "
                         "each other; never touches the reference tables)")
    args = ap.parse_args()

    _load_dotenv(HERE / ".env")
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

    # reference tables — priority: bundled local CSVs (fast, no network) ->
    # Supabase (if SUPABASE_DB_URL set) -> the workbook. An engine is still made
    # when a URL is present so --save-to-db can write outputs.
    url = os.environ.get("SUPABASE_DB_URL")
    engine = make_engine(url) if url else None
    if REF_DIR.exists() and any(REF_DIR.glob("*.csv")):
        ref = load_reference_data_local(REF_DIR)
        print("  reference: read from bundled local files (reference_data/)")
    elif engine is not None:
        ref = load_reference_data_sql(engine)
        print("  reference: read from Supabase")
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

    # optionally write the OUTPUT tables back to Supabase (never the reference) - #
    if engine is not None and args.save_to_db:
        saved = save_outputs(res, engine)
        print(f"  -> saved outputs into Supabase: {', '.join(saved)}")
    elif engine is not None:
        print("  -> outputs written to the local Excel only "
              "(add --save-to-db to also write them to Supabase)")

    print("\n" + ("ALL RECONCILIATIONS PASS ✓" if res.all_recons_pass
                  else "*** SOME RECONCILIATIONS FAILED ***"))


if __name__ == "__main__":
    main()
