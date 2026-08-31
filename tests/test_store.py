"""Tests for src.store: Envelope (de)serialization and the ArtifactStore."""

import json

import pytest

from src.kbc import InMemoryFilesBackend
from src.store import ArtifactStore, Envelope, tag_for_id


def _make_envelope(
    artifact_id: str = "abc123",
    owner_key: str = "1@connection.keboola.com",
    **overrides,
) -> Envelope:
    defaults = dict(
        id=artifact_id,
        title="Test Artifact",
        html="<html><body>hi</body></html>",
        source_type="html",
        source={},
        owner={
            "stack_url": "https://connection.keboola.com",
            "project_id": 1,
            "project_name": "Proj",
            "key": owner_key,
        },
        password=None,
        canonical_file_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        version=1,
        schema=1,
    )
    defaults.update(overrides)
    return Envelope(**defaults)


# --------------------------------------------------------------------------
# Envelope serialization
# --------------------------------------------------------------------------


class TestEnvelopeSerialization:
    def test_round_trip(self):
        env = _make_envelope()
        restored = Envelope.from_json(env.to_json())
        assert restored == env

    def test_unknown_keys_are_tolerated(self):
        env = _make_envelope()
        payload = json.loads(env.to_json())
        payload["some_future_field"] = {"nested": "value"}
        restored = Envelope.from_json(json.dumps(payload).encode("utf-8"))
        assert restored.id == env.id
        assert restored.title == env.title
        assert restored.html == env.html

    def test_missing_optional_fields_fall_back_to_defaults(self):
        restored = Envelope.from_json(json.dumps({"id": "xyz"}).encode("utf-8"))
        assert restored.id == "xyz"
        assert restored.title == ""
        assert restored.html == ""
        assert restored.source_type == "html"
        assert restored.source == {}
        assert restored.owner == {}
        assert restored.password is None
        assert restored.canonical_file_id is None
        assert restored.version == 1
        assert restored.schema == 1

    def test_not_json_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(b"this is not json")

    def test_json_array_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(b"[1, 2, 3]")

    def test_missing_id_raises_value_error(self):
        with pytest.raises(ValueError):
            Envelope.from_json(json.dumps({"title": "no id here"}).encode("utf-8"))


# --------------------------------------------------------------------------
# Envelope.public_meta
# --------------------------------------------------------------------------


class TestPublicMeta:
    def test_hides_owner_and_password(self):
        env = _make_envelope(password={"algo": "pbkdf2-sha256"})
        meta = env.public_meta()
        assert "owner" not in meta
        assert "password" not in meta

    def test_protected_true_when_password_set(self):
        env = _make_envelope(password={"algo": "pbkdf2-sha256"})
        assert env.public_meta()["protected"] is True

    def test_protected_false_when_no_password(self):
        env = _make_envelope(password=None)
        assert env.public_meta()["protected"] is False

    def test_includes_expected_public_fields(self):
        env = _make_envelope()
        meta = env.public_meta()
        assert meta["id"] == env.id
        assert meta["title"] == env.title
        assert meta["source_type"] == "html"
        assert meta["size_bytes"] == len(env.html.encode("utf-8"))
        assert meta["git"] is None

    def test_git_field_populated_for_git_source(self):
        env = _make_envelope(
            source_type="git-markdown",
            source={"git": {"url": "https://github.com/owner/repo.git", "ref": None}},
        )
        meta = env.public_meta()
        assert meta["git"] == {"url": "https://github.com/owner/repo.git", "ref": None}


# --------------------------------------------------------------------------
# ArtifactStore: publish / get / republish
# --------------------------------------------------------------------------


class TestPublishAndGet:
    def test_publish_then_get_round_trip(self, tmp_store):
        env = _make_envelope()
        tmp_store.publish(env)
        loaded = tmp_store.get(env.id)
        assert loaded is not None
        assert loaded.id == env.id
        assert loaded.title == env.title
        assert loaded.html == env.html

    def test_get_unknown_artifact_returns_none(self, tmp_store):
        assert tmp_store.get("does-not-exist") is None

    def test_republish_same_id_retires_old_backend_file(self, tmp_store, backend):
        env = _make_envelope()
        tmp_store.publish(env)

        env2 = _make_envelope(
            title="Updated Title", updated_at="2026-01-02T00:00:00+00:00"
        )
        tmp_store.publish(env2)

        loaded = tmp_store.get(env.id)
        assert loaded is not None
        assert loaded.title == "Updated Title"

        matching = [
            info
            for info, _ in backend.files.values()
            if tag_for_id(env.id) in info.tags
        ]
        assert len(matching) == 1


class TestHydrate:
    def test_hydrate_from_cold_store_finds_artifacts(self, backend, tmp_path, settings):
        env = _make_envelope()
        store1 = ArtifactStore(
            backend=backend,
            cache_dir=tmp_path / "cache1",
            cache_max_entries=settings.cache_max_entries,
        )
        store1.publish(env)

        store2 = ArtifactStore(
            backend=backend,
            cache_dir=tmp_path / "cache2",
            cache_max_entries=settings.cache_max_entries,
        )
        assert store2.count() == 0
        count = store2.hydrate()
        assert count == 1
        assert store2.count() == 1

        loaded = store2.get(env.id)
        assert loaded is not None
        assert loaded.id == env.id
        assert loaded.title == env.title


class TestDelete:
    def test_delete_removes_artifact(self, tmp_store):
        env = _make_envelope()
        tmp_store.publish(env)
        assert tmp_store.delete(env.id) is True
        assert tmp_store.get(env.id) is None

    def test_delete_unknown_artifact_returns_false(self, tmp_store):
        assert tmp_store.delete("nope") is False


class TestListOwner:
    def test_returns_only_matching_owner_sorted_by_updated_at_desc(self, tmp_store):
        owner_a = "1@connection.keboola.com"
        owner_b = "2@connection.keboola.com"
        env1 = _make_envelope(
            artifact_id="a1", owner_key=owner_a, updated_at="2026-01-01T00:00:00+00:00"
        )
        env2 = _make_envelope(
            artifact_id="a2", owner_key=owner_a, updated_at="2026-01-03T00:00:00+00:00"
        )
        env3 = _make_envelope(
            artifact_id="a3", owner_key=owner_b, updated_at="2026-01-02T00:00:00+00:00"
        )
        tmp_store.publish(env1)
        tmp_store.publish(env2)
        tmp_store.publish(env3)

        result = tmp_store.list_owner(owner_a)
        assert [meta["id"] for meta in result] == ["a2", "a1"]


class TestOwnerKeyOf:
    def test_returns_owner_key_for_known_artifact(self, tmp_store):
        env = _make_envelope(owner_key="7@connection.keboola.com")
        tmp_store.publish(env)
        assert tmp_store.owner_key_of(env.id) == "7@connection.keboola.com"

    def test_returns_none_for_unknown_artifact(self, tmp_store):
        assert tmp_store.owner_key_of("nope") is None


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------


class TestDiskCache:
    def test_cache_file_exists_after_get(self, tmp_store, tmp_path):
        env = _make_envelope()
        tmp_store.publish(env)
        loaded = tmp_store.get(env.id)
        assert loaded is not None

        cache_dir = tmp_path / "cache"
        cache_files = list(cache_dir.glob(f"{env.id}-*.json"))
        assert cache_files, "expected a disk cache file after publish + get"

    def test_corrupt_disk_cache_still_returns_valid_envelope(
        self, tmp_store, tmp_path, backend, settings
    ):
        env = _make_envelope()
        tmp_store.publish(env)

        cache_dir = tmp_path / "cache"
        cache_files = list(cache_dir.glob(f"{env.id}-*.json"))
        assert cache_files
        cache_files[0].write_bytes(b"not valid json at all")

        # A fresh store (empty in-process memory LRU) over the same backend
        # and the now-corrupted cache dir must fall back to Storage when the
        # disk cache fails to parse, rather than raising or returning None.
        fresh_store = ArtifactStore(
            backend=backend,
            cache_dir=cache_dir,
            cache_max_entries=settings.cache_max_entries,
        )
        fresh_store.hydrate()
        loaded = fresh_store.get(env.id)
        assert loaded is not None
        assert loaded.id == env.id
        assert loaded.title == env.title
