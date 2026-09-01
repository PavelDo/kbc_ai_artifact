"""Regression tests for two follow-ups left after the v0.10.0 security review.

1. SEC-100-006 follow-up: webhook key epochs were not seeded after a restart.
   Before this change, ``WebhookDispatcher._epochs`` (the live cache
   ``active_signing_keys`` consults) started empty on every process start and
   was only (re)seeded by the API layer when an owner called ``GET
   .../webhooks`` or the rotate-key route. A delivery that went out before
   either of those happened in a fresh process therefore signed with the
   legacy, epoch-less key even for a receiver that had already been rotated
   -- a receiver reconfigured with the rotated key would reject the
   genuinely current delivery. This module proves the fix: ``src/main.py``'s
   lifespan now syncs the configured overlap window into the dispatcher at
   construction time (no ``GET`` required), and ``_emit_webhook`` reseeds the
   dispatcher's cache from the durable ``ArtifactMeta.webhook_key_epochs``
   record on every emit, not only on a read or a rotation.

   Mirrors the "fresh store, hydrate, swap it into ``main.app.state``"
   restart-simulation technique already used elsewhere in this suite (see
   ``tests/test_review100_resource_bounds.py::
   test_live_etag_differs_after_a_restart_with_identical_content`` and
   ``tests/test_api.py::test_webhooks_survive_a_restart``), applied here to
   both the store and the webhook dispatcher.

2. ARCH-100-001 follow-up: a documentation-only sweep, covered by
   ``tests/test_review100_followups.py::TestReplicaWordingSweep`` below,
   asserting the "replica" wording that used to suggest multiple writers are
   supported is gone from the comments this task touched.

No real network or DNS is touched anywhere in this module.
"""

from __future__ import annotations

import dataclasses

import src.main as main
from src.comments import CommentStore
from src.security import KEY_LABEL_WEBHOOK, derive_key
from src.store import ArtifactStore
from src.webhooks import (
    PREVIOUS_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    WebhookDispatcher,
)

from tests.test_api import (
    AUTH_HEADERS,
    HOOK,
    _publish_markdown,
    _register_hook,
    _submit_version,
    api,  # noqa: F401 -- pytest fixture, re-exported for this module's tests
    hooked,  # noqa: F401 -- pytest fixture, re-exported for this module's tests
)
from tests.test_review100_webhook_keys import (
    _rotate,
    _receiver_row,
    _verifies,
)


# --------------------------------------------------------------------------
# 1a. The overlap window reaches the dispatcher at startup, with no GET call.
# --------------------------------------------------------------------------


def test_overlap_setting_reaches_the_dispatcher_without_a_get_call(api) -> None:
    """The lifespan must sync the overlap window itself, not wait for a read.

    ``api`` (and its underlying ``hooked``) never call ``GET
    .../webhooks`` or the rotate-key route before this assertion runs, so if
    ``WebhookDispatcher._key_overlap_s`` already matches the configured
    value here, it can only have come from the lifespan's own
    ``configure_key_overlap_s`` call at construction time.
    """
    dispatcher = main.app.state.webhooks
    assert dispatcher._key_overlap_s == api.settings.webhook_key_overlap_s


def test_overlap_setting_survives_a_fresh_dispatcher_construction(api) -> None:
    """A non-default overlap setting also reaches a brand-new dispatcher.

    Builds a second ``WebhookDispatcher`` exactly the way the lifespan does
    (same secret derivation, same settings), with a deliberately non-default
    overlap value, and checks the constructed-then-configured value sticks --
    the same sequence the lifespan now runs at startup.
    """
    custom_overlap = api.settings.webhook_key_overlap_s + 12345
    fresh = WebhookDispatcher(
        api.settings.webhook_timeout_s,
        api.settings.webhook_max_attempts,
        derive_key(api.settings.secret_key, KEY_LABEL_WEBHOOK),
        queue_max=api.settings.webhook_queue_max,
    )
    fresh.configure_key_overlap_s(custom_overlap)
    assert fresh._key_overlap_s == custom_overlap


# --------------------------------------------------------------------------
# 1b. A restart reseeds the dispatcher's epoch cache from persisted meta.
# --------------------------------------------------------------------------


def _restart(api, tmp_path):
    """Swap in a fresh store and a fresh, unseeded dispatcher over the same
    backend -- a container restart with an empty disk, exactly like
    ``test_live_etag_differs_after_a_restart_with_identical_content`` does
    for the store alone. Returns the fresh dispatcher so the test can wire up
    its own post/resolver stubs and drain it.
    """
    cache_dir = tmp_path / "restart"
    store = ArtifactStore(
        backend=api.backend,
        cache_dir=cache_dir,
        cache_max_entries=api.settings.cache_max_entries,
        max_versions=api.settings.max_versions,
        max_envelope_bytes=api.settings.max_envelope_bytes,
        max_proposed_versions=api.settings.max_proposed_versions,
    )
    store.hydrate()
    threads = CommentStore(
        backend=api.backend,
        cache_dir=cache_dir,
        cache_max_entries=api.settings.cache_max_entries,
    )
    threads.hydrate()
    main.app.state.store = store
    main.app.state.comments = threads

    # A brand-new WebhookDispatcher, constructed exactly the way the lifespan
    # builds one -- deliberately *not* seeded from anywhere, so any epoch it
    # signs with after this point can only have come from _emit_webhook's own
    # reseed-on-emit fix, not from a leftover in-process cache.
    fresh_dispatcher = WebhookDispatcher(
        api.settings.webhook_timeout_s,
        api.settings.webhook_max_attempts,
        derive_key(api.settings.secret_key, KEY_LABEL_WEBHOOK),
        queue_max=api.settings.webhook_queue_max,
    )
    fresh_dispatcher.configure_key_overlap_s(api.settings.webhook_key_overlap_s)
    main.app.state.webhooks = fresh_dispatcher
    return fresh_dispatcher


def test_delivery_after_restart_signs_with_the_rotated_key(hooked, tmp_path) -> None:
    """Rotate, restart, and deliver -- with no GET call in between.

    Before the fix, the freshly-restarted dispatcher's epoch cache was empty
    and nothing repopulated it until an owner called ``GET .../webhooks`` or
    rotated again, so this delivery would have signed with the legacy,
    epoch-less key -- which a receiver reconfigured with the rotated key
    would reject as forged.
    """
    api, _posts, _dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
    before = _receiver_row(api, artifact_id)
    old_key = before["signing_key"]

    rotated = _rotate(api, artifact_id, before["id"]).json()
    new_key = rotated["signing_key"]
    assert new_key != old_key

    fresh_dispatcher = _restart(api, tmp_path)
    posts: list[tuple[str, bytes, dict]] = []

    def record(url: str, body: bytes, headers: dict) -> int:
        posts.append((url, body, dict(headers)))
        return 200

    fresh_dispatcher._post = record
    fresh_dispatcher._resolver = lambda _hostname: ["93.184.216.34"]

    # No GET .../webhooks and no rotate call happened against fresh_dispatcher
    # -- the very first thing it ever does for this receiver is this emit.
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert fresh_dispatcher.drain() == 1
    [(_url, body, headers)] = posts

    assert _verifies(body, headers.get(SIGNATURE_HEADER), new_key)
    # Still within the (600s default) overlap window in wall-clock terms, so
    # the previous key must verify the secondary header too.
    assert _verifies(body, headers.get(PREVIOUS_SIGNATURE_HEADER), old_key)


def test_delivery_after_restart_for_an_unrotated_receiver_is_unaffected(
    hooked, tmp_path
) -> None:
    """A receiver that was never rotated keeps working the same way after a
    restart: no epoch on file, so the legacy epoch-less key is correct, both
    before and after the fix.
    """
    api, _posts, _dispatcher = hooked
    artifact_id = _publish_markdown(api, "# One")
    assert _register_hook(api, artifact_id, [HOOK]).status_code == 200
    legacy_key = _receiver_row(api, artifact_id)["signing_key"]

    fresh_dispatcher = _restart(api, tmp_path)
    posts: list[tuple[str, bytes, dict]] = []

    def record(url: str, body: bytes, headers: dict) -> int:
        posts.append((url, body, dict(headers)))
        return 200

    fresh_dispatcher._post = record
    fresh_dispatcher._resolver = lambda _hostname: ["93.184.216.34"]

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert fresh_dispatcher.drain() == 1
    [(_url, body, headers)] = posts

    assert _verifies(body, headers.get(SIGNATURE_HEADER), legacy_key)
    assert PREVIOUS_SIGNATURE_HEADER not in headers


def test_emit_seeds_every_registered_receiver_not_just_one(hooked, tmp_path) -> None:
    """Two receivers, only one rotated: emit must reseed both from meta, so
    the rotated one signs with its new key and the untouched one keeps
    signing with its legacy key -- neither leaks into the other.
    """
    api, _posts, _dispatcher = hooked
    second_hook = "https://hooks.test/second"
    artifact_id = _publish_markdown(api, "# One")
    assert (
        _register_hook(api, artifact_id, [HOOK, second_hook]).status_code == 200
    )
    rotated_row = _receiver_row(api, artifact_id, url=HOOK)
    second_key = _receiver_row(api, artifact_id, url=second_hook)["signing_key"]

    new_key = _rotate(api, artifact_id, rotated_row["id"]).json()["signing_key"]
    assert new_key != rotated_row["signing_key"]

    fresh_dispatcher = _restart(api, tmp_path)
    posts: list[tuple[str, bytes, dict]] = []

    def record(url: str, body: bytes, headers: dict) -> int:
        posts.append((url, body, dict(headers)))
        return 200

    fresh_dispatcher._post = record
    fresh_dispatcher._resolver = lambda _hostname: ["93.184.216.34"]

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert fresh_dispatcher.drain() == 2
    by_url = {url: (body, headers) for url, body, headers in posts}

    rotated_body, rotated_headers = by_url[HOOK]
    assert _verifies(rotated_body, rotated_headers.get(SIGNATURE_HEADER), new_key)

    second_body, second_headers = by_url[second_hook]
    assert _verifies(second_body, second_headers.get(SIGNATURE_HEADER), second_key)


# --------------------------------------------------------------------------
# 2. ARCH-100-001 follow-up: no leftover "replica" wording in touched files.
# --------------------------------------------------------------------------


class TestReplicaWordingSweep:
    """The single-instance invariant (CLAUDE.md, "Exactly one instance,
    ever") must not be undermined by comments that talk about "another
    replica" as if more than one writer were ever expected to run. This does
    not assert against the whole repository -- only the files this
    follow-up task touched -- so it stays a tight regression rather than a
    repo-wide lint.
    """

    TOUCHED_FILES = [
        "src/store.py",
        "src/comments.py",
        "src/pages.py",
        "src/main.py",
    ]

    def test_no_replica_wording_remains(self) -> None:
        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for relative in self.TOUCHED_FILES:
            text = (repo_root / relative).read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "replica" in line.lower():
                    offenders.append(f"{relative}:{lineno}: {line.strip()}")
        assert not offenders, "leftover 'replica' wording:\n" + "\n".join(offenders)
