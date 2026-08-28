"""SQL data layer — READ the reference tables from a database so the engine runs
from SQL instead of Excel.

The calculation engine NEVER changes: it only ever sees a ReferenceData object.
This module just swaps where that object comes from. Works on any SQLAlchemy
engine — Supabase / Postgres in production, SQLite for local development — because
it goes through pandas read_sql / to_sql.

READ-ONLY on reference by design. The `ref_*` reference tables are seeded once by
whoever owns the database; this module can only READ them, never overwrite them —
so a teammate running the engine can never damage the shared reference data. The
only thing it can WRITE is a run's own OUTPUT tables, and only when the caller
explicitly asks (see `save_outputs`).

Typical flow
------------
    from ceded_platform import run_cycle, PipelineConfig
    from ceded_platform.sqlstore import make_engine, load_reference_data_sql

    engine = make_engine(os.environ["SUPABASE_DB_URL"])   # pooler URL recommended
    ref = load_reference_data_sql(engine)                 # read the ref_* tables
    res = run_cycle(eb, fac, sub, backend, ref, PipelineConfig(...))

These `ref_*` table names mirror the workbook's "Tables to be created in SQL"
sheet, so the same schema carries over to Hadron's real SQL environment later.
"""

from __future__ import annotations

import time
from datetime import date, datetime

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, OperationalError

from .reference import ReferenceData

_RETRYABLE = (OperationalError, DBAPIError)


def make_engine(url: str, statement_timeout_ms: int = 0):
    """Create a SQLAlchemy engine. For Postgres/Supabase: lift the server
    statement_timeout at connect time (0 = unlimited), enable pool_pre_ping so a
    dropped connection is replaced not reused, and turn on TCP keepalives so a
    slow link doesn't get silently cut mid-transfer. Accepts a plain postgres://
    URL too."""
    for pfx in ("postgresql://", "postgres://"):
        if url.startswith(pfx):
            url = "postgresql+psycopg2://" + url[len(pfx):]
            break
    kwargs = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs["connect_args"] = {
            "options": f"-c statement_timeout={int(statement_timeout_ms)}",
            "keepalives": 1, "keepalives_idle": 30,
            "keepalives_interval": 10, "keepalives_count": 5,
        }
    return create_engine(url, **kwargs)


def _read_sql_retry(sql: str, engine, attempts: int = 4) -> pd.DataFrame:
    """One SELECT with retry — a transient connection drop (SSL EOF, pooler
    closing the socket on a slow link) reconnects and retries rather than aborting
    the whole run."""
    for i in range(attempts):
        try:
            return pd.read_sql(text(sql), con=engine)
        except _RETRYABLE:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))               # 2s, 4s, 6s backoff


def _read_table(engine, table: str, page: int = 5000) -> pd.DataFrame:
    """Read a whole table in paged LIMIT/OFFSET queries.

    Each page is a short, independent query — friendly to the Supabase connection
    pooler (which drops long-held server-side cursors) and safely under any
    statement-timeout. An ABSENT table returns an empty frame; a table that EXISTS
    but fails to read RAISES — a read failure must never masquerade as 'no data'."""
    if not inspect(engine).has_table(table):
        return pd.DataFrame()
    try:
        total = int(_read_sql_retry(
            f'SELECT count(*) AS n FROM "{table}"', engine)["n"].iloc[0])
        if total == 0:                            # keep the columns
            return _read_sql_retry(f'SELECT * FROM "{table}"', engine)
        parts = []
        for off in range(0, total, page):
            parts.append(_read_sql_retry(
                f'SELECT * FROM "{table}" LIMIT {page} OFFSET {off}', engine))
        return pd.concat(parts, ignore_index=True)
    except Exception as ex:                       # surface, never swallow
        raise RuntimeError(f"failed reading SQL table {table!r}: {ex}") from ex


# ReferenceData field -> SQL table name (the 'Tables to be created in SQL' set)
TABLES = {
    "loss_ratios": "ref_loss_ratios",
    "fac_lob_map": "ref_fac_lob_map",
    "ceded_id_map": "ref_ceded_id_map",
    "exclusion_rules": "ref_exclusion_rules",
    "contract_exclusions": "ref_contract_exclusions",
    "reinsurer_panel": "ref_reinsurer_panel",
    "settlements": "ref_settlements",
    "collateral": "ref_collateral",
    "fet_payable": "ref_fet_payable",
    "reinsurer_master": "ref_reinsurer_master",
    "gaap_account_codes": "ref_account_codes",
    "gaap_reclass": "ref_gaap_reclass",
}


def load_reference_data_sql(engine) -> ReferenceData:
    """Rebuild ReferenceData from the SQL tables (no Excel). Missing/absent
    tables come back empty. This is READ-ONLY — it never writes the ref_* tables."""
    def rd(table: str) -> pd.DataFrame:
        return _read_table(engine, table)

    return ReferenceData(
        loss_ratios=rd(TABLES["loss_ratios"]),
        fac_lob_map=rd(TABLES["fac_lob_map"]),
        ceded_id_map=rd(TABLES["ceded_id_map"]),
        exclusion_rules=rd(TABLES["exclusion_rules"]),
        contract_exclusions=rd(TABLES["contract_exclusions"]),
        reinsurer_panel=rd(TABLES["reinsurer_panel"]),
        settlements=rd(TABLES["settlements"]),
        collateral=rd(TABLES["collateral"]),
        fet_payable=rd(TABLES["fet_payable"]),
        reinsurer_master=rd(TABLES["reinsurer_master"]),
        gaap_account_codes=rd(TABLES["gaap_account_codes"]),
        gaap_reclass=rd(TABLES["gaap_reclass"]),
    )


def reconciliations_frame(reconciliations) -> pd.DataFrame:
    """The run's reconciliation checks as a table a human can open and verify:
    each check, what was expected vs what came out, the variance, and pass/fail."""
    return pd.DataFrame([{
        "check": r.name,
        "expected": round(float(r.expected), 4),
        "actual": round(float(r.actual), 4),
        "variance": round(float(r.variance), 4),
        "passed": bool(r.passed),
        "note": r.note,
    } for r in (reconciliations or [])])


def _sql_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Copy with columns that hold Timestamps/dates/lists (not bindable, or
    mixed date+serial) coerced to strings so any SQL driver accepts them."""
    safe = df.copy()
    for c in safe.columns:
        if safe[c].map(lambda v: isinstance(
                v, (list, dict, pd.Timestamp, datetime, date))).any():
            safe[c] = safe[c].astype(str)
    return safe


def save_outputs(res, engine, if_exists: str = "replace") -> dict:
    """OPT-IN: persist a run's OUTPUT tables to SQL (fact grain, movement,
    allocation audit, journal entry, backend snapshot, reconciliation checks).

    Writes only these output tables — never the ref_* reference tables. On a
    SHARED database, only the caller who explicitly asked (run.py --save-to-db)
    reaches here, so ordinary testers can't overwrite each other's outputs."""
    out = {
        "fact_ceded_calc": res.fact_ceded_calc,
        "movement": res.movement,
        "allocation_audit": res.allocation_audit,
        "journal_entry": res.journal_entry,
        "backend_snapshot": res.backend,
        "reconciliations": reconciliations_frame(res.reconciliations),
    }
    counts: dict = {}
    for table, df in out.items():
        if df is None or df.shape[1] == 0:
            continue
        _sql_safe(df).to_sql(table, engine, if_exists=if_exists, index=False,
                             chunksize=1000, method="multi")
        counts[table] = len(df)
    return counts
