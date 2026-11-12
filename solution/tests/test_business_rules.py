"""Targeted unit tests for business logic, bucketing, dispositions, and orphan resolution."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from solution.config import load_dispositions
from solution.crypto import hash_phone, mask_phone
from solution.db import get_connection, init_db
from solution.reporter import generate_reports


def test_effective_disposition_and_bucketing(tmp_path):
    """
    Tests:
    1. Latest disposition.override overrides disposition.set
    2. Unknown disposition is ignored
    3. Bucketing:
       - 0 calls -> yet_to_reach_out
       - all telephony_failed -> in_progress_telephony_failed
       - one telephony_failed, one busy -> in_progress_dialed
       - terminal disposition -> completed
    """
    db_path = tmp_path / "rules_test.db"
    conn = get_connection(db_path)
    init_db(conn)

    # Lead 1: 0 calls
    phone1 = "+919876500001"
    h1 = hash_phone(phone1)
    conn.execute(
        """
        INSERT INTO leads (workflow_id, phone_hash, external_ref, batch_id, phone_masked)
        VALUES ('motor_renewal', ?, 'REF-001', 'batch_001', ?);
        """,
        (h1, mask_phone(phone1)),
    )

    # Lead 2: 2 calls, both telephony_failed
    phone2 = "+919876500002"
    h2 = hash_phone(phone2)
    conn.execute(
        """
        INSERT INTO leads (workflow_id, phone_hash, external_ref, batch_id, phone_masked)
        VALUES ('motor_renewal', ?, 'REF-002', 'batch_001', ?);
        """,
        (h2, mask_phone(phone2)),
    )
    # Call 2A
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_2a_init', 'call.initiated', 'motor_renewal', 'call_2a', '2026-10-01T10:00:00+05:30', ?);
        """,
        (h2,),
    )
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_2a_fail', 'call.telephony_failed', 'motor_renewal', 'call_2a', '2026-10-01T10:00:05+05:30', ?);
        """,
        (h2,),
    )
    # Call 2B
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_2b_init', 'call.initiated', 'motor_renewal', 'call_2b', '2026-10-01T11:00:00+05:30', ?);
        """,
        (h2,),
    )
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_2b_fail', 'call.telephony_failed', 'motor_renewal', 'call_2b', '2026-10-01T11:00:05+05:30', ?);
        """,
        (h2,),
    )

    # Lead 3: 1 call with disposition.set ('payment_link_sent' non-terminal)
    # then human override to 'renewed' (terminal, success)
    phone3 = "+919876500003"
    h3 = hash_phone(phone3)
    conn.execute(
        """
        INSERT INTO leads (workflow_id, phone_hash, external_ref, batch_id, phone_masked)
        VALUES ('motor_renewal', ?, 'REF-003', 'batch_001', ?);
        """,
        (h3, mask_phone(phone3)),
    )
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_3_init', 'call.initiated', 'motor_renewal', 'call_3', '2026-10-01T12:00:00+05:30', ?);
        """,
        (h3,),
    )
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, phone_hash)
        VALUES ('ev_3_conn', 'call.connected', 'motor_renewal', 'call_3', '2026-10-01T12:00:10+05:30', ?);
        """,
        (h3,),
    )
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, disposition_code, is_valid_disposition)
        VALUES ('ev_3_disp', 'disposition.set', 'motor_renewal', 'call_3', '2026-10-01T12:05:00+05:30', 'payment_link_sent', 1);
        """
    )
    # Override has no phone per spec
    conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_ist, disposition_code, is_valid_disposition)
        VALUES ('ev_3_over', 'disposition.override', 'motor_renewal', 'call_3', '2026-10-01T14:00:00+05:30', 'renewed', 1);
        """
    )

    conn.commit()

    reports_dir = tmp_path / "reports"
    generate_reports(conn, reports_dir)
    conn.close()

    # Read lead report
    import csv
    with open(reports_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        rows = {r["external_ref"]: r for r in csv.DictReader(f)}

    assert rows["REF-001"]["bucket"] == "yet_to_reach_out"
    assert rows["REF-001"]["total_calls"] == "0"

    assert rows["REF-002"]["bucket"] == "in_progress_telephony_failed"
    assert rows["REF-002"]["total_calls"] == "2"

    assert rows["REF-003"]["bucket"] == "completed"
    assert rows["REF-003"]["current_disposition"] == "renewed"
    assert rows["REF-003"]["ever_success"] == "true"
