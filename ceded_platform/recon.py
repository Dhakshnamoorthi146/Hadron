"""Reconciliation controls ('TO BE Reconciliation Steps') + the POC hard-failure
gates (Hadron POC Requirements Document, section 8.1).

Every control is programmatic and returns expected/actual/variance so the
month-end run leaves an audit trail.

  R1  A = B + C          the merge lost no premium (transcript standup1:295-330)
  R2  offset nets to 0   quota share correctly derived from gross
  R3  grouped ties raw   step-5 grouping preserved premium
  R4/R6 written split     FAC carved out of gross and reversible
  R5  FAC carried         FAC premium survived the merge
  R7  cession grossed up  step-14 QS cession % are internally consistent
  R8  allocation ties     sum of by-reinsurer allocations = program seeded total
                          × panel-share-sum, per Ceded ID  (POC hard gate #1,
                          FR-ALLOC-3 / FR-VAL-1) — catches leakage / dropped
                          reinsurers in step 19.

Panel completeness and dropped-reinsurer detection (POC hard gate #3) are
surfaced separately via `allocation_audit()` as an audit frame + flag list,
because a program legitimately retains the un-ceded remainder.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import (CALC_PATH_QS, PIVOT_MEASURES, SOURCE_BDX, SOURCE_OFFSET,
                     SOURCE_SUBLEDGER, PipelineConfig)


@dataclass
class ReconResult:
    name: str
    expected: float
    actual: float
    passed: bool
    note: str = ""

    @property
    def variance(self) -> float:
        return self.actual - self.expected


def _premium_sum(df: pd.DataFrame, col: str = "Premium") -> float:
    if df is None or df.empty or col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0.0).sum())


def run_recons(eb_bdx: pd.DataFrame, fac_bdx: pd.DataFrame,
               raw_bdx: pd.DataFrame, grouped_with_offsets: pd.DataFrame,
               merged: pd.DataFrame, fact: pd.DataFrame,
               movement: pd.DataFrame, books: dict,
               ref, cfg: PipelineConfig) -> list[ReconResult]:
    tol = cfg.recon_tolerance
    out: list[ReconResult] = []
    raw_total = _premium_sum(raw_bdx)

    # R1 - A = B + C : merged premium == EB premium + FAC premium, computed from
    # the two INDEPENDENT raw feeds (catches rows lost in the step-1/2 merge).
    b_eb = _premium_sum(eb_bdx)
    c_fac = _premium_sum(fac_bdx)
    out.append(ReconResult(
        "Recon 1: merged premium = EB + FAC (A = B + C)",
        b_eb + c_fac, raw_total, abs(raw_total - (b_eb + c_fac)) <= tol,
        f"B(EB)={b_eb:.4f} + C(FAC)={c_fac:.4f}; no rows lost in the merge."))

    # R2 - THE OFFSET INVARIANT: grouped positives + offsets net to zero
    g = grouped_with_offsets
    net = _premium_sum(g)
    out.append(ReconResult("Recon 2: offsets net grouped premium to zero",
                           0.0, net, abs(net) <= tol,
                           "Every FAC/EB dollar re-routed, none duplicated."))

    # R3 - grouped positives tie back to raw
    pos = _premium_sum(g.loc[g["source"] == SOURCE_BDX])
    out.append(ReconResult("Recon 3: grouped premium ties to raw",
                           raw_total, pos, abs(pos - raw_total) <= tol,
                           "Step 5 grouping preserves total premium."))

    # R4 - gross written excluding FAC = subledger written + negative offsets
    gross_ex_fac = float(pd.to_numeric(merged["Written"], errors="coerce")
                         .fillna(0.0).sum())
    subledger_written = float(pd.to_numeric(
        merged.loc[merged["source"] == SOURCE_SUBLEDGER, "Written"],
        errors="coerce").fillna(0.0).sum())
    offset_written = float(pd.to_numeric(
        merged.loc[merged["source"] == SOURCE_OFFSET, "Written"],
        errors="coerce").fillna(0.0).sum())
    out.append(ReconResult("Recon 4: gross written excl FAC = subledger + offsets",
                           subledger_written + offset_written, gross_ex_fac,
                           abs(gross_ex_fac - (subledger_written
                                               + offset_written)) <= tol))

    # R5 - FAC premium carried intact through 5 -> 8 -> 12
    fac_total = float(pd.to_numeric(merged["FAC Premium"], errors="coerce")
                      .fillna(0.0).sum())
    out.append(ReconResult("Recon 5: FAC premium carried through the merge",
                           raw_total, fac_total,
                           abs(fac_total - raw_total) <= tol))

    # R6 - reassembly: gross-ex-FAC + FAC = original subledger written total
    out.append(ReconResult("Recon 6: gross + FAC reconstructs written",
                           subledger_written, gross_ex_fac + fac_total,
                           abs(gross_ex_fac + fac_total
                               - subledger_written) <= tol,
                           "The FAC split is reversible."))

    # R7 - gross ceded written back up to 100% cession = written (QS rows)
    qs = fact[(fact["Calc Path"] == CALC_PATH_QS) & fact["Ceded ID"].notna()
              & (pd.to_numeric(fact["Ceded Percentage"],
                               errors="coerce").fillna(0) > 0)]
    grossed = float((qs["Ceded Written Premium"]
                     / qs["Ceded Percentage"]).sum())
    qs_written = float(qs["Written"].sum())
    out.append(ReconResult("Recon 7: ceded written grossed to 100% = written",
                           qs_written, grossed,
                           abs(grossed - qs_written) <= tol,
                           "Validates Step 14 cession percentages."))

    # R8 - ALLOCATION ARITHMETIC INTEGRITY (POC hard gate #1): the sum of every
    # by-reinsurer allocation must equal the program seeded total scaled by the
    # panel share-sum, for each measure — proves step 19 dropped/leaked nothing.
    panel = getattr(ref, "reinsurer_panel", None)
    audit = allocation_audit(movement, books, panel=panel)
    if audit.empty:
        out.append(ReconResult("Recon 8: allocation ties to program total",
                               0.0, 0.0, True, "No allocations produced."))
    else:
        exp = float(audit["expected_alloc"].sum())
        act = float(audit["allocated"].sum())
        n_missing = int(audit["missing_reinsurers"].apply(len).sum())
        note = "Sum of reinsurer allocations = seeded x panel share, per Ceded ID."
        if n_missing:
            note += f"  FLAG: {n_missing} panel reinsurer(s) missing from output."
        out.append(ReconResult(
            "Recon 8: allocation ties to program total",
            exp, act, abs(act - exp) <= tol and n_missing == 0, note))
    return out


def allocation_audit(movement: pd.DataFrame, books: dict,
                     panel: pd.DataFrame = None,
                     measure: str = "Ceded Written Premium") -> pd.DataFrame:
    """Per-Ceded-ID allocation control frame (POC hard gates #1 and #3).

    Columns: Ceded ID, seeded (program total for `measure`), share_sum
    (sum of panel Ceded % for that program), expected_alloc (=seeded*share_sum),
    allocated (sum of the by-reinsurer allocations actually produced), variance,
    n_reinsurers, panel_complete (share_sum ~= 1.0), missing_reinsurers (panel
    reinsurers absent from the output books).

    `panel` (ref.reinsurer_panel) is used to detect reinsurers that the panel
    lists for a program but that never reached the output (POC hard gate #3)."""
    # Only short-circuit when there is genuinely nothing seeded. If movement HAS
    # seeded premium but `books` is empty (e.g. step 19's panel join dropped
    # everything on a Ceded ID label mismatch), we must NOT return empty — that
    # would let Recon 8 pass while 100% of the premium was allocated to nobody.
    if movement is None or movement.empty:
        return pd.DataFrame()
    books = books or {}

    # panel reinsurers + intended share-sum expected per Ceded ID (the authority
    # for dropped-reinsurer detection and the expected allocation total)
    expected_by_cid: dict = {}
    panel_share_by_cid: dict = {}
    if panel is not None and not panel.empty and "Ceded ID" in panel.columns:
        share_col = "Ceded %" if "Ceded %" in panel.columns else None
        for cid, grp in panel.groupby("Ceded ID", dropna=False):
            if "Reinsurer" in grp.columns:
                expected_by_cid[cid] = set(grp["Reinsurer"].dropna().astype(str))
            panel_share_by_cid[cid] = (
                float(pd.to_numeric(grp[share_col], errors="coerce").fillna(0.0)
                      .sum()) if share_col else np.nan)

    ralloc = f"Reinsurer {measure}"
    seeded = (movement.groupby("Ceded ID", dropna=False)[measure].sum()
              if measure in movement.columns else pd.Series(dtype=float))

    # collect every (Ceded ID, Reinsurer, share, allocated) produced by step 19
    rows = []
    for reinsurer, book in books.items():
        if "Ceded ID" not in book.columns:
            continue
        share_col = "Ceded %" if "Ceded %" in book.columns else None
        for cid, grp in book.groupby("Ceded ID", dropna=False):
            rows.append({
                "Ceded ID": cid, "Reinsurer": reinsurer,
                "share": float(pd.to_numeric(grp[share_col], errors="coerce")
                               .fillna(0.0).sum()) if share_col else np.nan,
                "allocated": float(pd.to_numeric(grp.get(ralloc), errors="coerce")
                                   .fillna(0.0).sum()) if ralloc in grp else 0.0,
            })
    prod = pd.DataFrame(rows)

    out = []
    cids = sorted(set(seeded.index) | set(prod["Ceded ID"]) if not prod.empty
                  else set(seeded.index))
    for cid in cids:
        s = float(seeded.get(cid, 0.0))
        sub = prod[prod["Ceded ID"] == cid] if not prod.empty else prod
        allocated = float(sub["allocated"].sum()) if not sub.empty else 0.0
        produced = set(sub["Reinsurer"].astype(str)) if not sub.empty else set()
        missing = sorted(expected_by_cid.get(cid, set()) - produced)
        # expected share-sum is the PANEL's, not the produced books' — so a
        # dropped reinsurer makes `allocated` fall short of `expected_alloc`
        # (if no panel is supplied, fall back to the produced share-sum).
        share_sum = panel_share_by_cid.get(
            cid, float(sub["share"].sum()) if not sub.empty else 0.0)
        if share_sum != share_sum:                     # NaN -> use produced
            share_sum = float(sub["share"].sum()) if not sub.empty else 0.0
        out.append({
            "Ceded ID": cid, "seeded": s, "share_sum": share_sum,
            "expected_alloc": s * share_sum, "allocated": allocated,
            "variance": allocated - s * share_sum,
            "n_reinsurers": int(len(sub)),
            "panel_complete": abs(share_sum - 1.0) <= 1e-6,
            "missing_reinsurers": missing,
        })
    return pd.DataFrame(out)
