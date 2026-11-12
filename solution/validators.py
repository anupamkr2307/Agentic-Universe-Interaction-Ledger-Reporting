"""Field parsing, validation, and normalization routines."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from solution.config import IST


def normalize_header(header: str) -> str:
    """
    To normalise a header: lower-case it, trim it, and replace every run of
    '_' or whitespace with a single space.
    """
    if not header:
        return ""
    s = header.lower().strip()
    return re.sub(r"[_\s]+", " ", s)


def match_headers(csv_headers: list[str], schema_fields: list[dict[str, Any]]) -> dict[str, str]:
    """
    Matches CSV header names to schema field names.
    Returns: {normalized_csv_header: schema_field_name}
    Columns matching nothing are ignored.
    """
    field_alias_map: dict[str, str] = {}
    for field in schema_fields:
        canonical_name = field["name"]
        norm_name = normalize_header(canonical_name)
        field_alias_map[norm_name] = canonical_name
        for alias in field.get("aliases", []):
            field_alias_map[normalize_header(alias)] = canonical_name

    mapping: dict[str, str] = {}
    for h in csv_headers:
        norm_h = normalize_header(h)
        if norm_h in field_alias_map:
            mapping[h] = field_alias_map[norm_h]
    return mapping


def is_blank_row(row_values: list[str | None]) -> bool:
    """Returns True if every cell in the row is empty or whitespace."""
    for v in row_values:
        if v is not None and str(v).strip():
            return False
    return True


def clean_str(val: Any) -> str | None:
    """Trims value; empty value returns None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def validate_phone(val: Any) -> tuple[bool, str | None]:
    """
    Validates and normalizes an Indian mobile number:
    - Strip every non-digit.
    - If 12 digits starting with 91, drop the 91.
    - If 11 digits starting with 0, drop the 0.
    - Valid only if exactly 10 digits and starts with 6, 7, 8 or 9.
    - Store canonically as +91XXXXXXXXXX.
    Returns: (is_valid, canonical_phone)
    """
    cleaned = clean_str(val)
    if not cleaned:
        return False, None

    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return True, f"+91{digits}"
    return False, None


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
]


def validate_date(val: Any) -> tuple[bool, str | None]:
    """
    Validates and normalizes date:
    - Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (case-insensitive month name).
    - Dates are day-first.
    - Impossible dates (like 31/02) are invalid.
    Returns: (is_valid, canonical_date_YYYY_MM_DD)
    """
    cleaned = clean_str(val)
    if not cleaned:
        return False, None

    for fmt in _DATE_FORMATS:
        try:
            # Handle month abbreviation case-insensitively
            dt = datetime.strptime(cleaned, fmt)
            return True, dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return False, None


_CURRENCY_PREFIX_REGEX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)


def validate_decimal(val: Any) -> tuple[bool, float | None]:
    """
    Validates and normalizes decimal amount:
    - Optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
    - Commas are thousands separators.
    - Must be >= 0.
    - Round to 2 places.
    Returns: (is_valid, amount_float)
    """
    cleaned = clean_str(val)
    if not cleaned:
        return False, None

    s = _CURRENCY_PREFIX_REGEX.sub("", cleaned).strip()
    s = s.replace(",", "")
    try:
        f = float(s)
        if f < 0:
            return False, None
        return True, round(f, 2)
    except ValueError:
        return False, None


def validate_enum(val: Any, allowed_values: list[str]) -> tuple[bool, str | None]:
    """
    Validates enum:
    - Compared case-insensitively against values.
    - Stored lower-case.
    Returns: (is_valid, canonical_enum_val)
    """
    cleaned = clean_str(val)
    if not cleaned:
        return False, None

    lower_val = cleaned.lower()
    allowed_lower = {v.lower(): v.lower() for v in allowed_values}
    if lower_val in allowed_lower:
        return True, allowed_lower[lower_val]
    return False, None


def validate_field(
    val: Any, field_def: dict[str, Any]
) -> tuple[bool, Any]:
    """
    Dispatches to appropriate validator based on field type:
    Returns: (is_valid, normalized_val)
    """
    ftype = field_def.get("type", "string")
    if ftype == "string":
        cleaned = clean_str(val)
        return True, cleaned
    elif ftype == "phone":
        return validate_phone(val)
    elif ftype == "date":
        return validate_date(val)
    elif ftype == "decimal":
        return validate_decimal(val)
    elif ftype == "enum":
        return validate_enum(val, field_def.get("values", []))
    else:
        # Default fallback
        cleaned = clean_str(val)
        return True, cleaned


def parse_timestamp(s: str) -> datetime:
    """
    Parses timestamp in three formats:
    - UTC (...Z)
    - offset (...:+05:30)
    - naive (2026-10-03 10:01:00) -> Naive means IST per spec.
    Returns aware datetime in IST.
    """
    s = s.strip()
    if s.endswith("Z") or "+" in s or (len(s) > 10 and "-" in s[10:]):
        dt = datetime.fromisoformat(s)
        return dt.astimezone(IST)
    else:
        # Naive: per spec: "Naive means IST."
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=IST)
