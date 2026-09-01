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

#: Domain-separation labels for :func:`derive_key`. One per consumer of the
#: master secret; they are part of the wire protocol (changing one invalidates
#: everything signed under the old label), so they live here as constants.
KEY_LABEL_UNLOCK_COOKIE = "artifact-unlock-cookie"
KEY_LABEL_WEBHOOK = "webhook-signature"


def new_artifact_id() -> str:
    """Generate a new unguessable artifact ID."""
    return secrets.token_urlsafe(18)


def derive_key(secret: str, label: str) -> str:
    """Derive a labeled subkey from the master secret: HMAC-SHA256(secret, label).

    Domain separation. ``HUB_SECRET_KEY`` is one configured secret, but it
    backs two *different* trust domains:

    * unlock cookies, which only the hub may mint and verify, and
    * webhook signatures, which every receiver necessarily learns in order to
      verify a delivery.

    Handing both consumers the same key collapses those domains into a single
    signing authority: a webhook receiver could forge an unlock cookie for any
    artifact (for an unprotected one the cookie scope is the literal ``open``
    value, so nothing about it is secret). Giving each consumer its own key
    derived under a distinct ``label`` keeps a compromise of one from
    conferring anything in the other — the derived keys are independent, and
    neither reveals the master secret.

    Returns the digest as lowercase hex, so the result is a plain ``str``
    usable anywhere the raw secret was.
    """
    return hmac.new(
        secret.encode("utf-8"), label.encode("utf-8"), hashlib.sha256
    ).hexdigest()


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
    """Signs and verifies unlock cookies scoped to a single artifact ID.

    An optional ``scope`` is mixed into the signed payload so a cookie can be
    bound to state that must revoke it. The unlock flow passes a short digest
    of the artifact's *current* password record: replacing or clearing the
    password changes the scope, and every cookie issued under the previous
    password stops verifying immediately instead of living on until it
    expires.
    """

    def __init__(self, secret_key: str) -> None:
        self._signer = TimestampSigner(secret_key, salt=_COOKIE_SALT)

    @staticmethod
    def _payload(artifact_id: str, scope: str) -> str:
        """The string that actually gets signed."""
        return f"{artifact_id}.{scope}" if scope else artifact_id

    def make(self, artifact_id: str, scope: str = "") -> str:
        """Produce a signed cookie value for an artifact ID and scope."""
        return self._signer.sign(self._payload(artifact_id, scope)).decode("utf-8")

    def check(
        self, artifact_id: str, value: str, max_age_s: int, scope: str = ""
    ) -> bool:
        """Verify a cookie value was signed for this ID/scope and is fresh.

        Returns True only if the signature is valid, unexpired, and the
        unsigned payload matches ``artifact_id`` and ``scope`` exactly. Any
        exception (bad signature, expired, malformed input) yields False.
        """
        try:
            payload = self._signer.unsign(value, max_age=max_age_s)
        except Exception:
            return False
        return payload.decode("utf-8") == self._payload(artifact_id, scope)
