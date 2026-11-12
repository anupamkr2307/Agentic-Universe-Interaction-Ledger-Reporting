# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
Google Antigravity AI Assistant

## What I used them for
- Scaffolding the initial project architecture under `solution/` (database connection, configuration loader, CLI boilerplate).
- Drafted the regex expressions and initial test cases for Indian phone number normalization and multi-format day-first date validation.
- Formatted the Mermaid architectural diagram for the implementation plan.

## What I wrote or decided myself
- The core data model and append-only event ledger architecture.
- The PII security strategy: using keyed HMAC-SHA256 (`phone_hash`) as a deterministic join index between events and leads without revealing raw phone numbers in plaintext, coupled with authenticated Fernet encryption for at-rest storage.
- The dynamic orphan resolution design: recognizing that events can precede leads across drops, ensuring events remain uncorrupted in the ledger and dynamically re-link upon subsequent lead arrival.
- Strict idempotency strategy using `file_metrics` to ensure repeated ingest commands do not inflate quality counters.
- The exact bucketing priority sequence (`yet_to_reach_out` -> `completed` -> `in_progress_telephony_failed` -> `in_progress_dialed`).

## Where the AI was wrong or unhelpful
- In the initial timestamp parsing draft, the AI attempted to use naive `datetime.strptime` with fixed format strings for UTC strings with `Z`, which mishandled timezone conversion into IST. I replaced this with Python's ISO-8601 parser and explicit `.astimezone(IST)` conversions.
- In early event ingestion drafts, the AI attempted to discard orphan events immediately upon ingestion. I corrected this because the specification explicitly requires orphan events to be counted and linked once their corresponding leads arrive in subsequent drops.

## How I verified AI-generated code
- Executed unit tests (`test_validators.py` and `test_business_rules.py`) covering all edge cases (bad phones, leap year/impossible dates, currency prefixes, enum normalization, disposition overrides).
- Executed end-to-end integration tests (`test_pipeline_e2e.py`) on generated synthetic data.
- Verified exact report schemas, header names, sort orders, and verified strict re-ingestion idempotency by diffing report files before and after re-runs.
