"""Steps 15-19: ITD pivot, prior month, movement, persist, reinsurer workbooks."""

from __future__ import annotations

import logging

import pandas as pd

from .config import CEDED_MEASURES, PIVOT_MEASURES, PipelineConfig
from .reference import ReferenceData, excel_serial_to_date, num, to_ts

log = logging.getLogger("ceded_platform")


def step15_itd_pivot(fact: pd.DataFrame, cfg: PipelineConfig,
                     group_cols=("Ceded ID",)) -> pd.DataFrame:
    # Pivot ALL 15 ceded measures (not just the 3 premium ones) so the backend
    # snapshot carries every measure — the journal entry needs a period movement
    # for all of them, and month-over-month diffs only work if the prior snapshot
    # stored them too.
    measures = [m for m in CEDED_MEASURES if m in fact.columns]
    piv = (fact.dropna(subset=["Ceded ID"])
               .groupby(list(group_cols), dropna=False, as_index=False)
               [measures].sum())
    piv["Accounting Period"] = f"ITD-{cfg.current_period}"
    return piv


def step16_prior(backend: pd.DataFrame, cfg: PipelineConfig,
                 group_cols=("Ceded ID",)) -> pd.DataFrame:
    cols = list(group_cols)
    if backend is None or backend.empty \
            or "Accounting Period" not in backend.columns:
        return pd.DataFrame(columns=cols)      # first run / empty SQL snapshot
    prior = backend[backend["Accounting Period"] == cfg.prior_period]
    measures = [m for m in CEDED_MEASURES if m in prior.columns]
    if prior.empty or not measures:
        return pd.DataFrame(columns=cols)
    return prior.groupby(cols, dropna=False, as_index=False)[measures].sum()


def step17_movement(itd: pd.DataFrame, prior: pd.DataFrame,
                    cfg: PipelineConfig, group_cols=("Ceded ID",)) -> pd.DataFrame:
    """Period movement = ITD - prior, for every ceded measure present. Robust to
    a prior snapshot that carries fewer measures than the current ITD (missing
    prior measures count as 0 — first-period / legacy backends)."""
    cols = list(group_cols)
    measures = [m for m in CEDED_MEASURES if m in itd.columns]
    itd_i = itd.set_index(cols)[measures]
    if prior is None or prior.empty:
        prior_i = pd.DataFrame(0.0, index=itd_i.index, columns=measures)
    else:
        pmeas = [m for m in measures if m in prior.columns]
        prior_i = (prior.set_index(cols)[pmeas]
                        .reindex(columns=measures).fillna(0.0))
    idx = itd_i.index.union(prior_i.index)
    diff = itd_i.reindex(idx).fillna(0.0) - prior_i.reindex(idx).fillna(0.0)
    out = diff.reset_index()
    out["Accounting Period"] = cfg.current_period
    return out


def step18_persist(backend: pd.DataFrame, itd: pd.DataFrame,
                   cfg: PipelineConfig) -> pd.DataFrame:
    """Write the ITD snapshot back so it becomes next month's prior.
    Idempotent: re-running a period replaces that period's snapshot."""
    snap = itd.copy()
    snap["Accounting Period"] = cfg.current_period
    if backend is None or backend.empty \
            or "Accounting Period" not in backend.columns:
        return snap.reset_index(drop=True)         # first run / empty snapshot
    keep = backend[backend["Accounting Period"] != cfg.current_period]
    if keep.empty:                       # avoid the all-NA concat FutureWarning
        return snap.reset_index(drop=True)
    return pd.concat([keep, snap], ignore_index=True)


def _name_aliases(ref: ReferenceData, name: str) -> set:
    """All names/codes that refer to the same reinsurer, via the T10 master.

    The panel names a reinsurer one way ("Arch Capital") while the
    settlement/collateral/FET tables may use a code or a variant ("ARC 1").
    Bridge them through the reinsurer master so the point-in-time balances link.
    Falls back to just the name itself when no master row matches."""
    aliases = {str(name).strip()}
    m = ref.reinsurer_master
    if m is None or m.empty:
        return aliases
    lower = {str(name).strip().lower()}
    # ONLY the reinsurer's own identity columns bridge — never descriptive
    # columns like 'Parent Company Name' or 'Group Name', which are shared across
    # distinct reinsurers and would alias two different entities together.
    ID_COLS = {"reinsurer_id", "reinsurer_code", "reinsurer_name", "reinsurer"}
    label_cols = [c for c in m.columns if str(c).strip().lower() in ID_COLS]
    if not label_cols:
        return aliases
    for _, row in m.iterrows():
        vals = {str(row[c]).strip() for c in label_cols if pd.notna(row[c])}
        if lower & {v.lower() for v in vals}:
            aliases |= vals
    return {a for a in aliases if a and a.lower() != "nan"}


def _as_of(df: pd.DataFrame, name_aliases: set, period_col: str,
           amount_col: str, fme: pd.Timestamp, cumulative: bool):
    """Point-in-time (latest period <= FME) or cumulative (sum of periods
    <= FME) balance for a reinsurer, matched by any of its name aliases."""
    if df is None or df.empty or "Reinsurer" not in df.columns \
            or amount_col not in df.columns:
        return 0.0, None
    hit = df[df["Reinsurer"].astype(str).str.strip().isin(name_aliases)].copy()
    if hit.empty:
        return 0.0, None
    if period_col in hit.columns:
        per = to_ts(hit[period_col])
        hit = hit[per.notna() & (per <= fme)] if fme is not None else hit
        if hit.empty:
            return 0.0, None
        if not cumulative:                     # latest snapshot only
            per = to_ts(hit[period_col])
            hit = hit[per == per.max()]
    amt = num(pd.to_numeric(hit[amount_col], errors="coerce").fillna(0.0).sum())
    return amt, hit


def step19_reinsurer_workbooks(movement: pd.DataFrame, ref: ReferenceData,
                               cfg: PipelineConfig) -> dict[str, pd.DataFrame]:
    """Explode movement across the reinsurance panel (T6) by each reinsurer's
    share, then attach settlements (T7), collateral (T8) and FET (T9).

    Settlements are cumulative-to-date (<= For Month Ending); collateral and FET
    are point-in-time (the latest snapshot <= For Month Ending) — matched by
    reinsurer with name bridging through the T10 master. A `_linkage` note flags
    any reinsurer whose balances could not be linked, so nothing is silently 0.
    """
    panel = ref.reinsurer_panel
    j = movement.merge(panel, on="Ceded ID", how="inner", suffixes=("", "_p"))
    share = pd.to_numeric(j["Ceded %"], errors="coerce").fillna(0.0)
    for m in PIVOT_MEASURES:
        j[f"Reinsurer {m}"] = j[m] * share
    fme = pd.Timestamp(excel_serial_to_date(cfg.for_month_ending))
    j["For Month Ending"] = fme

    books: dict[str, pd.DataFrame] = {}
    for reinsurer, grp in j.groupby("Reinsurer"):
        b = grp.copy()
        aliases = _name_aliases(ref, reinsurer)
        settled, _ = _as_of(ref.settlements, aliases, "Period Settled",
                            "Settlement Amount", fme, cumulative=True)
        coll, cc = _as_of(ref.collateral, aliases, "Period",
                          "Collateral Amount", fme, cumulative=False)
        fet, _ = _as_of(ref.fet_payable, aliases, "Period",
                        "FET Payable", fme, cumulative=False)
        b["Settlements To Date"] = settled
        b["Collateral Held"] = coll
        b["Collateral Type"] = (cc["Collateral Type"].iloc[0]
                                if cc is not None and "Collateral Type" in cc
                                and len(cc) else None)
        b["FET Payable"] = fet
        linked = any(x for x in (settled, coll, fet))
        b["_linkage"] = "linked" if linked else "no settlement/collateral/FET match"
        books[reinsurer] = b.reset_index(drop=True)
    log.info("Step 19: built %d reinsurer workbooks", len(books))
    return books
