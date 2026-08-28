"""Steps 1-12: ingest, date drivers, grouping, THE OFFSET, enrichment, merge.

The offset (step 5b) is the load-bearing wall of the whole design:
FAC/EB premium already sits inside the QS feed, so for every grouped FAC/EB row
we emit a mirror row with Premium Type re-tagged to 'QS' and Premium negated.
Net effect: total premium unchanged, routing corrected. Verified against the
sample sheet (rows 22-27: J25=-J22, J26=-J23, J27=-J24, B25:B27='QS',
J28=SUM=0 which is Reconciliation step 2).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import (AUM_BUCKETS, BDX_COLUMN_MAP, CALC_PATH_EB, CALC_PATH_FAC,
                     CALC_PATH_QS, FAC_EB_PATHS, SOURCE_BDX, SOURCE_OFFSET,
                     SOURCE_SUBLEDGER, STEP8_ZERO_COLUMNS, PipelineConfig)
from .reference import ReferenceData, excel_serial_to_date, to_ts

log = logging.getLogger("ceded_platform")


# ---- Steps 1-2: ingest + merge the bordereaux ------------------------------ #

def step1_2_ingest_bdx(eb_bdx: pd.DataFrame, fac_bdx: pd.DataFrame) -> pd.DataFrame:
    """Map MGA column variants to canonical names and stack EB + FAC."""
    frames = []
    for df, default_type in ((eb_bdx, CALC_PATH_EB), (fac_bdx, CALC_PATH_FAC)):
        f = df.copy()
        # map variant names, but never create duplicate columns
        ren = {s: d for s, d in BDX_COLUMN_MAP.items()
               if s in f.columns and d not in f.columns}
        drop = [s for s, d in BDX_COLUMN_MAP.items()
                if s in f.columns and d in f.columns]
        f = f.rename(columns=ren).drop(columns=drop)
        if "Premium Type" not in f.columns:
            f["Premium Type"] = default_type
        frames.append(f)
    out = pd.concat(frames, ignore_index=True, sort=False)
    log.info("Step 1-2: merged BDX rows=%d", len(out))
    return out


# ---- Steps 3-4: date drivers + accident year ------------------------------- #

def step3_4_dates(df: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    """Policy Days = Exp - Eff; Days Earned = FME - Eff; Accident Year."""
    out = df.copy()
    eff = to_ts(out["Policy Effective Date"])
    exp = to_ts(out["Policy Expiration Date"])
    fme = pd.Timestamp(excel_serial_to_date(cfg.for_month_ending))
    out["Policy Days"] = (exp - eff).dt.days
    out["Days Earned"] = (fme - eff).dt.days
    if "For Month Ending" not in out.columns:
        out["For Month Ending"] = fme
    if "Accident Year" not in out.columns:
        out["Accident Year"] = eff.dt.year
    return out


# ---- Step 5: group + sum --------------------------------------------------- #

def step5_group(df: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    keys = [k for k in cfg.group_keys if k in df.columns]
    grouped = df.groupby(keys, dropna=False, as_index=False)["Premium"].sum()
    grouped["source"] = SOURCE_BDX
    log.info("Step 5: %d raw rows -> %d grouped rows", len(df), len(grouped))
    return grouped


# ---- Step 5b: THE OFFSET ---------------------------------------------------- #

def step5b_offsets(grouped: pd.DataFrame) -> pd.DataFrame:
    """Mirror every grouped FAC/EB row: Premium Type -> 'QS', Premium -> -Premium.

    Returns grouped + offsets. sum(Premium) over the result is 0 by
    construction (Recon 2)."""
    offsets = grouped.copy()
    offsets["Premium"] = -offsets["Premium"]
    offsets["Premium Type"] = CALC_PATH_QS
    offsets["source"] = SOURCE_OFFSET
    out = pd.concat([grouped, offsets], ignore_index=True)
    net = out["Premium"].sum()
    if abs(net) > 1e-6:
        raise AssertionError(f"Offset invariant broken: net premium {net!r} != 0")
    log.info("Step 5b: emitted %d offset rows (net premium = 0)", len(offsets))
    return out


# ---- Steps 6-8: reference joins, deal breakout, zeroed direct columns ------- #

def deal_breakout_scalar(trading_partner, umr, risk_effective_date):
    if trading_partner != "AUM":
        return umr
    d = excel_serial_to_date(risk_effective_date)
    if d is None:
        return umr
    for upper, label in AUM_BUCKETS:
        if d < upper:
            return label
    return umr


def _join_lob_attrs(df: pd.DataFrame, ref: ReferenceData) -> pd.DataFrame:
    """Step 6: T2 attrs by (Trading Partner, Calc Path); fall back to the first
    T2 row for the partner (workbook XLOOKUP behaviour) for QS/offset rows."""
    t2 = ref.fac_lob_map.rename(columns={
        "Entry type": "Entry Type", "UW Year": "UW year"})
    attr_cols = ["UMR", "MGA Alias", "Entry Type", "Product Name",
                 "Line of Business", "Class of Business", "ASLOB Code",
                 "UW year"]
    keep = [c for c in attr_cols if c in t2.columns]
    exact = t2.drop_duplicates(["Trading Partner", "Calc Path"])[
        ["Trading Partner", "Calc Path"] + keep]
    first = t2.drop_duplicates("Trading Partner")[["Trading Partner"] + keep]

    out = df.merge(exact, how="left",
                   left_on=["Trading Partner", "Premium Type"],
                   right_on=["Trading Partner", "Calc Path"]).drop(
                       columns=["Calc Path"])
    fb = df[["Trading Partner"]].merge(first, how="left", on="Trading Partner")
    for c in keep:
        out[c] = out[c].where(out[c].notna(), fb[c].values)
    return out


def step6_8_enrich(grouped_plus_offsets: pd.DataFrame,
                   ref: ReferenceData) -> pd.DataFrame:
    g = grouped_plus_offsets
    out = _join_lob_attrs(g, ref)

    # Step 7: Calc Path = Premium Type; Deal Breakout; carry the date drivers
    out["Calc Path"] = out["Premium Type"]
    out["Risk Effective Date"] = to_ts(out["Policy Effective Date"])
    out["accident_year"] = out["Accident Year"]
    out["Deal Breakout"] = [
        deal_breakout_scalar(tp, u, d) for tp, u, d in
        zip(out["Trading Partner"], out["UMR"], out["Risk Effective Date"])]

    # Step 8: Written carries QS premium (offsets land here as negatives);
    # FAC Premium carries EB/FAC premium; direct columns hardcoded to 0.
    is_qs = out["Calc Path"].eq(CALC_PATH_QS)
    out["Written"] = np.where(is_qs, out["Premium"], 0.0)
    out["FAC Premium"] = np.where(is_qs, 0.0, out["Premium"])
    for c in STEP8_ZERO_COLUMNS:
        out[c] = 0.0
    log.info("Step 6-8: enriched %d rows", len(out))
    return out


# ---- Steps 9-11: subledger preparation -------------------------------------- #

def step9_11_subledger(subledger: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    out = subledger.copy()
    out["Risk Effective Date"] = to_ts(out["Risk Effective Date"])
    out["Deal Breakout"] = [
        deal_breakout_scalar(tp, u, d) for tp, u, d in
        zip(out["Trading Partner"], out["UMR"], out["Risk Effective Date"])]
    out["Policy Days"] = np.nan
    out["Days Earned"] = np.nan
    out["Calc Path"] = CALC_PATH_QS
    out["For Month Ending"] = pd.Timestamp(
        excel_serial_to_date(cfg.for_month_ending))
    out["FAC Premium"] = 0.0
    out["source"] = SOURCE_SUBLEDGER
    log.info("Step 9-11: subledger rows=%d", len(out))
    return out


# ---- Step 12: merge ---------------------------------------------------------- #

NUMERIC_MERGE_COLS = ["Written", "FAC Premium"] + STEP8_ZERO_COLUMNS


def step12_merge(subledger_prepared: pd.DataFrame,
                 bdx_prepared: pd.DataFrame) -> pd.DataFrame:
    merged = pd.concat([subledger_prepared, bdx_prepared],
                       ignore_index=True, sort=False)
    for c in NUMERIC_MERGE_COLS:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0.0)
    # Normalize Entry Type casing for a clean output — the subledger feed writes
    # 'actual'/'accrual' while the reference join writes 'Actual'. Only this
    # column (it's a display label, never used in matching, and has no acronyms).
    if "Entry Type" in merged.columns:
        merged["Entry Type"] = merged["Entry Type"].apply(
            lambda v: str(v).title() if pd.notna(v) and str(v).strip() else v)
    # Normalize date columns to one real date — one feed carries Excel serials
    # (46173) while the other carries datetimes; make them consistent.
    for c in ("For Month Ending", "Risk Effective Date",
              "Policy Effective Date", "Policy Expiration Date"):
        if c in merged.columns:
            merged[c] = to_ts(merged[c])
    merged["row_id"] = np.arange(len(merged))   # audit key
    log.info("Step 12: merged rows=%d", len(merged))
    return merged
