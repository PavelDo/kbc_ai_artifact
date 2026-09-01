"""Tests for src.store: envelopes, meta records and the versioned store."""

import json
import threading

import pytest

from src.kbc import BackendError, InMemoryFilesBackend
from src.store import (
    ACCEPT_ALLOWLIST,
    ACCEPT_ANYONE,
    ACCEPT_OFF,
    ARTIFACT_DRAFT,
    ARTIFACT_FINAL,
    COMMENTS_ALLOWLIST,
    COMMENTS_ANYONE,
    COMMENTS_OFF,
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
        # accept_versions is deliberately not defaulted here: passing it
        # explicitly wins over accept_versions_mode (see ArtifactMeta), so a
        # default would stop tests from setting a mode.
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

    def test_phase3_defaults(self):
        meta = _make_meta()
        assert meta.accept_versions_mode == ACCEPT_OFF
        assert meta.accept_versions is False
        assert meta.contributors == []
        assert meta.comments_mode == COMMENTS_ANYONE
        assert meta.status == ARTIFACT_DRAFT

    def test_phase3_fields_round_trip(self):
        meta = _make_meta(
            accept_versions_mode=ACCEPT_ALLOWLIST,
            contributors=[OWNER_B],
            comments_mode=COMMENTS_OFF,
            status=ARTIFACT_FINAL,
        )
        restored = ArtifactMeta.from_json(meta.to_json())
        assert restored == meta
        assert restored.accept_versions_mode == ACCEPT_ALLOWLIST
        assert restored.contributors == [OWNER_B]
        assert restored.comments_mode == COMMENTS_OFF
        assert restored.status == ARTIFACT_FINAL

    def test_unknown_enum_values_fall_back(self):
        raw = json.dumps(
            {
                "id": "x",
                "accept_versions_mode": "sideways",
                "comments_mode": "sideways",
                "status": "sideways",
                "contributors": "not a list",
            }
        ).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.accept_versions_mode == ACCEPT_OFF
        assert meta.comments_mode == COMMENTS_ANYONE
        assert meta.status == ARTIFACT_DRAFT
        assert meta.contributors == []

    def test_constructor_normalizes_unknown_enum_values(self):
        meta = _make_meta(
            accept_versions_mode="sideways",
            comments_mode="sideways",
            status="sideways",
            contributors=None,
        )
        assert meta.accept_versions_mode == ACCEPT_OFF
        assert meta.comments_mode == COMMENTS_ANYONE
        assert meta.status == ARTIFACT_DRAFT
        assert meta.contributors == []


class TestAcceptVersionsBackCompat:
    """``accept_versions`` (bool) stays a working view of ``accept_versions_mode``."""

    def test_legacy_true_reads_as_anyone(self):
        raw = json.dumps({"id": "x", "accept_versions": True}).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.accept_versions_mode == ACCEPT_ANYONE
        assert meta.accept_versions is True

    def test_legacy_false_reads_as_off(self):
        raw = json.dumps({"id": "x", "accept_versions": False}).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.accept_versions_mode == ACCEPT_OFF
        assert meta.accept_versions is False

    def test_mode_wins_over_a_disagreeing_legacy_bool(self):
        raw = json.dumps(
            {"id": "x", "accept_versions": False, "accept_versions_mode": "allowlist"}
        ).encode("utf-8")
        meta = ArtifactMeta.from_json(raw)
        assert meta.accept_versions_mode == ACCEPT_ALLOWLIST
        assert meta.accept_versions is True

    def test_to_json_writes_both_representations(self):
        payload = json.loads(_make_meta(accept_versions_mode=ACCEPT_ALLOWLIST).to_json())
        assert payload["accept_versions"] is True
        assert payload["accept_versions_mode"] == ACCEPT_ALLOWLIST

        payload = json.loads(_make_meta().to_json())
        assert payload["accept_versions"] is False
        assert payload["accept_versions_mode"] == ACCEPT_OFF

    def test_constructing_with_the_bool_sets_the_mode(self):
        assert _make_meta(accept_versions=True).accept_versions_mode == ACCEPT_ANYONE
        assert _make_meta(accept_versions=False).accept_versions_mode == ACCEPT_OFF

    def test_assigning_the_bool_flips_the_mode(self):
        meta = _make_meta()
        meta.accept_versions = True
        assert meta.accept_versions_mode == ACCEPT_ANYONE
        meta.accept_versions = False
        assert meta.accept_versions_mode == ACCEPT_OFF

    def test_assigning_true_does_not_widen_an_allowlist(self):
        meta = _make_meta(accept_versions_mode=ACCEPT_ALLOWLIST)
        meta.accept_versions = True
        assert meta.accept_versions_mode == ACCEPT_ALLOWLIST

    def test_assigning_false_closes_an_allowlist(self):
        meta = _make_meta(accept_versions_mode=ACCEPT_ALLOWLIST)
        meta.accept_versions = False
        assert meta.accept_versions_mode == ACCEPT_OFF

    def test_bool_and_mode_together_keep_the_mode(self):
        meta = _make_meta(accept_versions_mode=ACCEPT_ALLOWLIST, accept_versions=True)
        assert meta.accept_versions_mode == ACCEPT_ALLOWLIST

    def test_mode_only_construction_is_not_reset_by_the_bool_default(self):
        meta = ArtifactMeta(id="x", accept_versions_mode=ACCEPT_ALLOWLIST)
        assert meta.accept_versions_mode == ACCEPT_ALLOWLIST
        assert meta.accept_versions is True

    def test_the_flipped_bool_survives_a_storage_round_trip(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope())
        meta = tmp_store.get_meta("abc123")
        assert meta is not None
        meta.accept_versions = True  # what the phase-2 API layer does
        tmp_store.save_meta(meta)

        reloaded = tmp_store.get_meta("abc123")
        assert reloaded is not None
        assert reloaded.accept_versions is True
        assert reloaded.accept_versions_mode == ACCEPT_ANYONE


class TestPermissionHelpers:
    def test_owner_may_always_contribute_while_drafting(self):
        for mode in (ACCEPT_OFF, ACCEPT_ANYONE, ACCEPT_ALLOWLIST):
            meta = _make_meta(accept_versions_mode=mode)
            assert meta.allows_versions_from(OWNER_A) is True
        for mode in (COMMENTS_OFF, COMMENTS_ANYONE, COMMENTS_ALLOWLIST):
            meta = _make_meta(comments_mode=mode)
            assert meta.allows_comments_from(OWNER_A) is True

    def test_versions_truth_table(self):
        cases = {
            ACCEPT_OFF: False,
            ACCEPT_ANYONE: True,
            ACCEPT_ALLOWLIST: False,
        }
        for mode, expected in cases.items():
            meta = _make_meta(accept_versions_mode=mode)
            assert meta.allows_versions_from(OWNER_B) is expected

    def test_versions_allowlist_admits_listed_contributors(self):
        meta = _make_meta(
            accept_versions_mode=ACCEPT_ALLOWLIST, contributors=[OWNER_B]
        )
        assert meta.allows_versions_from(OWNER_B) is True
        assert meta.allows_versions_from("9@connection.keboola.com") is False

    def test_comments_truth_table(self):
        cases = {
            COMMENTS_OFF: False,
            COMMENTS_ANYONE: True,
            COMMENTS_ALLOWLIST: False,
        }
        for mode, expected in cases.items():
            meta = _make_meta(comments_mode=mode)
            assert meta.allows_comments_from(OWNER_B) is expected

    def test_comments_allowlist_admits_listed_contributors(self):
        meta = _make_meta(comments_mode=COMMENTS_ALLOWLIST, contributors=[OWNER_B])
        assert meta.allows_comments_from(OWNER_B) is True
        assert meta.allows_comments_from("9@connection.keboola.com") is False

    def test_final_freezes_everyone_including_the_owner(self):
        meta = _make_meta(
            status=ARTIFACT_FINAL,
            accept_versions_mode=ACCEPT_ANYONE,
            comments_mode=COMMENTS_ANYONE,
            contributors=[OWNER_B],
        )
        assert meta.is_final() is True
        for key in (OWNER_A, OWNER_B, "", "9@connection.keboola.com"):
            assert meta.allows_versions_from(key) is False
            assert meta.allows_comments_from(key) is False

    def test_an_empty_key_is_never_the_owner(self):
        meta = _make_meta(
            accept_versions_mode=ACCEPT_ALLOWLIST,
            comments_mode=COMMENTS_ALLOWLIST,
            contributors=[""],
            owner_key="",
        )
        assert meta.allows_versions_from("") is False
        assert meta.allows_comments_from("") is False


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
            "accept_versions_mode": ACCEPT_ANYONE,
            "comments_mode": COMMENTS_ANYONE,
            "status": ARTIFACT_DRAFT,
            "protected": True,
            "head_version": 1,
            "versions_count": 2,
            "proposed_count": 1,
        }

    def test_rows_report_the_phase3_policy(self, tmp_store):
        tmp_store.create(
            _make_meta(
                accept_versions_mode=ACCEPT_ALLOWLIST,
                contributors=[OWNER_B],
                comments_mode=COMMENTS_OFF,
                status=ARTIFACT_FINAL,
            ),
            _make_envelope(),
        )
        row = tmp_store.list_owner(OWNER_A)[0]
        assert row["accept_versions"] is True
        assert row["accept_versions_mode"] == ACCEPT_ALLOWLIST
        assert row["comments_mode"] == COMMENTS_OFF
        assert row["status"] == ARTIFACT_FINAL

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


# --------------------------------------------------------------------------
# Test doubles for failure-injection / concurrency
# --------------------------------------------------------------------------


class _FailingDeleteBackend(InMemoryFilesBackend):
    """In-memory backend whose delete raises for a chosen set of file ids."""

    def __init__(self, fail_ids: set[int] | None = None) -> None:
        super().__init__()
        self.fail_ids: set[int] = set(fail_ids or set())

    def delete(self, file_id: int) -> None:
        if file_id in self.fail_ids:
            raise BackendError(f"boom deleting {file_id}")
        super().delete(file_id)


class _LockingBackend(InMemoryFilesBackend):
    """Serializes uploads so a threaded test never races the id counter."""

    def __init__(self) -> None:
        super().__init__()
        self._upload_lock = threading.Lock()

    def upload(self, name: str, content: bytes, tags: list[str]) -> int:
        with self._upload_lock:
            return super().upload(name, content, tags)


# --------------------------------------------------------------------------
# Finding 1: envelope/meta id must match the requested artifact id
# --------------------------------------------------------------------------


class TestEnvelopeIdVerification:
    def test_version_file_with_wrong_payload_id_is_skipped(self, backend, tmp_path):
        # A file tagged for artifact A but whose JSON declares id B must not be
        # served under A (cross-artifact content leak).
        store = _store_with_limit(backend, tmp_path, 50)
        payload = json.loads(_make_envelope(artifact_id="B", version=1).to_json())
        backend.upload(
            "artifact-A-v1.json",
            json.dumps(payload).encode("utf-8"),
            [TAG_ALL, tag_for_id("A"), tag_for_version(1)],
        )
        store.hydrate()
        assert store.get_head("A") is None
        assert store.get_version("A", 1) is None
        assert store.list_versions("A") == []

    def test_meta_file_with_wrong_payload_id_is_skipped(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 50)
        payload = json.loads(_make_meta(artifact_id="B").to_json())
        backend.upload(
            "artifact-A-meta.json",
            json.dumps(payload).encode("utf-8"),
            [TAG_ALL, tag_for_id("A"), TAG_META, tag_for_owner(OWNER_A)],
        )
        store.hydrate()
        assert store.get_meta("A") is None

    def test_version_tag_mismatch_is_skipped(self, backend, tmp_path):
        # File tagged v2 but its payload says version 5: the tag is
        # authoritative and the mislabelled record is skipped.
        store = _store_with_limit(backend, tmp_path, 50)
        payload = json.loads(_make_envelope(artifact_id="A", version=5).to_json())
        backend.upload(
            "artifact-A-v2.json",
            json.dumps(payload).encode("utf-8"),
            [TAG_ALL, tag_for_id("A"), tag_for_version(2)],
        )
        store.hydrate()
        assert store.get_version("A", 2) is None

    def test_matching_id_still_serves(self, tmp_store):
        tmp_store.create(_make_meta(artifact_id="ok1"), _make_envelope("ok1"))
        assert tmp_store.get_head("ok1") is not None


# --------------------------------------------------------------------------
# Finding 2: non-string title/html rejected as corrupt
# --------------------------------------------------------------------------


class TestEnvelopeValueValidation:
    def test_non_string_title_raises(self):
        raw = json.dumps({"id": "x", "title": 123}).encode("utf-8")
        with pytest.raises(ValueError):
            Envelope.from_json(raw)

    def test_non_string_html_raises(self):
        raw = json.dumps({"id": "x", "html": {"unexpected": True}}).encode("utf-8")
        with pytest.raises(ValueError):
            Envelope.from_json(raw)

    def test_unknown_source_type_normalizes_to_html(self):
        raw = json.dumps({"id": "x", "source_type": "wat"}).encode("utf-8")
        assert Envelope.from_json(raw).source_type == "html"

    def test_non_string_source_type_normalizes_to_html(self):
        raw = json.dumps({"id": "x", "source_type": 7}).encode("utf-8")
        assert Envelope.from_json(raw).source_type == "html"

    def test_corrupt_title_makes_store_skip_the_record(self, backend, tmp_path):
        # A malformed persisted record must be quarantined, not 500 later.
        store = _store_with_limit(backend, tmp_path, 50)
        backend.upload(
            "artifact-A-v1.json",
            json.dumps({"id": "A", "title": 123, "html": "<p>hi</p>"}).encode("utf-8"),
            [TAG_ALL, tag_for_id("A"), tag_for_version(1)],
        )
        store.hydrate()
        assert store.get_version("A", 1) is None


# --------------------------------------------------------------------------
# Finding 3: oversized records are skipped before decode
# --------------------------------------------------------------------------


class TestEnvelopeSizeBound:
    def _small_store(self, backend, tmp_path, limit_bytes):
        return ArtifactStore(
            backend=backend,
            cache_dir=tmp_path / "cache",
            cache_max_entries=50,
            max_versions=50,
            max_envelope_bytes=limit_bytes,
        )

    def test_oversized_version_is_skipped(self, backend, tmp_path):
        # Seed an oversized version file directly and read it from a cold store
        # (empty LRU), so the read goes through the size-bounded download path.
        big_html = "x" * 5000
        backend.upload(
            "artifact-abc123-v1.json",
            _make_envelope(html=big_html).to_json(),
            [TAG_ALL, tag_for_id("abc123"), tag_for_version(1)],
        )
        store = self._small_store(backend, tmp_path, 1000)
        store.hydrate()
        # The record exceeds the 1000-byte bound, so it is not served.
        assert store.get_head("abc123") is None
        assert store.get_version("abc123", 1) is None

    def test_within_bound_is_served(self, backend, tmp_path):
        store = self._small_store(backend, tmp_path, 20 * 1024 * 1024)
        store.create(_make_meta(), _make_envelope())
        assert store.get_head("abc123") is not None

    def test_zero_bound_disables_the_limit(self, backend, tmp_path):
        store = self._small_store(backend, tmp_path, 0)
        store.create(_make_meta(), _make_envelope(html="x" * 100000))
        assert store.get_head("abc123") is not None

    def test_oversized_disk_cache_is_dropped(self, backend, tmp_path):
        store = self._small_store(backend, tmp_path, 1000)
        # Write a valid small artifact, then corrupt its cache to be oversized.
        big_store = self._small_store(backend, tmp_path, 20 * 1024 * 1024)
        big_store.create(_make_meta(), _make_envelope())
        cache_files = list((tmp_path / "cache").glob("abc123-*.json"))
        assert cache_files
        for path in cache_files:
            path.write_bytes(b"x" * 5000)
        # store shares the same cache dir; the oversized cache file is dropped
        # and (since the backend copy is small) the record still loads.
        assert store.get_head("abc123") is not None


# --------------------------------------------------------------------------
# Finding 4: atomic version allocation (no colliding numbers)
# --------------------------------------------------------------------------


class TestAddVersionNext:
    def test_sequential_calls_get_distinct_numbers(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        v_a = tmp_store.add_version_next(_make_envelope(title="a"))
        v_b = tmp_store.add_version_next(_make_envelope(title="b"))
        assert {v_a, v_b} == {2, 3}
        assert [row["version"] for row in tmp_store.list_versions("abc123")] == [3, 2, 1]

    def test_returns_the_assigned_number_and_ignores_env_version(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        # env carries version=99 but the store assigns the real next number.
        assigned = tmp_store.add_version_next(_make_envelope(version=99, title="x"))
        assert assigned == 2
        env = tmp_store.get_version("abc123", 2)
        assert env is not None and env.version == 2 and env.title == "x"

    def test_first_version_is_one(self, tmp_store):
        assigned = tmp_store.add_version_next(_make_envelope())
        assert assigned == 1

    def test_concurrent_allocations_do_not_collide(self, tmp_path):
        backend = _LockingBackend()
        store = ArtifactStore(
            backend=backend,
            cache_dir=tmp_path / "cache",
            cache_max_entries=50,
            max_versions=50,
        )
        store.create(_make_meta(), _make_envelope(version=1))

        assigned: list[int] = []
        lock = threading.Lock()
        start = threading.Barrier(4)

        def worker(i: int) -> None:
            start.wait()
            n = store.add_version_next(_make_envelope(title=f"t{i}"))
            with lock:
                assigned.append(n)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Four distinct numbers, none reused, contiguous with the seed v1.
        assert sorted(assigned) == [2, 3, 4, 5]
        numbers = [row["version"] for row in store.list_versions("abc123")]
        assert sorted(numbers) == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------
# Finding 5: delete reports partial failure
# --------------------------------------------------------------------------


class TestDeletePartialFailure:
    def test_delete_returns_false_when_a_backend_delete_fails(self, tmp_path):
        backend = _FailingDeleteBackend()
        store = _store_with_limit(backend, tmp_path, 50)
        store.create(_make_meta(), _make_envelope(version=1))
        store.add_version(_make_envelope(version=2))

        # Make one of the artifact's files undeletable.
        victim = next(
            info.id
            for info, _ in backend.files.values()
            if tag_for_id("abc123") in info.tags
        )
        backend.fail_ids = {victim}

        assert store.delete("abc123") is False
        # The still-present file keeps the index entry alive (no false success).
        remaining = [
            info for info, _ in backend.files.values() if tag_for_id("abc123") in info.tags
        ]
        assert remaining  # the undeletable file is still there

    def test_delete_returns_true_when_all_deleted(self, tmp_store, backend):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        assert tmp_store.delete("abc123") is True

    def test_delete_unknown_returns_false(self, tmp_store):
        assert tmp_store.delete("nope") is False

    def test_delete_version_returns_false_when_backend_fails(self, tmp_path):
        backend = _FailingDeleteBackend()
        store = _store_with_limit(backend, tmp_path, 50)
        store.create(_make_meta(), _make_envelope(version=1))
        store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))

        victim = next(
            info.id
            for info, _ in backend.files.values()
            if tag_for_version(2) in info.tags
        )
        backend.fail_ids = {victim}
        assert store.delete_version("abc123", 2) is False
        # Index still knows about v2 (its file was not removed).
        assert store.get_version("abc123", 2) is not None


# --------------------------------------------------------------------------
# Finding 6: deterministic single head / verify_single_head
# --------------------------------------------------------------------------


class TestVerifySingleHead:
    def test_true_for_a_normal_artifact(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        assert tmp_store.verify_single_head("abc123") is True
        assert tmp_store.get_head("abc123").version == 2

    def test_highest_live_wins_even_with_a_stale_duplicate_file(self, backend, tmp_path):
        # Two files claim v1 (an interrupted write left an older one); the newest
        # file id wins and the head is still the highest live version.
        store = _store_with_limit(backend, tmp_path, 50)
        store.create(_make_meta(), _make_envelope(version=1, title="v1"))
        store.add_version(_make_envelope(version=2, title="v2"))
        # Inject a second, older-looking v1 file directly.
        backend.upload(
            "artifact-abc123-v1.json",
            _make_envelope(version=1, title="stale").to_json(),
            [TAG_ALL, tag_for_id("abc123"), tag_for_version(1)],
        )
        store.refresh("abc123")
        assert store.verify_single_head("abc123") is True
        assert store.get_head("abc123").version == 2

    def test_pinned_live_head_is_verified(self, tmp_store):
        tmp_store.create(_make_meta(), _make_envelope(version=1))
        tmp_store.add_version(_make_envelope(version=2))
        tmp_store.save_meta(_make_meta(head_mode=HEAD_PINNED, head_version=1))
        assert tmp_store.get_head("abc123").version == 1
        assert tmp_store.verify_single_head("abc123") is True

    def test_false_when_no_live_version(self, tmp_store):
        assert tmp_store.verify_single_head("nope") is False


# --------------------------------------------------------------------------
# Finding 7: refresh picks up cross-replica writes
# --------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_picks_up_a_new_version(self, backend, tmp_path):
        writer = _store_with_limit(backend, tmp_path / "w", 50)
        reader = _store_with_limit(backend, tmp_path / "r", 50)
        writer.create(_make_meta(), _make_envelope(version=1))
        reader.hydrate()
        assert reader.get_head("abc123").version == 1

        # Another replica adds v2; the reader is stale until it refreshes.
        writer.add_version(_make_envelope(version=2, title="second"))
        assert reader.get_head("abc123").version == 1  # still stale (index hit)

        assert reader.refresh("abc123") is True
        head = reader.get_head("abc123")
        assert head is not None and head.version == 2 and head.title == "second"

    def test_get_head_fresh_true_reads_through(self, backend, tmp_path):
        writer = _store_with_limit(backend, tmp_path / "w", 50)
        reader = _store_with_limit(backend, tmp_path / "r", 50)
        writer.create(_make_meta(), _make_envelope(version=1))
        reader.hydrate()
        writer.add_version(_make_envelope(version=2, title="second"))

        stale = reader.get_head("abc123")
        assert stale is not None and stale.version == 1
        fresh = reader.get_head("abc123", fresh=True)
        assert fresh is not None and fresh.version == 2

    def test_refresh_of_deleted_artifact_returns_false(self, backend, tmp_path):
        store = _store_with_limit(backend, tmp_path, 50)
        store.create(_make_meta(), _make_envelope(version=1))
        store.hydrate()
        store.delete("abc123")
        assert store.refresh("abc123") is False


# --------------------------------------------------------------------------
# Finding 8: proposed versions are capped
# --------------------------------------------------------------------------


class TestProposalCap:
    def _capped_store(self, backend, tmp_path, cap):
        return ArtifactStore(
            backend=backend,
            cache_dir=tmp_path / "cache",
            cache_max_entries=50,
            max_versions=50,
            max_proposed_versions=cap,
        )

    def test_oldest_proposals_pruned_over_cap(self, backend, tmp_path):
        store = self._capped_store(backend, tmp_path, 2)
        store.create(_make_meta(), _make_envelope(version=1))  # live head
        for n in range(2, 7):  # v2..v6 proposed
            store.add_version(
                _make_envelope(version=n, status=STATUS_PROPOSED, author_key=OWNER_B)
            )

        rows = {row["version"]: row["status"] for row in store.list_versions("abc123")}
        proposed = [v for v, s in rows.items() if s == STATUS_PROPOSED]
        # Only the newest two proposals survive.
        assert sorted(proposed) == [5, 6]
        # The live head is untouched.
        assert store.get_head("abc123").version == 1

    def test_zero_cap_disables_pruning(self, backend, tmp_path):
        store = self._capped_store(backend, tmp_path, 0)
        store.create(_make_meta(), _make_envelope(version=1))
        for n in range(2, 6):
            store.add_version(_make_envelope(version=n, status=STATUS_PROPOSED))
        proposed = [
            row["version"]
            for row in store.list_versions("abc123")
            if row["status"] == STATUS_PROPOSED
        ]
        assert sorted(proposed) == [2, 3, 4, 5]

    def test_pinned_proposal_is_spared(self, backend, tmp_path):
        store = self._capped_store(backend, tmp_path, 1)
        store.create(_make_meta(), _make_envelope(version=1))
        store.add_version(_make_envelope(version=2, status=STATUS_PROPOSED))
        # Pin v2 (a proposal) as head_version; get_head falls back to live v1
        # but the pin target must survive pruning.
        store.save_meta(_make_meta(head_mode=HEAD_PINNED, head_version=2))
        for n in range(3, 6):
            store.add_version(_make_envelope(version=n, status=STATUS_PROPOSED))
        rows = {row["version"]: row["status"] for row in store.list_versions("abc123")}
        assert 2 in rows  # the pinned proposal was spared
