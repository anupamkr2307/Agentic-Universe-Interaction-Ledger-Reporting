"""FastAPI Web Dashboard for Assignment A: Interaction Ledger & Reporting Pipeline.
Compatible with local environments and Vercel Serverless Functions.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from solution.config import DEFAULT_DB_PATH, REPO_ROOT
from solution.db import get_connection, init_db
from solution.event_ingestor import ingest_all_events_in_drop
from solution.lead_ingestor import ingest_all_leads_in_drop
from solution.reporter import generate_reports

app = FastAPI(title="Agentic Universe - Interaction Ledger & Reporting Pipeline")

IS_VERCEL = bool(os.environ.get("VERCEL"))
if IS_VERCEL:
    _BASE_TMP = Path(tempfile.gettempdir()) / "au_pipeline"
    REPORTS_DIR = _BASE_TMP / "reports"
    DB_PATH = _BASE_TMP / "pipeline.db"
    DATA_DIR = _BASE_TMP / "data"
else:
    REPORTS_DIR = REPO_ROOT / "reports"
    DB_PATH = DEFAULT_DB_PATH
    DATA_DIR = REPO_ROOT / "data"


def ensure_reports_initialized() -> Path:
    """Ensures reports exist; if not (e.g. on clean Vercel deploy), generates them automatically."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dq_file = REPORTS_DIR / "data_quality.json"

    if not dq_file.exists():
        # Auto-generate small synthetic data and run pipeline
        gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
        if gen_script.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                # Run generator
                from assignment_a_data_tools import run_generate  # fallback below
            except ImportError:
                subprocess.run(
                    [
                        sys.executable,
                        str(gen_script),
                        "--seed",
                        "42",
                        "--scale",
                        "small",
                        "--out",
                        str(DATA_DIR),
                    ],
                    check=False,
                )

            # Ingest and report
            conn = get_connection(DB_PATH)
            init_db(conn)
            if (DATA_DIR / "drop_1").exists():
                ingest_all_leads_in_drop(conn, DATA_DIR / "drop_1")
                ingest_all_events_in_drop(conn, DATA_DIR / "drop_1")
            if (DATA_DIR / "drop_2").exists():
                ingest_all_leads_in_drop(conn, DATA_DIR / "drop_2")
                ingest_all_events_in_drop(conn, DATA_DIR / "drop_2")
            generate_reports(conn, REPORTS_DIR)
            conn.close()

    return REPORTS_DIR


@app.get("/api/quality")
def get_data_quality() -> dict[str, Any]:
    r_dir = ensure_reports_initialized()
    dq_file = r_dir / "data_quality.json"
    if dq_file.exists():
        with open(dq_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@app.get("/api/batch-summary")
def get_batch_summary() -> list[dict[str, Any]]:
    r_dir = ensure_reports_initialized()
    bs_file = r_dir / "batch_summary.csv"
    if bs_file.exists():
        with open(bs_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return []


@app.get("/api/daily-overview")
def get_daily_overview() -> list[dict[str, Any]]:
    r_dir = ensure_reports_initialized()
    do_file = r_dir / "daily_overview.csv"
    if do_file.exists():
        with open(do_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return []


@app.get("/api/leads")
def get_leads(
    q: str = "",
    bucket: str = "",
    workflow_id: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
) -> dict[str, Any]:
    r_dir = ensure_reports_initialized()
    lr_file = r_dir / "lead_report.csv"
    if not lr_file.exists():
        return {"total": 0, "page": page, "page_size": page_size, "items": []}

    with open(lr_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_leads = list(reader)

    filtered = []
    q_lower = q.lower().strip()
    for row in all_leads:
        if workflow_id and row["workflow_id"] != workflow_id:
            continue
        if bucket and row["bucket"] != bucket:
            continue
        if q_lower:
            text = f"{row['external_ref']} {row['phone_masked']} {row['batch_id']}".lower()
            if q_lower not in text:
                continue
        filtered.append(row)

    total = len(filtered)
    start = (page - 1) * page_size
    items = filtered[start : start + page_size]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@app.get("/api/rejections")
def get_rejections(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
) -> dict[str, Any]:
    r_dir = ensure_reports_initialized()
    rej_file = r_dir / "rejections.csv"
    if not rej_file.exists():
        return {"total": 0, "page": page, "page_size": page_size, "items": []}

    with open(rej_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rej = list(reader)

    total = len(all_rej)
    start = (page - 1) * page_size
    items = all_rej[start : start + page_size]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@app.post("/api/action/ingest")
def trigger_ingest(drop: str = Query("drop_1")) -> dict[str, str]:
    drop_dir = DATA_DIR / drop
    if not drop_dir.exists():
        # Fallback to REPO_ROOT / data
        drop_dir = REPO_ROOT / "data" / drop
    if not drop_dir.exists():
        return {"status": "error", "message": f"Drop directory '{drop}' not found"}

    conn = get_connection(DB_PATH)
    init_db(conn)
    ingest_all_leads_in_drop(conn, drop_dir)
    ingest_all_events_in_drop(conn, drop_dir)
    conn.close()

    # Re-generate reports
    conn = get_connection(DB_PATH)
    generate_reports(conn, REPORTS_DIR)
    conn.close()

    return {"status": "success", "message": f"Successfully ingested {drop} and regenerated reports"}


@app.post("/api/action/report")
def trigger_report() -> dict[str, str]:
    conn = get_connection(DB_PATH)
    init_db(conn)
    generate_reports(conn, REPORTS_DIR)
    conn.close()
    return {"status": "success", "message": "Reports refreshed successfully"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Agentic Universe – Data & Interaction Ledger Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body { background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .navbar { background: #0f172a; }
    .card { border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    .stat-card { border-left: 4px solid #3b82f6; }
    .badge-completed { background-color: #10b981; }
    .badge-in_progress_dialed { background-color: #3b82f6; }
    .badge-in_progress_telephony_failed { background-color: #f59e0b; }
    .badge-yet_to_reach_out { background-color: #64748b; }
    .nav-pills .nav-link.active { background-color: #2563eb; }
    .table th { background-color: #f1f5f9; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.05em; color: #475569; }
    .table td { font-size: 0.9rem; vertical-align: middle; }
    .code-pill { font-family: monospace; background: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }
  </style>
</head>
<body class="pb-5">
  <nav class="navbar navbar-dark px-4 py-3 mb-4">
    <div class="container-fluid">
      <span class="navbar-brand fw-bold fs-5">
        <i class="bi bi-diagram-3-fill me-2 text-primary"></i> Agentic Universe &middot; Interaction Ledger & Reporting
      </span>
      <div class="d-flex gap-2">
        <button class="btn btn-sm btn-outline-light" onclick="triggerIngest('drop_1')">
          <i class="bi bi-cloud-arrow-down me-1"></i> Ingest Drop 1
        </button>
        <button class="btn btn-sm btn-outline-light" onclick="triggerIngest('drop_2')">
          <i class="bi bi-cloud-arrow-down me-1"></i> Ingest Drop 2
        </button>
        <button class="btn btn-sm btn-primary" onclick="triggerReport()">
          <i class="bi bi-arrow-clockwise me-1"></i> Refresh Reports
        </button>
      </div>
    </div>
  </nav>

  <div class="container-fluid px-4">
    <!-- Key Metric Cards -->
    <div class="row g-3 mb-4" id="stat-cards">
      <div class="col-6 col-md-3">
        <div class="card p-3 stat-card">
          <div class="text-secondary small fw-semibold">TOTAL LEADS READ</div>
          <div class="fs-3 fw-bold text-dark" id="stat-leads-total">--</div>
          <div class="small text-muted" id="stat-leads-valid">--</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3 stat-card" style="border-left-color: #ef4444;">
          <div class="text-secondary small fw-semibold">REJECTED ROWS</div>
          <div class="fs-3 fw-bold text-danger" id="stat-rejections">--</div>
          <div class="small text-muted" id="stat-merged-dups">--</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3 stat-card" style="border-left-color: #10b981;">
          <div class="text-secondary small fw-semibold">TOTAL EVENTS READ</div>
          <div class="fs-3 fw-bold text-success" id="stat-events-total">--</div>
          <div class="small text-muted" id="stat-events-dropped">--</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card p-3 stat-card" style="border-left-color: #f59e0b;">
          <div class="text-secondary small fw-semibold">ORPHAN EVENTS</div>
          <div class="fs-3 fw-bold text-warning" id="stat-orphans">--</div>
          <div class="small text-muted" id="stat-malformed">--</div>
        </div>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <ul class="nav nav-pills mb-3" id="pills-tab" role="tablist">
      <li class="nav-item">
        <button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-batch" onclick="loadBatchSummary()">
          <i class="bi bi-stack me-1"></i> Batch Summary
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-daily" onclick="loadDailyOverview()">
          <i class="bi bi-calendar-event me-1"></i> Daily Overview
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-leads" onclick="loadLeads(1)">
          <i class="bi bi-people me-1"></i> Lead Explorer
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-rejections" onclick="loadRejections(1)">
          <i class="bi bi-shield-x me-1"></i> Rejections Log
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-quality" onclick="loadQualityJson()">
          <i class="bi bi-code-slash me-1"></i> Data Quality JSON
        </button>
      </li>
    </ul>

    <!-- Tab Contents -->
    <div class="tab-content" id="pills-tabContent">
      <!-- 1. Batch Summary -->
      <div class="tab-pane fade show active" id="tab-batch">
        <div class="card p-3">
          <h5 class="fw-bold mb-3"><i class="bi bi-stack me-2 text-primary"></i>Batch Summary (batch_summary.csv)</h5>
          <div class="table-responsive">
            <table class="table table-hover table-striped align-middle" id="table-batch">
              <thead>
                <tr>
                  <th>Workflow ID</th>
                  <th>Batch ID</th>
                  <th>Total Leads</th>
                  <th>Yet to Reach Out</th>
                  <th>Telephony Failed</th>
                  <th>Dialed In Progress</th>
                  <th>Completed</th>
                </tr>
              </thead>
              <tbody id="tbody-batch"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 2. Daily Overview -->
      <div class="tab-pane fade" id="tab-daily">
        <div class="card p-3">
          <h5 class="fw-bold mb-3"><i class="bi bi-calendar-event me-2 text-primary"></i>Daily Overview (daily_overview.csv)</h5>
          <div class="table-responsive">
            <table class="table table-hover table-striped align-middle" id="table-daily">
              <thead>
                <tr>
                  <th>Workflow ID</th>
                  <th>Date (IST)</th>
                  <th>Total Calls</th>
                  <th>Connected Calls</th>
                  <th>Connectivity Rate</th>
                  <th>Success Leads</th>
                </tr>
              </thead>
              <tbody id="tbody-daily"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 3. Lead Explorer -->
      <div class="tab-pane fade" id="tab-leads">
        <div class="card p-3">
          <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
            <h5 class="fw-bold mb-0"><i class="bi bi-people me-2 text-primary"></i>Lead Report (lead_report.csv)</h5>
            <div class="d-flex gap-2">
              <input type="text" id="lead-search" class="form-control form-control-sm" placeholder="Search ref or phone..." style="width: 220px;" onkeyup="debouncedLeadSearch()">
              <select id="lead-filter-bucket" class="form-select form-select-sm" style="width: 180px;" onchange="loadLeads(1)">
                <option value="">All Buckets</option>
                <option value="completed">Completed</option>
                <option value="in_progress_dialed">In Progress Dialed</option>
                <option value="in_progress_telephony_failed">Telephony Failed</option>
                <option value="yet_to_reach_out">Yet To Reach Out</option>
              </select>
            </div>
          </div>
          <div class="table-responsive">
            <table class="table table-hover table-striped align-middle" id="table-leads">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>External Ref</th>
                  <th>Batch</th>
                  <th>Masked Phone</th>
                  <th>Bucket</th>
                  <th>Calls</th>
                  <th>Connected</th>
                  <th>Last Call (IST)</th>
                  <th>Current Disposition</th>
                  <th>Ever Success</th>
                </tr>
              </thead>
              <tbody id="tbody-leads"></tbody>
            </table>
          </div>
          <div class="d-flex justify-content-between align-items-center mt-3">
            <span class="small text-muted" id="leads-pagination-info">Showing --</span>
            <div class="btn-group btn-group-sm">
              <button class="btn btn-outline-secondary" id="btn-prev-lead" onclick="prevLeadPage()">Previous</button>
              <button class="btn btn-outline-secondary" id="btn-next-lead" onclick="nextLeadPage()">Next</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 4. Rejections Log -->
      <div class="tab-pane fade" id="tab-rejections">
        <div class="card p-3">
          <h5 class="fw-bold mb-3"><i class="bi bi-shield-x me-2 text-danger"></i>Rejections Log (rejections.csv)</h5>
          <div class="table-responsive">
            <table class="table table-hover table-striped align-middle" id="table-rejections">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Batch ID</th>
                  <th>Row Number</th>
                  <th>Rejection Reason</th>
                </tr>
              </thead>
              <tbody id="tbody-rejections"></tbody>
            </table>
          </div>
          <div class="d-flex justify-content-between align-items-center mt-3">
            <span class="small text-muted" id="rejections-pagination-info">Showing --</span>
            <div class="btn-group btn-group-sm">
              <button class="btn btn-outline-secondary" id="btn-prev-rej" onclick="prevRejPage()">Previous</button>
              <button class="btn btn-outline-secondary" id="btn-next-rej" onclick="nextRejPage()">Next</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 5. Data Quality JSON -->
      <div class="tab-pane fade" id="tab-quality">
        <div class="card p-3">
          <h5 class="fw-bold mb-3"><i class="bi bi-code-slash me-2 text-primary"></i>Data Quality Metrics (data_quality.json)</h5>
          <pre class="bg-light p-3 rounded border" id="code-quality-json" style="max-height: 500px; overflow-y: auto; font-size: 0.95rem;"></pre>
        </div>
      </div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    let currentLeadPage = 1;
    let currentRejPage = 1;
    let leadSearchTimeout = null;

    async function loadStats() {
      try {
        const res = await fetch('/api/quality');
        const q = await res.json();
        document.getElementById('stat-leads-total').innerText = (q.lead_rows_total || 0).toLocaleString();
        document.getElementById('stat-leads-valid').innerText = `${(q.field_warnings || 0).toLocaleString()} field warnings`;
        document.getElementById('stat-rejections').innerText = (q.lead_rows_rejected || 0).toLocaleString();
        document.getElementById('stat-merged-dups').innerText = `${(q.duplicate_lead_rows_merged || 0).toLocaleString()} duplicates merged`;
        document.getElementById('stat-events-total').innerText = (q.events_total || 0).toLocaleString();
        document.getElementById('stat-events-dropped').innerText = `${(q.duplicate_events_dropped || 0).toLocaleString()} duplicate events dropped`;
        document.getElementById('stat-orphans').innerText = (q.orphan_events || 0).toLocaleString();
        document.getElementById('stat-malformed').innerText = `${(q.malformed_events || 0).toLocaleString()} malformed lines`;
      } catch (e) { console.error(e); }
    }

    async function loadBatchSummary() {
      const res = await fetch('/api/batch-summary');
      const rows = await res.json();
      const tbody = document.getElementById('tbody-batch');
      tbody.innerHTML = rows.map(r => `
        <tr>
          <td><span class="badge bg-dark">${r.workflow_id}</span></td>
          <td class="fw-semibold">${r.batch_id}</td>
          <td class="fw-bold">${r.total_leads}</td>
          <td><span class="badge badge-yet_to_reach_out">${r.yet_to_reach_out}</span></td>
          <td><span class="badge badge-in_progress_telephony_failed">${r.in_progress_telephony_failed}</span></td>
          <td><span class="badge badge-in_progress_dialed">${r.in_progress_dialed}</span></td>
          <td><span class="badge badge-completed">${r.completed}</span></td>
        </tr>
      `).join('');
    }

    async function loadDailyOverview() {
      const res = await fetch('/api/daily-overview');
      const rows = await res.json();
      const tbody = document.getElementById('tbody-daily');
      tbody.innerHTML = rows.map(r => {
        const ratePct = (parseFloat(r.connectivity_rate) * 100).toFixed(1);
        return `
        <tr>
          <td><span class="badge bg-dark">${r.workflow_id}</span></td>
          <td class="font-monospace">${r.date}</td>
          <td class="fw-semibold">${r.total_calls}</td>
          <td>${r.connected_calls}</td>
          <td>
            <div class="d-flex align-items-center gap-2">
              <div class="progress flex-grow-1" style="height: 6px;">
                <div class="progress-bar bg-primary" style="width: ${ratePct}%"></div>
              </div>
              <span class="small font-monospace">${r.connectivity_rate}</span>
            </div>
          </td>
          <td><span class="badge bg-success">${r.success_leads}</span></td>
        </tr>
      `}).join('');
    }

    async function loadLeads(page = 1) {
      currentLeadPage = page;
      const q = document.getElementById('lead-search').value;
      const bucket = document.getElementById('lead-filter-bucket').value;
      const res = await fetch(`/api/leads?page=${page}&q=${encodeURIComponent(q)}&bucket=${encodeURIComponent(bucket)}`);
      const data = await res.json();
      const tbody = document.getElementById('tbody-leads');

      tbody.innerHTML = data.items.map(r => `
        <tr>
          <td><span class="badge bg-dark">${r.workflow_id}</span></td>
          <td class="font-monospace fw-semibold">${r.external_ref}</td>
          <td>${r.batch_id}</td>
          <td class="font-monospace">${r.phone_masked}</td>
          <td><span class="badge badge-${r.bucket}">${r.bucket.replace(/_/g, ' ')}</span></td>
          <td>${r.total_calls}</td>
          <td>${r.connected_calls}</td>
          <td class="small font-monospace text-muted">${r.last_call_at || '--'}</td>
          <td>${r.current_disposition ? '<span class="code-pill">' + r.current_disposition + '</span>' : '--'}</td>
          <td>${r.ever_success === 'true' ? '<span class="badge bg-success">Yes</span>' : '<span class="badge bg-secondary">No</span>'}</td>
        </tr>
      `).join('');

      document.getElementById('leads-pagination-info').innerText =
        `Showing ${(page-1)*data.page_size + (data.items.length ? 1 : 0)} to ${Math.min(page*data.page_size, data.total)} of ${data.total} leads`;
      document.getElementById('btn-prev-lead').disabled = (page <= 1);
      document.getElementById('btn-next-lead').disabled = (page * data.page_size >= data.total);
    }

    function prevLeadPage() { if (currentLeadPage > 1) loadLeads(currentLeadPage - 1); }
    function nextLeadPage() { loadLeads(currentLeadPage + 1); }

    function debouncedLeadSearch() {
      clearTimeout(leadSearchTimeout);
      leadSearchTimeout = setTimeout(() => loadLeads(1), 300);
    }

    async function loadRejections(page = 1) {
      currentRejPage = page;
      const res = await fetch(`/api/rejections?page=${page}`);
      const data = await res.json();
      const tbody = document.getElementById('tbody-rejections');

      tbody.innerHTML = data.items.map(r => `
        <tr>
          <td><span class="badge bg-dark">${r.workflow_id}</span></td>
          <td class="fw-semibold">${r.batch_id}</td>
          <td class="font-monospace">${r.row_number}</td>
          <td><span class="badge bg-danger-subtle text-danger border border-danger-subtle px-2 py-1">${r.reason}</span></td>
        </tr>
      `).join('');

      document.getElementById('rejections-pagination-info').innerText =
        `Showing ${(page-1)*data.page_size + (data.items.length ? 1 : 0)} to ${Math.min(page*data.page_size, data.total)} of ${data.total} rejections`;
      document.getElementById('btn-prev-rej').disabled = (page <= 1);
      document.getElementById('btn-next-rej').disabled = (page * data.page_size >= data.total);
    }

    function prevRejPage() { if (currentRejPage > 1) loadRejections(currentRejPage - 1); }
    function nextRejPage() { loadRejections(currentRejPage + 1); }

    async function loadQualityJson() {
      const res = await fetch('/api/quality');
      const data = await res.json();
      document.getElementById('code-quality-json').innerText = JSON.stringify(data, null, 2);
    }

    async function triggerIngest(drop) {
      if (!confirm(`Ingest ${drop} now?`)) return;
      const res = await fetch(`/api/action/ingest?drop=${drop}`, { method: 'POST' });
      const data = await res.json();
      alert(data.message);
      refreshAll();
    }

    async function triggerReport() {
      const res = await fetch('/api/action/report', { method: 'POST' });
      const data = await res.json();
      alert(data.message);
      refreshAll();
    }

    function refreshAll() {
      loadStats();
      loadBatchSummary();
      loadDailyOverview();
      loadLeads(1);
      loadRejections(1);
      loadQualityJson();
    }

    // Init on load
    window.onload = refreshAll;
  </script>
</body>
</html>
"""
