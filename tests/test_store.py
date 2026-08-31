"""Tests for src.store: envelopes, meta records and the versioned store."""

import json

import pytest

from src.store import (
    HEAD_LATEST,
    HEAD_PINNED,
    STATUS_LIVE,
    STATUS_PROPOSED,
    TAG_ALL,
    TAG_META,
    ArtifactMeta,
    ArtifactStore,
    Envelope,
    migrate_legacy,
    tag_for_id,
    tag_for_owner,
    tag_for_version,
)

OWNER_A = "1@connection.keboola.com"
OWNER_B = "2@connection.keboola.com"


def _identity(project_id: int = 1, key: str = OWNER_A) -> dict:
    return {
        "stack_url": "https://connection.keboola.com",
        "project_id": project_id,
        "project_name": "Proj",
        "key": key,
    }


def _make_meta(
    artifact_id: str = "abc123", owner_key: str = OWNER_A, **overrides
) -> ArtifactMeta:
    defaults = dict(
        id=artifact_id,
        owner=_identity(key=owner_key),
        password=None,
        accept_versions=False,
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return ArtifactMeta(**defaults)


def _make_envelope(
    artifact_id: str = "abc123",
    version: int = 1,
    author_key: str = OWNER_A,
    **overrides,
) -> Envelope:
    defaults = dict(
        id=artifact_id,
        version=version,
        title=f"Test Artifact v{version}",
        html="<html><body>hi</body></html>",
        source_type="html",
        source={},
        author=_identity(key=author_key),
        status=STATUS_LIVE,
        note=None,
        canonical_file_id=None,
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Envelope(**defaults)


def _seed_legacy(backend, artifact_id: str = "legacy1", owner_key: str = OWNER_A) -> int:
    """Write a raw schema-1 envelope tagged the old way and return its file id."""
    payload = {
        "id": artifact_id,
        "title": "Legacy Artifact",
        "html": "<html><body>legacy</body></html>",
        "source_type": "markdown",
        "source": {"markdown": "# legacy"},
        "owner": _identity(key=owner_key),
        "password": {"algo": "pbkdf2-sha256", "hash": "x", "salt": "y"},
        "canonical_file_id": 42,
        "created_at": "2025-06-01T00:00:00+00:00",
        "updated_at": "2025-06-02T00:00:00+00:00",
        "version": 1,
        "schema": 1,
    }
    return backend.upload(
        f"artifact-{artifact_id}.json",
        json.dumps(payload).encode("utf-8"),
        [TAG_ALL, tag_for_id(artifact_id), tag_for_owner(owner_key)],
    )


# --------------------------------------------------------------------------
# Envelope serialization
# --------------------------------------------------------------------------


class TestEnvelopeSerialization:
    def test_round_trip(self):
        env = _make_envelope(note="what changed", canonical_file_id=7)
        assert Envelope.from_json(env.to_json()) == env

    def test_unknown_keys_are_tolerated(self):
        env = _make_envelope()
        payload = json.loads(env.to_json())
        payload["some_future_field"] = {"nested": "value"}
        restored = Envelope.from_json(json.dumps(payload).encode("utf-8"))
        assert restored.id == env.id
        assert restored.html == env.html

    def test_missing_optional_fields_fall_back_to_defaults(self):
        restored = Envelope.from_json(json.dumps({"id": "xyz"}).encode("utf-8"))
        assert restored.id == "xyz"
        assert restored.version == 1
        assert restored.title == ""
        assert restored.html == ""
        assert restored.source_type == "html"
        assert restored.source == {}
        assert restored.author == {}
        assert restored.status == STATUS_LIVE
        assert restored.note is None
        assert restored.canonical_file_id is None

    def test_unknown_status_falls_back_to_live(self):
        raw = json.dumps({"id": "x", "status": "whatever"}).encode("utf-8")
        assert Envelope.from_json(raw).status == STATUS_LIVE

    def test_legacy_schema1_payload_maps_owner_to_author(self):
        raw = json.dumps(
            {
                "id": "old",
                "title": "Old",
                "html": "<p>old</p>",
                "owner": _identity(),
                "password": {"algo": "pbkdf2-sha256"},
                "updated_at": "2025-01-01T00:00:00+00:00",
                "schema": 1,
            }
        ).encode("utf-8")
        env = Envelope.from_json(raw)
        assert env.author == _identity()
        assert env.status == STATUS_LIVE
        assert env.version == 1
        # The password is artifact-level state and must not ride in a version.
        assert "password" not in json.loads(env.to_json())

    def test_not_json_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(b"this is not json")

    def test_json_array_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(b"[1, 2, 3]")

    def test_missing_id_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(json.dumps({"title": "no id"}).encode("utf-8"))


class TestMetaSerialization:
    def test_round_trip(self):
        meta = _make_meta(
            password={"algo": "pbkdf2-sha256"},
            accept_versions=True,
            head_mode=HEAD_PINNED,
            head_version=3,
        )
        assert ArtifactMeta.from_json(meta.to_json()) == meta

    def test_defaults_and_unknown_keys(self):
        raw = json.dumps({"id": "x", "future": 1, "head_mode": "nope"}).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.id == "x"
        assert meta.owner == {}
        assert meta.password is None
        assert meta.accept_versions is False
        assert meta.head_mode == HEAD_LATEST
        assert meta.head_version is None

    def test_missing_id_raises_value_error(self):
        with pytest.raises(ValueError):
            ArtifactMeta.from_json(json.dumps({"owner": {}}).encode("utf-8"))


# --------------------------------------------------------------------------
# Envelope.public_meta
# --------------------------------------------------------------------------


class TestPublicMeta:
    def test_exposes_only_safe_author_fields(self):
        env = _make_envelope()
        meta = env.public_meta()
        assert meta["author"] == {
            "project_id": 1,
            "project_name": "Proj",
            "stack_host": "connection.keboola.com",
        }
        assert "stack_url" not in meta["author"]
        assert "key" not in meta["author"]

    def test_includes_expected_fields(self):
        env = _make_envelope(version=4, status=STATUS_PROPOSED, note="typo fix")
        meta = env.public_meta(is_head=False)
        assert meta["version"] == 4
        assert meta["status"] == STATUS_PROPOSED
        assert meta["note"] == "typo fix"
        assert meta["is_head"] is False
        assert meta["source_type"] == "html"
        assert meta["size_bytes"] == len(env.html.encode("utf-8"))
        assert meta["git"] is None

    def test_is_head_flag(self):
        assert _make_envelope().public_meta(is_head=True)["is_head"] is True

    def test_git_field_populated_for_git_source(self):
        env = _make_envelope(
            source_type="git-markdown",
            source={"git": {"url": "https://github.com/owner/repo.git", "ref": None}},
        )
        assert env.public_meta()["git"] == {
            "url": "https://github.com/owner/repo.git",
            "ref": None,
        }


# --------------------------------------------------------------------------
# create / get_head / get_version
# --------------------------------------------------------------------------


class TestCreateAndRead:
    def test_create_then_read_round_trip(self, tmp_store):
        meta = _make_meta()
        env = _make_envelope()
        tmp_store.create(meta, env)

        head = tmp_store.get_head("abc123")
        assert head is not None
        assert head.version == 1
        assert head.html == env.html

        loaded_meta = tmp_store.get_meta("abc123")
        assert loaded_meta is not None
        assert loaded_meta.owner["key"] == OWNER_A
        assert loaded_meta.accept_versions is False

    def test_unknown_artifact_reads_none(self, tmp_store):
        assert tmp_store.get_head("nope") is None
        assert tmp_store.get_meta("nope") is None
        assert tmp_store.get_version("nope", 1) is None
        assert tmp_store.list_versions("nope") == []
        assert tmp_store.owner_key_of("nope") is None

    def test_files_use_the_versioned_layout(self, tmp_store, backend):
        tmp_store.create(_make_meta(), _make_envelope())
        by_name = {info.name: info for info, _ in backend.files.values()}
        assert "artifact-abc123-meta.json" in by_name
        assert "artifact-abc123-v1.json" in by_name
        assert TAG_META in by_name["artifact-abc123-meta.json"].tags
        assert tag_for_owner(OWNER_A) in by_name["artifact-abc123-meta.json"].tags
        assert tag_for_version(1) in by_name["artifact-abc123-v1.json"].tags

    def test_head_is_latest_live_version(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2, title="second"))
        tmp_store.add_version(_make_envelope(version=3, title="third"))
        head = tmp_store.get_head("abc123")
        assert head is not None and head.version == 3

    def test_proposed_version_is_never_head(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(
            _make_envelope(version=2, status=STATUS_PROPOSED, author_key=OWNER_B)
        )
        head = tmp_store.get_head("abc123")
        assert head is not None and head.version == 1

    def test_head_pinned(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        tmp_store.add_version(_make_envelope(version=3))
        tmp_store.save_meta(_make_meta(head_mode=HEAD_PINNED, head_version=2))
        head = tmp_store.get_head("abc123")
        assert head is not None and head.version == 2

    def test_pinned_to_proposed_falls_back_to_latest_live(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        tmp_store.save_meta(_make_meta(head_mode=HEAD_PINNED, head_version=2))
        head = tmp_store.get_head("abc123")
        assert head is not None and head.version == 1

    def test_get_version_returns_proposed_content(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(
            _make_envelope(version=2, status=STATUS_PROPOSED, title="draft")
        )
        env = tmp_store.get_version("abc123", 2)
        assert env is not None
        assert env.title == "draft"
        assert env.status == STATUS_PROPOSED

    def test_next_version_counts_proposals(self, tmp_store):
        assert tmp_store.next_version("abc123") == 1
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        assert tmp_store.next_version("abc123") == 2
        tmp_store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        assert tmp_store.next_version("abc123") == 3


class TestSaveMeta:
    def test_resave_retires_the_old_meta_file(self, tmp_store, backend):
        tmp_store.create(_make_meta(), _make_envelope())
        tmp_store.save_meta(_make_meta(accept_versions=True))

        metas = [
            info
            for info, _ in backend.files.values()
            if TAG_META in info.tags and tag_for_id("abc123") in info.tags
        ]
        assert len(metas) == 1
        loaded = tmp_store.get_meta("abc123")
        assert loaded is not None and loaded.accept_versions is True


class TestListVersions:
    def test_newest_first_with_head_flag(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        tmp_store.add_version(
            _make_envelope(version=3, status=STATUS_PROPOSED, note="proposal")
        )

        rows = tmp_store.list_versions("abc123")
        assert [row["version"] for row in rows] == [3, 2, 1]
        assert [row["is_head"] for row in rows] == [False, True, False]
        assert rows[0]["status"] == STATUS_PROPOSED
        assert rows[0]["note"] == "proposal"


class TestSetStatus:
    def test_promote_proposal_makes_it_head(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(
            _make_envelope(
                version=2, status=STATUS_PROPOSED, author_key=OWNER_B, title="community"
            )
        )
        assert tmp_store.get_head("abc123").version == 1

        assert tmp_store.set_status("abc123", 2, STATUS_LIVE) is True
        head = tmp_store.get_head("abc123")
        assert head is not None
        assert head.version == 2
        assert head.title == "community"
        assert head.author["key"] == OWNER_B

    def test_promote_leaves_exactly_one_file_for_the_version(self, tmp_store, backend):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        tmp_store.set_status("abc123", 2, STATUS_LIVE)
        files = [
            info for info, _ in backend.files.values() if tag_for_version(2) in info.tags
        ]
        assert len(files) == 1

    def test_demote_live_to_proposed(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        assert tmp_store.set_status("abc123", 2, STATUS_PROPOSED) is True
        assert tmp_store.get_head("abc123").version == 1

    def test_unknown_version_returns_false(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        assert tmp_store.set_status("abc123", 9, STATUS_LIVE) is False

    def test_unknown_status_raises(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        with pytest.raises(ValueError):
            tmp_store.set_status("abc123", 1, "deleted")


class TestDeleteVersion:
    def test_delete_proposal(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        assert tmp_store.delete_version("abc123", 2) is True
        assert tmp_store.get_version("abc123", 2) is None
        assert [row["version"] for row in tmp_store.list_versions("abc123")] == [1]

    def test_refuses_to_delete_the_only_live_version(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        assert tmp_store.delete_version("abc123", 1) is False
        assert tmp_store.get_head("abc123") is not None

    def test_deletes_an_older_live_version(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        assert tmp_store.delete_version("abc123", 1) is True
        assert tmp_store.get_version("abc123", 1) is None
        assert tmp_store.get_head("abc123").version == 2

    def test_unknown_version_returns_false(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        assert tmp_store.delete_version("abc123", 7) is False


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def _store_with_limit(backend, tmp_path, limit: int) -> ArtifactStore:
    return ArtifactStore(
        backend=backend,
        cache_dir=tmp_path / "cache",
        cache_max_entries=50,
        max_versions=limit,
    )


class TestRetention:
    def test_prunes_oldest_live_versions_above_the_limit(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 3)
        store.create(_make_meta(), _make_envelope(version=1))
        for n in range(2, 7):
            store.add_version(_make_envelope(version=n))

        numbers = [row["version"] for row in store.list_versions("abc123")]
        assert numbers == [6, 5, 4]
        assert store.get_head("abc123").version == 6

    def test_never_prunes_the_head_or_the_pinned_version(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 2)
        store.create(_make_meta(), _make_envelope(version=1))
        store.add_version(_make_envelope(version=2))
        store.save_meta(_make_meta(head_mode=HEAD_PINNED, head_version=1))
        for n in range(3, 6):
            store.add_version(_make_envelope(version=n))

        numbers = [row["version"] for row in store.list_versions("abc123")]
        # v1 is pinned (and therefore also the head) so it survives; the newest
        # version always survives too.
        assert 1 in numbers
        assert 5 in numbers
        assert len(numbers) == 2

    def test_never_prunes_proposals(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 2)
        store.create(_make_meta(), _make_envelope(version=1))
        store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        for n in range(3, 7):
            store.add_version(_make_envelope(version=n))

        rows = {row["version"]: row["status"] for row in store.list_versions("abc123")}
        assert rows[2] == STATUS_PROPOSED
        live = [v for v, status in rows.items() if status == STATUS_LIVE]
        assert len(live) == 2


# --------------------------------------------------------------------------
# Hydrate / delete / list_owner
# --------------------------------------------------------------------------


class TestHydrate:
    def test_cold_store_rebuilds_the_index(self, backend, tmp_path, settings):
        store1 = _store_with_limit(backend, tmp_path / "one", 50)
        store1.create(_make_meta(), _make_envelope(version=1))
        store1.add_version(_make_envelope(version=2, title="second"))
        store1.create(_make_meta(artifact_id="other"), _make_envelope("other"))

        store2 = _store_with_limit(backend, tmp_path / "two", 50)
        assert store2.count() == 0
        assert store2.hydrate() == 2
        assert store2.count() == 2

        head = store2.get_head("abc123")
        assert head is not None
        assert head.version == 2
        assert head.title == "second"
        assert store2.get_meta("abc123").owner["key"] == OWNER_A


class TestDelete:
    def test_delete_removes_every_file(self, tmp_store, backend):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        assert tmp_store.delete("abc123") is True
        assert tmp_store.get_head("abc123") is None
        assert tmp_store.get_meta("abc123") is None
        assert not [
            info for info, _ in backend.files.values() if tag_for_id("abc123") in info.tags
        ]

    def test_delete_unknown_artifact_returns_false(self, tmp_store):
        assert tmp_store.delete("nope") is False


class TestListOwner:
    def test_shape_and_ordering(self, tmp_store):
        tmp_store.create(
            _make_meta(artifact_id="a1", updated_at="2026-01-01T00:00:00+00:00"),
            _make_envelope("a1", title="first"),
        )
        tmp_store.create(
            _make_meta(
                artifact_id="a2",
                updated_at="2026-01-03T00:00:00+00:00",
                accept_versions=True,
                password={"algo": "pbkdf2-sha256"},
            ),
            _make_envelope("a2", title="second"),
        )
        tmp_store.add_version(
            _make_envelope("a2", version=2, status=STATUS_PROPOSED, author_key=OWNER_B)
        )
        tmp_store.create(
            _make_meta(artifact_id="a3", owner_key=OWNER_B),
            _make_envelope("a3", author_key=OWNER_B),
        )

        rows = tmp_store.list_owner(OWNER_A)
        assert [row["id"] for row in rows] == ["a2", "a1"]
        assert rows[0] == {
            "id": "a2",
            "title": "second",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-03T00:00:00+00:00",
            "accept_versions": True,
            "protected": True,
            "head_version": 1,
            "versions_count": 2,
            "proposed_count": 1,
        }

    def test_owner_key_of(self, tmp_store):
        tmp_store.create(
            _make_meta(owner_key="7@connection.keboola.com"),
            _make_envelope(author_key="7@connection.keboola.com"),
        )
        assert tmp_store.owner_key_of("abc123") == "7@connection.keboola.com"


# --------------------------------------------------------------------------
# Legacy schema-1 migration
# --------------------------------------------------------------------------


class TestMigrateLegacyHelper:
    def test_splits_envelope_and_meta(self, backend):
        _seed_legacy(backend)
        raw = backend.download(1)
        env, meta = migrate_legacy(raw)

        assert env.version == 1
        assert env.status == STATUS_LIVE
        assert env.author["key"] == OWNER_A
        assert env.source == {"markdown": "# legacy"}
        assert env.canonical_file_id == 42

        assert meta.id == env.id
        assert meta.owner["key"] == OWNER_A
        assert meta.password == {"algo": "pbkdf2-sha256", "hash": "x", "salt": "y"}
        assert meta.accept_versions is False
        assert meta.head_mode == HEAD_LATEST
        assert meta.created_at == "2025-06-01T00:00:00+00:00"
        assert meta.updated_at == "2025-06-02T00:00:00+00:00"


class TestLegacyArtifactEndToEnd:
    def test_reads_like_a_versioned_artifact(self, backend, tmp_path):
        _seed_legacy(backend)
        store = _store_with_limit(backend, tmp_path, 50)
        assert store.hydrate() == 1

        meta = store.get_meta("legacy1")
        assert meta is not None
        assert meta.owner["key"] == OWNER_A
        assert meta.password is not None
        assert store.owner_key_of("legacy1") == OWNER_A

        head = store.get_head("legacy1")
        assert head is not None
        assert head.version == 1
        assert head.html == "<html><body>legacy</body></html>"

        v1 = store.get_version("legacy1", 1)
        assert v1 is not None and v1.title == "Legacy Artifact"

        rows = store.list_versions("legacy1")
        assert len(rows) == 1
        assert rows[0]["version"] == 1
        assert rows[0]["is_head"] is True
        assert rows[0]["status"] == STATUS_LIVE

        assert store.next_version("legacy1") == 2

    def test_listed_for_its_owner(self, backend, tmp_path):
        _seed_legacy(backend)
        store = _store_with_limit(backend, tmp_path, 50)
        store.hydrate()
        rows = store.list_owner(OWNER_A)
        assert len(rows) == 1
        assert rows[0]["id"] == "legacy1"
        assert rows[0]["title"] == "Legacy Artifact"
        assert rows[0]["protected"] is True
        assert rows[0]["head_version"] == 1
        assert rows[0]["versions_count"] == 1

    def test_owner_adds_a_second_version(self, backend, tmp_path):
        _seed_legacy(backend)
        store = _store_with_limit(backend, tmp_path, 50)
        store.hydrate()
        store.save_meta(_make_meta(artifact_id="legacy1"))
        store.add_version(_make_envelope("legacy1", version=2, title="v2"))

        head = store.get_head("legacy1")
        assert head is not None and head.version == 2
        assert [row["version"] for row in store.list_versions("legacy1")] == [2, 1]

    def test_resolves_without_hydrate(self, backend, tmp_path):
        _seed_legacy(backend)
        store = _store_with_limit(backend, tmp_path, 50)
        # No hydrate(): the index miss must fall back to a tag search.
        head = store.get_head("legacy1")
        assert head is not None and head.version == 1

    def test_delete_removes_the_legacy_file(self, backend, tmp_path):
        _seed_legacy(backend)
        store = _store_with_limit(backend, tmp_path, 50)
        store.hydrate()
        assert store.delete("legacy1") is True
        assert store.get_head("legacy1") is None
        assert backend.files == {}


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------


class TestDiskCache:
    def test_cache_file_exists_after_read(self, tmp_store, tmp_path):
        tmp_store.create(_make_meta(), _make_envelope())
        assert tmp_store.get_head("abc123") is not None
        assert list((tmp_path / "cache").glob("abc123-*.json"))

    def test_corrupt_disk_cache_falls_back_to_storage(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 50)
        store.create(_make_meta(), _make_envelope())
        assert store.get_head("abc123") is not None

        cache_files = list((tmp_path / "cache").glob("abc123-*.json"))
        assert cache_files
        for path in cache_files:
            path.write_bytes(b"not valid json at all")

        fresh = _store_with_limit(backend, tmp_path, 50)
        fresh.hydrate()
        head = fresh.get_head("abc123")
        assert head is not None
        assert head.title == "Test Artifact v1"
