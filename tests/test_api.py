"""FastAPI-layer tests for the KBC Artifact Hub.

The whole HTTP surface of ``src.main`` is exercised through a single
``api`` fixture built on top of ``fastapi.testclient.TestClient``:

- the hub's serving Storage backend is a fresh ``InMemoryFilesBackend``
  (exposed via ``api.backend`` so tests can inspect Storage state directly,
  e.g. to simulate a container restart by hydrating a second store over the
  same backend),
- ``src.main.verify_token`` is monkeypatched so ``"good-token"`` verifies as
  project 123 and ``"other-token"`` as project 999; any other token raises
  ``AuthError``,
- the canonical-copy upload (``KbcFilesBackend`` constructed with the
  *caller's* token inside ``src.main._store_canonical``) is intercepted at
  the same seam (``src.main.KbcFilesBackend``) and, for any stack/token pair
  that is not the hub's own, records the call into ``api.canonical_calls``
  and returns a fake incrementing file id instead of touching real Storage,
- the TestClient's ``base_url`` is ``https://testserver`` so ``Secure``
  unlock cookies round-trip.

The fixture is function-scoped (defined at module level in this file, not in
``conftest.py``) so every test gets an isolated backend, cache dir and call
log.
"""

from __future__ import annotations

import dataclasses
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

import src.main as main
from src.auth import STACK_ALIASES, AuthError, Owner
from src.kbc import InMemoryFilesBackend
from src.store import ArtifactStore

AUTH_HEADERS = {"X-StorageApi-Token": "good-token", "X-Kbc-Stack": "us"}
OTHER_AUTH_HEADERS = {"X-StorageApi-Token": "other-token", "X-Kbc-Stack": "us"}

#: token -> (project_id, project_name) recognized by the fake verify_token.
_OWNER_PROJECTS: dict[str, tuple[int, str]] = {
    "good-token": (123, "Test"),
    "other-token": (999, "Other"),
}


class Api(NamedTuple):
    client: TestClient
    backend: InMemoryFilesBackend
    canonical_calls: list[dict[str, Any]]
    settings: Any


@pytest.fixture
def api(tmp_path, settings, monkeypatch):
    """A TestClient over ``src.main.app`` wired to fakes, per the module docstring."""
    test_settings = dataclasses.replace(settings, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(main, "settings", test_settings)

    backend = InMemoryFilesBackend()
    canonical_calls: list[dict[str, Any]] = []
    counter = {"n": 9000}

    class _CanonicalBackend:
        """Fake KbcFilesBackend used only for the author's canonical copy."""

        def __init__(self, stack_url: str, token: str) -> None:
            self.stack_url = stack_url
            self.token = token

        def upload(self, name: str, content: bytes, tags: list[str]) -> int:
            counter["n"] += 1
            file_id = counter["n"]
            canonical_calls.append(
                {
                    "stack_url": self.stack_url,
                    "token": self.token,
                    "name": name,
                    "content": content,
                    "tags": list(tags),
                    "file_id": file_id,
                }
            )
            return file_id

    def fake_kbc_files_backend(stack_url: str, token: str):
        """Route hub-project calls to the shared in-memory backend, else fake."""
        if (
            stack_url == test_settings.hub_stack_url
            and token == test_settings.hub_storage_token
        ):
            return backend
        return _CanonicalBackend(stack_url, token)

    def fake_verify_token(stack_url: str, token: str, timeout_s: int = 15) -> Owner:
        project = _OWNER_PROJECTS.get(token)
        if project is None:
            raise AuthError("Storage token rejected by the stack")
        project_id, project_name = project
        return Owner(
            stack_url=stack_url, project_id=project_id, project_name=project_name
        )

    monkeypatch.setattr(main, "KbcFilesBackend", fake_kbc_files_backend)
    monkeypatch.setattr(main, "verify_token", fake_verify_token)

    with TestClient(main.app, base_url="https://testserver") as client:
        yield Api(
            client=client,
            backend=backend,
            canonical_calls=canonical_calls,
            settings=test_settings,
        )


def _publish_markdown(
    api: Api,
    markdown: str,
    *,
    title: str | None = None,
    password: str | None = None,
    headers: dict[str, str] = AUTH_HEADERS,
) -> str:
    """Publish a markdown artifact and return its id, asserting success."""
    payload: dict[str, Any] = {"markdown": markdown}
    if title is not None:
        payload["title"] = title
    if password is not None:
        payload["password"] = password
    resp = api.client.post("/api/artifacts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _assert_no_sensitive_keys(obj: Any) -> None:
    """Recursively assert no 'owner' or 'password' key appears anywhere."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key not in ("owner", "password"), f"found sensitive key {key!r}"
            _assert_no_sensitive_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_sensitive_keys(item)


# --------------------------------------------------------------------------
# Platform / discovery endpoints
# --------------------------------------------------------------------------


def test_platform_probe_returns_ok(api: Api) -> None:
    resp = api.client.post("/")
    assert resp.status_code == 200
    assert resp.text == "OK"


def test_health_shape(api: Api) -> None:
    resp = api.client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status", "artifacts", "hydrated"}
    assert body["status"] == "ok"
    assert isinstance(body["artifacts"], int)
    assert isinstance(body["hydrated"], bool)
    assert body["hydrated"] is True
    assert body["artifacts"] == 0


def test_context_lists_all_endpoints_and_stack_aliases(api: Api) -> None:
    resp = api.client.get("/context")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["endpoints"]) == 14
    assert body["auth"]["stack_aliases"] == dict(STACK_ALIASES)
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    expected = {
        ("GET", "/"),
        ("POST", "/"),
        ("GET", "/health"),
        ("GET", "/context"),
        ("GET", "/skill"),
        ("GET", "/a/{id}"),
        ("POST", "/a/{id}/unlock"),
        ("GET", "/a/{id}/raw"),
        ("GET", "/a/{id}/source"),
        ("GET", "/a/{id}/meta"),
        ("POST", "/api/artifacts"),
        ("PUT", "/api/artifacts/{id}"),
        ("GET", "/api/artifacts"),
        ("DELETE", "/api/artifacts/{id}"),
    }
    assert paths == expected


def test_skill_returns_markdown(api: Api) -> None:
    resp = api.client.get("/skill")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.strip() != ""


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------


def test_publish_markdown_returns_full_shape_and_records_canonical_upload(
    api: Api,
) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello\n\nWorld", "title": "Hello doc"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    for key in ("id", "url", "raw_url", "source_url", "meta_url"):
        assert key in body and body[key]
    artifact_id = body["id"]
    assert body["url"] == f"https://testserver/a/{artifact_id}"
    assert body["raw_url"] == f"{body['url']}/raw"
    assert body["source_url"] == f"{body['url']}/source"
    assert body["meta_url"] == f"{body['url']}/meta"

    assert len(api.canonical_calls) == 1
    call = api.canonical_calls[0]
    assert call["tags"] == ["kbc-artifact", f"artifact-id-{artifact_id}"]
    assert call["file_id"] == body["canonical_file_id"]


def test_read_artifact_renders_html_with_no_index_headers(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    resp = api.client.get(f"/a/{artifact_id}")
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert resp.headers["cache-control"] == "no-cache"
    assert "Body text" in resp.text


def test_raw_returns_exact_same_html_as_rendered_page(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nRaw check")
    rendered = api.client.get(f"/a/{artifact_id}")
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200
    assert raw.text == rendered.text


def test_source_returns_original_markdown(api: Api) -> None:
    md = "# Hi\n\nSome *text* here."
    artifact_id = _publish_markdown(api, md)
    resp = api.client.get(f"/a/{artifact_id}/source")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == md


def test_meta_public_shape_has_no_owner_or_password(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Hi there")
    resp = api.client.get(f"/a/{artifact_id}/meta")
    assert resp.status_code == 200
    body = resp.json()
    _assert_no_sensitive_keys(body)
    for key in (
        "id",
        "title",
        "source_type",
        "created_at",
        "updated_at",
        "version",
        "protected",
        "size_bytes",
        "url",
        "raw_url",
        "source_url",
        "meta_url",
    ):
        assert key in body
    assert body["protected"] is False


def test_publish_html_source_matches_submission_exactly(api: Api) -> None:
    html_doc = "<html><body><h1>Hi</h1></body></html>"
    resp = api.client.post(
        "/api/artifacts", json={"html": html_doc}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 201
    artifact_id = resp.json()["id"]
    source = api.client.get(f"/a/{artifact_id}/source")
    assert source.status_code == 200
    assert source.text == html_doc


def test_publish_both_html_and_markdown_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>x</p>", "markdown": "# x"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_publish_neither_content_field_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"title": "Empty"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422


def test_publish_oversized_html_is_413(api: Api, monkeypatch) -> None:
    tiny_settings = dataclasses.replace(api.settings, max_html_bytes=10)
    monkeypatch.setattr(main, "settings", tiny_settings)
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<html>" * 5},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 413


def test_publish_missing_token_header_is_401(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-Kbc-Stack": "us"},
    )
    assert resp.status_code == 401


def test_publish_bad_token_is_401(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-StorageApi-Token": "totally-wrong", "X-Kbc-Stack": "us"},
    )
    assert resp.status_code == 401


def test_publish_bad_stack_header_is_400(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"html": "<p>hi</p>"},
        headers={"X-StorageApi-Token": "good-token", "X-Kbc-Stack": "not-a-stack"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Password protection
# --------------------------------------------------------------------------


def test_password_protected_read_paths(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")

    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 401
    assert "password" in page.text.lower()
    assert page.headers["content-type"].startswith("text/html")

    raw_no_password = api.client.get(f"/a/{artifact_id}/raw")
    assert raw_no_password.status_code == 401
    assert raw_no_password.json()["error"] == "password required"

    raw_wrong = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
    )
    assert raw_wrong.status_code == 401

    raw_right = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "hunter2"}
    )
    assert raw_right.status_code == 200

    meta = api.client.get(f"/a/{artifact_id}/meta")
    assert meta.status_code == 200
    assert meta.json()["protected"] is True


def test_unlock_form_wrong_then_right_password_sets_cookie(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret content", password="hunter2")

    wrong = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "nope"},
        follow_redirects=False,
    )
    assert wrong.status_code == 401
    assert "Wrong password" in wrong.text

    right = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert right.status_code == 303
    assert right.headers["location"] == f"/a/{artifact_id}"

    set_cookie = right.headers.get("set-cookie", "")
    assert f"art_{artifact_id}=" in set_cookie
    assert f"Path=/a/{artifact_id}" in set_cookie
    assert "HttpOnly" in set_cookie

    followup = api.client.get(f"/a/{artifact_id}")
    assert followup.status_code == 200


# --------------------------------------------------------------------------
# Update (PUT)
# --------------------------------------------------------------------------


def test_put_update_by_owner_bumps_version_and_changes_content(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Version One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Version Two"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2

    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert "Version Two" in raw.text
    assert "Version One" not in raw.text


def test_put_by_foreign_project_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Owner content")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"title": "hijack"},
        headers=OTHER_AUTH_HEADERS,
    )
    assert resp.status_code == 403


def test_put_unknown_id_is_404(api: Api) -> None:
    resp = api.client.put(
        "/api/artifacts/does-not-exist", json={"title": "x"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


def test_put_clear_password_removes_protection(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Protected", password="secret")
    assert api.client.get(f"/a/{artifact_id}").status_code == 401

    put_resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"clear_password": True},
        headers=AUTH_HEADERS,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["protected"] is False

    resp = api.client.get(f"/a/{artifact_id}")
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------


def test_list_artifacts_only_own_project_with_urls_merged(api: Api) -> None:
    own_id = _publish_markdown(api, "# Mine", headers=AUTH_HEADERS)
    foreign_id = _publish_markdown(api, "# Not mine", headers=OTHER_AUTH_HEADERS)

    resp = api.client.get("/api/artifacts", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == 123
    ids = {a["id"] for a in body["artifacts"]}
    assert own_id in ids
    assert foreign_id not in ids

    mine = next(a for a in body["artifacts"] if a["id"] == own_id)
    assert mine["url"] == f"https://testserver/a/{own_id}"
    assert mine["raw_url"] == f"{mine['url']}/raw"
    assert mine["source_url"] == f"{mine['url']}/source"
    assert mine["meta_url"] == f"{mine['url']}/meta"


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def test_delete_unknown_is_404(api: Api) -> None:
    resp = api.client.delete("/api/artifacts/does-not-exist", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_delete_foreign_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Mine")
    resp = api.client.delete(f"/api/artifacts/{artifact_id}", headers=OTHER_AUTH_HEADERS)
    assert resp.status_code == 403


def test_delete_own_removes_serving_copy(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Mine")
    resp = api.client.delete(f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    resp2 = api.client.get(f"/a/{artifact_id}")
    assert resp2.status_code == 404


# --------------------------------------------------------------------------
# Restart survival
# --------------------------------------------------------------------------


def test_restart_survival_second_store_over_same_backend(api: Api, tmp_path) -> None:
    """A fresh ArtifactStore over the same backend must serve what was published.

    This simulates a container restart: the process is stateless except for
    Storage, so hydrating a brand-new store over the same backend must find
    everything a previous process instance published.
    """
    artifact_id = _publish_markdown(api, "# Survives restart")

    second_store = ArtifactStore(
        backend=api.backend,
        cache_dir=tmp_path / "restart-cache",
        cache_max_entries=api.settings.cache_max_entries,
    )
    count = second_store.hydrate()
    assert count >= 1

    envelope = second_store.get(artifact_id)
    assert envelope is not None
    assert "Survives restart" in envelope.html
