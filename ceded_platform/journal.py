"""Step 20: the summarized program-level journal entry (NetSuite-ready).

The POC's final deliverable (FR-OUT-2 / FR-CALC-3): one summarized journal entry
per program per month, booked at the contract/program level, ready to upload to
NetSuite. Each program's ceded measures are mapped to their GL accounts from the
workbook's 'FAC Account Codes' sheet, and an explicit intercompany balancing line
is added so total debits equal total credits.

FLAG for Tori / Jenna (workbook-faithful + flag, per the build decision):
  - The debit/credit convention here is "positive amount -> Debit, negative ->
    Credit"; the balancing line is an implied intercompany plug. Neither the
    convention nor the UK->US GAAP reclass ('FAC GAAPReClass') has been tied to a
    worked Hadron NetSuite journal, because the workbook supplies the mapping
    tables but no worked journal to validate against. Confirm against one real
    month's entry before relying on the Dr/Cr presentation.
  - Measures with no matching GL account in 'FAC Account Codes' (e.g. Ceded
    Earned Premium, which is derived, not posted) are omitted and logged.
"""

from __future__ import annotations

import logging

import pandas as pd

from .config import CEDED_MEASURES, PipelineConfig
from .reference import ReferenceData

log = logging.getLogger("ceded_platform")

# Intercompany account that absorbs the net (FAC Account Codes: 120005 "Ceded
# Intercompany Receivable from HSIC"). Made explicit so the entry balances.
BALANCING_ACCOUNT = "120005"
BALANCING_DESC = "Ceded Intercompany Receivable/Payable (balancing)"


def _account_lookup(codes: pd.DataFrame) -> dict:
    """measure-name(lower) -> {account, internal_id, tranID, department} from the
    FAC Account Codes sheet (col 0 = description, 1 = account number, 2 = internal
    id, 3 = tranID, 5 = department). First occurrence of a name wins."""
    if codes is None or codes.empty or codes.shape[1] < 2:
        return {}
    cols = list(codes.columns)
    desc_c, acct_c = cols[0], cols[1]
    iid_c = cols[2] if len(cols) > 2 else None
    tran_c = cols[3] if len(cols) > 3 else None
    dept_c = cols[5] if len(cols) > 5 else None
    out: dict = {}
    for _, r in codes.iterrows():
        d = str(r[desc_c]).strip()
        if not d or d.lower() == "nan":
            continue
        key = d.lower()
        if key in out:
            continue
        out[key] = {
            "account": r[acct_c],
            "internal_id": r[iid_c] if iid_c is not None else None,
            "tranID": r[tran_c] if tran_c is not None else None,
            "department": r[dept_c] if dept_c is not None else None,
        }
    return out


def build_journal_entry(movement: pd.DataFrame, ref: ReferenceData,
                        cfg: PipelineConfig) -> pd.DataFrame:
    """Program-level summarized journal entry from the period MOVEMENT (step 17).

    Books the current month's movement (ITD - prior), NOT the cumulative ITD, so
    re-running month after month never re-books prior periods. Aggregates the 15
    ceded measures per Ceded ID, maps each to its GL account, and appends a
    balancing line per program so debits == credits. Returns an empty frame when
    no GL account codes are available (e.g. sample fixtures without the sheet)."""
    codes = _account_lookup(ref.gaap_account_codes)
    if not codes or movement is None or movement.empty \
            or "Ceded ID" not in movement.columns:
        return pd.DataFrame()

    measures = [m for m in CEDED_MEASURES if m in movement.columns]
    agg = (movement.dropna(subset=["Ceded ID"])
                   .groupby("Ceded ID", dropna=False)[measures].sum())

    period = cfg.current_period
    lines: list[dict] = []
    unmapped: set = set()
    for cid, row in agg.iterrows():
        prog: list[dict] = []
        for measure in measures:
            amt = float(row.get(measure, 0.0) or 0.0)
            if abs(amt) < 1e-9:
                continue
            info = codes.get(measure.lower())
            if info is None:
                unmapped.add(measure)
                continue
            prog.append({
                "Ceded ID": cid, "Accounting Period": period,
                "Line Memo": measure,
                "Account Number": info["account"],
                "Internal ID": info["internal_id"],
                "tranID": info["tranID"],
                "Department": info["department"],
                "Amount": round(amt, 2),
                "Debit": round(amt, 2) if amt > 0 else 0.0,
                "Credit": round(-amt, 2) if amt < 0 else 0.0,
            })
        net = round(sum(x["Amount"] for x in prog), 2)
        if abs(net) > 1e-9:                     # explicit balancing plug
            prog.append({
                "Ceded ID": cid, "Accounting Period": period,
                "Line Memo": BALANCING_DESC,
                "Account Number": BALANCING_ACCOUNT,
                "Internal ID": None, "tranID": "Underwriting Premium Entry",
                "Department": "Underwriting : Underwriting",
                "Amount": round(-net, 2),
                "Debit": round(-net, 2) if -net > 0 else 0.0,
                "Credit": round(net, 2) if -net < 0 else 0.0,
            })
        lines.extend(prog)

    je = pd.DataFrame(lines)
    if unmapped:
        log.info("Step 20: %d measure(s) with no GL account, omitted: %s",
                 len(unmapped), ", ".join(sorted(unmapped)))
    log.info("Step 20: journal entry built — %d lines across %d programs",
             len(je), agg.shape[0])
    return je


def journal_balances(je: pd.DataFrame, tol: float = 0.01) -> bool:
    """True if total debits equal total credits for every program (POC: a valid
    journal entry must balance)."""
    if je is None or je.empty:
        return True
    g = je.groupby("Ceded ID")[["Debit", "Credit"]].sum()
    return bool((g["Debit"] - g["Credit"]).abs().max() <= tol)
