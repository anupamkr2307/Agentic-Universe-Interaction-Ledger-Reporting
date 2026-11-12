"""Database schema, connection management, and indexes for SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from solution.config import DEFAULT_DB_PATH


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Creates a sqlite3 connection with WAL mode and foreign keys enabled."""
    path = str(db_path or DEFAULT_DB_PATH)
    # Ensure directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initializes tables and indexes if they do not already exist."""
    with conn:
        # Leads table: stores canonical leads with PII protected at rest
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                phone_encrypted TEXT,
                customer_name_encrypted TEXT,
                fields_json TEXT NOT NULL DEFAULT '{}',
                created_row_order INTEGER DEFAULT 0,
                PRIMARY KEY (workflow_id, phone_hash)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_wf_batch ON leads(workflow_id, batch_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_wf_ref ON leads(workflow_id, external_ref);"
        )

        # Append-only interaction ledger: stores all valid call events
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at_ist TEXT NOT NULL,
                phone_hash TEXT,
                disposition_code TEXT,
                is_valid_disposition INTEGER DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_call_id ON events(call_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_wf_phone ON events(workflow_id, phone_hash);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at_ist);"
        )

        # Rejections table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejections (
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (workflow_id, batch_id, row_number)
            );
            """
        )

        # File tracking and metrics for strict idempotency
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_metrics (
                file_path TEXT PRIMARY KEY,
                file_type TEXT NOT NULL,
                lead_rows_total INTEGER DEFAULT 0,
                lead_rows_rejected INTEGER DEFAULT 0,
                duplicate_lead_rows_merged INTEGER DEFAULT 0,
                field_warnings INTEGER DEFAULT 0,
                events_total INTEGER DEFAULT 0,
                malformed_events INTEGER DEFAULT 0,
                duplicate_events_dropped INTEGER DEFAULT 0,
                unknown_disposition_events INTEGER DEFAULT 0
            );
            """
        )
