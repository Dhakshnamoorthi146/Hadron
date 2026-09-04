"""Tests for the local quick-win gap fixes (exclusion log, per-reinsurer books,
GAAP reclass, T3 corridor). Run:  python -m pytest tests/test_gaps.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ceded_platform import PipelineConfig, load_reference_data, run_cycle
from ceded_platform.reference import load_reference_data_local
from ceded_platform.engine_calc import _corridor_ok
from ceded_platform.journal import (_account_lookup, build_journal_entry,
                                    build_reclass_entry, journal_balances)
from ceded_platform.report import _safe_name, write_reinsurer_workbooks

REF_DIR = ROOT / "reference_data"
CFG = PipelineConfig(for_month_ending="2026-05-31",
                     current_period="May'26", prior_period="April'26")


def _ref():
    return load_reference_data_local(REF_DIR)


# ---- T3 corridor exclusion (#4) ------------------------------------------- #
def test_corridor_no_op_when_column_absent():
    df = pd.DataFrame({"Written": [1.0, 2.0, 3.0]})          # no limit column
    contract = pd.Series({"Lower Limit Exclusion": 0.0,
                          "Higher Limit Exclusion": 2_500_000.0})
    ok = _corridor_ok(df, contract, CFG, warned=set())
    assert ok.all()                                          # nothing excluded


def test_corridor_excludes_out_of_band_rows():
    df = pd.DataFrame({"Policy Limit": [1_000_000, 3_000_000, np.nan]})
    contract = pd.Series({"Lower Limit Exclusion": 0.0,
                          "Higher Limit Exclusion": 2_500_000.0})
    ok = _corridor_ok(df, contract, CFG, warned=set())
    # 1M in band -> keep; 3M above band -> exclude; NaN untestable -> keep
    assert list(ok) == [True, False, True]


def test_corridor_no_band_is_no_op():
    df = pd.DataFrame({"Policy Limit": [999]})
    contract = pd.Series({"Lower Limit Exclusion": np.nan,
                          "Higher Limit Exclusion": np.nan})
    assert _corridor_ok(df, contract, CFG, warned=set()).all()


# ---- GAAP reclass (#3) ---------------------------------------------------- #
def test_account_lookup_prefers_ceded_over_direct():
    codes = pd.DataFrame({
        "Column": ["Ceded Indemnity IBNR", "Direct Indemnity IBNR"],
        "Account Number": [211002, 211002],
        "Internal ID": [1, 2], "tranID": ["x", "y"],
        "Summarize?": ["Ceded", "No"], "Department": ["UW", "UW"]})
    lut = _account_lookup(codes)
    # both share account 211002 but distinct descriptions -> both resolvable,
    # and the ceded description carries the Ceded summarize flag.
    assert lut["ceded indemnity ibnr"]["_summ"] == "ceded"


def test_reclass_maps_and_balances():
    ref = _ref()
    res = run_cycle(_eb(), _fac(), _qs(),
                    pd.DataFrame(columns=["Ceded ID", "Accounting Period"]),
                    ref, CFG)
    je, rc = res.journal_entry, res.reclass_entry
    assert not je.empty and not rc.empty
    assert len(rc) == len(je)                    # every base line carried through
    assert rc["reclassified"].sum() > 0          # at least some remapped
    # US-GAAP entry still balances per program
    g = rc.groupby("Ceded ID")[["Debit", "Credit"]].sum()
    assert (g["Debit"] - g["Credit"]).abs().max() <= 0.01


# ---- per-reinsurer workbooks (#2) ----------------------------------------- #
def test_safe_name():
    assert _safe_name("Arch Capital") == "Arch Capital"
    assert _safe_name("obo/Accident:Fund*?") == "obo_Accident_Fund__"


def test_write_reinsurer_workbooks(tmp_path):
    books = {"Arch Capital": pd.DataFrame({"Ceded ID": ["CX0COR"], "x": [1]}),
             "Swiss Re": pd.DataFrame({"Ceded ID": ["CX0COR"], "x": [2]})}
    written = write_reinsurer_workbooks(books, tmp_path)
    assert len(written) == 2
    assert (tmp_path / "Arch Capital.xlsx").exists()


# ---- end-to-end anchor unchanged ------------------------------------------ #
def test_anchor_and_recons_unchanged():
    ref = _ref()
    res = run_cycle(_eb(), _fac(), _qs(),
                    pd.DataFrame(columns=["Ceded ID", "Accounting Period"]),
                    ref, CFG)
    assert res.all_recons_pass
    cx0 = res.movement.loc[res.movement["Ceded ID"] == "CX0COR",
                           "Ceded Written Premium"].iloc[0]
    assert cx0 == pytest.approx(47620.9802, abs=1e-3)


# ---- demo inputs (the bundled clean sample) ------------------------------- #
def _read(name):
    p = ROOT / "demo_input" / name
    return pd.read_excel(p)


def _qs():
    return _read("QS.xlsx")


def _fac():
    df = _read("FAC.xlsx").rename(columns={
        "Policy Eff Date": "Policy Effective Date",
        "Policy Exp Date": "Policy Expiration Date",
        "Fac Re Premium": "Premium", "Location State": "Risk State"})
    if "Premium Type" not in df.columns:
        df["Premium Type"] = "FAC"
    if "Trading Partner" not in df.columns:
        df["Trading Partner"] = "COR"
    return df


def _eb():
    df = _read("EB.xlsx").rename(columns={
        "Current Policy Number": "Policy Number",
        "EB Premium": "Premium", "Principal State": "Risk State"})
    if "Premium Type" not in df.columns:
        df["Premium Type"] = "EB"
    if "Trading Partner" not in df.columns:
        df["Trading Partner"] = "COR"
    return df
