"""End-to-end integration tests for the ingestion and reporting pipeline."""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from solution.config import REPO_ROOT
from solution.db import get_connection, init_db
from solution.event_ingestor import ingest_all_events_in_drop
from solution.lead_ingestor import ingest_all_leads_in_drop
from solution.reporter import generate_reports


@pytest.fixture(scope="module")
def synthetic_data(tmp_path_factory):
    """Generates synthetic dataset using tools/generate_data.py on seed 42."""
    data_dir = tmp_path_factory.mktemp("test_data")
    generator_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"

    subprocess.run(
        [
            sys.executable,
            str(generator_script),
            "--seed",
            "42",
            "--scale",
            "small",
            "--out",
            str(data_dir),
        ],
        check=True,
    )
    return data_dir


def test_full_pipeline_ingestion_and_reporting(synthetic_data, tmp_path):
    """
    Tests:
    1. Ingestion of drop_1
    2. Ingestion of drop_2
    3. Idempotent re-ingestion of drop_1
    4. Report generation and output contracts
    """
    db_path = tmp_path / "test_pipeline.db"
    reports_dir = tmp_path / "reports"

    conn = get_connection(db_path)
    init_db(conn)

    # Ingest drop 1
    drop_1 = synthetic_data / "drop_1"
    ingest_all_leads_in_drop(conn, drop_1)
    ingest_all_events_in_drop(conn, drop_1)

    # Ingest drop 2
    drop_2 = synthetic_data / "drop_2"
    ingest_all_leads_in_drop(conn, drop_2)
    ingest_all_events_in_drop(conn, drop_2)

    # Re-ingest drop 1 to test idempotency
    ingest_all_leads_in_drop(conn, drop_1)
    ingest_all_events_in_drop(conn, drop_1)

    # Generate reports
    generate_reports(conn, reports_dir)
    conn.close()

    # 1. Verify batch_summary.csv
    batch_summary = reports_dir / "batch_summary.csv"
    assert batch_summary.exists()
    with open(batch_summary, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ]
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            tot = int(r["total_leads"])
            b_sum = (
                int(r["yet_to_reach_out"])
                + int(r["in_progress_telephony_failed"])
                + int(r["in_progress_dialed"])
                + int(r["completed"])
            )
            assert tot == b_sum, f"Mismatch in batch {r['batch_id']}: {tot} != {b_sum}"

    # 2. Verify daily_overview.csv
    daily_overview = reports_dir / "daily_overview.csv"
    assert daily_overview.exists()
    with open(daily_overview, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ]
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            tot = int(r["total_calls"])
            conn_calls = int(r["connected_calls"])
            rate = float(r["connectivity_rate"])
            assert tot >= 1
            assert conn_calls <= tot
            assert len(r["connectivity_rate"].split(".")[1]) == 4  # exactly 4 decimals

    # 3. Verify lead_report.csv
    lead_report = reports_dir / "lead_report.csv"
    assert lead_report.exists()
    with open(lead_report, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
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
        ]
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            assert r["phone_masked"].startswith("+91******")
            assert len(r["phone_masked"]) == 13
            assert r["bucket"] in (
                "yet_to_reach_out",
                "completed",
                "in_progress_telephony_failed",
                "in_progress_dialed",
            )
            assert r["ever_success"] in ("true", "false")
            if int(r["total_calls"]) == 0:
                assert r["bucket"] == "yet_to_reach_out"
                assert r["last_call_at"] == ""
                assert r["current_disposition"] == ""
            else:
                assert r["last_call_at"] != ""

    # 4. Verify rejections.csv
    rejections = reports_dir / "rejections.csv"
    assert rejections.exists()
    with open(rejections, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            "workflow_id",
            "batch_id",
            "row_number",
            "reason",
        ]
        rows = list(reader)
        assert len(rows) > 0

    # 5. Verify data_quality.json
    dq_path = reports_dir / "data_quality.json"
    assert dq_path.exists()
    with open(dq_path, "r", encoding="utf-8") as f:
        dq = json.load(f)
        required_keys = [
            "lead_rows_total",
            "lead_rows_rejected",
            "rejections_by_reason",
            "duplicate_lead_rows_merged",
            "field_warnings",
            "events_total",
            "malformed_events",
            "duplicate_events_dropped",
            "orphan_events",
            "unknown_disposition_events",
        ]
        for k in required_keys:
            assert k in dq
        assert dq["lead_rows_total"] > 0
        assert dq["lead_rows_rejected"] > 0
        assert dq["duplicate_lead_rows_merged"] > 0
        assert dq["events_total"] > 0
        assert dq["malformed_events"] > 0
        assert dq["duplicate_events_dropped"] > 0
