"""Pipeline configuration — every tunable in one place, no magic values in code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

CALC_PATH_QS = "QS"
CALC_PATH_EB = "EB"
CALC_PATH_FAC = "FAC"
CALC_PATH_XOL = "XOL"          # defined in T3/T4 but no formula set exists yet
FAC_EB_PATHS = (CALC_PATH_EB, CALC_PATH_FAC)

# Row provenance tags (drive exclusion policy + audit trail)
# NOTE ON NAMING: the business (standup transcripts) calls input 1 the "gross"
# feed — the output of the upstream gross calculation engine ('Collider'), which
# the offset (step 5b) turns INTO quota share. This code historically calls that
# feed "subledger" / SOURCE_SUBLEDGER. They are the SAME feed; only the label
# differs. Read SOURCE_SUBLEDGER as "the gross / quota-share feed (input 1)".
SOURCE_BDX = "BDX"             # positive FAC/EB rows from the bordereaux (input 2)
SOURCE_OFFSET = "OFFSET"       # negative QS rows created by the offset step
SOURCE_SUBLEDGER = "SUBLEDGER" # the gross feed (input 1) -> quota share

# Step 5 grouping keys ('TO BE Process Steps' step 5 / sample sheet A21:I21)
STEP5_GROUP_KEYS = [
    "Policy Effective Date", "Premium Type", "Trading Partner",
    "Accounting Period", "Policy Days", "Days Earned", "Accident Year",
    "For Month Ending", "Risk State",
]

# Direct columns hard-coded to 0 at Step 8
STEP8_ZERO_COLUMNS = [
    "Commission", "Receivable", "Earned Premium", "Unearned Premium", "DAC",
    "Deferred Receivable", "Direct Idemnity IBNR", "Direct AO IBNR",
    "Direct DCC IBNR", "Direct ULAE IBNR", "Direct ULAE Reserves",
]

# The 15 ceded measures produced by Step 14
CEDED_MEASURES = [
    "Ceded Written Premium", "Ceded Earned Premium", "Ceded Unearned Premium",
    "Ceded Commission", "Ceded Commission DAC", "Ceded Fronting Commission",
    "Ceded Fronting Fee DAC", "Ceded Reinsurance Payable", "Ceded Brokerage Fee",
    "Ceded Brokerage Fee DAC", "Ceded Indemnity IBNR", "Ceded A&O IBNR",
    "Ceded DCC IBNR", "Ceded ULAE IBNR", "Ceded ULAE Reserves",
]

PIVOT_MEASURES = ["Ceded Unearned Premium", "Ceded Earned Premium",
                  "Ceded Written Premium"]

# AUM deal-breakout buckets — (exclusive upper bound, label). VERIFIED against
# the workbook's actual IFS formula (sheet 'TO BE Sample Calc BDX FAC Prem', col
# AA): IFS(B<>"AUM", A, L<DATE(2024,7,1),"AUM1", L<DATE(2025,1,1),"AUM2",
# L<DATE(2025,4,1),"AUM3", L<DATE(2025,7,1),"AUM4", L<DATE(2026,1,1),"AUM5",
# TRUE,"AUM6"). The standup transcript said "AUM1..AUM5" but that was a verbal
# simplification — the workbook has 6 buckets and the real T3 uses AUM6.
AUM_BUCKETS = [
    (date(2024, 7, 1), "AUM1"), (date(2025, 1, 1), "AUM2"),
    (date(2025, 4, 1), "AUM3"), (date(2025, 7, 1), "AUM4"),
    (date(2026, 1, 1), "AUM5"), (date(9999, 12, 31), "AUM6"),
]

# Exclusion rule_type -> the input column(s) it tests. Per the standup, rule
# types are data-driven ("rule type can be any categorical column in the input")
# — so an UNKNOWN rule_type falls back to a column named exactly after it, and
# new rule types need only a mapping entry here, never new code.
RULE_TYPE_COLUMNS = {
    "LOB": ("Line of Business", "Class of Business"),
    "BOLT_ON": ("Line of Business", "Class of Business"),
    "MGA": ("MGA Alias",),
    "TERM_LIMIT": ("Policy Limit",),
}

# MGA feeds use different column names — map each variant to the canonical name
# here (extend per-MGA as new feeds onboard).
BDX_COLUMN_MAP = {
    "FAC Risk State": "Risk State",
    "Accident year": "Accident Year",
    "Policy Limit ($)": "Policy Limit",
}


@dataclass
class PipelineConfig:
    for_month_ending: object            # Excel serial, date or ISO string
    current_period: str                 # e.g. "May'26"
    prior_period: str                   # e.g. "April'26"
    # The sample workbook applies exclusion rules to subledger rows (which carry
    # a real Line of Business) but NOT to BDX-derived rows/offsets, whose LOB is
    # a join artifact. Flip to True once the BDX feed carries a real LOB.
    apply_exclusions_to_bdx: bool = False
    # Drop bolt-on / XOL sub-program exclusion rules when assigning main-stream
    # (QS / EB / FAC) rows — those whitelists belong to separate feeds. Turn off
    # only once bolt-on / XOL feeds are onboarded and routed to their own rules.
    scope_out_bolton_xol: bool = True
    # Allow the tier-2 fallback that assigns a row to a contract of a DIFFERENT
    # trading partner (Calc Path + date only). Off = workbook Step 13 (partner
    # must match); a row matching no contract stays unassigned/retained.
    cross_partner_fallback: bool = False
    recon_tolerance: float = 0.01
    group_keys: list = field(default_factory=lambda: list(STEP5_GROUP_KEYS))
    # T3 corridor exclusion (spec §7.1): the ceded_id_map's Lower/Higher Limit
    # Exclusion columns band on a per-row limit/size value. This names the input
    # column that carries it. When the column is absent from the feed (the current
    # premium case), the corridor check is a logged no-op — the mechanism exists
    # but nothing is silently excluded on data that can't be tested.
    corridor_limit_column: str = "Policy Limit"
