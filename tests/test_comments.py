"""Tests for src.comments: threads, selectors and the comment store."""

import json

import pytest

from src.comments import (
    MAX_BODY_CHARS,
    MAX_QUOTE_CHARS,
    TAG_CMT_ALL,
    CommentStore,
    CommentThread,
    Reply,
    Selector,
    author_key_of,
    guest_author,
    tag_cmt_artifact,
    tag_cmt_id,
)
from src.kbc import BackendError, InMemoryFilesBackend

ARTIFACT = "abc123"
AUTHOR_A = "1@connection.keboola.com"
AUTHOR_B = "2@connection.keboola.com"


def _identity(project_id: int = 1, key: str = AUTHOR_A) -> dict:
    return {
        "stack_url": "https://connection.keboola.com",
        "project_id": project_id,
        "project_name": "Proj",
        "key": key,
    }


def _make_thread(
    thread_id: str = "t1",
    artifact_id: str = ARTIFACT,
    author_key: str = AUTHOR_A,
    created_at: str = "2026-01-01T00:00:00+00:00",
    **overrides,
) -> CommentThread:
    defaults = dict(
        id=thread_id,
        artifact_id=artifact_id,
        version=1,
        selector=Selector(exact="the quoted bit", prefix="before ", suffix=" after"),
        body="Is this still true?",
        author=_identity(key=author_key),
        created_at=created_at,
    )
    defaults.update(overrides)
    return CommentThread(**defaults)


@pytest.fixture
def cmt_store(backend: InMemoryFilesBackend, tmp_path, settings) -> CommentStore:
    """A CommentStore over the shared in-memory backend and a tmp disk cache."""
    return CommentStore(
        backend=backend,
        cache_dir=tmp_path / "cache",
        cache_max_entries=settings.cache_max_entries,
    )


def _cold_store(backend, tmp_path, settings) -> CommentStore:
    """A second store over the same backend/cache — no warm in-process state."""
    return CommentStore(
        backend=backend,
        cache_dir=tmp_path / "cache",
        cache_max_entries=settings.cache_max_entries,
    )


def _files_tagged(backend, tag: str) -> list:
    return [info for info, _ in backend.files.values() if tag in info.tags]


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


class TestSerialization:
    def test_round_trip(self):
        thread = _make_thread(
            resolved=True,
            resolved_by=_identity(key=AUTHOR_B),
            replies=[
                Reply(
                    author=_identity(key=AUTHOR_B),
                    body="Fixed in v2.",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            ],
        )
        assert CommentThread.from_json(thread.to_json()) == thread

    def test_selector_round_trip(self):
        selector = Selector(exact="x", prefix="p", suffix="s")
        assert Selector.from_dict(selector.to_dict()) == selector

    def test_selector_defaults_and_garbage(self):
        assert Selector.from_dict(None) == Selector(exact="")
        assert Selector.from_dict({"exact": "only"}) == Selector(exact="only")

    def test_unknown_keys_are_tolerated(self):
        payload = json.loads(_make_thread().to_json())
        payload["some_future_field"] = {"nested": 1}
        restored = CommentThread.from_json(json.dumps(payload).encode("utf-8"))
        assert restored.id == "t1"
        assert restored.body == "Is this still true?"

    def test_missing_optional_fields_fall_back_to_defaults(self):
        raw = json.dumps({"id": "t9", "artifact_id": ARTIFACT}).encode("utf-8")
        restored = CommentThread.from_json(raw)
        assert restored.version == 1
        assert restored.selector == Selector(exact="")
        assert restored.body == ""
        assert restored.author == {}
        assert restored.resolved is False
        assert restored.resolved_by is None
        assert restored.replies == []

    def test_garbage_replies_and_version_are_tolerated(self):
        raw = json.dumps(
            {
                "id": "t9",
                "artifact_id": ARTIFACT,
                "version": "not a number",
                "replies": "not a list",
                "resolved_by": "not a dict",
            }
        ).encode("utf-8")
        restored = CommentThread.from_json(raw)
        assert restored.version == 1
        assert restored.replies == []
        assert restored.resolved_by is None

    def test_reply_from_garbage(self):
        assert Reply.from_dict(None) == Reply(author={}, body="", created_at="")

    def test_not_json_raises_value_error(self):
        with pytest.raises(ValueError):
            CommentThread.from_json(b"nope")

    def test_json_array_raises_value_error(self):
        with pytest.raises(ValueError):
            CommentThread.from_json(b"[1, 2]")

    def test_missing_ids_raise_value_error(self):
        with pytest.raises(ValueError):
            CommentThread.from_json(json.dumps({"artifact_id": ARTIFACT}).encode())
        with pytest.raises(ValueError):
            CommentThread.from_json(json.dumps({"id": "t1"}).encode())


class TestPublicDict:
    def test_hides_stack_url_and_owner_key(self):
        thread = _make_thread(
            resolved=True,
            resolved_by=_identity(key=AUTHOR_B),
            replies=[
                Reply(
                    author=_identity(project_id=2, key=AUTHOR_B),
                    body="Agreed.",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            ],
        )
        public = thread.public_dict()
        blob = json.dumps(public)

        assert "stack_url" not in blob
        assert "https://connection.keboola.com" not in blob
        assert AUTHOR_A not in blob
        assert AUTHOR_B not in blob
        assert "key" not in public["author"]

    def test_exposes_the_expected_shape(self):
        thread = _make_thread()
        public = thread.public_dict()
        assert public["id"] == "t1"
        assert public["artifact_id"] == ARTIFACT
        assert public["version"] == 1
        assert public["selector"] == {
            "exact": "the quoted bit",
            "prefix": "before ",
            "suffix": " after",
        }
        assert public["body"] == "Is this still true?"
        assert public["author"] == {
            "project_id": 1,
            "project_name": "Proj",
            "stack_host": "connection.keboola.com",
        }
        assert public["resolved"] is False
        assert public["resolved_by"] is None
        assert public["replies"] == []

    def test_guest_authors_are_reduced_to_kind_and_name(self):
        """A guest has no project — and their invitation id stays internal."""
        thread = _make_thread(
            author=guest_author("inv-secret-id", "Jana"),
            replies=[
                Reply(
                    author=guest_author("inv-secret-id", "Jana"),
                    body="Following up.",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            ],
        )
        public = thread.public_dict()
        assert public["author"] == {"kind": "guest", "name": "Jana"}
        assert public["replies"][0]["author"] == {"kind": "guest", "name": "Jana"}
        assert "inv-secret-id" not in json.dumps(public)

    def test_reply_identities_are_summarized_too(self):
        thread = _make_thread(
            replies=[
                Reply(
                    author=_identity(project_id=7, key=AUTHOR_B),
                    body="Agreed.",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            ]
        )
        reply = thread.public_dict()["replies"][0]
        assert reply["author"] == {
            "project_id": 7,
            "project_name": "Proj",
            "stack_host": "connection.keboola.com",
        }
        assert reply["body"] == "Agreed."


class TestAuthorKey:
    """Project keys and guest keys share one namespace and must not collide."""

    def test_project_author_keeps_its_owner_key(self):
        assert author_key_of(_identity(key=AUTHOR_A)) == AUTHOR_A
        assert _make_thread().author_key == AUTHOR_A

    def test_guest_author_is_prefixed_with_its_invitation(self):
        author = guest_author("inv1", "Jana")
        assert author == {"kind": "guest", "name": "Jana", "invitation_id": "inv1"}
        assert author_key_of(author) == "guest:inv1"
        assert _make_thread(author=author).author_key == "guest:inv1"

    def test_a_guest_key_can_never_look_like_a_project_key(self):
        """The prefix is what keeps one namespace from answering for the other."""
        assert author_key_of(guest_author(AUTHOR_A, "impostor")) != AUTHOR_A

    def test_unusable_authors_have_no_key(self):
        assert author_key_of(None) == ""
        assert author_key_of({}) == ""
        assert author_key_of("not a dict") == ""
        # A guest record with no invitation id could never be matched to one.
        assert author_key_of({"kind": "guest", "name": "Jana"}) == ""

    def test_guest_authors_survive_a_json_round_trip(self):
        thread = _make_thread(
            author=guest_author("inv1", "Jana"),
            replies=[
                Reply(
                    author=guest_author("inv2", "Petr"),
                    body="Agreed.",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            ],
        )
        restored = CommentThread.from_json(thread.to_json())
        assert restored.author_key == "guest:inv1"
        assert restored.replies[0].author_key == "guest:inv2"


# --------------------------------------------------------------------------
# create / get / list
# --------------------------------------------------------------------------


class TestCreateAndRead:
    def test_create_then_get(self, cmt_store, backend):
        thread = _make_thread()
        cmt_store.create(thread)

        loaded = cmt_store.get(ARTIFACT, "t1")
        assert loaded is not None
        assert loaded.body == thread.body
        assert loaded.selector == thread.selector

    def test_file_name_and_tags(self, cmt_store, backend):
        cmt_store.create(_make_thread())
        info = next(info for info, _ in backend.files.values())
        assert info.name == f"comment-{ARTIFACT}-t1.json"
        assert set(info.tags) == {
            TAG_CMT_ALL,
            tag_cmt_artifact(ARTIFACT),
            tag_cmt_id("t1"),
        }

    def test_get_unknown_thread_is_none(self, cmt_store):
        assert cmt_store.get(ARTIFACT, "nope") is None

    def test_get_falls_back_to_tag_search_on_index_miss(
        self, backend, tmp_path, settings
    ):
        writer = _cold_store(backend, tmp_path, settings)
        writer.create(_make_thread())

        # A second replica that never hydrated must still find the thread.
        reader = CommentStore(
            backend=backend,
            cache_dir=tmp_path / "other-cache",
            cache_max_entries=settings.cache_max_entries,
        )
        assert reader.count() == 0
        found = reader.get(ARTIFACT, "t1")
        assert found is not None and found.id == "t1"
        # ...and it is indexed afterwards.
        assert reader.count() == 1

    def test_get_rejects_a_thread_of_another_artifact(self, cmt_store):
        cmt_store.create(_make_thread(artifact_id="other"))
        assert cmt_store.get(ARTIFACT, "t1") is None

    def test_list_for_is_oldest_first(self, cmt_store):
        cmt_store.create(_make_thread("t2", created_at="2026-03-01T00:00:00+00:00"))
        cmt_store.create(_make_thread("t1", created_at="2026-01-01T00:00:00+00:00"))
        cmt_store.create(_make_thread("t3", created_at="2026-02-01T00:00:00+00:00"))

        assert [t.id for t in cmt_store.list_for(ARTIFACT)] == ["t1", "t3", "t2"]

    def test_list_for_ignores_other_artifacts(self, cmt_store):
        cmt_store.create(_make_thread("t1"))
        cmt_store.create(_make_thread("t2", artifact_id="zzz"))
        assert [t.id for t in cmt_store.list_for(ARTIFACT)] == ["t1"]

    def test_list_for_unknown_artifact_is_empty(self, cmt_store):
        assert cmt_store.list_for("nothing-here") == []

    def test_list_for_skips_a_corrupt_file(self, cmt_store, backend):
        cmt_store.create(_make_thread("t1"))
        backend.upload(
            f"comment-{ARTIFACT}-t2.json",
            b"{ not json",
            [TAG_CMT_ALL, tag_cmt_artifact(ARTIFACT), tag_cmt_id("t2")],
        )
        assert [t.id for t in cmt_store.list_for(ARTIFACT)] == ["t1"]

    def test_count_open_for(self, cmt_store):
        cmt_store.create(_make_thread("t1"))
        cmt_store.create(_make_thread("t2"))
        cmt_store.create(_make_thread("t3", resolved=True))
        assert cmt_store.count_open_for(ARTIFACT) == 2
        assert cmt_store.count_open_for("nothing-here") == 0


# --------------------------------------------------------------------------
# update: reply + resolve
# --------------------------------------------------------------------------


class TestUpdate:
    def test_reply_retires_the_older_file(self, cmt_store, backend):
        thread = _make_thread()
        cmt_store.create(thread)

        thread.replies.append(
            Reply(
                author=_identity(project_id=2, key=AUTHOR_B),
                body="Still true.",
                created_at="2026-01-02T00:00:00+00:00",
            )
        )
        cmt_store.update(thread)

        assert len(_files_tagged(backend, tag_cmt_id("t1"))) == 1
        loaded = cmt_store.get(ARTIFACT, "t1")
        assert loaded is not None
        assert [r.body for r in loaded.replies] == ["Still true."]

    def test_resolve_round_trips(self, cmt_store, backend):
        thread = _make_thread()
        cmt_store.create(thread)

        thread.resolved = True
        thread.resolved_by = _identity(key=AUTHOR_B)
        cmt_store.update(thread)

        loaded = cmt_store.get(ARTIFACT, "t1")
        assert loaded is not None
        assert loaded.resolved is True
        assert loaded.resolved_by == _identity(key=AUTHOR_B)
        assert len(_files_tagged(backend, tag_cmt_id("t1"))) == 1

    def test_update_is_visible_to_a_cold_reader(
        self, cmt_store, backend, tmp_path, settings
    ):
        thread = _make_thread()
        cmt_store.create(thread)
        thread.resolved = True
        cmt_store.update(thread)

        cold = CommentStore(
            backend=backend,
            cache_dir=tmp_path / "cold",
            cache_max_entries=settings.cache_max_entries,
        )
        cold.hydrate()
        loaded = cold.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.resolved is True


# --------------------------------------------------------------------------
# hydrate
# --------------------------------------------------------------------------


class TestHydrate:
    def test_hydrate_from_a_cold_store(self, backend, tmp_path, settings):
        writer = _cold_store(backend, tmp_path, settings)
        writer.create(_make_thread("t1"))
        writer.create(_make_thread("t2"))
        writer.create(_make_thread("t3", artifact_id="other"))

        cold = CommentStore(
            backend=backend,
            cache_dir=tmp_path / "cold",
            cache_max_entries=settings.cache_max_entries,
        )
        assert cold.hydrate() == 3
        assert cold.count() == 3
        assert [t.id for t in cold.list_for(ARTIFACT)] == ["t1", "t2"]

    def test_hydrate_prefers_the_newest_file_of_a_thread(
        self, backend, tmp_path, settings
    ):
        stale = _make_thread(body="stale")
        fresh = _make_thread(body="fresh")
        tags = [TAG_CMT_ALL, tag_cmt_artifact(ARTIFACT), tag_cmt_id("t1")]
        backend.upload("comment-abc123-t1.json", stale.to_json(), tags)
        backend.upload("comment-abc123-t1.json", fresh.to_json(), tags)

        cold = _cold_store(backend, tmp_path, settings)
        assert cold.hydrate() == 1
        loaded = cold.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.body == "fresh"

    def test_hydrate_skips_files_without_tags(self, backend, tmp_path, settings):
        backend.upload("comment-orphan.json", _make_thread().to_json(), [TAG_CMT_ALL])
        cold = _cold_store(backend, tmp_path, settings)
        assert cold.hydrate() == 0

    def test_hydrate_ignores_artifact_files(self, backend, tmp_path, settings, tmp_store):
        """Artifact and comment tags are disjoint namespaces."""
        from src.store import ArtifactMeta, Envelope

        tmp_store.create(
            ArtifactMeta(id=ARTIFACT, owner=_identity()),
            Envelope(
                id=ARTIFACT,
                version=1,
                title="T",
                html="<p>hi</p>",
                source_type="html",
                author=_identity(),
            ),
        )
        cold = _cold_store(backend, tmp_path, settings)
        assert cold.hydrate() == 0


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


class TestDelete:
    def test_delete_one_thread(self, cmt_store, backend):
        cmt_store.create(_make_thread("t1"))
        cmt_store.create(_make_thread("t2"))

        assert cmt_store.delete(ARTIFACT, "t1") is True
        assert cmt_store.get(ARTIFACT, "t1") is None
        assert [t.id for t in cmt_store.list_for(ARTIFACT)] == ["t2"]
        assert _files_tagged(backend, tag_cmt_id("t1")) == []

    def test_delete_unknown_thread_is_false(self, cmt_store):
        assert cmt_store.delete(ARTIFACT, "nope") is False

    def test_delete_refuses_a_thread_of_another_artifact(self, cmt_store, backend):
        cmt_store.create(_make_thread("t1", artifact_id="other"))
        assert cmt_store.delete(ARTIFACT, "t1") is False
        assert len(_files_tagged(backend, tag_cmt_id("t1"))) == 1

    def test_delete_all_for(self, cmt_store, backend):
        cmt_store.create(_make_thread("t1"))
        cmt_store.create(_make_thread("t2"))
        cmt_store.create(_make_thread("t3", artifact_id="other"))

        assert cmt_store.delete_all_for(ARTIFACT) == 2
        assert cmt_store.list_for(ARTIFACT) == []
        # Only the other artifact's thread is left indexed...
        assert cmt_store.count() == 1
        # ...and its file is untouched.
        assert len(_files_tagged(backend, tag_cmt_artifact("other"))) == 1

    def test_delete_all_for_unknown_artifact_is_zero(self, cmt_store):
        assert cmt_store.delete_all_for("nothing-here") == 0

    def test_delete_all_for_purges_the_disk_cache(self, cmt_store, tmp_path):
        cmt_store.create(_make_thread("t1"))
        cache_dir = tmp_path / "cache"
        assert list(cache_dir.glob("cmt.*.json"))
        cmt_store.delete_all_for(ARTIFACT)
        assert list(cache_dir.glob("cmt.*.json")) == []


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------


class TestCaching:
    def test_cache_files_are_namespaced_away_from_artifact_caches(
        self, cmt_store, tmp_path
    ):
        cmt_store.create(_make_thread("t1"))
        names = [p.name for p in (tmp_path / "cache").glob("*.json")]
        assert names and all(name.startswith("cmt.") for name in names)
        # ArtifactStore._purge_disk_cache globs "{artifact_id}-*.json"; the dot
        # is not a legal artifact-ID character, so those globs cannot match.
        assert not list((tmp_path / "cache").glob(f"{ARTIFACT}-*.json"))

    def test_disk_cache_serves_a_cold_store_without_downloading(
        self, cmt_store, backend, tmp_path, settings
    ):
        cmt_store.create(_make_thread("t1"))

        cold = _cold_store(backend, tmp_path, settings)

        def _boom(file_id: int) -> bytes:
            raise AssertionError("download must be served from the disk cache")

        backend.download = _boom  # type: ignore[method-assign]
        loaded = cold.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.body == "Is this still true?"

    def test_corrupt_disk_cache_is_dropped_and_refetched(
        self, cmt_store, backend, tmp_path, settings
    ):
        cmt_store.create(_make_thread("t1"))
        cache_dir = tmp_path / "cache"
        cached = next(iter(cache_dir.glob("cmt.*.json")))
        cached.write_bytes(b"{ truncated")

        cold = _cold_store(backend, tmp_path, settings)
        loaded = cold.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.body == "Is this still true?"
        # The bad entry was dropped and rewritten from Storage.
        assert json.loads(cached.read_bytes())["id"] == "t1"

    def test_zero_entry_memory_cache_still_works(self, backend, tmp_path):
        store = CommentStore(
            backend=backend, cache_dir=tmp_path / "cache", cache_max_entries=0
        )
        store.create(_make_thread("t1"))
        loaded = store.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.id == "t1"


# --------------------------------------------------------------------------
# validation and error propagation
# --------------------------------------------------------------------------


class TestValidation:
    def test_empty_body_is_rejected(self, cmt_store):
        with pytest.raises(ValueError, match="must not be empty"):
            cmt_store.create(_make_thread(body="   "))

    def test_oversized_body_is_rejected(self, cmt_store):
        with pytest.raises(ValueError, match="too long"):
            cmt_store.create(_make_thread(body="x" * (MAX_BODY_CHARS + 1)))

    def test_empty_quote_is_rejected(self, cmt_store):
        with pytest.raises(ValueError, match="quoted selection"):
            cmt_store.create(_make_thread(selector=Selector(exact="")))

    def test_oversized_quote_is_rejected(self, cmt_store):
        with pytest.raises(ValueError, match="quoted selection is too long"):
            cmt_store.create(
                _make_thread(selector=Selector(exact="q" * (MAX_QUOTE_CHARS + 1)))
            )

    def test_empty_reply_body_is_rejected_on_update(self, cmt_store):
        thread = _make_thread()
        cmt_store.create(thread)
        thread.replies.append(Reply(author=_identity(), body="", created_at="now"))
        with pytest.raises(ValueError, match="Reply body must not be empty"):
            cmt_store.update(thread)

    def test_a_rejected_create_writes_nothing(self, cmt_store, backend):
        with pytest.raises(ValueError):
            cmt_store.create(_make_thread(body=""))
        assert backend.files == {}


class TestBackendErrors:
    def test_list_for_propagates_backend_errors(self, cmt_store, backend):
        def _boom(tag: str):
            raise BackendError("Storage is down")

        backend.search_by_tag = _boom  # type: ignore[method-assign]
        with pytest.raises(BackendError):
            cmt_store.list_for(ARTIFACT)

    def test_get_propagates_a_download_failure(self, cmt_store, backend, tmp_path):
        cmt_store.create(_make_thread("t1"))
        # Drop both caches so the read has to hit the backend.
        for path in (tmp_path / "cache").glob("cmt.*.json"):
            path.unlink()
        cold = CommentStore(
            backend=backend, cache_dir=tmp_path / "cache", cache_max_entries=10
        )

        def _boom(file_id: int) -> bytes:
            raise BackendError("Storage is down")

        backend.download = _boom  # type: ignore[method-assign]
        with pytest.raises(BackendError):
            cold.get(ARTIFACT, "t1")

    def test_update_survives_a_failing_cleanup_search(self, cmt_store, backend):
        thread = _make_thread("t1")
        cmt_store.create(thread)

        real_search = backend.search_by_tag

        def _flaky(tag: str):
            if tag == tag_cmt_id("t1"):
                raise BackendError("Storage is down")
            return real_search(tag)

        backend.search_by_tag = _flaky  # type: ignore[method-assign]
        thread.resolved = True
        cmt_store.update(thread)  # best effort — must not raise

        loaded = cmt_store.get(ARTIFACT, "t1")
        assert loaded is not None and loaded.resolved is True
