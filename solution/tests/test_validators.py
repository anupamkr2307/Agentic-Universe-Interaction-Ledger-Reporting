"""Tests for validators, normalization, and crypto routines."""
import pytest
from datetime import datetime
from solution.config import IST
from solution.crypto import decrypt_pii, encrypt_pii, hash_phone, mask_phone
from solution.validators import (
    clean_str,
    is_blank_row,
    match_headers,
    normalize_header,
    parse_timestamp,
    validate_date,
    validate_decimal,
    validate_enum,
    validate_phone,
)


def test_normalize_header():
    assert normalize_header("Customer Name") == "customer name"
    assert normalize_header("  Mobile_No  ") == "mobile no"
    assert normalize_header("Policy__Number") == "policy number"
    assert normalize_header("order_id") == "order id"


def test_match_headers():
    schema_fields = [
        {"name": "customer_name", "aliases": ["name", "customer name", "insured name"]},
        {"name": "phone", "aliases": ["mobile", "mobile no", "contact"]},
    ]
    raw_headers = ["Insured Name", "Mobile_No", "Unknown Col"]
    mapping = match_headers(raw_headers, schema_fields)
    assert mapping == {
        "Insured Name": "customer_name",
        "Mobile_No": "phone",
    }
    assert "Unknown Col" not in mapping


def test_is_blank_row():
    assert is_blank_row(["", "  ", None]) is True
    assert is_blank_row(["", "a", None]) is False


def test_validate_phone():
    # Valid phones
    assert validate_phone("9876543210") == (True, "+919876543210")
    assert validate_phone("+91 98765 43210") == (True, "+919876543210")
    assert validate_phone("09876543210") == (True, "+919876543210")
    assert validate_phone("91-98765-43210") == (True, "+919876543210")
    assert validate_phone("(+91) 8876543210") == (True, "+918876543210")
    assert validate_phone("+917876543210") == (True, "+917876543210")
    assert validate_phone("6876543210") == (True, "+916876543210")

    # Invalid phones
    assert validate_phone("5876543210")[0] is False  # starts with 5
    assert validate_phone("987654321")[0] is False   # 9 digits
    assert validate_phone("022-2345-6789")[0] is False  # landline
    assert validate_phone("+1 415 555 0134")[0] is False  # US number
    assert validate_phone("")[0] is False
    assert validate_phone(None)[0] is False


def test_validate_date():
    # Valid formats (day-first)
    assert validate_date("2026-10-15") == (True, "2026-10-15")
    assert validate_date("15/10/2026") == (True, "2026-10-15")
    assert validate_date("15-10-2026") == (True, "2026-10-15")
    assert validate_date("01-Dec-2026") == (True, "2026-12-01")
    assert validate_date("01-dec-2026") == (True, "2026-12-01")

    # Day-first check (01/02/2026 is Feb 1st, not Jan 2nd)
    assert validate_date("01/02/2026") == (True, "2026-02-01")

    # Impossible dates
    assert validate_date("31/02/2026")[0] is False
    assert validate_date("2026-13-01")[0] is False
    assert validate_date("soon")[0] is False


def test_validate_decimal():
    assert validate_decimal("12000") == (True, 12000.0)
    assert validate_decimal("12000.50") == (True, 12000.50)
    assert validate_decimal("₹12,000") == (True, 12000.0)
    assert validate_decimal("Rs. 12,500.75") == (True, 12500.75)
    assert validate_decimal("INR 4500.5") == (True, 4500.5)
    assert validate_decimal("Rs 8,000") == (True, 8000.0)

    # Invalid
    assert validate_decimal("-500")[0] is False
    assert validate_decimal("N/A")[0] is False


def test_validate_enum():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert validate_enum("EN", allowed) == (True, "en")
    assert validate_enum(" hi ", allowed) == (True, "hi")
    assert validate_enum("fr", allowed)[0] is False


def test_parse_timestamp():
    # UTC timestamp with Z
    ts_utc = parse_timestamp("2026-10-05T13:20:21Z")
    assert ts_utc.tzinfo == IST
    # 13:20:21 UTC is 18:50:21 IST
    assert ts_utc.hour == 18
    assert ts_utc.minute == 50
    assert ts_utc.second == 21

    # Offset timestamp
    ts_off = parse_timestamp("2026-10-05T18:50:21+05:30")
    assert ts_off.tzinfo == IST
    assert ts_off.hour == 18

    # Naive timestamp (means IST)
    ts_naive = parse_timestamp("2026-10-03 10:01:00")
    assert ts_naive.tzinfo == IST
    assert ts_naive.hour == 10


def test_crypto_and_masking():
    phone = "+919876543210"
    masked = mask_phone(phone)
    assert masked == "+91******3210"

    h1 = hash_phone(phone)
    h2 = hash_phone(phone)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex string

    # Encryption / Decryption roundtrip
    name = "Aarav Sharma"
    enc = encrypt_pii(name)
    assert enc != name
    dec = decrypt_pii(enc)
    assert dec == name
