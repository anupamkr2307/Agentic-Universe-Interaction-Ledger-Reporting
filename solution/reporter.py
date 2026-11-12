"""Reporting engine: derives metrics from ledger and outputs CSV and JSON reports."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from solution.config import load_dispositions


def generate_reports(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    Generates the five report files matching the output contract:
    1. batch_summary.csv
    2. daily_overview.csv
    3. lead_report.csv
    4. rejections.csv
    5. data_quality.json
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = load_dispositions()

    # -------------------------------------------------------------
    # 1. Resolve call links, initiation times, statuses, and effective dispositions
    # -------------------------------------------------------------
    # Step 1a: Map call_id -> (workflow_id, phone_hash)
    cur = conn.execute(
        """
        SELECT workflow_id, call_id, phone_hash
        FROM events
        WHERE phone_hash IS NOT NULL
        GROUP BY workflow_id, call_id;
        """
    )
    call_to_phone: dict[str, tuple[str, str]] = {}
    for r in cur.fetchall():
        call_to_phone[r["call_id"]] = (r["workflow_id"], r["phone_hash"])

    # Step 1b: Fetch all leads into memory for reporting
    cur = conn.execute(
        """
        SELECT workflow_id, phone_hash, external_ref, batch_id, phone_masked
        FROM leads;
        """
    )
    leads_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in cur.fetchall():
        key = (r["workflow_id"], r["phone_hash"])
        leads_by_key[key] = {
            "workflow_id": r["workflow_id"],
            "phone_hash": r["phone_hash"],
            "external_ref": r["external_ref"],
            "batch_id": r["batch_id"],
            "phone_masked": r["phone_masked"],
            "calls": [],  # list of call dicts
        }

    # Step 1c: Fetch all events, grouped by call_id
    cur = conn.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id, occurred_at_ist,
               disposition_code, is_valid_disposition
        FROM events
        ORDER BY occurred_at_ist ASC;
        """
    )
    events_by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in cur.fetchall():
        events_by_call[r["call_id"]].append(dict(r))

    # Step 1d: Process calls
    # A call is counted only if its call.initiated event is present (and linked)
    calls_by_lead: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    calls_by_date_wf: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for call_id, ev_list in events_by_call.items():
        if call_id not in call_to_phone:
            continue
        wf_id, phone_hash = call_to_phone[call_id]
        lead_key = (wf_id, phone_hash)
        if lead_key not in leads_by_key:
            # Orphan call (phone not in leads)
            continue

        # Check call.initiated
        init_events = [e for e in ev_list if e["event_type"] == "call.initiated"]
        if not init_events:
            # Not counted as a call per spec
            continue

        # Sort init_events by occurred_at_ist (usually just one)
        first_init = sorted(init_events, key=lambda e: e["occurred_at_ist"])[0]
        init_time_str = first_init["occurred_at_ist"]
        call_date = init_time_str[:10]  # YYYY-MM-DD in IST

        has_connected = any(e["event_type"] == "call.connected" for e in ev_list)
        has_telephony_failed = any(
            e["event_type"] == "call.telephony_failed" for e in ev_list
        )

        # Effective disposition:
        # Latest disposition.override by occurred_at.
        # If no override, latest disposition.set.
        # Must be valid in catalogue.
        overrides = [
            e
            for e in ev_list
            if e["event_type"] == "disposition.override" and e["is_valid_disposition"] == 1
        ]
        sets = [
            e
            for e in ev_list
            if e["event_type"] == "disposition.set" and e["is_valid_disposition"] == 1
        ]

        effective_disp: str | None = None
        if overrides:
            effective_disp = sorted(overrides, key=lambda e: e["occurred_at_ist"])[-1][
                "disposition_code"
            ]
        elif sets:
            effective_disp = sorted(sets, key=lambda e: e["occurred_at_ist"])[-1][
                "disposition_code"
            ]

        # Catalogue lookup
        is_terminal = False
        is_success = False
        if effective_disp:
            disp_info = catalog.get(wf_id, {}).get(effective_disp)
            if disp_info:
                is_terminal = disp_info["is_terminal"]
                is_success = disp_info["is_success"]

        call_data = {
            "call_id": call_id,
            "workflow_id": wf_id,
            "lead_key": lead_key,
            "call_date": call_date,
            "init_time": init_time_str,
            "connected": has_connected,
            "telephony_failed": has_telephony_failed,
            "effective_disposition": effective_disp,
            "is_terminal": is_terminal,
            "is_success": is_success,
        }
        calls_by_lead[lead_key].append(call_data)
        calls_by_date_wf[(wf_id, call_date)].append(call_data)

    # -------------------------------------------------------------
    # 2. Derive Lead Metrics & Bucketing
    # -------------------------------------------------------------
    # Bucket rules:
    # 1. yet_to_reach_out: no calls
    # 2. completed: any call's effective disposition is terminal (is_terminal=true)
    # 3. in_progress_telephony_failed: every call has a call.telephony_failed event
    # 4. in_progress_dialed: everything else
    lead_rows_out = []
    batch_stats: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "total_leads": 0,
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        }
    )

    for lead_key, lead in leads_by_key.items():
        wf_id = lead["workflow_id"]
        batch_id = lead["batch_id"]
        lead_calls = calls_by_lead.get(lead_key, [])
        total_calls = len(lead_calls)

        connected_calls = sum(1 for c in lead_calls if c["connected"])
        ever_success = any(c["is_success"] for c in lead_calls)

        # Bucket calculation
        if total_calls == 0:
            bucket = "yet_to_reach_out"
            last_call_at = ""
            current_disposition = ""
        else:
            # Sort calls by initiated time
            lead_calls_sorted = sorted(lead_calls, key=lambda c: c["init_time"])
            last_call_at = lead_calls_sorted[-1]["init_time"]

            # Current disposition: effective disposition of the most recent call
            # (by initiated time) that has one. Empty if none.
            calls_with_disp = [
                c for c in lead_calls_sorted if c["effective_disposition"] is not None
            ]
            current_disposition = (
                calls_with_disp[-1]["effective_disposition"] if calls_with_disp else ""
            )

            if any(c["is_terminal"] for c in lead_calls):
                bucket = "completed"
            elif all(c["telephony_failed"] for c in lead_calls):
                bucket = "in_progress_telephony_failed"
            else:
                bucket = "in_progress_dialed"

        # Accumulate batch stats
        bst = batch_stats[(wf_id, batch_id)]
        bst["total_leads"] += 1
        bst[bucket] += 1

        lead_rows_out.append(
            {
                "workflow_id": wf_id,
                "external_ref": lead["external_ref"],
                "batch_id": batch_id,
                "phone_masked": lead["phone_masked"],
                "bucket": bucket,
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "last_call_at": last_call_at,
                "current_disposition": current_disposition,
                "ever_success": "true" if ever_success else "false",
            }
        )

    # -------------------------------------------------------------
    # 3. Write Reports
    # -------------------------------------------------------------

    # 1. batch_summary.csv: sorted by workflow_id, batch_id
    batch_summary_path = out_dir / "batch_summary.csv"
    with open(batch_summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ]
        )
        for (wf_id, b_id), st in sorted(batch_stats.items(), key=lambda x: (x[0][0], x[0][1])):
            writer.writerow(
                [
                    wf_id,
                    b_id,
                    st["total_leads"],
                    st["yet_to_reach_out"],
                    st["in_progress_telephony_failed"],
                    st["in_progress_dialed"],
                    st["completed"],
                ]
            )

    # 2. daily_overview.csv: one row per workflow per IST date that has at least one call, sorted by workflow_id, date
    daily_overview_path = out_dir / "daily_overview.csv"
    with open(daily_overview_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ]
        )
        for (wf_id, cdate), clist in sorted(
            calls_by_date_wf.items(), key=lambda x: (x[0][0], x[0][1])
        ):
            tot_calls = len(clist)
            if tot_calls == 0:
                continue
            conn_calls = sum(1 for c in clist if c["connected"])
            rate = f"{(conn_calls / tot_calls):.4f}"

            # Distinct leads with at least one call on that date whose effective disposition has is_success=true
            succ_leads_set = {
                c["lead_key"] for c in clist if c["is_success"]
            }
            writer.writerow(
                [
                    wf_id,
                    cdate,
                    tot_calls,
                    conn_calls,
                    rate,
                    len(succ_leads_set),
                ]
            )

    # 3. lead_report.csv: sorted by workflow_id, external_ref
    lead_report_path = out_dir / "lead_report.csv"
    lead_rows_out.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    with open(lead_report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ],
        )
        writer.writeheader()
        writer.writerows(lead_rows_out)

    # 4. rejections.csv: sorted by workflow_id, batch_id, row_number. No PII.
    rejections_path = out_dir / "rejections.csv"
    cur = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number;
        """
    )
    with open(rejections_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        for r in cur.fetchall():
            writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])

    # 5. data_quality.json
    # Lead metrics from file_metrics
    cur = conn.execute(
        """
        SELECT
            COALESCE(SUM(lead_rows_total), 0) AS lead_rows_total,
            COALESCE(SUM(duplicate_lead_rows_merged), 0) AS duplicate_lead_rows_merged,
            COALESCE(SUM(field_warnings), 0) AS field_warnings
        FROM file_metrics
        WHERE file_type = 'lead_csv';
        """
    )
    l_row = cur.fetchone()

    # Rejections total and by reason
    cur = conn.execute("SELECT COUNT(*) AS total_rej FROM rejections;")
    rej_tot = cur.fetchone()["total_rej"]

    cur = conn.execute(
        """
        SELECT reason, COUNT(*) as cnt
        FROM rejections
        GROUP BY reason
        ORDER BY reason;
        """
    )
    rejections_by_reason = {r["reason"]: r["cnt"] for r in cur.fetchall()}

    # Event metrics from file_metrics
    cur = conn.execute(
        """
        SELECT
            COALESCE(SUM(events_total), 0) AS events_total,
            COALESCE(SUM(malformed_events), 0) AS malformed_events,
            COALESCE(SUM(duplicate_events_dropped), 0) AS duplicate_events_dropped,
            COALESCE(SUM(unknown_disposition_events), 0) AS unknown_disposition_events
        FROM file_metrics
        WHERE file_type = 'event_jsonl';
        """
    )
    e_row = cur.fetchone()

    # Orphan events: events that cannot be linked to any valid lead
    cur = conn.execute(
        """
        WITH call_phones AS (
            SELECT workflow_id, call_id, phone_hash
            FROM events
            WHERE phone_hash IS NOT NULL
            GROUP BY workflow_id, call_id
        ),
        linked_calls AS (
            SELECT cp.workflow_id, cp.call_id
            FROM call_phones cp
            JOIN leads l ON cp.workflow_id = l.workflow_id AND cp.phone_hash = l.phone_hash
        )
        SELECT COUNT(*) AS orphan_count
        FROM events e
        WHERE NOT EXISTS (
            SELECT 1 FROM linked_calls lc
            WHERE lc.workflow_id = e.workflow_id AND lc.call_id = e.call_id
        );
        """
    )
    orphan_events = cur.fetchone()["orphan_count"]

    dq_data = {
        "lead_rows_total": l_row["lead_rows_total"],
        "lead_rows_rejected": rej_tot,
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": l_row["duplicate_lead_rows_merged"],
        "field_warnings": l_row["field_warnings"],
        "events_total": e_row["events_total"],
        "malformed_events": e_row["malformed_events"],
        "duplicate_events_dropped": e_row["duplicate_events_dropped"],
        "orphan_events": orphan_events,
        "unknown_disposition_events": e_row["unknown_disposition_events"],
    }

    dq_path = out_dir / "data_quality.json"
    with open(dq_path, "w", encoding="utf-8") as f:
        json.dump(dq_data, f, indent=2)
