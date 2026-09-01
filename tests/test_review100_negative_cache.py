"""SEC-100-002 follow-up: unknown ids must not cost a Storage round trip each.

The SEC-100-002 fix stopped an anonymous caller from growing the per-artifact
lock registry with made-up ids, but it paid for that by resolving the target
*before* authentication: ``_serialized_per_comment_target`` called
``resolve_share`` and then ``get_meta``, and both fell through to
``ArtifactStore._resolve``, which re-checked Storage on an index miss and
remembered nothing about the miss. A well-formed but nonexistent id therefore
bought the caller two real Keboola Storage tag searches, unauthenticated --
cheaper than the old unbounded registry, but a much better amplifier than the
404 it produced.

Two changes close it, and this module pins both:

* ``ArtifactStore`` keeps a bounded LRU of ids Storage has confirmed absent,
  consulted by ``_resolve`` before the backend and invalidated by every path
  that can make an id appear, so repeating an unknown id is free.
* the decorator asks the store once (``resolve_lock_target``) instead of
  twice, so even a *first* sighting of an unknown id costs one tag search
  rather than two.

The ``api`` fixture and its helpers come from ``tests.test_api`` so this
module stays a pure addition -- no live Storage, no live stack, no DNS.
"""

from __future__ import annotations

import pytest

import src.main as main
from src.config import load_settings
from src.kbc import InMemoryFilesBackend
from src.store import (
    HEAD_LATEST,
    STATUS_LIVE,
    ArtifactMeta,
    ArtifactStore,
    Envelope,
)
from tests.test_api import (
    AUTH_HEADERS,
    Api,
    _publish_markdown,
    api,  # noqa: F401 - the fixture this module runs on
)

OWNER = "1@connection.keboola.com"

COMMENT_BODY = {
    "version": 1,
    "exact": "quote",
    "prefix": "",
    "suffix": "",
    "body": "probe",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _count_searches(api: Api) -> dict[str, int]:
    """Wrap the backend's tag search with a counter, returning the tally."""
    tally = {"n": 0}
    original = api.backend.search_by_tag

    def counting(tag: str):
        tally["n"] += 1
        return original(tag)

    api.backend.search_by_tag = counting  # type: ignore[method-assign]
    return tally


def _identity(key: str = OWNER) -> dict:
    return {
        "stack_url": "https://connection.keboola.com",
        "project_id": 1,
        "project_name": "Proj",
        "key": key,
    }


def _meta(artifact_id: str) -> ArtifactMeta:
    return ArtifactMeta(
        id=artifact_id,
        owner=_identity(),
        password=None,
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _envelope(artifact_id: str) -> Envelope:
    return Envelope(
        id=artifact_id,
        version=1,
        title="Test Artifact v1",
        html="<html><body>hi</body></html>",
        source_type="html",
        source={},
        author=_identity(),
        status=STATUS_LIVE,
        note=None,
        canonical_file_id=None,
        created_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    """A small store whose negative cache is tight enough to see it evict."""
    return ArtifactStore(
        backend=InMemoryFilesBackend(),
        cache_dir=tmp_path / "cache",
        cache_max_entries=50,
        max_versions=50,
        negative_lookup_cache_entries=8,
    )


# --------------------------------------------------------------------------
# The amplifier itself
# --------------------------------------------------------------------------


def test_many_unknown_comment_targets_cost_at_most_one_search_each(
    api: Api,
) -> None:
    """250 made-up ids must not buy 500 Storage tag searches.

    The review probe's shape, with the assertion moved from "the registry
    grew" to "the backend was asked twice per id". One search per id is the
    unavoidable floor for an id this process has genuinely never seen; two
    was the regression.
    """
    main._artifact_locks.clear()
    api.client.get("/health")  # force hydration before the counter starts
    tally = _count_searches(api)

    statuses: set[int] = set()
    for index in range(250):
        response = api.client.post(
            f"/api/artifacts/notanartifact{index}/comments", json=COMMENT_BODY
        )
        statuses.add(response.status_code)

    # Exactly one per distinct id: the floor, not merely under the ceiling.
    assert tally["n"] == 250, tally["n"]
    assert statuses and statuses <= {400, 401, 404}, statuses
    assert len(main._artifact_locks) == 0


def test_the_same_unknown_id_is_looked_up_in_storage_exactly_once(
    api: Api,
) -> None:
    """A confirmed absence is remembered, so a repeat costs nothing."""
    api.client.get("/health")
    tally = _count_searches(api)

    for _ in range(50):
        response = api.client.post(
            "/api/artifacts/neverminted1/comments", json=COMMENT_BODY
        )
        assert response.status_code in {400, 401, 404}, response.status_code

    assert tally["n"] == 1, tally["n"]


def test_an_unknown_id_on_a_read_route_is_also_only_looked_up_once(
    api: Api,
) -> None:
    """The cache lives in the store, so every read path inherits it."""
    api.client.get("/health")
    tally = _count_searches(api)

    for _ in range(25):
        assert api.client.get("/a/neverminted2").status_code == 404

    assert tally["n"] == 1, tally["n"]


# --------------------------------------------------------------------------
# The cache is bounded
# --------------------------------------------------------------------------


def test_the_negative_cache_never_grows_past_its_bound(store: ArtifactStore) -> None:
    """An attacker's id stream must not become an unbounded dict of its own."""
    for index in range(200):
        assert store.get_meta(f"absent{index}") is None

    assert store.negative_cache_size() <= 8, store.negative_cache_size()


def test_a_bound_of_zero_disables_the_negative_cache(tmp_path) -> None:
    """Operators can turn the memory off; correctness must not depend on it."""
    backend = InMemoryFilesBackend()
    store = ArtifactStore(
        backend=backend,
        cache_dir=tmp_path / "cache",
        cache_max_entries=50,
        max_versions=50,
        negative_lookup_cache_entries=0,
    )
    assert store.get_meta("absent") is None
    assert store.get_meta("absent") is None
    assert store.negative_cache_size() == 0


def test_the_bound_comes_from_settings(monkeypatch) -> None:
    """The tunable is env-driven configuration, not a literal in the store."""
    monkeypatch.delenv("HUB_NEGATIVE_LOOKUP_CACHE_ENTRIES", raising=False)
    assert load_settings().negative_lookup_cache_entries == 4096
    monkeypatch.setenv("HUB_NEGATIVE_LOOKUP_CACHE_ENTRIES", "17")
    assert load_settings().negative_lookup_cache_entries == 17


# --------------------------------------------------------------------------
# Invalidation: nothing may stay "absent" once it exists
# --------------------------------------------------------------------------


def test_creating_an_artifact_whose_id_was_cached_absent_makes_it_resolvable(
    store: ArtifactStore,
) -> None:
    """The single-writer rule makes this the only race that matters.

    Only this process creates artifacts, and it creates them through the
    store, so invalidating on the write is sufficient: no other writer can
    make an id appear behind the cache's back.
    """
    assert store.get_meta("appears1") is None

    store.create(_meta("appears1"), _envelope("appears1"))

    assert store.get_meta("appears1") is not None
    assert store.resolve_share("appears1") == "appears1"


def test_hydrate_clears_absences_learned_before_the_rebuild(
    store: ArtifactStore,
) -> None:
    """A rebuild is the moment Storage's own state becomes authoritative."""
    assert store.get_meta("appears2") is None

    # Write straight through the backend, the way a rebuild-from-tags start
    # would find files this process never wrote itself.
    other = ArtifactStore(
        backend=store._backend,
        cache_dir=store._cache_dir,
        cache_max_entries=50,
        max_versions=50,
    )
    other.create(_meta("appears2"), _envelope("appears2"))

    store.hydrate()

    assert store.get_meta("appears2") is not None


def test_refresh_clears_the_absence_when_the_artifact_turns_up(
    store: ArtifactStore,
) -> None:
    """``refresh`` is the explicit "re-read Storage" call; it must win."""
    assert store.get_meta("appears3") is None

    other = ArtifactStore(
        backend=store._backend,
        cache_dir=store._cache_dir,
        cache_max_entries=50,
        max_versions=50,
    )
    other.create(_meta("appears3"), _envelope("appears3"))

    assert store.refresh("appears3") is True
    assert store.get_meta("appears3") is not None


def test_a_rotated_share_id_resolves_even_if_it_was_probed_first(
    store: ArtifactStore,
) -> None:
    """Rotation mints an id an anonymous caller may already have probed."""
    store.create(_meta("rot1"), _envelope("rot1"))
    # Somebody guesses the id rotation is about to mint; it is absent now.
    assert store.resolve_share("newshare1") is None

    minted = store.rotate_share("rot1", generate=lambda: "newshare1")

    assert minted == "newshare1"
    assert store.resolve_share("newshare1") == "rot1"
    # The old handle is revoked, which is the point of rotating.
    assert store.resolve_share("rot1") is None


def test_rotation_through_the_api_still_serves_the_new_link(api: Api) -> None:
    """End to end: probe, rotate, and the new public link works at once."""
    artifact_id = _publish_markdown(api, "# Rotated")
    assert api.client.get("/a/thisisnotminted").status_code == 404

    rotated = api.client.post(
        f"/api/artifacts/{artifact_id}/rotate-link", headers=AUTH_HEADERS
    )
    assert rotated.status_code == 200, rotated.text
    new_share = rotated.json()["share_id"]

    assert api.client.get(f"/a/{new_share}").status_code == 200


def test_deleting_an_artifact_makes_its_id_absent_again(store: ArtifactStore) -> None:
    """The cache must follow deletions too, or a purge would keep serving."""
    store.create(_meta("gone1"), _envelope("gone1"))
    assert store.get_meta("gone1") is not None

    assert store.delete("gone1") is True

    assert store.get_meta("gone1") is None
    assert store.resolve_share("gone1") is None
