"""Tests closing SEC-100-006: webhook receiver key rotation.

Before this change, a receiver's signing key was derived deterministically
from ``(artifact_id, url)`` under the hub's webhook key (``src/webhooks.py``
~251-269) — re-registering the same URL always returned the same key, and a
leaked key could only be replaced by changing the URL or the hub's own master
secret. This module proves the fix: a per-receiver key epoch, persisted on
the artifact's meta record and backward compatible with records written
before it existed, an owner-only rotate endpoint that mints a fresh epoch
without touching the URL, and a short signed-overlap grace period so a
delivery in flight during rotation still verifies.

Mirrors the review's reproducer shape and the conventions of
``tests/test_api.py``'s existing webhook tests: the ``hooked`` fixture
(imported from there) stubs DNS/SSRF and network for the one test host it
accepts, and deliveries are driven synchronously with ``dispatcher.drain()``.
No real network or DNS is touched anywhere in this module.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from src.store import ArtifactMeta
from src.webhooks import (
    DEFAULT_KEY_OVERLAP_S,
    PREVIOUS_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    WebhookDispatcher,
    active_signing_keys,
    mint_key_epoch,
    receiver_id_for,
    receiver_signing_key,
)

from tests.test_api import (
    AUTH_HEADERS,
    HOOK,
    OTHER_AUTH_HEADERS,
    _publish_markdown,
    _register_hook,
    _submit_version,
    api,  # noqa: F401 -- pytest fixture, re-exported for this module's tests
    hooked,  # noqa: F401 -- pytest fixture, re-exported for this module's tests
)

SECRET = "webhook-signing-secret-for-review100-tests"


def _verifies(body: bytes, header_value: str | None, key: str) -> bool:
    """True iff ``header_value`` is ``sha256=<hmac>`` of ``body`` under ``key``."""
    if not header_value:
        return False
    expected = (
        "sha256=" + hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, header_value)


def _list_receivers(api, artifact_id: str):
    resp = api.client.get(
        f"/api/artifacts/{artifact_id}/webhooks", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp


def _rotate(api, artifact_id: str, receiver_id: str, headers=AUTH_HEADERS):
    return api.client.post(
        f"/api/artifacts/{artifact_id}/webhooks/{receiver_id}/rotate-key",
        headers=headers,
    )


def _receiver_row(api, artifact_id: str, url: str = HOOK) -> dict:
    listed = _list_receivers(api, artifact_id).json()
    return next(row for row in listed["webhooks"] if row["url"] == url)


def _iso_plus(ts: str, seconds: float) -> str:
    """``ts`` (main._now()-shaped ISO 8601) advanced by ``seconds``."""
    then = datetime.fromisoformat(ts)
    return (then + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Rotation changes the key
# --------------------------------------------------------------------------


class TestRotationChangesTheKey:
    def test_rotate_mints_a_different_key(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

        before = _receiver_row(api, artifact_id)
        assert before["rotated_at"] is None  # never rotated yet

        rotated = _rotate(api, artifact_id, before["id"])
        assert rotated.status_code == 200, rotated.text
        body = rotated.json()
        assert body["receiver_id"] == before["id"]
        assert body["signing_key"] != before["signing_key"]
        assert body["overlap_seconds"] == api.settings.webhook_key_overlap_s
        assert body["rotated_at"]

        after = _receiver_row(api, artifact_id)
        assert after["signing_key"] == body["signing_key"]
        assert after["rotated_at"] == body["rotated_at"]

    def test_a_second_rotation_changes_it_again(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        receiver_id = _receiver_row(api, artifact_id)["id"]

        first = _rotate(api, artifact_id, receiver_id).json()
        second = _rotate(api, artifact_id, receiver_id).json()
        assert second["signing_key"] != first["signing_key"]

    def test_rotating_does_not_touch_the_registered_url_or_other_receivers(
        self, hooked
    ) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        other = "https://hooks.test/second"
        assert _register_hook(api, artifact_id, [HOOK, other]).status_code == 200

        hook_id = _receiver_row(api, artifact_id, HOOK)["id"]
        other_key_before = _receiver_row(api, artifact_id, other)["signing_key"]

        assert _rotate(api, artifact_id, hook_id).status_code == 200

        listed = _list_receivers(api, artifact_id).json()["webhooks"]
        assert {row["url"] for row in listed} == {HOOK, other}
        assert next(r for r in listed if r["url"] == other)["signing_key"] == (
            other_key_before
        )

    def test_receiver_id_is_stable_and_url_derived(self) -> None:
        # Same URL always yields the same id (needed so the rotate route stays
        # addressable across repeated rotations), and different URLs yield
        # different ids (so one receiver can never be mistaken for another).
        assert receiver_id_for(HOOK) == receiver_id_for(HOOK)
        assert receiver_id_for(HOOK) != receiver_id_for("https://hooks.test/other")


# --------------------------------------------------------------------------
# Overlap grace period: both signatures while active, only the new one after
# --------------------------------------------------------------------------


class TestGracePeriodSignatures:
    def test_both_signatures_present_during_the_overlap_window(self, hooked) -> None:
        api, posts, dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        before = _receiver_row(api, artifact_id)

        rotated = _rotate(api, artifact_id, before["id"]).json()
        new_key = rotated["signing_key"]
        old_key = before["signing_key"]

        # Real wall-clock time elapses only microseconds between rotating and
        # delivering, well inside the (600s default) overlap window.
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        assert dispatcher.drain() == 1
        [(_url, body, headers)] = posts

        assert _verifies(body, headers.get(SIGNATURE_HEADER), new_key)
        assert _verifies(body, headers.get(PREVIOUS_SIGNATURE_HEADER), old_key)
        # The two headers never carry the same value -- that would defeat the
        # point of a second header entirely.
        assert headers[SIGNATURE_HEADER] != headers[PREVIOUS_SIGNATURE_HEADER]

    def test_old_key_stops_verifying_once_the_overlap_window_elapses(
        self, hooked
    ) -> None:
        api, posts, dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        before = _receiver_row(api, artifact_id)
        old_key = before["signing_key"]

        rotated = _rotate(api, artifact_id, before["id"]).json()
        new_key = rotated["signing_key"]
        overlap_s = rotated["overlap_seconds"]

        # Monkeypatched clock, the same injection pattern _sleep/_post/
        # _resolver already use in this dispatcher: fast-forward "now" to one
        # second past the end of the grace period without any real sleeping.
        future = _iso_plus(rotated["rotated_at"], overlap_s + 1)
        dispatcher._clock = lambda: future

        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        assert dispatcher.drain() == 1
        [(_url, body, headers)] = posts

        assert _verifies(body, headers.get(SIGNATURE_HEADER), new_key)
        # Past the window there is nothing to verify the old key against --
        # no second header at all, and the old key does not verify the
        # primary one either.
        assert PREVIOUS_SIGNATURE_HEADER not in headers
        assert not _verifies(body, headers.get(SIGNATURE_HEADER), old_key)

    def test_first_ever_rotation_still_honours_the_pre_rotation_key_in_grace(
        self, hooked
    ) -> None:
        """The receiver's *very first* rotation: "previous" is the legacy,
        epoch-less key, not another epoch. Grace must cover that transition
        too, or the first rotation any receiver ever goes through would break
        it immediately instead of easing into the new key.
        """
        api, posts, dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        legacy_key = _receiver_row(api, artifact_id)["signing_key"]

        receiver_id = _receiver_row(api, artifact_id)["id"]
        _rotate(api, artifact_id, receiver_id)

        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        assert dispatcher.drain() == 1
        [(_url, body, headers)] = posts
        assert _verifies(body, headers.get(PREVIOUS_SIGNATURE_HEADER), legacy_key)


# --------------------------------------------------------------------------
# Cache-Control: no-store on every key-bearing response
# --------------------------------------------------------------------------


class TestNoStoreHeaders:
    def test_listing_is_no_store(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

        resp = _list_receivers(api, artifact_id)
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_rotate_response_is_no_store(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        receiver_id = _receiver_row(api, artifact_id)["id"]

        resp = _rotate(api, artifact_id, receiver_id)
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


class TestRotateAuthorizationAndErrors:
    def test_foreign_project_token_gets_403(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
        receiver_id = _receiver_row(api, artifact_id)["id"]

        resp = _rotate(api, artifact_id, receiver_id, headers=OTHER_AUTH_HEADERS)
        assert resp.status_code == 403

        # Untouched: no rotation happened under the foreign token.
        assert _receiver_row(api, artifact_id)["id"] == receiver_id
        assert _receiver_row(api, artifact_id)["rotated_at"] is None

    def test_unknown_artifact_is_404(self, api) -> None:
        resp = _rotate(api, "does-not-exist", receiver_id_for(HOOK))
        assert resp.status_code == 404

    def test_unknown_receiver_id_is_404(self, hooked) -> None:
        api, _posts, _dispatcher = hooked
        artifact_id = _publish_markdown(api, "# One")
        assert _register_hook(api, artifact_id, [HOOK]).status_code == 200

        resp = _rotate(api, artifact_id, "not-a-real-receiver-id")
        assert resp.status_code == 404

    def test_rotate_requires_a_stack_and_token(self, api) -> None:
        artifact_id = _publish_markdown(api, "# One")
        # Same 400-before-401 ordering as every other owner route: no headers
        # at all fails resolve_stack before token verification runs.
        assert (
            _rotate(api, artifact_id, receiver_id_for(HOOK), headers={}).status_code
            == 400
        )
        assert (
            _rotate(
                api,
                artifact_id,
                receiver_id_for(HOOK),
                headers={"X-StorageApi-Token": "nope", "X-Kbc-Stack": "us"},
            ).status_code
            == 401
        )


# --------------------------------------------------------------------------
# Backward compatibility: an old-shape persisted receiver still verifies
# --------------------------------------------------------------------------


class TestOldShapeRecordHydration:
    def test_meta_with_no_webhook_key_epochs_key_at_all_parses_cleanly(self) -> None:
        """Every meta file Storage holds from before SEC-100-006 looks like this:
        no ``webhook_key_epochs`` key in the JSON whatsoever.
        """
        import json

        raw = json.dumps(
            {
                "id": "old1",
                "owner": {"stack_url": "https://x", "project_id": 1, "key": "x@x"},
                "webhooks": ["https://example.com/hook"],
            }
        ).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.webhook_key_epochs == {}

    def test_garbage_values_are_dropped_not_fatal(self) -> None:
        import json

        for bad_epochs in (
            "https://example.com/h",
            7,
            ["not", "a", "dict"],
            None,
            {"https://example.com/h": "not-a-record-dict"},
            {"https://example.com/h": {"previous_epoch": "x"}},  # missing epoch
            {"": {"epoch": "abc"}},  # empty url key
        ):
            raw = json.dumps(
                {"id": "old1", "webhook_key_epochs": bad_epochs}
            ).encode("utf-8")
            assert ArtifactMeta.from_json(raw).webhook_key_epochs == {}

    def test_round_trips_a_real_epoch_through_storage_json(self) -> None:
        record = mint_key_epoch(None, now="2026-09-01T09:30:00+00:00")
        meta = ArtifactMeta(
            id="abc123",
            owner={"stack_url": "https://x", "project_id": 1, "key": "x@x"},
            webhooks=["https://example.com/hook"],
            webhook_key_epochs={"https://example.com/hook": record},
        )
        restored = ArtifactMeta.from_json(meta.to_json())
        assert restored.webhook_key_epochs == {"https://example.com/hook": record}

    def test_dispatcher_signs_an_unrotated_or_old_shape_receiver_with_the_legacy_key(
        self,
    ) -> None:
        """A receiver whose meta record predates rotation (no epoch on file, or
        never rotated) must sign exactly the way it always did -- the original
        two-argument derivation -- with no code path treating "no epoch" as
        an error or a different key.
        """
        dispatcher = WebhookDispatcher(timeout_s=5, max_attempts=1, sign_secret=SECRET)
        url = "https://example.com/hook"
        legacy = receiver_signing_key(SECRET, "abc123", url)

        # Never seeded at all (a fresh process, receiver untouched since
        # restart) and explicitly seeded with an empty/old-shape record both
        # produce the same, original key.
        assert dispatcher.signing_key_for("abc123", url) == legacy
        dispatcher.seed_epoch("abc123", url, None)
        assert dispatcher.signing_key_for("abc123", url) == legacy


# --------------------------------------------------------------------------
# Pure-function unit tests: mint_key_epoch / active_signing_keys
# --------------------------------------------------------------------------


class TestActiveSigningKeysBoundary:
    NOW = "2026-09-01T10:00:00+00:00"

    def test_no_record_returns_only_the_legacy_key(self) -> None:
        keys = active_signing_keys(
            SECRET, "abc123", "https://x/hook", None, now=self.NOW, overlap_s=600
        )
        assert keys == [receiver_signing_key(SECRET, "abc123", "https://x/hook")]

    def test_within_the_window_returns_both_current_first(self) -> None:
        record = mint_key_epoch("old-epoch", now=self.NOW)
        keys = active_signing_keys(
            SECRET,
            "abc123",
            "https://x/hook",
            record,
            now=_iso_plus(self.NOW, 599),
            overlap_s=600,
        )
        assert keys == [
            receiver_signing_key(SECRET, "abc123", "https://x/hook", record["epoch"]),
            receiver_signing_key(SECRET, "abc123", "https://x/hook", "old-epoch"),
        ]

    def test_at_the_boundary_second_the_previous_key_is_gone(self) -> None:
        record = mint_key_epoch("old-epoch", now=self.NOW)
        keys = active_signing_keys(
            SECRET,
            "abc123",
            "https://x/hook",
            record,
            now=_iso_plus(self.NOW, 600),
            overlap_s=600,
        )
        assert len(keys) == 1

    def test_overlap_zero_disables_the_grace_period_entirely(self) -> None:
        record = mint_key_epoch("old-epoch", now=self.NOW)
        keys = active_signing_keys(
            SECRET,
            "abc123",
            "https://x/hook",
            record,
            now=self.NOW,
            overlap_s=0,
        )
        assert len(keys) == 1

    def test_a_clock_that_moved_backward_is_treated_as_outside_the_window(
        self,
    ) -> None:
        # Defensive: a negative "elapsed" (bad clock, clock skew) must not be
        # read as "always within the window".
        record = mint_key_epoch("old-epoch", now=self.NOW)
        keys = active_signing_keys(
            SECRET,
            "abc123",
            "https://x/hook",
            record,
            now=_iso_plus(self.NOW, -5),
            overlap_s=600,
        )
        assert len(keys) == 1


class TestMintKeyEpoch:
    def test_epochs_are_random_and_unguessable(self) -> None:
        first = mint_key_epoch(None, now="2026-09-01T09:30:00+00:00")
        second = mint_key_epoch(
            first["epoch"], now="2026-09-01T09:40:00+00:00"
        )
        assert first["epoch"] != second["epoch"]
        assert len(first["epoch"]) >= 32  # secrets.token_hex(16) -> 32 hex chars

    def test_previous_epoch_chains_across_rotations(self) -> None:
        first = mint_key_epoch(None, now="t0")
        assert first["previous_epoch"] is None
        second = mint_key_epoch(first["epoch"], now="t1")
        assert second["previous_epoch"] == first["epoch"]
        third = mint_key_epoch(second["epoch"], now="t2")
        assert third["previous_epoch"] == second["epoch"]


class TestReceiverSigningKeyEpochArgument:
    def test_none_and_omitted_epoch_are_identical_and_legacy(self) -> None:
        assert receiver_signing_key(SECRET, "abc123", "https://x/hook") == (
            receiver_signing_key(SECRET, "abc123", "https://x/hook", None)
        )

    def test_an_epoch_changes_the_key(self) -> None:
        legacy = receiver_signing_key(SECRET, "abc123", "https://x/hook")
        epoched = receiver_signing_key(SECRET, "abc123", "https://x/hook", "e1")
        assert legacy != epoched

    def test_different_epochs_of_the_same_receiver_are_unrelated(self) -> None:
        e1 = receiver_signing_key(SECRET, "abc123", "https://x/hook", "e1")
        e2 = receiver_signing_key(SECRET, "abc123", "https://x/hook", "e2")
        assert e1 != e2


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class TestWebhookKeyOverlapSetting:
    def test_default_matches_the_module_constant(self, settings) -> None:
        assert settings.webhook_key_overlap_s == DEFAULT_KEY_OVERLAP_S

    def test_env_override_is_honoured(self, monkeypatch) -> None:
        from src.config import load_settings

        monkeypatch.setenv("HUB_WEBHOOK_KEY_OVERLAP_S", "42")
        assert load_settings().webhook_key_overlap_s == 42
