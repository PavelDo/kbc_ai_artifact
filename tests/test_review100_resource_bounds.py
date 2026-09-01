"""Resource bounds on the two public amplification surfaces (v0.10.0 review).

Two findings share one theme: a cheap public request that costs the single
hub container an unbounded amount of work.

* ``REL-100-002`` — ``GET /a/{id}/export/vault`` loaded every visible version
  and comment thread, rendered them, and built the whole ZIP inside a
  ``BytesIO`` before answering. A capability-URL holder could repeat that at
  will, so configured per-record limits multiplied into arbitrarily large
  CPU/RAM spikes. The fix is a pre-build size budget (413), a ZIP streamed
  through a 0600 temporary file that is unlinked after the response, and an
  hourly per-(artifact, client) build budget (429).
* ``REL-100-003`` — ``GET /a/{id}/live`` enumerated versions and comment
  threads *before* comparing the ``If-None-Match`` tag, so a 304 saved
  response bytes but no backend work. The fix is an in-memory monotonic
  revision per artifact, bumped by every mutation, from which the ETag is
  derived before anything is enumerated.

The probes below mirror the review's own reproducers: they assert the *work*
is gone (no temporary file, no enumeration), not merely that the status code
looks right.
"""

from __future__ import annotations

import dataclasses
import io
import os
import stat
import zipfile

import pytest

import src.main as main
from src.comments import CommentStore
from src.export import ExportTooLarge, build_vault, build_vault_file, envelope_source_chars
from src.store import ArtifactStore
from tests.test_api import (
    AUTH_HEADERS,
    OTHER_AUTH_HEADERS,
    Api,
    _comment,
    _policy,
    _publish_markdown,
    _submit_version,
    api,  # noqa: F401 - the fixture this module runs on
)
from tests.test_export import (  # noqa: F401 - hand-built vault fixtures
    envelopes,
    meta,
    threads,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _retune(api: Api, monkeypatch, **fields) -> None:
    """Replace ``main.settings`` with a copy carrying tighter limits."""
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, **fields)
    )


def _vault_temp_files(api: Api) -> list[str]:
    """Every export scratch file currently sitting in the cache directory."""
    cache_dir = api.settings.cache_dir
    if not cache_dir.exists():
        return []
    return sorted(p.name for p in cache_dir.iterdir() if p.name.startswith("vault-"))


def _seed_artifact(api: Api) -> str:
    """An artifact with two live versions, a proposal and a comment thread."""
    artifact_id = _publish_markdown(
        api, "# Q3 review\n\nOld line\n", title="Q3 review", accept_versions=True
    )
    assert _submit_version(api, artifact_id, "# Q3 review\n\nNew line\n").status_code == 201
    assert (
        _submit_version(
            api, artifact_id, "# Proposal\n", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    assert _comment(api, artifact_id, exact="Old line").status_code == 201
    return artifact_id


# --------------------------------------------------------------------------
# REL-100-002 — the vault export is budgeted, streamed and rate-limited
# --------------------------------------------------------------------------


def test_export_budget_refuses_before_any_zip_is_written(api: Api, monkeypatch) -> None:
    """A too-large artifact is refused with 413 and never touches the disk."""
    artifact_id = _seed_artifact(api)
    # One byte of budget: whatever this artifact holds is over it.
    _retune(api, monkeypatch, export_max_bytes=1)

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error"] == "export too large"
    assert body["limit"] == 1
    assert "HUB_EXPORT_MAX_BYTES" in body["detail"]

    # The refusal happens before the archive is built, so no scratch file was
    # ever created -- which is the whole point of a *pre*-build budget.
    assert _vault_temp_files(api) == []


def test_export_streams_through_a_temporary_file_and_cleans_it_up(api: Api) -> None:
    """A successful export leaves nothing behind in the cache directory."""
    artifact_id = _seed_artifact(api)
    assert _vault_temp_files(api) == []

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == (
        'attachment; filename="q3-review-vault.zip"'
    )
    assert zipfile.ZipFile(io.BytesIO(resp.content)).namelist()

    # The background task unlinks the scratch file once the response is done.
    assert _vault_temp_files(api) == []


def test_streamed_export_is_byte_identical_to_the_in_memory_build(api: Api) -> None:
    """Streaming changed how the ZIP is written, never what it contains."""
    artifact_id = _seed_artifact(api)
    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 200

    store = main.app.state.store
    meta = store.get_meta(artifact_id)
    envelopes = [
        env
        for env in (
            store.get_version(artifact_id, row["version"])
            for row in store.list_versions(artifact_id)
        )
        if env is not None and main.may_see(meta, env, None)
    ]
    threads = main.app.state.comments.list_for(artifact_id)
    name, expected = build_vault(meta, envelopes, threads)

    assert resp.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert resp.content == expected


def test_export_rate_limit_trips_with_the_standard_429_shape(
    api: Api, monkeypatch
) -> None:
    """A capability holder gets a bounded number of builds per hour."""
    artifact_id = _seed_artifact(api)
    _retune(api, monkeypatch, max_exports_per_hour=2)

    assert api.client.get(f"/a/{artifact_id}/export/vault").status_code == 200
    assert api.client.get(f"/a/{artifact_id}/export/vault").status_code == 200

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["error"] == "too many exports this hour"
    assert body["limit"] == 2
    assert body["detail"]
    # A spent budget must not have produced a half-written archive either.
    assert _vault_temp_files(api) == []


def test_export_rate_limit_is_per_artifact(api: Api, monkeypatch) -> None:
    """Spending one artifact's budget leaves another artifact's untouched."""
    first = _publish_markdown(api, "# One\n\nBody\n", title="One")
    second = _publish_markdown(api, "# Two\n\nBody\n", title="Two")
    _retune(api, monkeypatch, max_exports_per_hour=1)

    assert api.client.get(f"/a/{first}/export/vault").status_code == 200
    assert api.client.get(f"/a/{first}/export/vault").status_code == 429
    assert api.client.get(f"/a/{second}/export/vault").status_code == 200


def test_export_is_documented_in_openapi(api: Api) -> None:
    operation = api.client.get("/openapi.json").json()["paths"][
        "/a/{artifact_id}/export/vault"
    ]["get"]
    assert {"200", "401", "404", "413", "429", "502"} <= set(operation["responses"])
    for response in operation["responses"].values():
        assert response["description"].strip()
    assert "HUB_EXPORT_MAX_BYTES" in operation["description"]
    assert "HUB_MAX_EXPORTS_PER_HOUR" in operation["description"]


def test_export_budget_stops_loading_versions_once_it_is_exceeded(
    api: Api, monkeypatch
) -> None:
    """The budget is checked incrementally while loading, not after the fact.

    Regression for a wrong fix of REL-100-002: computing the size estimate
    only after every visible version has already been downloaded and parsed
    lets the allocation the budget exists to bound (up to
    ``HUB_MAX_VERSIONS x HUB_MAX_ENVELOPE_BYTES``) happen before the budget is
    ever consulted. Instead, ``get_version`` must be called one version at a
    time, with the running total checked after each call, so that once the
    first two versions alone already exceed the budget, the third, fourth and
    fifth are never fetched from Storage at all.
    """
    artifact_id = _publish_markdown(
        api, "# Big\n\nfirst\n", title="Big", accept_versions=True
    )
    for i in range(4):
        assert (
            _submit_version(api, artifact_id, f"# Big\n\nversion {i}\n").status_code
            == 201
        )
    store = main.app.state.store
    rows = store.list_versions(artifact_id)
    assert len(rows) == 5

    # Work out, from the real envelopes, a budget the first two versions
    # already cross but the first alone does not -- so the incremental check
    # must load exactly two versions before refusing, not zero and not five.
    running = 0
    running_after: list[int] = []
    for row in rows:
        env = store.get_version(artifact_id, row["version"])
        running += envelope_source_chars(env)
        running_after.append(running)
    limit = running_after[0]
    assert running_after[1] > limit, "second version must push the total over the limit"
    _retune(api, monkeypatch, export_max_bytes=limit)

    calls: list[int] = []
    original_get_version = store.get_version

    def counting_get_version(artifact_id_, version, fresh=False):
        calls.append(version)
        return original_get_version(artifact_id_, version, fresh=fresh)

    monkeypatch.setattr(store, "get_version", counting_get_version)

    thread_calls: list[str] = []
    original_list_for = main.app.state.comments.list_for

    def counting_list_for(artifact_id_):
        thread_calls.append(artifact_id_)
        return original_list_for(artifact_id_)

    monkeypatch.setattr(main.app.state.comments, "list_for", counting_list_for)

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 413, resp.text

    # Only two of the five versions should ever have been fetched.
    assert len(calls) == 2, f"loaded {len(calls)} versions, expected exactly 2"
    # And comments should never have been listed at all: the versions alone
    # already tripped the budget.
    assert thread_calls == []


def test_export_within_budget_loads_every_version_and_matches_bytes(
    api: Api, monkeypatch
) -> None:
    """The happy path is unaffected: everything is loaded, ZIP is unchanged.

    Companion to the "stop early" regression above -- the incremental budget
    check must not skip anything when the artifact is within budget.
    """
    artifact_id = _seed_artifact(api)
    store = main.app.state.store
    expected_versions = {row["version"] for row in store.list_versions(artifact_id)}

    calls: list[int] = []
    original_get_version = store.get_version

    def counting_get_version(artifact_id_, version, fresh=False):
        calls.append(version)
        return original_get_version(artifact_id_, version, fresh=fresh)

    monkeypatch.setattr(store, "get_version", counting_get_version)

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 200, resp.text
    assert set(calls) == expected_versions

    meta = store.get_meta(artifact_id)
    envelopes = [
        env
        for env in (
            store.get_version(artifact_id, row["version"])
            for row in store.list_versions(artifact_id)
        )
        if env is not None and main.may_see(meta, env, None)
    ]
    threads = main.app.state.comments.list_for(artifact_id)
    name, expected = build_vault(meta, envelopes, threads)
    assert resp.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert resp.content == expected


# --------------------------------------------------------------------------
# REL-100-002 — build_vault_file, at the module level
# --------------------------------------------------------------------------


def test_build_vault_file_matches_build_vault_byte_for_byte(
    tmp_path, meta, envelopes, threads
) -> None:
    expected_name, expected = build_vault(meta, envelopes, threads)
    name, path, size = build_vault_file(meta, envelopes, threads, tmp_path)
    try:
        assert name == expected_name
        assert path.read_bytes() == expected
        assert size == len(expected)
    finally:
        path.unlink(missing_ok=True)


def test_build_vault_file_is_owner_only(tmp_path, meta, envelopes, threads) -> None:
    """The scratch archive holds a full copy of the artifact; 0600, always."""
    _, path, _ = build_vault_file(meta, envelopes, threads, tmp_path)
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        path.unlink(missing_ok=True)


def test_build_vault_file_aborts_and_unlinks_when_the_archive_overruns(
    tmp_path, meta, envelopes, threads
) -> None:
    """The write-time ceiling is the backstop when the estimate under-counts."""
    with pytest.raises(ExportTooLarge):
        build_vault_file(meta, envelopes, threads, tmp_path, max_bytes=1)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# REL-100-003 — the live poll answers 304 without enumerating anything
# --------------------------------------------------------------------------


def _live(api: Api, share_id: str, etag: str | None = None):
    headers = {"If-None-Match": etag} if etag else {}
    return api.client.get(f"/a/{share_id}/live", headers=headers)


def test_matching_live_etag_answers_304_without_enumerating(
    api: Api, monkeypatch
) -> None:
    """The 304 must cost no version listing and no comment listing at all."""
    artifact_id = _seed_artifact(api)
    first = _live(api, artifact_id)
    assert first.status_code == 200
    etag = first.headers["etag"]

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a matching ETag must not enumerate anything")

    monkeypatch.setattr(main.app.state.store, "list_versions", explode)
    monkeypatch.setattr(main.app.state.store, "get_head", explode)
    monkeypatch.setattr(main.app.state.comments, "list_for", explode)

    again = _live(api, artifact_id, etag)
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "no-store"


def _add_version(api: Api, artifact_id: str) -> None:
    assert api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Q3 review\n\nAnother line\n"},
        headers=AUTH_HEADERS,
    ).status_code == 200


def _propose(api: Api, artifact_id: str) -> None:
    assert _submit_version(
        api, artifact_id, "# Another proposal\n", headers=OTHER_AUTH_HEADERS
    ).status_code == 201


def _promote(api: Api, artifact_id: str) -> None:
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/versions/3/promote", headers=AUTH_HEADERS
    ).status_code == 200


def _withdraw(api: Api, artifact_id: str) -> None:
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/3", headers=OTHER_AUTH_HEADERS
    ).status_code == 200


def _finalise(api: Api, artifact_id: str) -> None:
    assert _policy(api, artifact_id, status="final").status_code == 200


def _pin_head(api: Api, artifact_id: str) -> None:
    assert api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 1},
        headers=AUTH_HEADERS,
    ).status_code == 200


def _new_thread(api: Api, artifact_id: str) -> None:
    assert _comment(api, artifact_id, exact="New line", version=2).status_code == 201


def _reply(api: Api, artifact_id: str) -> None:
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    thread_id = threads[0]["id"]
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "Still true."},
        headers=AUTH_HEADERS,
    ).status_code == 201


def _resolve_thread(api: Api, artifact_id: str) -> None:
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    thread_id = threads[0]["id"]
    assert api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
        headers=AUTH_HEADERS,
    ).status_code == 200


def _delete_thread(api: Api, artifact_id: str) -> None:
    threads = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    thread_id = threads[0]["id"]
    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}", headers=AUTH_HEADERS
    ).status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_add_version, id="version-added"),
        pytest.param(_propose, id="version-proposed"),
        pytest.param(_promote, id="proposal-promoted"),
        pytest.param(_withdraw, id="version-deleted"),
        pytest.param(_finalise, id="status-changed"),
        pytest.param(_pin_head, id="head-pinned"),
        pytest.param(_new_thread, id="thread-created"),
        pytest.param(_reply, id="thread-replied"),
        pytest.param(_resolve_thread, id="thread-resolved"),
        pytest.param(_delete_thread, id="thread-deleted"),
    ],
)
def test_every_mutation_kind_changes_the_live_etag(api: Api, mutate) -> None:
    artifact_id = _seed_artifact(api)
    before = _live(api, artifact_id)
    assert before.status_code == 200

    mutate(api, artifact_id)

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200, "the poll must notice this mutation"
    assert after.headers["etag"] != before.headers["etag"]


def test_link_rotation_changes_the_live_etag(api: Api) -> None:
    """Rotation is a meta save, and it also changes the reported 'id'."""
    artifact_id = _seed_artifact(api)
    before = _live(api, artifact_id)
    share_id = api.client.post(
        f"/api/artifacts/{artifact_id}/rotate-link", headers=AUTH_HEADERS
    ).json()["share_id"]

    after = _live(api, share_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]


def test_live_etag_differs_after_a_restart_with_identical_content(api: Api) -> None:
    """A cold process must never re-issue a pre-restart tag (boot nonce)."""
    artifact_id = _seed_artifact(api)
    before = _live(api, artifact_id)
    assert before.status_code == 200

    # A restart: brand-new stores over the very same Storage backend, with the
    # index rebuilt from tags exactly as the lifespan does it.
    cache_dir = api.settings.cache_dir
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

    after = _live(api, artifact_id, before.headers["etag"])
    assert after.status_code == 200
    assert after.headers["etag"] != before.headers["etag"]
    # ...and the body is unchanged, which is what makes this a *nonce* test.
    assert after.json() == before.json()


def test_revision_ledger_forgets_a_purged_artifact(api: Api) -> None:
    """A create/purge loop must not grow the ledger without bound."""
    store = main.app.state.store
    artifact_id = _publish_markdown(api, "# Gone\n\nBody\n")
    assert store.revision(artifact_id) != store.revision("never-existed")

    assert api.client.delete(
        f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
    ).status_code == 200
    assert store.revision(artifact_id) == store.revision("never-existed")
