# Submission

## Candidate
- **Name:** Anupam Kumar
- **Assignment chosen:** A (Data: Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** ~5h

## How to run
```bash
# 1. Setup virtual environment & install dependencies
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

pip install -r requirements.txt

# 2. Run unit and end-to-end integration tests
pytest

# 3. Generate sample synthetic data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest drops sequentially
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 5. Generate reports
python -m solution.cli report --out reports/
```

## Design
The pipeline is designed around an **append-only event ledger** and a **strictly normalized lead store** backed by SQLite in Write-Ahead Logging (`WAL`) mode (and directly portable to PostgreSQL).

### Core Architecture
1. **PII Protection at Rest (`solution/crypto.py`)**:
   - `phone`: PII fields cannot be stored in plaintext. To allow fast indexing and deterministic matching with incoming call events without exposing phone numbers, we compute a keyed HMAC-SHA256 (`phone_hash`) using a secret pepper. For reporting, we compute the masked representation (`+91******` + last 4 digits). For secure reversible storage, the raw canonical phone is encrypted using Fernet (AES-128-CBC with HMAC authentication).
   - `customer_name`: Stored encrypted at rest using Fernet.
2. **Lead Normalization & Merging (`solution/lead_ingestor.py`)**:
   - Headers are normalized (lowercase, trimmed, underscores/whitespace collapsed) and mapped to schema fields and aliases.
   - Blank rows are skipped and excluded from row numbering.
   - Validation occurs in strict schema field order. The first failure triggers rejection (`missing_required:<field>` or `invalid_phone`).
   - For valid rows, optional field errors increment `field_warnings` and store `null`.
   - Lead identity is `(workflow_id, phone_hash)`. The first valid row locks in `batch_id` permanently. Subsequent duplicate rows increment `duplicate_lead_rows_merged` and merge non-null values (last write wins). Schema defaults apply after merging to remaining nulls.
3. **Append-Only Event Ledger (`solution/event_ingestor.py`)**:
   - Every valid, non-duplicate event is appended into the `events` table with its IST timestamp, `call_id`, `workflow_id`, and `phone_hash`.
   - Disposition events are validated against `dispositions.csv`; unrecognized codes are logged as `unknown_disposition_events` and ignored for effective outcome resolution.
   - Overrides never update existing events; they are simply appended to the ledger.
4. **Idempotency & Re-runs (`solution/db.py`, `file_metrics`)**:
   - Ingestion tracks files in `file_metrics`. Re-running `ingest` on any already-ingested drop or file is safely skipped, ensuring true idempotency without double-counting metrics.
5. **Dynamic Link Resolution & Reporting (`solution/reporter.py`)**:
   - Events can arrive before leads (in drop 1 before drop 2). The reporting engine dynamically resolves call-to-lead relationships using `call_id -> phone_hash -> leads`. An event that was an orphan after drop 1 automatically links when its lead arrives in drop 2.
   - Generates all 5 output contract files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`) with exact headers, sorting, and formatting.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **HMAC-SHA256 for Phone Matching** | Allows fast indexing and joins between events and leads while keeping plaintext phone off disk and preventing rainbow table attacks. | Plain SHA-256 (vulnerable to dictionary attacks) or decrypting on every join (prohibitively slow). |
| **SQLite with WAL Mode as Default DB** | Zero external infrastructure required; single-file storage enables single-command grading and fast integration testing. | PostgreSQL (supported via DB abstraction, but requires running Docker). |
| **Batch `executemany` Insertion** | Dramatically speeds up bulk ingestion by committing 5,000 records per C-level SQLite call. | Row-by-row `INSERT` (slow on large drops). |
| **Dynamic Orphan Resolution at Report Time** | Events can arrive before leads across drops. Dynamic resolution ensures no early event is permanently lost or misclassified. | Eagerly failing or discarding unlinked events during ingestion. |
| **`file_metrics` Ledger for Idempotency** | Prevents double-counting quality metrics (e.g. `lead_rows_total`, `duplicate_lead_rows_merged`) when drops are re-ingested. | Truncating database on re-run (destroys incremental data) or blind upserts without tracking. |

## Failure modes considered
- **Malformed & Truncated Lines**: Trapped via JSON decode checks; counted in `malformed_events` and skipped without crashing.
- **Duplicate Event IDs**: Detected in-memory and against database index; dropped and counted in `duplicate_events_dropped`.
- **Clock / Timezone Differences**: Handles ISO UTC (`Z`), offset (`+05:30`), and naive strings (spec states naive means IST). All stored and compared canonically in IST.
- **Re-runs & Crashes**: File operations are wrapped in transactions; interrupted runs can be safely re-executed.
- **Overrides Without Phone**: Spec notes `disposition.override` has no phone; linked through `call_id` to the call's other events.
- **What is NOT handled**: Distributed file locking across multiple concurrent writing processes without a distributed lock coordinator.

## Testing
- **Unit Tests (`test_validators.py`)**:
  - Phone normalisation with multiple prefixes (`+91`, `91-`, `0`, spaces, landline rejections, bad prefix 5).
  - Day-first date parsing (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`, invalid dates like `31/02`).
  - Currency cleaning (`₹`, `Rs`, `Rs.`, `INR`, comma separators).
  - Timestamp parsing across UTC, offset, and naive IST.
  - PII encryption and decryption roundtrips.
- **Business Logic Tests (`test_business_rules.py`)**:
  - Verification that `disposition.override` takes precedence over `disposition.set`.
  - Verification of bucket criteria (`yet_to_reach_out`, `in_progress_telephony_failed`, `in_progress_dialed`, `completed`).
- **End-to-End Pipeline Tests (`test_pipeline_e2e.py`)**:
  - Full drop 1 and drop 2 ingestion, re-run idempotency check, file contract verification, and metric consistency.

## Results
- **Timings (Seed 42, Small scale)**:
  - Ingestion Drop 1: ~1.2s
  - Ingestion Drop 2: ~1.1s
  - Report Generation: ~0.8s
  - End-to-end tests: 11 tests passed in ~3.8s
- **Observations in Data**:
  - ~5% of generated leads contain defects (`bad_phone`, `no_name`, `no_ref`, `no_phone`).
  - Events frequently arrive out of order and across drops; `call.initiated` is the single source of truth for counting a call.

## If I had another day
1. **DuckDB / Polars Query Acceleration**: For 1M+ leads/day, leverage vectorized columnar execution for sub-second report aggregations.
2. **Automated Docker Compose Suite**: Integrate test runners directly into Docker for automated CI on PostgreSQL.
3. **Dead-Letter Queue / Alerting**: Expose Prometheus metrics for sudden spikes in `malformed_events` or `orphan_events`.
