"""Reference tables (T1-T10) + loaders from the source workbook.

In production these become SQL reads against ref_loss_ratios, ref_fac_lob_map,
ref_ceded_id_map, ref_exclusion_rules, ref_contract_excl, ref_reinsurer_panel,
ref_settlement, ref_collateral, ref_fet_payable (see 'Tables to be created in
SQL'). The DataFrame column names are the workbook headers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

EXCEL_EPOCH = date(1899, 12, 30)


def excel_serial_to_date(v) -> date | None:
    """Excel serial / datetime / ISO string -> date (None-safe)."""
    # isna FIRST — pd.NaT is an instance of datetime, so it must be caught
    # before the datetime branch (a bare NaT would otherwise crash the AUM
    # deal-breakout on any row with a missing effective date).
    if isinstance(v, str):
        return pd.to_datetime(v).date() if v.strip() else None
    if pd.isna(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return EXCEL_EPOCH + timedelta(days=int(v))


def to_ts(series: pd.Series) -> pd.Series:
    """Vector version -> pandas Timestamps (NaT-safe) for fast comparison."""
    return pd.to_datetime(series.map(excel_serial_to_date))


def num(x, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(v) else v


@dataclass
class ReferenceData:
    loss_ratios: pd.DataFrame          # T1: IBNR factors by TP + Calc Path
    fac_lob_map: pd.DataFrame          # T2: LOB/product attrs by TP + Calc Path
    ceded_id_map: pd.DataFrame         # T3: contracts (Ceded ID, %, dates...)
    exclusion_rules: pd.DataFrame      # T4: rule definitions
    contract_exclusions: pd.DataFrame  # T5: contract_id -> rule instances
    reinsurer_panel: pd.DataFrame      # T6: Ceded ID x Reinsurer shares
    settlements: pd.DataFrame          # T7
    collateral: pd.DataFrame           # T8
    fet_payable: pd.DataFrame          # T9
    reinsurer_master: pd.DataFrame = field(default_factory=pd.DataFrame)  # T10
    gaap_account_codes: pd.DataFrame = field(default_factory=pd.DataFrame)  # FAC Account Codes
    gaap_reclass: pd.DataFrame = field(default_factory=pd.DataFrame)        # FAC GAAPReClass

    def __post_init__(self) -> None:
        cim = self.ceded_id_map
        for col in ("Effective Date", "Expiration Date"):
            if col in cim.columns:
                cim[col] = to_ts(cim[col])
        for col in ("Ceded Percentage", "Fronting Commission %",
                    "Ceded Commission %", "Brokerage Commission %",
                    "ULAE IBNR Cession", "ULAE Reserves Cession"):
            if col in cim.columns:
                cim[col] = cim[col].map(num)

    def ibnr_factor_frame(self) -> pd.DataFrame:
        """T1 keyed on (Trading Partner, Calc Path) -> factor columns."""
        lr = self.loss_ratios
        out = lr[["Trading Partner", "Calc Path"]].copy()
        for src, dst in [("Indemnity", "f_indemnity"), ("A&O", "f_ao"),
                         ("DCC", "f_dcc"), ("ULAE", "f_ulae")]:
            out[dst] = lr[src].map(num) if src in lr.columns else 0.0
        return out.drop_duplicates(["Trading Partner", "Calc Path"])


# --------------------------------------------------------------------------- #
# Local bundled reference (CSV) — no database, no workbook needed              #
# --------------------------------------------------------------------------- #

# The 12 reference tables, in ReferenceData constructor order.
REFERENCE_FIELDS = (
    "loss_ratios", "fac_lob_map", "ceded_id_map", "exclusion_rules",
    "contract_exclusions", "reinsurer_panel", "settlements", "collateral",
    "fet_payable", "reinsurer_master", "gaap_account_codes", "gaap_reclass",
)


def save_reference_data_local(ref: "ReferenceData", out_dir) -> dict:
    """Dump every reference table to <out_dir>/<field>.csv so the engine can run
    from bundled local files — no Supabase, no workbook. Returns {file: rows}."""
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts: dict = {}
    for name in REFERENCE_FIELDS:
        df = getattr(ref, name, None)
        df = pd.DataFrame() if df is None else df
        df.to_csv(out / f"{name}.csv", index=False)
        counts[f"{name}.csv"] = len(df)
    return counts


def load_reference_data_local(in_dir) -> "ReferenceData":
    """Read the bundled reference CSVs from <in_dir> back into ReferenceData.
    Missing files load as empty frames (the engine handles that). Dates/numbers
    are re-coerced by ReferenceData.__post_init__, so CSV round-tripping is safe."""
    from pathlib import Path
    d = Path(in_dir)

    def rd(name: str) -> pd.DataFrame:
        p = d / f"{name}.csv"
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    return ReferenceData(**{name: rd(name) for name in REFERENCE_FIELDS})


# --------------------------------------------------------------------------- #
# Workbook loaders                                                             #
# --------------------------------------------------------------------------- #

SHEETS = {
    "loss_ratios":         ("TO BE FAC References", 2, 11),
    "fac_lob_map":         ("TO BE FAC References", 9, 11),
    "ceded_id_map":        ("TO BE Ceded ID mapping", 2, 27),
    "exclusion_rules":     ("TO BE Exclusion Rules", 2, 7),
    "contract_exclusions": ("TO BE Exclusion Rules", 27, 10),  # T5 header is row 27
    "reinsurer_panel":     ("TO BE Reinsurance Panel", 2, 17),
    "reinsurer_master":    ("TO-BE Reinsurer Master", 2, 15),
}


def _read_block(path: str, sheet: str, header_row: int, ncols: int) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hdr = raw.iloc[header_row - 1, :ncols].tolist()
    body = raw.iloc[header_row:, :ncols].copy()
    body.columns = [str(h).strip() if pd.notna(h) else f"col{i}"
                    for i, h in enumerate(hdr)]
    return body.dropna(how="all").reset_index(drop=True)


def load_reference_data(path: str) -> ReferenceData:
    """Build ReferenceData straight from the source workbook (untested offsets:
    validate once against the real file — see README open items)."""
    loss_ratios = _read_block(path, *SHEETS["loss_ratios"])
    loss_ratios = loss_ratios[loss_ratios["Trading Partner"].notna()]

    fac_lob_map = _read_block(path, *SHEETS["fac_lob_map"])
    fac_lob_map = fac_lob_map[fac_lob_map["Trading Partner"].notna()]

    ceded_id_map = _read_block(path, *SHEETS["ceded_id_map"])
    ceded_id_map = ceded_id_map[ceded_id_map["Ceded ID"].notna()]

    exclusion_rules = _read_block(path, *SHEETS["exclusion_rules"])
    exclusion_rules = exclusion_rules[
        pd.to_numeric(exclusion_rules["rule_id"], errors="coerce").notna()]

    contract_exclusions = _read_block(path, *SHEETS["contract_exclusions"])
    contract_exclusions = contract_exclusions[
        contract_exclusions["contract_id"].notna()]

    panel = _read_block(path, *SHEETS["reinsurer_panel"])
    panel = panel.rename(columns={panel.columns[0]: "Ceded ID",
                                  panel.columns[1]: "Reinsurer ID"})
    panel = panel[panel["Ceded ID"].notna()]

    master = _read_block(path, *SHEETS["reinsurer_master"])
    # T10 col0 is the numeric surrogate key (reinsurer_id); the description row
    # under the header coerces to NaN and is dropped.
    master = master[pd.to_numeric(master.iloc[:, 0], errors="coerce").notna()]

    # 'Reinsurer'/'Period' headers repeat across the 3 side-by-side tables, so
    # pandas de-dups them to 'Reinsurer.1', 'Period.1' etc. Strip the suffix.
    def _dedup(df: pd.DataFrame) -> pd.DataFrame:
        df.columns = [re.sub(r"\.\d+$", "", str(c)) for c in df.columns]
        return df

    settlements = _dedup(pd.read_excel(path, sheet_name="Settlements",
                                       header=1, usecols="A:E").dropna(how="all"))
    collateral = _dedup(pd.read_excel(path, sheet_name="Settlements",
                                      header=1, usecols="I:M").dropna(how="all"))
    fet = _dedup(pd.read_excel(path, sheet_name="Settlements",
                               header=1, usecols="O:Q").dropna(how="all"))

    # GL mapping for the journal entry (FAC Account Codes / FAC GAAPReClass) —
    # header on row 1 (0-based), so header=0 reads it directly.
    def _opt(sheet, ncols):
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=0).dropna(how="all")
            return df.iloc[:, :ncols]
        except Exception:                       # sheet absent in some copies
            return pd.DataFrame()

    account_codes = _opt("FAC Account Codes", 6)
    reclass = _opt("FAC GAAPReClass", 9)

    return ReferenceData(loss_ratios, fac_lob_map, ceded_id_map,
                         exclusion_rules, contract_exclusions, panel,
                         settlements, collateral, fet, master,
                         gaap_account_codes=account_codes, gaap_reclass=reclass)
