"""Event JSONL ingestion, deduplication, timestamp parsing, and ledger storage."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from solution.config import load_dispositions
from solution.crypto import hash_phone
from solution.validators import clean_str, parse_timestamp, validate_phone


def ingest_event_file(
    conn: sqlite3.Connection,
    event_file_path: Path,
    disposition_catalog: dict[str, dict[str, Any]],
    seen_event_ids: set[str] | None = None,
) -> dict[str, int]:
    """
    Ingests a single events JSONL file into the append-only events ledger:
    - Checks for malformed lines (invalid JSON or missing event_id)
    - Deduplicates event_id
    - Normalizes timestamp to IST
    - Checks disposition against catalogue
    - Persists into events ledger table using batch inserts
    Returns per-file metrics:
        events_total, malformed_events, duplicate_events_dropped, unknown_disposition_events
    """
    metrics = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "unknown_disposition_events": 0,
    }

    if seen_event_ids is None:
        seen_event_ids = {
            r[0] for r in conn.execute("SELECT event_id FROM events;").fetchall()
        }

    batch_to_insert = []
    batch_size = 5000

    with open(event_file_path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue

            metrics["events_total"] += 1

            # 1. Malformed check
            try:
                data = json.loads(line_str)
                if not isinstance(data, dict):
                    metrics["malformed_events"] += 1
                    continue
                raw_eid = data.get("event_id")
                if not raw_eid:
                    metrics["malformed_events"] += 1
                    continue
                event_id = str(raw_eid).strip()
                if not event_id:
                    metrics["malformed_events"] += 1
                    continue
            except (json.JSONDecodeError, UnicodeDecodeError):
                metrics["malformed_events"] += 1
                continue

            # 2. Duplicate check
            if event_id in seen_event_ids:
                metrics["duplicate_events_dropped"] += 1
                continue

            seen_event_ids.add(event_id)

            # 3. Parse fields
            event_type = str(data.get("event_type", "")).strip()
            workflow_id = str(data.get("workflow_id", "")).strip()
            call_id = str(data.get("call_id", "")).strip()
            raw_ts = str(data.get("occurred_at", "")).strip()

            try:
                occurred_at_ist = parse_timestamp(raw_ts).isoformat()
            except Exception:
                metrics["malformed_events"] += 1
                continue

            raw_phone = data.get("phone")
            phone_hash = None
            if raw_phone is not None:
                is_valid_p, canon_p = validate_phone(raw_phone)
                if is_valid_p and canon_p:
                    phone_hash = hash_phone(canon_p)

            payload = data.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            # 4. Unknown disposition check
            is_valid_disp = 0
            disp_code = None
            if event_type in ("disposition.set", "disposition.override"):
                raw_disp = payload.get("disposition_code")
                cleaned_disp = clean_str(raw_disp)
                wf_catalog = disposition_catalog.get(workflow_id, {})
                if cleaned_disp and cleaned_disp in wf_catalog:
                    is_valid_disp = 1
                    disp_code = cleaned_disp
                else:
                    metrics["unknown_disposition_events"] += 1
                    is_valid_disp = 0
                    disp_code = None

            batch_to_insert.append(
                (
                    event_id,
                    event_type,
                    workflow_id,
                    call_id,
                    occurred_at_ist,
                    phone_hash,
                    disp_code,
                    is_valid_disp,
                    json.dumps(payload),
                )
            )

            if len(batch_to_insert) >= batch_size:
                conn.executemany(
                    """
                    INSERT INTO events (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_ist, phone_hash, disposition_code,
                        is_valid_disposition, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    batch_to_insert,
                )
                batch_to_insert.clear()

    if batch_to_insert:
        conn.executemany(
            """
            INSERT INTO events (
                event_id, event_type, workflow_id, call_id,
                occurred_at_ist, phone_hash, disposition_code,
                is_valid_disposition, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            batch_to_insert,
        )
        batch_to_insert.clear()

    return metrics


def ingest_all_events_in_drop(
    conn: sqlite3.Connection,
    drop_dir: Path,
    disposition_catalog: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    Finds and ingests all event JSONL files under <drop_dir>/events/*.jsonl.
    Processes files in filename order. Skips files already ingested.
    """
    if disposition_catalog is None:
        disposition_catalog = load_dispositions()

    events_base = drop_dir / "events"
    if not events_base.exists():
        return

    jsonl_files = sorted(events_base.glob("*.jsonl"), key=lambda p: p.name)
    if not jsonl_files:
        return

    # Cache seen event_ids for the drop
    seen_event_ids: set[str] = {
        r[0] for r in conn.execute("SELECT event_id FROM events;").fetchall()
    }

    for jpath in jsonl_files:
        rel_path = str(jpath.relative_to(drop_dir.parent))
        # Check if already ingested
        cur = conn.execute(
            "SELECT 1 FROM file_metrics WHERE file_path = ?;", (rel_path,)
        )
        if cur.fetchone() is not None:
            continue

        with conn:
            m = ingest_event_file(conn, jpath, disposition_catalog, seen_event_ids)
            conn.execute(
                """
                INSERT INTO file_metrics (
                    file_path, file_type, events_total, malformed_events,
                    duplicate_events_dropped, unknown_disposition_events
                ) VALUES (?, 'event_jsonl', ?, ?, ?, ?);
                """,
                (
                    rel_path,
                    m["events_total"],
                    m["malformed_events"],
                    m["duplicate_events_dropped"],
                    m["unknown_disposition_events"],
                ),
            )
