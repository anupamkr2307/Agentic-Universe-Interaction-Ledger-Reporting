"""PII protection routines: hashing for matching and encryption for storage at rest."""
from __future__ import annotations

import hashlib
import hmac
from cryptography.fernet import Fernet
from solution.config import PII_ENCRYPTION_KEY, PII_PEPPER

# Initialize Fernet cipher
_CIPHER = Fernet(PII_ENCRYPTION_KEY.encode("utf-8"))


def hash_phone(canonical_phone: str) -> str:
    """
    Produces a deterministic keyed HMAC-SHA256 hash of a canonical phone number (+91XXXXXXXXXX).
    This allows exact matching and indexing between leads and events while never storing
    the phone number in plaintext.
    """
    if not canonical_phone:
        return ""
    return hmac.new(
        PII_PEPPER.encode("utf-8"),
        canonical_phone.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def encrypt_pii(plaintext: str | None) -> str | None:
    """Encrypts sensitive PII string using AES authenticated encryption (Fernet)."""
    if plaintext is None:
        return None
    return _CIPHER.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str | None) -> str | None:
    """Decrypts encrypted PII ciphertext back to plaintext."""
    if ciphertext is None:
        return None
    return _CIPHER.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def mask_phone(canonical_phone: str) -> str:
    """
    Masks a canonical phone number per spec:
    +91****** followed by the last 4 digits.
    Example: '+919876543210' -> '+91******3210'
    """
    if not canonical_phone:
        return ""
    digits = canonical_phone[-4:] if len(canonical_phone) >= 4 else canonical_phone
    return f"+91******{digits}"
