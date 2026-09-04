"""Orchestrator: one monthly ceded cycle, Steps 1-19 + Recon 1-7."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .closing import (step15_itd_pivot, step16_prior, step17_movement,
                      step18_persist, step19_reinsurer_workbooks)
from .config import PipelineConfig
from .engine_calc import step13_assign, step14_calculate
from .journal import build_journal_entry, build_reclass_entry, journal_balances
from .recon import ReconResult, allocation_audit, run_recons
from .reference import ReferenceData
from .steps import (step1_2_ingest_bdx, step3_4_dates, step5_group,
                    step5b_offsets, step6_8_enrich, step9_11_subledger,
                    step12_merge)

log = logging.getLogger("ceded_platform")


@dataclass
class RunResult:
    raw_bdx: pd.DataFrame
    grouped_with_offsets: pd.DataFrame
    stg_bdx_merged: pd.DataFrame
    fact_ceded_calc: pd.DataFrame
    itd_pivot: pd.DataFrame
    prior: pd.DataFrame
    movement: pd.DataFrame
    backend: pd.DataFrame
    reinsurer_books: dict = field(default_factory=dict)
    exclusion_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    reconciliations: list = field(default_factory=list)
    allocation_audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    journal_entry: pd.DataFrame = field(default_factory=pd.DataFrame)
    reclass_entry: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def all_recons_pass(self) -> bool:
        return all(r.passed for r in self.reconciliations)


def run_cycle(eb_bdx: pd.DataFrame, fac_bdx: pd.DataFrame,
              subledger: pd.DataFrame, backend: pd.DataFrame,
              ref: ReferenceData, cfg: PipelineConfig) -> RunResult:
    log.info("=== Ceded cycle: %s (prior %s) ===",
             cfg.current_period, cfg.prior_period)

    raw = step1_2_ingest_bdx(eb_bdx, fac_bdx)          # steps 1-2
    raw = step3_4_dates(raw, cfg)                      # steps 3-4
    grouped = step5_group(raw, cfg)                    # step 5
    with_offsets = step5b_offsets(grouped)             # step 5b: THE OFFSET
    bdx_side = step6_8_enrich(with_offsets, ref)       # steps 6-8
    sub_side = step9_11_subledger(subledger, cfg)      # steps 9-11
    merged = step12_merge(sub_side, bdx_side)          # step 12

    assigned, excl_log = step13_assign(merged, ref, cfg)   # step 13
    fact = step14_calculate(assigned, ref)                 # step 14

    itd = step15_itd_pivot(fact, cfg)                  # step 15
    prior = step16_prior(backend, cfg)                 # step 16
    movement = step17_movement(itd, prior, cfg)        # step 17
    backend_out = step18_persist(backend, itd, cfg)    # step 18
    books = step19_reinsurer_workbooks(movement, ref, cfg)  # step 19
    journal = build_journal_entry(movement, ref, cfg)      # step 20 (period movement)
    reclass = build_reclass_entry(journal, ref, cfg)       # step 20b (UK->US GAAP)

    recons = run_recons(eb_bdx, fac_bdx, raw, with_offsets, merged, fact,
                        movement, books, ref, cfg)
    if not journal.empty:                                  # JE balance gate
        recons.append(ReconResult(
            "Recon 9: journal entry balances (debits = credits)",
            0.0, float((journal["Debit"] - journal["Credit"]).sum()),
            journal_balances(journal, cfg.recon_tolerance),
            "Every program's summarized entry balances."))
    for r in recons:
        log.info("%-58s expected=%16.4f actual=%16.4f %s", r.name,
                 r.expected, r.actual, "PASS" if r.passed else "FAIL")

    audit = allocation_audit(movement, books, panel=ref.reinsurer_panel)

    return RunResult(raw, with_offsets, merged, fact, itd, prior, movement,
                     backend_out, books, excl_log, recons,
                     allocation_audit=audit, journal_entry=journal,
                     reclass_entry=reclass)
