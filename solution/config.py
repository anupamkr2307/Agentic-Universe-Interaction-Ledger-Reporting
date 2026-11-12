"""Configuration, schema loading, and path utilities."""
from __future__ import annotations

import csv
import json
import os
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

# Timezone definition per spec: IST (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Repository root (parent of solution/)
REPO_ROOT = Path(__file__).resolve().parent.parent

# Schemas directory
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"

# Default SQLite database path
DEFAULT_DB_PATH = REPO_ROOT / "solution" / "pipeline.db"

# PII secret keys (can be overridden via environment variables)
PII_PEPPER = os.environ.get("PII_PEPPER", "agentic-universe-pii-pepper-2026-key")
# 32-byte urlsafe base64 key for Fernet encryption
PII_ENCRYPTION_KEY = os.environ.get(
    "PII_ENCRYPTION_KEY", "u7h3jKq4_L2tG8xQp9yZm1W4eRt6Yu8Io0Pq2Ws4ExY="
)


def load_dispositions(schemas_dir: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Loads disposition catalogue:
    returns: {workflow_id: {code: {"is_terminal": bool, "is_success": bool, "label": str}}}
    """
    s_dir = schemas_dir or SCHEMAS_DIR
    path = s_dir / "dispositions.csv"
    if not path.exists():
        raise FileNotFoundError(f"dispositions.csv not found at {path}")

    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wf = row["workflow_id"].strip()
            code = row["code"].strip()
            is_terminal = row["is_terminal"].strip().lower() == "true"
            is_success = row["is_success"].strip().lower() == "true"
            label = row["label"].strip()
            if wf not in catalog:
                catalog[wf] = {}
            catalog[wf][code] = {
                "is_terminal": is_terminal,
                "is_success": is_success,
                "label": label,
            }
    return catalog


def load_workflow_schemas(schemas_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """
    Loads JSON schemas for workflows:
    returns: {workflow_id: schema_dict}
    """
    s_dir = schemas_dir or SCHEMAS_DIR
    schemas: dict[str, dict[str, Any]] = {}
    for p in s_dir.glob("*.json"):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            wf_id = data.get("workflow_id")
            if wf_id:
                schemas[wf_id] = data
    return schemas
