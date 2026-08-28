"""Steps 13-14: ceded-contract assignment (with exclusion rules) and the
dual-path calculation engine. Both vectorized: step 13 loops over ~50 contracts
(not 400k rows); step 14 is pure numpy.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import (CALC_PATH_QS, CEDED_MEASURES, RULE_TYPE_COLUMNS,
                     SOURCE_SUBLEDGER, PipelineConfig)
from .reference import ReferenceData, num, to_ts

log = logging.getLogger("ceded_platform")

CONTRACT_ATTRS = {
    "Ceded ID": "Ceded ID",
    "Ceded Percentage": "Ceded Percentage",
    "Fronting Commission %": "Fronting Commission %",
    "Ceded Commission %": "Ceded Commission %",
    "Brokerage Commission %": "Brokerage Commission %",
    "Ceded UW Year": "Ceded UW Year",
    "ULAE IBNR Cession": "ULAE IBNR Cession",
    "ULAE Reserves Cession": "ULAE Reserves Cession",
    "Direct ID": "Direct ID",
}


# --------------------------------------------------------------------------- #
# Step 13                                                                      #
# --------------------------------------------------------------------------- #

def _exclusion_ok(rows: pd.DataFrame, contract_id: str, ref: ReferenceData,
                  cfg: PipelineConfig) -> np.ndarray:
    """Vector mask: True where a row survives the contract's T5 rules.

    Semantics (per T4/T5):
      exclusionary  (LOB '=', MGA 'NOT_IN', TERM_LIMIT '<' '>') -> all must pass
      inclusionary  (BOLT_ON, any 'IN')  -> row must match at least one value
                                            of each inclusive rule-type group
    Rules only bind inside their own effective window vs the row's Risk
    Effective Date. Exclusions apply to subledger rows always; to BDX/offset
    rows only when cfg.apply_exclusions_to_bdx (their LOB is a join artifact).

    SOURCE-PROGRAM SCOPING (per T5 `source_program`): each contract's rules are
    grouped by sub-program — the main quota-share program ("AUM", "CORE QS"), the
    bolt-on program ("... Bolt - On"), the EB/FAC programs, and "XOL". The BOLT_ON
    "include only these lines" whitelists live in the bolt-on program; applying
    them to a MAIN quota-share row wrongly excludes all normal business (Property,
    GL, ...). Since the current feeds carry only main-stream rows (no bolt-on/XOL
    rows), we drop bolt-on and XOL sub-program rules here. When bolt-on/XOL feeds
    are onboarded, route their rows to those rules explicitly.
    """
    n = len(rows)
    ok = np.ones(n, dtype=bool)
    ce = ref.contract_exclusions
    if ce.empty:
        return ok
    rules = ce[ce["contract_id"] == contract_id]
    if rules.empty:
        return ok
    if cfg.scope_out_bolton_xol and "source_program" in rules.columns:
        sp = rules["source_program"].astype(str).str.lower()
        rules = rules[~(sp.str.contains("bolt") | sp.str.contains("xol"))]
        if rules.empty:
            return ok

    subject = rows["source"].eq(SOURCE_SUBLEDGER).to_numpy() if not \
        cfg.apply_exclusions_to_bdx else np.ones(n, dtype=bool)
    red = to_ts(rows["Risk Effective Date"])

    # DATA-DRIVEN rule types (standup: "rule type can be any categorical column").
    # rule_type -> the input columns it tests; an unknown type falls back to a
    # column named after it. LOB/BOLT_ON test both Line of Business AND Class of
    # Business (subledger rows carry the excluded line in either field).
    def col_arrays(raw_rtype: str) -> list:
        cols = RULE_TYPE_COLUMNS.get(raw_rtype.upper(), (raw_rtype,))
        return [rows.get(c, pd.Series([None] * n)).to_numpy() for c in cols]

    # inclusive rules are grouped BY rule_type: a row must match >=1 value of
    # each inclusive group that applies to it, else it is excluded.
    inclusive: dict[str, np.ndarray] = {}
    for _, r in rules.iterrows():
        raw_rtype = str(r.get("rule_type", ""))
        rtype = raw_rtype.upper()
        op = str(r.get("comparison_op", "")).strip().upper()
        val = r.get("rule_value")

        applicable = subject.copy()
        f, t = r.get("effective_from"), r.get("effective_to")
        if pd.notna(f):
            applicable &= (red >= pd.Timestamp(to_ts(pd.Series([f]))[0])).to_numpy()
        if pd.notna(t):
            applicable &= (red <= pd.Timestamp(to_ts(pd.Series([t]))[0])).to_numpy()

        if op in ("<", ">"):                        # numeric threshold band
            numv = pd.to_numeric(pd.Series(col_arrays(raw_rtype)[0]),
                                 errors="coerce").to_numpy()
            has = ~np.isnan(numv)
            thr = num(val)
            keep = (numv > thr) if op == ">" else (numv < thr)
            ok &= ~(applicable & has & ~keep)
            continue

        matched = np.zeros(n, dtype=bool)
        for arr in col_arrays(raw_rtype):
            matched |= (arr == val)
        if op == "IN":                              # inclusive group (per type)
            inclusive[rtype] = inclusive.get(rtype, np.zeros(n, bool)) | matched
            inclusive.setdefault(rtype + "_app", np.zeros(n, bool))
            inclusive[rtype + "_app"] |= applicable
        else:                                       # '=' / 'NOT_IN' -> exclude match
            ok &= ~(applicable & matched)

    for key in [k for k in inclusive if not k.endswith("_app")]:
        ok &= ~inclusive[key + "_app"] | inclusive[key]
    return ok


def step13_assign(merged: pd.DataFrame, ref: ReferenceData,
                  cfg: PipelineConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assign each row a Ceded ID.

    Tier 1 (the workbook's Step 13): Trading Partner + Calc Path + date window +
    exclusion rules. A row that matches no contract stays UNASSIGNED — it is
    retained, or belongs to a program (bolt-on / XOL) not yet fed in.

    Tier 2 (OFF by default): a cross-partner fallback on Calc Path + date only.
    This mis-routes an excluded AUM row into a COR contract, so it is gated behind
    cfg.cross_partner_fallback and left off — cession must not cross partners.
    Returns (rows with contract attrs, exclusion log)."""
    df = merged.reset_index(drop=True).copy()
    n = len(df)
    red = to_ts(df["Risk Effective Date"])
    cp = df["Calc Path"].to_numpy()
    tp = df["Trading Partner"].to_numpy()

    assigned = np.full(n, -1)
    cim = ref.ceded_id_map.reset_index(drop=True)
    excl_hits: list[dict] = []

    tiers = (1, 2) if cfg.cross_partner_fallback else (1,)
    for tier in tiers:
        for ci, c in cim.iterrows():
            todo = assigned == -1
            if not todo.any():
                break
            m = todo & (cp == c["Premium Type"])
            if tier == 1:
                m &= tp == c["Trading Partner"]
            if pd.notna(c["Effective Date"]):
                m &= (red >= c["Effective Date"]).to_numpy()
            if pd.notna(c["Expiration Date"]):
                m &= (red <= c["Expiration Date"]).to_numpy()
            if not m.any():
                continue
            rules_ok = _exclusion_ok(df, c["Ceded ID"], ref, cfg)
            rejected = m & ~rules_ok
            for i in np.flatnonzero(rejected):
                excl_hits.append({
                    "row_id": df.at[i, "row_id"], "tier": tier,
                    "candidate_ceded_id": c["Ceded ID"],
                    "Trading Partner": tp[i], "Calc Path": cp[i],
                    "Line of Business": df.at[i, "Line of Business"]
                    if "Line of Business" in df.columns else None,
                })
            assigned = np.where(m & rules_ok, ci, assigned)

    hit = assigned >= 0
    for src, dst in CONTRACT_ATTRS.items():
        vals = cim[src].to_numpy()
        df[dst] = [vals[a] if a >= 0 else None for a in assigned]
    for c in ("Ceded Percentage", "Fronting Commission %", "Ceded Commission %",
              "Brokerage Commission %", "ULAE IBNR Cession",
              "ULAE Reserves Cession"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    log.info("Step 13: assigned %d/%d rows (%d exclusion-log entries)",
             int(hit.sum()), n, len(excl_hits))
    return df, pd.DataFrame(excl_hits)


# --------------------------------------------------------------------------- #
# Step 14                                                                      #
# --------------------------------------------------------------------------- #

def step14_calculate(df: pd.DataFrame, ref: ReferenceData) -> pd.DataFrame:
    """Dual-path ceded calc, fully vectorized. Sign conventions differ:
    QS flips commission/fronting/payable, EB/FAC does not (workbook
    'TO BE Formula based on CalcPath')."""
    out = df.reset_index(drop=True).copy()
    out = out.merge(ref.ibnr_factor_frame(), how="left",
                    on=["Trading Partner", "Calc Path"])
    for c in ("f_indemnity", "f_ao", "f_dcc", "f_ulae"):
        out[c] = out[c].fillna(0.0)

    has = out["Ceded ID"].notna().to_numpy()
    qs = out["Calc Path"].eq(CALC_PATH_QS).to_numpy() & has

    def g(c):
        if c not in out.columns:
            return np.zeros(len(out))
        return pd.to_numeric(out[c], errors="coerce").fillna(0.0).to_numpy()
    cpct, ccp, ffp, bkp = g("Ceded Percentage"), g("Ceded Commission %"), \
        g("Fronting Commission %"), g("Brokerage Commission %")
    u_ibnr, u_res = g("ULAE IBNR Cession"), g("ULAE Reserves Cession")
    written, ep, uep = g("Written"), g("Earned Premium"), g("Unearned Premium")
    fac, pdays, dearn = g("FAC Premium"), g("Policy Days"), g("Days Earned")

    # EB/FAC earned base & unearned fraction (Policy Days = 0 guard)
    with np.errstate(divide="ignore", invalid="ignore"):
        efrac = np.where(pdays == 0, 1.0, dearn / np.where(pdays == 0, 1, pdays))
        ufrac = np.where(pdays == 0, 0.0,
                         (pdays - dearn) / np.where(pdays == 0, 1, pdays))
    earned_base = fac * efrac

    m = {}
    # --- premiums
    m["Ceded Written Premium"] = np.where(qs, written * cpct, fac * cpct)
    cuep_fac = (fac - earned_base) * cpct
    m["Ceded Unearned Premium"] = np.where(qs, uep * cpct, cuep_fac)
    m["Ceded Earned Premium"] = np.where(
        qs, ep * cpct, m["Ceded Written Premium"] - cuep_fac)
    # --- commissions
    cc = np.where(qs, -m["Ceded Written Premium"] * ccp,
                  m["Ceded Written Premium"] * ccp)
    m["Ceded Commission"] = cc
    m["Ceded Commission DAC"] = np.where(
        qs, ccp * -m["Ceded Unearned Premium"], ufrac * cc)
    cfc = np.where(qs, -m["Ceded Written Premium"] * ffp,
                   m["Ceded Written Premium"] * ffp)
    m["Ceded Fronting Commission"] = cfc
    m["Ceded Fronting Fee DAC"] = np.where(
        qs, ffp * -m["Ceded Unearned Premium"], ufrac * cfc)
    m["Ceded Reinsurance Payable"] = np.where(
        qs, -(m["Ceded Written Premium"] + cc + cfc),
        m["Ceded Written Premium"] - cc - cfc)
    bf = np.where(qs, m["Ceded Written Premium"] * bkp, fac * bkp)
    m["Ceded Brokerage Fee"] = bf
    m["Ceded Brokerage Fee DAC"] = np.where(
        qs, bkp * m["Ceded Unearned Premium"], ufrac * bf)
    # --- IBNR / reserves
    m["Ceded Indemnity IBNR"] = np.where(
        qs, -g("Direct Idemnity IBNR") * cpct,
        g("f_indemnity") * earned_base * cpct)
    m["Ceded A&O IBNR"] = np.where(
        qs, -g("Direct AO IBNR") * cpct, g("f_ao") * earned_base * cpct)
    m["Ceded DCC IBNR"] = np.where(
        qs, -g("Direct DCC IBNR") * cpct, g("f_dcc") * earned_base * cpct)
    # NOTE: EB/FAC ULAE IBNR uses the ULAE cession %, not the ceded % (workbook)
    m["Ceded ULAE IBNR"] = np.where(
        qs, -g("Direct ULAE IBNR") * cpct * u_ibnr,
        g("f_ulae") * earned_base * u_ibnr)
    # NOTE: EB/FAC ULAE Reserves returns the cession % itself (workbook BD=AK)
    m["Ceded ULAE Reserves"] = np.where(
        qs, -g("Direct ULAE Reserves") * cpct * u_res, u_res)

    for k in CEDED_MEASURES:
        out[k] = np.where(has, m[k], np.nan)
    log.info("Step 14: calculated %d rows (%d QS, %d EB/FAC)",
             len(out), int(qs.sum()), int((has & ~qs).sum()))
    return out
