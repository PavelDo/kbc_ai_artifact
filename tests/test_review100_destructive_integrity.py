"""Destructive-integrity regressions from the v0.10.0 review.

Three findings, one theme: a destructive operation must never leave state that
nothing can reach, authorize or repair.

- ``REL-100-001`` -- :meth:`src.store.ArtifactStore.delete` iterated the
  artifact's Storage files in whatever order the tag search returned, so a
  version-delete failure could happen *after* the meta record (the file
  ``_owner_only`` authorizes against) was already gone. The route answered 502
  "retry", but the retry could no longer find the artifact and 404ed, leaving
  an orphan version file in Storage forever.
- ``COR-075-006`` -- deleting the version the head is pinned to left the stored
  head pointing at a version that no longer exists, while serving silently fell
  back to the newest live one. Stored state and served state disagreed.
- ``REL-075-009`` -- documented residual: a process death between the
  caller-project canonical upload and the hub version write orphans a file the
  hub can never delete again. The test here only pins the documentation, which
  is the whole remediation.

The probe these mirror is ``_review/v0.10.0/runtime/test_lock_registry_probe.py``.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from datetime import datetime, timedelta, timezone

import src.main as main
from src.kbc import BackendError
from src.store import (
    TAG_ALL,
    TAG_META,
    ArtifactStore,
    tag_for_id,
    tag_for_owner,
)
from tests.test_api import (
    AUTH_HEADERS,
    Api,
    _publish_markdown,
    _submit_version,
    api,  # noqa: F401 - the fixture is used by name
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cold_store(api: Api, tmp_path, name: str) -> ArtifactStore:
    """A second store over the same backend: a container restart, empty disk."""
    store = ArtifactStore(
        backend=api.backend,
        cache_dir=tmp_path / name,
        cache_max_entries=api.settings.cache_max_entries,
        max_versions=api.settings.max_versions,
    )
    store.hydrate()
    return store


def _files_of(api: Api, artifact_id: str):
    return api.backend.search_by_tag(tag_for_id(artifact_id))


def _break_delete_of(api: Api, monkeypatch, file_id: int):
    """Make exactly one file id undeletable; return the restore callable."""
    real_delete = api.backend.delete

    def failing_delete(target_id: int) -> None:
        if target_id == file_id:
            raise BackendError("simulated Storage delete failure")
        real_delete(target_id)

    monkeypatch.setattr(api.backend, "delete", failing_delete)
    return lambda: monkeypatch.setattr(api.backend, "delete", real_delete)


# --------------------------------------------------------------------------
# REL-100-001 -- purge must keep the authorizing meta until every child is gone
# --------------------------------------------------------------------------


class TestPurgeDeletesTheAuthorizingMetaLast:
    def test_a_failed_version_delete_keeps_the_meta_and_the_retry_finishes(
        self, api: Api, monkeypatch, tmp_path
    ) -> None:
        """Port of ``test_partial_artifact_purge_can_delete_authorizing_meta_before_failure``.

        The probe recorded ``partial_artifact_purge=502``, a surviving version
        file, ``fresh_meta_present=False`` and ``retry_after_restart=404``. The
        first three lines below are the same observation; the last two are the
        fix: the meta outlives the failure, so a cold store still finds the
        artifact and the retry completes the purge.
        """
        artifact_id = _publish_markdown(api, "# Must be erasable")
        files = _files_of(api, artifact_id)
        version_file = next(info for info in files if info.name.endswith("-v1.json"))
        meta_file = next(info for info in files if TAG_META in info.tags)

        restore = _break_delete_of(api, monkeypatch, version_file.id)
        first = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        restore()

        assert first.status_code == 502, first.text
        remaining = {info.id for info in _files_of(api, artifact_id)}
        assert version_file.id in remaining, "the failing version file must stay"
        assert meta_file.id in remaining, (
            "the authorizing meta was deleted before a surviving child; the "
            "purge is no longer retryable"
        )

        # A restart with an empty disk must still see the artifact -- otherwise
        # the retry the 502 asked for cannot authenticate.
        fresh = _cold_store(api, tmp_path, "cold-after-partial-purge")
        assert fresh.get_meta(artifact_id) is not None
        monkeypatch.setattr(main.app.state, "store", fresh)

        retry = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        assert retry.status_code == 200, retry.text
        assert _files_of(api, artifact_id) == [], "the retry left files behind"

    def test_the_meta_is_the_last_file_the_store_deletes(
        self, api: Api, monkeypatch
    ) -> None:
        """Ordering, asserted directly rather than inferred from a failure."""
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        meta_file = next(
            info for info in _files_of(api, artifact_id) if TAG_META in info.tags
        )

        order: list[int] = []
        real_delete = api.backend.delete

        def recording_delete(file_id: int) -> None:
            order.append(file_id)
            real_delete(file_id)

        monkeypatch.setattr(api.backend, "delete", recording_delete)
        assert main.app.state.store.delete(artifact_id) is True

        assert len(order) >= 3, order
        assert order[-1] == meta_file.id, (
            f"meta file {meta_file.id} was not deleted last: {order}"
        )

    def test_a_failed_child_delete_leaves_the_meta_untouched(
        self, api: Api, monkeypatch
    ) -> None:
        """The invariant, stated as an invariant: no orphan without its meta."""
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        v2_file = next(
            info for info in _files_of(api, artifact_id) if info.name.endswith("-v2.json")
        )
        meta_file = next(
            info for info in _files_of(api, artifact_id) if TAG_META in info.tags
        )

        restore = _break_delete_of(api, monkeypatch, v2_file.id)
        assert main.app.state.store.delete(artifact_id) is False
        restore()

        remaining = {info.id for info in _files_of(api, artifact_id)}
        assert v2_file.id in remaining
        assert meta_file.id in remaining

    def test_a_failed_meta_delete_still_leaves_a_finishable_purge(
        self, api: Api, monkeypatch, tmp_path
    ) -> None:
        """The other half of the ordering: the last step failing is survivable.

        Every child is gone, so nothing is served any more, and the record the
        owner authorizes with is the one thing left -- which is what lets the
        retry (here, from a cold store) finish the job.
        """
        artifact_id = _publish_markdown(api, "# One")
        meta_file = next(
            info for info in _files_of(api, artifact_id) if TAG_META in info.tags
        )

        restore = _break_delete_of(api, monkeypatch, meta_file.id)
        first = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        restore()

        assert first.status_code == 502, first.text
        assert [info.id for info in _files_of(api, artifact_id)] == [meta_file.id]
        assert api.client.get(f"/a/{artifact_id}").status_code == 404

        fresh = _cold_store(api, tmp_path, "cold-after-meta-failure")
        assert fresh.get_meta(artifact_id) is not None
        monkeypatch.setattr(main.app.state, "store", fresh)
        retry = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        assert retry.status_code == 200, retry.text
        assert _files_of(api, artifact_id) == []

    def test_a_legacy_envelope_is_deleted_before_the_meta(
        self, api: Api, monkeypatch, tmp_path
    ) -> None:
        """A schema-1 record carries neither the meta nor a version tag.

        It is still a child: it holds content, and the meta is what authorizes
        removing it, so it must go first like any version file.
        """
        artifact_id = _publish_markdown(api, "# One")
        meta = main.app.state.store.get_meta(artifact_id)
        legacy_id = api.backend.upload(
            f"artifact-{artifact_id}.json",
            b'{"id": "%s", "html": "<p>legacy</p>"}' % artifact_id.encode("ascii"),
            [TAG_ALL, tag_for_id(artifact_id), tag_for_owner(meta.owner_key)],
        )
        meta_file = next(
            info for info in _files_of(api, artifact_id) if TAG_META in info.tags
        )

        restore = _break_delete_of(api, monkeypatch, legacy_id)
        assert main.app.state.store.delete(artifact_id) is False
        restore()

        remaining = {info.id for info in _files_of(api, artifact_id)}
        assert legacy_id in remaining
        assert meta_file.id in remaining, "the legacy record outlived its meta"


class TestHydrateToleratesAMetaWithoutVersions:
    """The shape a purge leaves if the process dies just before the meta delete.

    Decision (documented in ``ArtifactStore._reap_aborted_publishes``): the
    record is *kept* while it is young, so the owner can authenticate and
    finish the purge, and reaped by the existing age gate once it is too old to
    be an operation still in flight.
    """

    @staticmethod
    def _age_meta(api: Api, artifact_id: str, seconds: int) -> int:
        meta_file = next(
            info for info in _files_of(api, artifact_id) if TAG_META in info.tags
        )
        info, content = api.backend.files[meta_file.id]
        created = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        api.backend.files[meta_file.id] = (
            dataclasses.replace(info, created=created),
            content,
        )
        return meta_file.id

    @staticmethod
    def _strip_versions(api: Api, artifact_id: str) -> None:
        for info in _files_of(api, artifact_id):
            if TAG_META not in info.tags:
                api.backend.delete(info.id)

    def _store(self, api: Api, tmp_path, name: str, reap_after: int) -> ArtifactStore:
        return ArtifactStore(
            backend=api.backend,
            cache_dir=tmp_path / name,
            cache_max_entries=api.settings.cache_max_entries,
            max_versions=api.settings.max_versions,
            reap_aborted_after_s=reap_after,
        )

    def test_a_young_meta_without_versions_survives_hydrate_and_is_purgeable(
        self, api: Api, monkeypatch, tmp_path
    ) -> None:
        artifact_id = _publish_markdown(api, "# Half purged")
        self._strip_versions(api, artifact_id)
        meta_id = self._age_meta(api, artifact_id, seconds=5)

        fresh = self._store(api, tmp_path, "young", reap_after=3600)
        fresh.hydrate()
        assert meta_id in api.backend.files
        assert fresh.get_meta(artifact_id) is not None
        # Nothing is served -- there is no version -- but the owner can still
        # authorize, which is the whole point of keeping the record.
        assert fresh.get_head(artifact_id) is None

        monkeypatch.setattr(main.app.state, "store", fresh)
        assert api.client.get(f"/a/{artifact_id}").status_code == 404
        finish = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        assert finish.status_code == 200, finish.text
        assert _files_of(api, artifact_id) == []

    def test_an_old_meta_without_versions_is_reaped_by_hydrate(
        self, api: Api, tmp_path
    ) -> None:
        artifact_id = _publish_markdown(api, "# Abandoned")
        self._strip_versions(api, artifact_id)
        meta_id = self._age_meta(api, artifact_id, seconds=7200)

        fresh = self._store(api, tmp_path, "old", reap_after=3600)
        fresh.hydrate()
        assert meta_id not in api.backend.files
        assert fresh.get_meta(artifact_id) is None


# --------------------------------------------------------------------------
# COR-075-006 -- a successful delete must never leave the head pinned to a ghost
# --------------------------------------------------------------------------


def _pin(api: Api, artifact_id: str, version: int):
    return api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": version},
        headers=AUTH_HEADERS,
    )


def _assert_head_is_consistent(api: Api, artifact_id: str) -> None:
    """Stored meta, ``get_head`` and the public page must agree.

    The v0.10.0 probe found all three disagreeing: meta said "pinned to v2",
    ``get_head`` served v1 and the page rendered v1 without a word about it.
    """
    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    head = store.get_head(artifact_id)
    assert head is not None
    live = {
        row["version"]
        for row in store.list_versions(artifact_id)
        if row.get("status") == "live"
    }
    if meta.head_mode == "pinned":
        assert meta.head_version in live, (
            f"head pinned to {meta.head_version}, which is not a live version "
            f"({sorted(live)})"
        )
        assert head.version == meta.head_version
    else:
        assert head.version == max(live)
    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 200
    assert f"/a/{artifact_id}/v/{head.version}" in page.text or head.title in page.text


class TestDeletingThePinnedVersion:
    def test_sequential_delete_of_the_pinned_version_is_refused(
        self, api: Api
    ) -> None:
        """Port of ``test_sequential_delete_leaves_dangling_pinned_head``.

        The probe recorded ``delete_pinned_version=200 head_mode=pinned
        stored_head_version=2 served_head_version=1``. The delete is now a 409
        that tells the owner how to proceed, and nothing is removed.
        """
        artifact_id = _publish_markdown(api, "# Version one")
        assert _submit_version(api, artifact_id, "# Version two").status_code == 201
        assert _pin(api, artifact_id, 2).status_code == 200

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 409, deleted.text
        body = deleted.json()
        assert body["error"] == "cannot delete the pinned head version"
        assert body["head_version"] == 2
        detail = body["detail"].lower()
        assert "pin" in detail and "latest" in detail

        meta = main.app.state.store.get_meta(artifact_id)
        assert (meta.head_mode, meta.head_version) == ("pinned", 2)
        assert api.client.get(f"/a/{artifact_id}/v/2").status_code == 200
        _assert_head_is_consistent(api, artifact_id)

    def test_repinning_then_deleting_works(self, api: Api) -> None:
        """The 409's own advice must actually unblock the owner."""
        artifact_id = _publish_markdown(api, "# Version one")
        assert _submit_version(api, artifact_id, "# Version two").status_code == 201
        assert _pin(api, artifact_id, 2).status_code == 200
        assert _pin(api, artifact_id, 1).status_code == 200

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["head_version"] == 1
        _assert_head_is_consistent(api, artifact_id)

    def test_switching_the_head_to_latest_then_deleting_works(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# Version one")
        assert _submit_version(api, artifact_id, "# Version two").status_code == 201
        assert _pin(api, artifact_id, 2).status_code == 200
        assert (
            api.client.put(
                f"/api/artifacts/{artifact_id}/head",
                json={"mode": "latest"},
                headers=AUTH_HEADERS,
            ).status_code
            == 200
        )

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 200, deleted.text
        meta = main.app.state.store.get_meta(artifact_id)
        assert meta.head_mode == "latest"
        _assert_head_is_consistent(api, artifact_id)

    def test_deleting_a_non_pinned_version_while_pinned_still_works(
        self, api: Api
    ) -> None:
        artifact_id = _publish_markdown(api, "# Version one")
        assert _submit_version(api, artifact_id, "# Version two").status_code == 201
        assert _submit_version(api, artifact_id, "# Version three").status_code == 201
        assert _pin(api, artifact_id, 3).status_code == 200

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 200, deleted.text
        meta = main.app.state.store.get_meta(artifact_id)
        assert (meta.head_mode, meta.head_version) == ("pinned", 3)
        assert deleted.json()["head_version"] == 3
        _assert_head_is_consistent(api, artifact_id)

    def test_deleting_with_head_mode_latest_still_works(self, api: Api) -> None:
        artifact_id = _publish_markdown(api, "# Version one")
        assert _submit_version(api, artifact_id, "# Version two").status_code == 201

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 200, deleted.text
        meta = main.app.state.store.get_meta(artifact_id)
        assert meta.head_mode == "latest"
        assert deleted.json()["head_version"] == 1
        _assert_head_is_consistent(api, artifact_id)

    def test_a_contributor_cannot_withdraw_a_pinned_proposal_either(
        self, api: Api, monkeypatch
    ) -> None:
        """The guard is on the stored pin, not on who is asking.

        A proposal cannot normally be pinned, so this reaches the state the
        only way it can be reached: by writing the meta record directly, as a
        rolled-back promotion or an older build could have left it.
        """
        artifact_id = _publish_markdown(api, "# One", accept_versions=True)
        assert (
            _submit_version(api, artifact_id, "# Two", headers=AUTH_HEADERS).status_code
            == 201
        )
        store = main.app.state.store
        meta = store.get_meta(artifact_id)
        store.save_meta(dataclasses.replace(meta, head_mode="pinned", head_version=2))

        deleted = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/2", headers=AUTH_HEADERS
        )
        assert deleted.status_code == 409, deleted.text
        assert deleted.json()["error"] == "cannot delete the pinned head version"

    def test_the_openapi_description_documents_the_refusal(self, api: Api) -> None:
        schema = api.client.get("/openapi.json").json()
        operation = schema["paths"]["/api/artifacts/{artifact_id}/versions/{version}"][
            "delete"
        ]
        assert "pinned" in operation["description"].lower()
        assert "pinned" in operation["responses"]["409"]["description"].lower()


# --------------------------------------------------------------------------
# REL-075-009 -- accepted residual, pinned by its documentation
# --------------------------------------------------------------------------


class TestCanonicalCrashOrphanIsDocumented:
    """The remediation is documentation, so the documentation is the test.

    ``canonical_file_id`` is written into the version envelope, and version
    files are immutable (CLAUDE.md, "Storage model"), so the external copy
    cannot be written after the hub record without a second write to a file
    that must never be rewritten. The window therefore stays open and is
    described instead -- these assertions keep the description from silently
    disappearing in a later refactor.
    """

    def test_claude_md_lists_the_residual_with_the_tag_to_search_for(self) -> None:
        text = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "## Known residual risks" in text
        section = text.split("## Known residual risks", 1)[1]
        assert "REL-075-009" in section
        # An operator needs the tag to find the orphan by hand.
        assert "kbc-artifact" in section

    def test_the_upload_site_explains_the_window(self) -> None:
        source = (_REPO_ROOT / "src" / "main.py").read_text(encoding="utf-8")
        marker = source.split("def _store_canonical", 1)[1][:3000]
        assert "REL-075-009" in marker

    def test_the_canonical_id_is_only_reported_never_resolved(self) -> None:
        """The premise of accepting the residual: nothing looks the id up.

        If this ever stops holding -- if some path starts resolving
        ``canonical_file_id`` back to a file -- the orphan stops being merely
        untidy and the finding needs reopening.
        """
        source = (_REPO_ROOT / "src" / "main.py").read_text(encoding="utf-8")
        # Read the AST, not the text: prose in a docstring names the field too,
        # and what matters is where the *value* flows, not where the word
        # appears. Every call that receives it must be one of:
        #   _discard_canonical -- the request-scope rollback, the one path that
        #       still holds the caller's token and so can act on the id at all,
        #   Envelope           -- storing it in the (immutable) version record,
        #   logger.info        -- naming it in a log line.
        # Anything else would mean some path resolves the id back to a file,
        # which is precisely the premise this residual is accepted on.
        allowed_consumers = {"_discard_canonical", "Envelope", "info"}
        consumers: set[str] = set()
        seen = False
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                referenced = any(
                    isinstance(arg, ast.Name) and arg.id == "canonical_file_id"
                    for arg in node.args
                ) or any(
                    isinstance(kw.value, ast.Name)
                    and kw.value.id == "canonical_file_id"
                    for kw in node.keywords
                )
                if referenced:
                    seen = True
                    func = node.func
                    consumers.add(
                        func.attr if isinstance(func, ast.Attribute) else
                        func.id if isinstance(func, ast.Name) else repr(func)
                    )
            elif isinstance(node, ast.Attribute) and node.attr == "canonical_file_id":
                seen = True
        assert seen, "the field vanished; revisit REL-075-009"
        assert consumers <= allowed_consumers, (
            f"canonical_file_id gained a new consumer: "
            f"{sorted(consumers - allowed_consumers)}"
        )
