"""Security primitives for the KBC Artifact Hub.

Provides:
- unguessable artifact ID generation,
- PBKDF2-SHA256 password hashing/verification for optional artifact
  passwords, and
- signed, time-limited unlock cookies (via itsdangerous) so a web reader
  who unlocked a password-protected artifact does not have to re-enter
  the password on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import TimestampSigner

PBKDF2_ITERATIONS = 200_000

_COOKIE_SALT = "artifact-unlock"
_SALT_BYTES = 16


def new_artifact_id() -> str:
    """Generate a new unguessable artifact ID."""
    return secrets.token_urlsafe(18)


def hash_password(password: str) -> dict:
    """Hash a password using PBKDF2-SHA256 with a random salt.

    Returns an envelope dict suitable for storage:
        {"algo": "pbkdf2-sha256", "iterations": PBKDF2_ITERATIONS,
         "salt": <hex>, "hash": <hex>}
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "algo": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def check_password(password: str, record: dict) -> bool:
    """Verify a password against a stored hash record.

    Tolerates malformed records (missing/invalid fields) by returning
    False rather than raising. Honors ``record["iterations"]`` when
    present so hashes created under an older iteration count still
    verify correctly.
    """
    try:
        salt_hex = record["salt"]
        expected_hex = record["hash"]
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (KeyError, TypeError, ValueError):
        return False

    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
    except (TypeError, ValueError):
        return False

    return hmac.compare_digest(actual, expected)


class CookieSigner:
    """Signs and verifies unlock cookies scoped to a single artifact ID."""

    def __init__(self, secret_key: str) -> None:
        self._signer = TimestampSigner(secret_key, salt=_COOKIE_SALT)

    def make(self, artifact_id: str) -> str:
        """Produce a signed cookie value for the given artifact ID."""
        return self._signer.sign(artifact_id).decode("utf-8")

    def check(self, artifact_id: str, value: str, max_age_s: int) -> bool:
        """Verify a cookie value was signed for ``artifact_id`` and is fresh.

        Returns True only if the signature is valid, unexpired, and the
        unsigned payload matches ``artifact_id`` exactly. Any exception
        (bad signature, expired, malformed input) yields False.
        """
        try:
            payload = self._signer.unsign(value, max_age=max_age_s)
        except Exception:
            return False
        return payload.decode("utf-8") == artifact_id
