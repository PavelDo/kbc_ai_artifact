"""Tests for src.security: password hashing, artifact IDs, unlock cookies."""

import hashlib
import string
import time

from src.security import CookieSigner, check_password, hash_password, new_artifact_id


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
        # module's current PBKDF2_ITERATIONS, to verify it.
        old_iterations = 1000
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
