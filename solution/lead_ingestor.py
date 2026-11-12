"""Lead CSV ingestion, schema validation, rejection logging, and deduplication."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from solution.config import load_workflow_schemas
from solution.crypto import encrypt_pii, hash_phone, mask_phone
from solution.validators import (
    clean_str,
    is_blank_row,
    match_headers,
    validate_field,
    validate_phone,
)


def ingest_lead_file(
    conn: sqlite3.Connection,
    csv_path: Path,
    workflow_id: str,
    schema: dict[str, Any],
    leads_cache: dict[str, tuple[str, str | None, dict[str, Any]]] | None = None,
) -> dict[str, int]:
    """
    Ingests a single lead CSV file:
    - Matches headers (with aliases)
    - Validates fields in schema order
    - Records rejections without PII
    - Inserts new leads with PII protected at rest
    - Merges duplicate leads (last write wins for non-null fields; batch_id permanent)
    Returns per-file metrics:
        lead_rows_total, lead_rows_rejected, duplicate_lead_rows_merged, field_warnings
    """
    batch_id = csv_path.stem
    metrics = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }

    schema_fields = schema.get("fields", [])
    ref_field_name = next(
        (f["name"] for f in schema_fields if f.get("reference")), "external_ref"
    )

    if leads_cache is None:
        cur = conn.execute(
            """
            SELECT phone_hash, external_ref, customer_name_encrypted, fields_json
            FROM leads
            WHERE workflow_id = ?;
            """,
            (workflow_id,),
        )
        leads_cache = {
            r[0]: (r[1], r[2], json.loads(r[3])) for r in cur.fetchall()
        }

    rejections_to_insert = []
    leads_to_insert = []
    leads_to_update = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return metrics

        header_map = match_headers(raw_headers, schema_fields)
        row_number = 0

        for row_cells in reader:
            if is_blank_row(row_cells):
                # Blank rows are skipped and not counted
                continue

            row_number += 1
            metrics["lead_rows_total"] += 1

            # Build dict of raw values mapped by canonical field name
            raw_row: dict[str, str | None] = {}
            for col_idx, cell in enumerate(row_cells):
                if col_idx < len(raw_headers):
                    header = raw_headers[col_idx]
                    if header in header_map:
                        field_name = header_map[header]
                        raw_row[field_name] = cell

            # Validate fields in schema order
            rejected_reason: str | None = None
            parsed_row: dict[str, Any] = {}
            row_warnings = 0

            for field_def in schema_fields:
                fname = field_def["name"]
                ftype = field_def.get("type", "string")
                is_req = field_def.get("required", False)
                raw_val = raw_row.get(fname)
                cleaned = clean_str(raw_val)

                if is_req:
                    if cleaned is None:
                        rejected_reason = f"missing_required:{fname}"
                        break
                    if ftype == "phone":
                        valid_phone, canonical_phone = validate_phone(cleaned)
                        if not valid_phone:
                            rejected_reason = "invalid_phone"
                            break
                        parsed_row[fname] = canonical_phone
                    else:
                        is_valid, pval = validate_field(cleaned, field_def)
                        if not is_valid:
                            rejected_reason = f"missing_required:{fname}"
                            break
                        parsed_row[fname] = pval
                else:
                    # Optional field
                    if cleaned is not None:
                        is_valid, pval = validate_field(cleaned, field_def)
                        if is_valid:
                            parsed_row[fname] = pval
                        else:
                            parsed_row[fname] = None
                            row_warnings += 1
                    else:
                        parsed_row[fname] = None

            if rejected_reason is not None:
                # Row is rejected
                metrics["lead_rows_rejected"] += 1
                rejections_to_insert.append(
                    (workflow_id, batch_id, row_number, rejected_reason)
                )
                continue

            # Row is valid: accumulate warnings
            metrics["field_warnings"] += row_warnings

            # Extract key attributes
            canon_phone = parsed_row.get("phone", "")
            p_hash = hash_phone(canon_phone)
            p_masked = mask_phone(canon_phone)
            p_encrypted = encrypt_pii(canon_phone)

            cust_name = parsed_row.get("customer_name")
            name_encrypted = encrypt_pii(cust_name) if cust_name else None
            ext_ref = str(parsed_row.get(ref_field_name, ""))

            # Remaining fields into fields_json
            other_fields: dict[str, Any] = {}
            for field_def in schema_fields:
                fname = field_def["name"]
                if fname not in ("phone", "customer_name", ref_field_name):
                    val = parsed_row.get(fname)
                    other_fields[fname] = val

            if p_hash not in leads_cache:
                # First valid row: creates lead
                for field_def in schema_fields:
                    fname = field_def["name"]
                    if fname in other_fields and other_fields[fname] is None:
                        if "default" in field_def:
                            other_fields[fname] = field_def["default"]

                leads_cache[p_hash] = (ext_ref, name_encrypted, other_fields)
                leads_to_insert.append(
                    (
                        workflow_id,
                        p_hash,
                        ext_ref,
                        batch_id,
                        p_masked,
                        p_encrypted,
                        name_encrypted,
                        json.dumps(other_fields),
                        row_number,
                    )
                )
            else:
                # Duplicate lead: merge non-null values
                metrics["duplicate_lead_rows_merged"] += 1
                exist_ext_ref, exist_name_enc, exist_fields = leads_cache[p_hash]

                new_ext_ref = ext_ref if ext_ref else exist_ext_ref
                new_name_enc = name_encrypted if name_encrypted else exist_name_enc
                merged_fields = dict(exist_fields)

                for k, v in other_fields.items():
                    if v is not None:
                        merged_fields[k] = v

                for field_def in schema_fields:
                    fname = field_def["name"]
                    if fname in merged_fields and merged_fields[fname] is None:
                        if "default" in field_def:
                            merged_fields[fname] = field_def["default"]

                leads_cache[p_hash] = (new_ext_ref, new_name_enc, merged_fields)
                leads_to_update.append(
                    (
                        new_ext_ref,
                        new_name_enc,
                        json.dumps(merged_fields),
                        workflow_id,
                        p_hash,
                    )
                )

    if rejections_to_insert:
        conn.executemany(
            """
            INSERT OR IGNORE INTO rejections (workflow_id, batch_id, row_number, reason)
            VALUES (?, ?, ?, ?);
            """,
            rejections_to_insert,
        )

    if leads_to_insert:
        conn.executemany(
            """
            INSERT INTO leads (
                workflow_id, phone_hash, external_ref, batch_id,
                phone_masked, phone_encrypted, customer_name_encrypted,
                fields_json, created_row_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            leads_to_insert,
        )

    if leads_to_update:
        conn.executemany(
            """
            UPDATE leads
            SET external_ref = ?,
                customer_name_encrypted = ?,
                fields_json = ?
            WHERE workflow_id = ? AND phone_hash = ?;
            """,
            leads_to_update,
        )

    return metrics


def ingest_all_leads_in_drop(
    conn: sqlite3.Connection,
    drop_dir: Path,
    schemas: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    Finds and ingests all lead CSVs under <drop_dir>/leads/<workflow_id>/*.csv.
    Processes files in filename order. Skips files already ingested.
    """
    if schemas is None:
        schemas = load_workflow_schemas()

    leads_base = drop_dir / "leads"
    if not leads_base.exists():
        return

    # Find workflow directories
    for wf_dir in sorted(leads_base.iterdir()):
        if not wf_dir.is_dir():
            continue
        wf_id = wf_dir.name
        schema = schemas.get(wf_id)
        if not schema:
            continue

        # Load existing leads cache for this workflow
        cur = conn.execute(
            """
            SELECT phone_hash, external_ref, customer_name_encrypted, fields_json
            FROM leads
            WHERE workflow_id = ?;
            """,
            (wf_id,),
        )
        leads_cache = {
            r[0]: (r[1], r[2], json.loads(r[3])) for r in cur.fetchall()
        }

        # Files in filename order
        csv_files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
        for csv_path in csv_files:
            rel_path = str(csv_path.relative_to(drop_dir.parent))
            # Check if file was already ingested
            cur = conn.execute(
                "SELECT 1 FROM file_metrics WHERE file_path = ?;", (rel_path,)
            )
            if cur.fetchone() is not None:
                continue

            with conn:
                m = ingest_lead_file(conn, csv_path, wf_id, schema, leads_cache)
                conn.execute(
                    """
                    INSERT INTO file_metrics (
                        file_path, file_type, lead_rows_total, lead_rows_rejected,
                        duplicate_lead_rows_merged, field_warnings
                    ) VALUES (?, 'lead_csv', ?, ?, ?, ?);
                    """,
                    (
                        rel_path,
                        m["lead_rows_total"],
                        m["lead_rows_rejected"],
                        m["duplicate_lead_rows_merged"],
                        m["field_warnings"],
                    ),
                )
