"""Tests for src.security: password hashing, artifact IDs, unlock cookies."""

import hashlib
import string
import time

from src.security import (
    KEY_LABEL_UNLOCK_COOKIE,
    MAX_PBKDF2_ITERATIONS,
    MIN_PBKDF2_ITERATIONS,
    KEY_LABEL_WEBHOOK,
    CookieSigner,
    check_password,
    derive_key,
    hash_password,
    new_artifact_id,
)


class TestPasswordHashing:
    def test_round_trip(self):
        record = hash_password("hunter2")
        assert check_password("hunter2", record) is True

    def test_wrong_password_is_false(self):
        record = hash_password("hunter2")
        assert check_password("wrong-password", record) is False

    def test_malformed_record_missing_fields_is_false(self):
        assert check_password("hunter2", {}) is False

    def test_malformed_record_non_hex_is_false(self):
        assert check_password("hunter2", {"salt": "zz", "hash": "zz"}) is False

    def test_malformed_record_wrong_types_is_false(self):
        assert check_password("hunter2", {"salt": None, "hash": None}) is False

    def test_hash_password_record_shape(self):
        record = hash_password("hunter2")
        assert record["algo"] == "pbkdf2-sha256"
        assert "iterations" in record
        assert "salt" in record
        assert "hash" in record

    def test_old_iterations_honored(self):
        # Simulate a record created under a lower, now-superseded iteration
        # count: check_password must use record["iterations"], not the
        # module's current PBKDF2_ITERATIONS, to verify it. The count stays
        # at or above MIN_PBKDF2_ITERATIONS -- honouring a stored cost is not
        # the same as honouring an arbitrarily downgraded one, and every
        # record this project has ever written used PBKDF2_ITERATIONS
        # anyway (see TestKdfCostPolicy).
        old_iterations = MIN_PBKDF2_ITERATIONS
        salt = bytes.fromhex(hash_password("placeholder")["salt"])
        digest = hashlib.pbkdf2_hmac("sha256", b"hunter2", salt, old_iterations)
        record = {
            "algo": "pbkdf2-sha256",
            "iterations": old_iterations,
            "salt": salt.hex(),
            "hash": digest.hex(),
        }
        assert check_password("hunter2", record) is True
        assert check_password("wrong-password", record) is False


class TestNewArtifactId:
    def test_length(self):
        # secrets.token_urlsafe(18) always yields exactly 24 base64url chars
        # (18 bytes = 144 bits = 24 * 6 bits, no padding needed).
        assert len(new_artifact_id()) == 24

    def test_uniqueness(self):
        ids = {new_artifact_id() for _ in range(200)}
        assert len(ids) == 200

    def test_urlsafe_charset(self):
        allowed = set(string.ascii_letters + string.digits + "-_")
        artifact_id = new_artifact_id()
        assert artifact_id
        assert set(artifact_id) <= allowed


class TestDeriveKey:
    """Domain separation between the consumers of HUB_SECRET_KEY."""

    SECRET = "master-secret-key-for-tests-only-0123456789"

    def test_labels_yield_different_keys(self):
        cookie_key = derive_key(self.SECRET, KEY_LABEL_UNLOCK_COOKIE)
        webhook_key = derive_key(self.SECRET, KEY_LABEL_WEBHOOK)
        assert cookie_key != webhook_key

    def test_neither_derived_key_is_the_master_secret(self):
        for label in (KEY_LABEL_UNLOCK_COOKIE, KEY_LABEL_WEBHOOK):
            derived = derive_key(self.SECRET, label)
            assert derived != self.SECRET
            assert self.SECRET not in derived

    def test_is_deterministic_hex_sha256(self):
        derived = derive_key(self.SECRET, KEY_LABEL_WEBHOOK)
        assert derived == derive_key(self.SECRET, KEY_LABEL_WEBHOOK)
        assert len(derived) == 64
        assert set(derived) <= set(string.hexdigits.lower())

    def test_a_different_master_secret_yields_a_different_key(self):
        assert derive_key("secret-a", KEY_LABEL_WEBHOOK) != derive_key(
            "secret-b", KEY_LABEL_WEBHOOK
        )

    def test_a_cookie_signed_with_the_cookie_key_verifies(self):
        signer = CookieSigner(derive_key(self.SECRET, KEY_LABEL_UNLOCK_COOKIE))
        value = signer.make("artifact-1", "open")
        assert signer.check("artifact-1", value, max_age_s=3600, scope="open")

    def test_the_webhook_key_cannot_forge_or_verify_an_unlock_cookie(self):
        """A webhook receiver learns the webhook key — and gains nothing by it.

        Before domain separation both consumers shared HUB_SECRET_KEY, so any
        receiver could mint an unlock cookie for any artifact (for an
        unprotected one the scope is the literal "open" value, so nothing about
        it is secret).
        """
        cookie_signer = CookieSigner(
            derive_key(self.SECRET, KEY_LABEL_UNLOCK_COOKIE)
        )
        webhook_signer = CookieSigner(derive_key(self.SECRET, KEY_LABEL_WEBHOOK))
        cookie = cookie_signer.make("artifact-1", "open")

        # The webhook key neither verifies a real cookie...
        assert (
            webhook_signer.check(
                "artifact-1", cookie, max_age_s=3600, scope="open"
            )
            is False
        )
        # ...nor forges one the hub would accept.
        forged = webhook_signer.make("artifact-1", "open")
        assert (
            cookie_signer.check(
                "artifact-1", forged, max_age_s=3600, scope="open"
            )
            is False
        )

    def test_the_raw_master_secret_cannot_forge_an_unlock_cookie(self):
        cookie_signer = CookieSigner(
            derive_key(self.SECRET, KEY_LABEL_UNLOCK_COOKIE)
        )
        forged = CookieSigner(self.SECRET).make("artifact-1", "open")
        assert (
            cookie_signer.check(
                "artifact-1", forged, max_age_s=3600, scope="open"
            )
            is False
        )


class TestCookieSigner:
    def test_make_and_check_round_trip(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1")
        assert signer.check("artifact-1", value, max_age_s=3600) is True

    def test_wrong_artifact_id_is_false(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1")
        assert signer.check("artifact-2", value, max_age_s=3600) is False

    def test_tampered_value_is_false(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1")
        # Flip a character near the middle of the signed value rather than at
        # either edge: the very last character of a base64 blob can have
        # spare bits that don't affect the decoded bytes (when the encoded
        # length isn't a multiple of 3), so editing there is not guaranteed
        # to change anything and would make this test flaky.
        chars = list(value)
        idx = len(chars) // 2
        while chars[idx] == ".":
            idx += 1
        chars[idx] = "0" if chars[idx] != "0" else "1"
        tampered = "".join(chars)
        assert signer.check("artifact-1", tampered, max_age_s=3600) is False

    def test_malformed_value_is_false(self):
        signer = CookieSigner("secret-key")
        assert signer.check("artifact-1", "not-a-signed-value", max_age_s=3600) is False

    def test_expired_is_false(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1")
        time.sleep(1)
        assert signer.check("artifact-1", value, max_age_s=0) is False

    def test_different_secret_key_is_false(self):
        signer_a = CookieSigner("secret-a")
        signer_b = CookieSigner("secret-b")
        value = signer_a.make("artifact-1")
        assert signer_b.check("artifact-1", value, max_age_s=3600) is False

    def test_scope_round_trip(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1", "pwhash-abc")
        assert signer.check("artifact-1", value, max_age_s=3600, scope="pwhash-abc")

    def test_a_different_scope_is_false(self):
        """A password change changes the scope, revoking every old cookie."""
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1", "pwhash-abc")
        assert (
            signer.check("artifact-1", value, max_age_s=3600, scope="pwhash-xyz")
            is False
        )

    def test_scoped_cookie_is_not_accepted_unscoped(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1", "pwhash-abc")
        assert signer.check("artifact-1", value, max_age_s=3600) is False

    def test_unscoped_cookie_is_not_accepted_with_a_scope(self):
        signer = CookieSigner("secret-key")
        value = signer.make("artifact-1")
        assert (
            signer.check("artifact-1", value, max_age_s=3600, scope="pwhash-abc")
            is False
        )

    def test_the_separator_cannot_occur_in_a_real_artifact_id(self):
        """Why "{id}.{scope}" is unambiguous in practice.

        Splitting on "." would be ambiguous if an artifact id could contain a
        dot — "a.b" with no scope signs the same payload as "a" with scope
        "b". It cannot: ids come from ``new_artifact_id`` (base64url), whose
        alphabet has no ".", so the two payload spaces never overlap.
        """
        assert all("." not in new_artifact_id() for _ in range(200))


class TestKdfCostPolicy:
    """A persisted record must not be able to dictate the KDF cost freely.

    ``check_password`` honours ``record["iterations"]`` so hashes made under
    an older cost keep verifying. That trust has to be bounded in both
    directions: a hostile or corrupted record could otherwise turn one unlock
    attempt into minutes of CPU, or silently downgrade the KDF to a cost that
    is cheap to brute-force.

    Each test below builds a record whose hash is *genuinely correct* for the
    out-of-policy count, so an implementation without the bound would answer
    True. Asserting False therefore tests the policy, not a hash mismatch.
    """

    @staticmethod
    def _record_at(password: str, iterations: int) -> dict:
        """A well-formed record whose hash really was computed at ``iterations``."""
        salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return {
            "algo": "pbkdf2-sha256",
            "iterations": iterations,
            "salt": salt.hex(),
            "hash": digest.hex(),
        }

    def test_a_correct_hash_below_the_policy_minimum_is_still_refused(self):
        record = self._record_at("hunter2", MIN_PBKDF2_ITERATIONS - 1)
        assert check_password("hunter2", record) is False

    def test_a_downgraded_record_cannot_make_verification_trivial(self):
        record = self._record_at("hunter2", 1)
        assert check_password("hunter2", record) is False

    def test_iterations_above_the_maximum_never_reach_the_kdf(self, monkeypatch):
        record = self._record_at("hunter2", MIN_PBKDF2_ITERATIONS)
        record["iterations"] = MAX_PBKDF2_ITERATIONS + 1

        def fail(*args, **kwargs):
            raise AssertionError("pbkdf2_hmac ran for an out-of-policy record")

        monkeypatch.setattr(hashlib, "pbkdf2_hmac", fail)
        assert check_password("hunter2", record) is False

    def test_an_absurd_iteration_count_is_rejected_quickly(self):
        record = self._record_at("hunter2", MIN_PBKDF2_ITERATIONS)
        record["iterations"] = 10_000_000_000
        started = time.monotonic()
        assert check_password("hunter2", record) is False
        assert time.monotonic() - started < 1.0

    def test_the_policy_range_admits_records_this_build_writes(self):
        record = hash_password("hunter2")
        assert MIN_PBKDF2_ITERATIONS <= record["iterations"] <= MAX_PBKDF2_ITERATIONS
        assert check_password("hunter2", record) is True
