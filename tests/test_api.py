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
import html
import io
import json
import logging
import re
import zipfile
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

import src.main as main
from src.auth import STACK_ALIASES, AuthError, Owner
from src.builder import BuiltArtifact
from src.comments import CommentStore
from src.config import load_settings
from src.kbc import BackendError, InMemoryFilesBackend
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
    # The per-contributor counters are module-level state; a test must never
    # inherit another test's tally.
    main._submission_counts.clear()
    main._comment_counts.clear()
    main._unlock_failures.clear()

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
    accept_versions: bool | None = None,
    headers: dict[str, str] = AUTH_HEADERS,
) -> str:
    """Publish a markdown artifact and return its id, asserting success."""
    payload: dict[str, Any] = {"markdown": markdown}
    if title is not None:
        payload["title"] = title
    if password is not None:
        payload["password"] = password
    if accept_versions is not None:
        payload["accept_versions"] = accept_versions
    resp = api.client.post("/api/artifacts", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _submit_version(
    api: Api,
    artifact_id: str,
    markdown: str,
    *,
    note: str | None = None,
    headers: dict[str, str] = AUTH_HEADERS,
):
    """POST one version; returns the raw response so tests can assert status."""
    payload: dict[str, Any] = {"markdown": markdown}
    if note is not None:
        payload["note"] = note
    return api.client.post(
        f"/api/artifacts/{artifact_id}/versions", json=payload, headers=headers
    )


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
    assert set(body) == {"status", "version", "artifacts", "hydrated"}
    assert body["status"] == "ok"
    assert body["version"] == main.SERVICE_VERSION
    assert body["version"]
    assert isinstance(body["artifacts"], int)
    assert isinstance(body["hydrated"], bool)
    assert body["hydrated"] is True
    assert body["artifacts"] == 0


def test_context_lists_all_endpoints_and_stack_aliases(api: Api) -> None:
    resp = api.client.get("/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == main.SERVICE_VERSION
    assert body["repository"] == main.GITHUB_REPO_URL
    assert body["auth"]["stack_aliases"] == dict(STACK_ALIASES)
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    expected = {
        ("GET", "/"),
        ("POST", "/"),
        ("GET", "/health"),
        ("GET", "/health/headers"),
        ("GET", "/context"),
        ("GET", "/skill"),
        ("GET", "/agent"),
        ("GET", "/changelog"),
        ("GET", "/changelog.md"),
        ("GET", "/admin"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/a/{id}"),
        ("POST", "/a/{id}/unlock"),
        ("GET", "/a/{id}/raw"),
        ("GET", "/a/{id}/source"),
        ("GET", "/a/{id}/meta"),
        ("GET", "/a/{id}/v/{n}"),
        ("GET", "/a/{id}/versions"),
        ("GET", "/a/{id}/diff/{a}..{b}"),
        ("GET", "/a/{id}/comments"),
        ("GET", "/a/{id}/review"),
        ("GET", "/a/{id}/export/markdown"),
        ("GET", "/a/{id}/export/vault"),
        ("POST", "/api/artifacts"),
        ("PUT", "/api/artifacts/{id}"),
        ("GET", "/api/artifacts"),
        ("DELETE", "/api/artifacts/{id}"),
        ("POST", "/api/artifacts/{id}/versions"),
        ("POST", "/api/artifacts/{id}/versions/{n}/promote"),
        ("DELETE", "/api/artifacts/{id}/versions/{n}"),
        ("PUT", "/api/artifacts/{id}/head"),
        ("POST", "/api/artifacts/{id}/comments"),
        ("POST", "/api/artifacts/{id}/comments/{tid}/replies"),
        ("POST", "/api/artifacts/{id}/comments/{tid}/resolve"),
        ("DELETE", "/api/artifacts/{id}/comments/{tid}"),
    }
    assert paths == expected
    assert len(body["endpoints"]) == len(expected)


def test_skill_returns_markdown(api: Api) -> None:
    resp = api.client.get("/skill")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.strip() != ""


def test_agent_serves_the_agent_definition_file(api: Api) -> None:
    """/agent must serve skills/artifact-hub-agent/AGENT.md verbatim.

    The expectation is read from disk rather than hard-coded so the test keeps
    passing while the definition itself is rewritten.
    """
    expected = main.AGENT_PATH.read_text(encoding="utf-8")
    resp = api.client.get("/agent")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == expected
    # Echoing the first line proves it is *that* file, not some other markdown.
    assert resp.text.splitlines()[0] == expected.splitlines()[0]


def test_context_documents_the_agent_and_admin_endpoints(api: Api) -> None:
    body = api.client.get("/context").json()
    by_path = {e["path"]: e for e in body["endpoints"]}

    agent = by_path["/agent"]
    assert agent["method"] == "GET"
    assert agent["auth"] == "none"
    assert "subagent" in agent["purpose"].lower()
    assert "~/.claude/agents" in agent["purpose"]

    admin = by_path["/admin"]
    assert admin["method"] == "GET"
    assert "sessionStorage" in admin["purpose"]


def test_changelog_renders_the_repository_file_as_html(api: Api) -> None:
    resp = api.client.get("/changelog")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<h1" in resp.text
    assert "Changelog" in resp.text


def test_changelog_md_serves_the_raw_file(api: Api) -> None:
    """Echoes the file's own first line so this survives a rewritten placeholder."""
    expected_first_line = main.CHANGELOG_PATH.read_text(
        encoding="utf-8"
    ).splitlines()[0]
    resp = api.client.get("/changelog.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.splitlines()[0] == expected_first_line


# --------------------------------------------------------------------------
# Interactive API docs
# --------------------------------------------------------------------------


def test_docs_serves_swagger_ui(api: Api) -> None:
    resp = api.client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "swagger" in resp.text.lower()


def test_openapi_json_has_expected_paths_and_security_schemes(api: Api) -> None:
    resp = api.client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    schema = resp.json()

    assert "openapi" in schema
    assert "/api/artifacts" in schema["paths"]
    assert "/a/{artifact_id}" in schema["paths"]

    security_schemes = schema["components"]["securitySchemes"]
    scheme_headers = {s["name"] for s in security_schemes.values()}
    assert "X-StorageApi-Token" in scheme_headers
    assert "X-Storage-Stack" in scheme_headers


def test_openapi_json_never_leaks_the_hub_storage_token(api: Api) -> None:
    resp = api.client.get("/openapi.json")
    assert resp.status_code == 200
    assert api.settings.hub_storage_token not in resp.text


# --------------------------------------------------------------------------
# OpenAPI documentation audit
#
# These tests are the standing guarantee that /docs stays usable: every
# operation explains itself, and every parameter a caller can send is
# described in the schema. They are mechanical on purpose — a new route or a
# new parameter that skips its documentation fails the suite.
# --------------------------------------------------------------------------

#: The tags declared on the app; every operation must carry exactly one.
_TAGS = {"public", "artifacts", "versions", "comments", "service"}


def _operations(schema: dict) -> list[tuple[str, str, dict]]:
    """Every (method, path, operation) in the generated document."""
    out: list[tuple[str, str, dict]] = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if isinstance(operation, dict):
                out.append((method.upper(), path, operation))
    return out


def test_openapi_every_operation_has_summary_description_and_one_tag(
    api: Api,
) -> None:
    schema = api.client.get("/openapi.json").json()
    operations = _operations(schema)
    assert operations, "no operations in the OpenAPI document"
    for method, path, operation in operations:
        where = f"{method} {path}"
        assert operation.get("summary", "").strip(), f"{where}: empty summary"
        assert (
            operation.get("description", "").strip()
        ), f"{where}: empty description"
        tags = operation.get("tags") or []
        assert len(tags) == 1, f"{where}: expected exactly one tag, got {tags}"
        assert tags[0] in _TAGS, f"{where}: unknown tag {tags[0]!r}"


def test_openapi_every_parameter_is_described(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    seen = 0
    for method, path, operation in _operations(schema):
        for parameter in operation.get("parameters", []):
            seen += 1
            where = f"{method} {path} [{parameter.get('in')} {parameter.get('name')}]"
            description = parameter.get("description") or parameter.get(
                "schema", {}
            ).get("description")
            assert description and description.strip(), f"{where}: no description"
    assert seen, "no parameters found; the audit would be vacuous"


def test_openapi_every_operation_documents_its_responses(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    for method, path, operation in _operations(schema):
        responses = operation.get("responses") or {}
        assert responses, f"{method} {path}: no responses documented"
        for code, response in responses.items():
            where = f"{method} {path} -> {code}"
            assert (
                response.get("description", "").strip()
            ), f"{where}: response without a description"


def test_openapi_api_operations_carry_the_security_schemes(api: Api) -> None:
    """/api/* is header-authenticated; the public read path must not claim to be."""
    schema = api.client.get("/openapi.json").json()
    for method, path, operation in _operations(schema):
        if path.startswith("/api/"):
            security = operation.get("security")
            assert security, f"{method} {path}: missing security requirement"
            names = set(security[0])
            assert names == {"StorageApiToken", "StorageStack"}, (
                f"{method} {path}: unexpected security {names}"
            )
        else:
            assert (
                "security" not in operation
            ), f"{method} {path}: public route must not require credentials"


def _parameter(schema: dict, path: str, method: str, name: str) -> dict:
    for parameter in schema["paths"][path][method].get("parameters", []):
        if parameter["name"] == name:
            return parameter
    raise AssertionError(f"{method.upper()} {path} does not document {name!r}")


def test_openapi_documents_the_versions_format_query_parameter(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    parameter = _parameter(schema, "/a/{artifact_id}/versions", "get", "format")
    assert parameter["in"] == "query"
    assert parameter["required"] is False
    assert parameter["schema"]["default"] == "json"
    assert parameter["schema"]["enum"] == ["json", "html"]
    assert "html" in parameter["description"]


def test_openapi_documents_the_diff_format_query_parameter(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    parameter = _parameter(schema, "/a/{artifact_id}/diff/{spec}", "get", "format")
    assert parameter["in"] == "query"
    assert parameter["schema"]["default"] == "html"
    assert parameter["schema"]["enum"] == ["html", "unified", "json"]
    assert "unified" in parameter["description"]


def test_openapi_documents_the_path_parameters(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()

    spec = _parameter(schema, "/a/{artifact_id}/diff/{spec}", "get", "spec")
    assert spec["in"] == "path"
    assert "OLD..NEW" in spec["description"]

    artifact_id = _parameter(schema, "/a/{artifact_id}", "get", "artifact_id")
    assert "capability" in artifact_id["description"]

    version = _parameter(
        schema, "/a/{artifact_id}/v/{version}", "get", "version"
    )
    assert "version" in version["description"].lower()


def test_openapi_declares_non_json_response_content_types(api: Api) -> None:
    """HTML pages, markdown and the unified diff must not look like JSON."""
    schema = api.client.get("/openapi.json").json()

    def content(path: str, method: str, code: str) -> set[str]:
        return set(
            schema["paths"][path][method]["responses"][code].get("content", {})
        )

    assert "text/html" in content("/", "get", "200")
    assert "text/html" in content("/admin", "get", "200")
    assert "text/plain" in content("/", "post", "200")
    assert any(
        media.startswith("text/markdown") for media in content("/skill", "get", "200")
    )
    assert any(
        media.startswith("text/markdown") for media in content("/agent", "get", "200")
    )
    assert "text/html" in content("/a/{artifact_id}", "get", "200")
    diff_content = content("/a/{artifact_id}/diff/{spec}", "get", "200")
    assert {"text/html", "text/plain", "application/json"} <= diff_content


def test_openapi_request_bodies_carry_examples_and_field_descriptions(
    api: Api,
) -> None:
    schema = api.client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    for name in (
        "PublishBody",
        "UpdateBody",
        "VersionBody",
        "HeadBody",
        "CommentBody",
        "ReplyBody",
        "ResolveBody",
    ):
        model = schemas[name]
        assert model.get("examples"), f"{name}: no request-body example"
        for field, definition in model["properties"].items():
            assert definition.get(
                "description", ""
            ).strip(), f"{name}.{field}: no description"


def test_openapi_unlock_form_field_is_documented(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    body = schema["paths"]["/a/{artifact_id}/unlock"]["post"]["requestBody"]
    assert "application/x-www-form-urlencoded" in body["content"]
    form_schema_ref = body["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["$ref"].rsplit("/", 1)[-1]
    form_schema = schema["components"]["schemas"][form_schema_ref]
    assert form_schema["properties"]["password"]["description"].strip()


# --------------------------------------------------------------------------
# Public origin behind the platform proxy
# --------------------------------------------------------------------------

PROXY_HEADERS = {
    "X-Forwarded-Host": "artifact-hub.example.com",
    "X-Forwarded-Proto": "https",
}


@pytest.fixture
def trusted_proxy(api: Api, monkeypatch) -> Api:
    """``api`` with HUB_TRUST_FORWARDED_HEADERS on.

    Forwarded headers are ignored by default (any client can forge them), so
    every test that expects them to name the public origin has to opt in the
    way a proxied deployment does.
    """
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, trust_forwarded_headers=True),
    )
    return api


def test_forwarded_headers_are_ignored_unless_trusted(api: Api) -> None:
    """Default deployment: a forged X-Forwarded-Host must not be honored."""
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"

    published = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello"},
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
    )
    assert published.status_code == 201
    body = published.json()
    assert body["url"] == f"https://testserver/a/{body['id']}"
    assert "artifact-hub.example.com" not in published.text


def test_trailing_slash_redirect_uses_the_forwarded_origin(
    trusted_proxy: Api,
) -> None:
    """The 307 must not leak the internal cluster hostname (see issue)."""
    api = trusted_proxy
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith(f"https://artifact-hub.example.com/a/{artifact_id}")
    assert "cluster.local" not in location
    assert "testserver" not in location


def test_api_trailing_slash_redirect_uses_the_forwarded_origin(
    trusted_proxy: Api,
) -> None:
    api = trusted_proxy
    resp = api.client.get(
        "/api/artifacts/",
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
        follow_redirects=False,
    )
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://artifact-hub.example.com/api/artifacts")
    assert "cluster.local" not in location
    assert "testserver" not in location


def test_public_base_url_wins_over_forwarded_headers_in_redirects(
    api: Api, monkeypatch
) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            api.settings,
            public_base_url="https://hub.example.org",
            trust_forwarded_headers=True,
        ),
    )
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/", headers=PROXY_HEADERS, follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith(
        f"https://hub.example.org/a/{artifact_id}"
    )


def test_redirect_without_forwarded_headers_is_unchanged(api: Api) -> None:
    """Regression: no proxy headers, no public base URL — behavior as before."""
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(f"/a/{artifact_id}/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"


def test_malformed_forwarded_host_is_ignored(trusted_proxy: Api) -> None:
    """A broken forwarded value must never become the redirect target."""
    api = trusted_proxy
    artifact_id = _publish_markdown(api, "# Hello")
    resp = api.client.get(
        f"/a/{artifact_id}/",
        headers={"X-Forwarded-Host": "  ", "X-Forwarded-Proto": "  "},
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://testserver/a/{artifact_id}"


def test_payload_urls_still_use_the_forwarded_host(trusted_proxy: Api) -> None:
    """Regression: the JSON URLs that were already correct stay correct."""
    api = trusted_proxy
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hello"},
        headers={**AUTH_HEADERS, **PROXY_HEADERS},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == f"https://artifact-hub.example.com/a/{body['id']}"
    assert body["versions_url"] == f"{body['url']}/versions"


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


def test_read_artifact_sandboxes_the_document_in_an_iframe(api: Api) -> None:
    """The artifact must never execute on the hub's own origin (sec-web)."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    raw = api.client.get(f"/a/{artifact_id}/raw")
    page = api.client.get(f"/a/{artifact_id}")
    assert page.status_code == 200

    # A sandbox without allow-same-origin: the document gets an opaque origin.
    assert 'sandbox="allow-scripts allow-popups allow-forms allow-downloads"' in (
        page.text
    )
    assert "allow-same-origin" not in page.text
    assert page.headers["content-security-policy"] == "frame-ancestors 'self'"

    # The whole document is inside srcdoc, html-escaped...
    assert f'srcdoc="{html.escape(raw.text, quote=True)}"' in page.text
    # ...so none of the artifact's own markup is live at top level.
    assert "<script" not in page.text
    assert "<h1" not in page.text
    assert "&lt;h1" in page.text


def test_read_version_is_sandboxed_too(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Only version")
    page = api.client.get(f"/a/{artifact_id}/v/1")
    assert page.status_code == 200
    assert "sandbox=" in page.text
    assert "allow-same-origin" not in page.text
    assert "<script" not in page.text


def test_raw_is_unchanged_byte_exact_html(api: Api) -> None:
    """/raw stays the artifact itself: machines get the bytes, not a wrapper."""
    artifact_id = _publish_markdown(api, "# Title\n\nRaw check")
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    raw = api.client.get(f"/a/{artifact_id}/raw")
    assert raw.status_code == 200
    assert raw.text == envelope.html
    assert "srcdoc" not in raw.text


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
# Private-repository publishing (transient git credentials)
# --------------------------------------------------------------------------

#: Token used by the private-repo tests; distinctive so a substring search for
#: it across a whole serialized response/envelope is meaningful.
GIT_TOKEN = "ghp_TestOnlyPrivateRepoToken0001"


def test_publish_git_token_without_git_url_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts", json={"git_token": GIT_TOKEN}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "git_url" in detail and "git_token" in detail


def test_publish_git_username_with_markdown_is_422(api: Api) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hi", "git_username": "someone"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "git_url" in resp.json()["detail"]


def test_put_git_token_without_git_url_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Original")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Updated", "git_token": GIT_TOKEN},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "git_url" in resp.json()["detail"]


def _stub_build_from_git(monkeypatch) -> dict[str, Any]:
    """Replace the git builder with a capture stub; returns the captured kwargs.

    A real clone is not available in tests, so the seam under test is the
    hand-off from the API layer into the builder: the token must arrive there,
    and must appear nowhere else afterwards.
    """
    captured: dict[str, Any] = {}

    def fake_build_from_git(
        git_url, ref, path, title, settings, *, git_username=None, git_token=None
    ):
        captured.update(
            git_url=git_url,
            ref=ref,
            path=path,
            title=title,
            git_username=git_username,
            git_token=git_token,
        )
        return BuiltArtifact(
            html="<html><body><h1>From a private repo</h1></body></html>",
            title="From a private repo",
            source_type="git-html",
            git_commit="0123456789abcdef0123456789abcdef01234567",
        )

    monkeypatch.setattr(main.builder, "build_from_git", fake_build_from_git)
    return captured


def test_publish_private_git_passes_credentials_to_the_builder(
    api: Api, monkeypatch
) -> None:
    captured = _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_ref": "main",
            "git_path": "docs/report.html",
            "git_username": "deploy-user",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert captured["git_token"] == GIT_TOKEN
    assert captured["git_username"] == "deploy-user"
    assert captured["git_url"] == "https://github.com/org/private-repo"
    assert captured["ref"] == "main"
    assert captured["path"] == "docs/report.html"


def test_publish_private_git_never_stores_or_echoes_the_token(
    api: Api, monkeypatch
) -> None:
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_username": "deploy-user",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert GIT_TOKEN not in resp.text

    artifact_id = resp.json()["id"]
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None

    serialized = envelope.to_json().decode("utf-8")
    assert GIT_TOKEN not in serialized
    assert "deploy-user" not in serialized
    assert "git_token" not in serialized
    assert envelope.source["git"] == {
        "url": "https://github.com/org/private-repo",
        "ref": None,
        "path": None,
        "commit": "0123456789abcdef0123456789abcdef01234567",
        # Records *that* a credential was needed, never the credential.
        "private": True,
    }

    # The same for every public read surface of the artifact.
    for path in (
        f"/a/{artifact_id}",
        f"/a/{artifact_id}/raw",
        f"/a/{artifact_id}/meta",
    ):
        read = api.client.get(path)
        assert GIT_TOKEN not in read.text, path

    # ...and for the canonical copy uploaded into the author's own project.
    assert api.canonical_calls
    assert GIT_TOKEN.encode() not in api.canonical_calls[-1]["content"]


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
        max_versions=api.settings.max_versions,
    )
    count = second_store.hydrate()
    assert count >= 1

    envelope = second_store.get_head(artifact_id)
    assert envelope is not None
    assert "Survives restart" in envelope.html
    assert second_store.get_meta(artifact_id) is not None


# --------------------------------------------------------------------------
# Shell pages
# --------------------------------------------------------------------------


def test_landing_page_advertises_repo_and_version(api: Api) -> None:
    resp = api.client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert main.GITHUB_REPO_URL in resp.text
    assert main.SERVICE_VERSION in resp.text
    assert "kbc-artifact-hub" in resp.text


def test_landing_page_links_the_admin_studio_and_the_agent_definition(
    api: Api,
) -> None:
    text = api.client.get("/").text
    assert "/admin" in text
    assert "/agent" in text
    assert "Admin studio" in text
    # The install one-liner from the "for agents" section.
    assert "~/.claude/agents/artifact-hub.md" in text


def test_admin_page_is_html_and_self_describing(api: Api) -> None:
    resp = api.client.get("/admin")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Admin studio" in resp.text


def test_admin_page_keeps_credentials_in_the_tab_only(api: Api) -> None:
    """The studio must never ship a token, and must never reach for localStorage.

    The page is public, so a leaked hub token would be catastrophic; and the
    whole security story rests on the visitor's own token living in
    sessionStorage (per-tab, cleared on close) rather than anywhere durable.
    Both are asserted mechanically so a later edit cannot quietly regress them.
    """
    text = api.client.get("/admin").text
    assert api.settings.hub_storage_token not in text
    assert "good-token" not in text
    assert "localStorage" not in text
    assert "sessionStorage" in text
    assert "hub_admin_auth" in text
    # The promise made to the visitor on the sign-in card.
    assert "Your token stays in this browser tab" in text


def test_admin_page_offers_every_stack_alias_and_a_custom_url(api: Api) -> None:
    text = api.client.get("/admin").text
    for alias in ("us", "gcp-us", "eu", "azure-eu", "gcp-eu"):
        assert f'<option value="{alias}">' in text
    assert '<option value="__custom__">' in text


# --------------------------------------------------------------------------
# Versioning: owner updates
# --------------------------------------------------------------------------


def test_put_by_owner_creates_version_two_and_both_are_listed(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    put = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Two"},
        headers=AUTH_HEADERS,
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    assert put.json()["head_version"] == 2

    listing = api.client.get(f"/a/{artifact_id}/versions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["head_version"] == 2
    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["is_head"] is True
    assert body["versions"][1]["is_head"] is False
    assert body["versions"][0]["url"] == f"https://testserver/a/{artifact_id}/v/2"

    assert "Two" in api.client.get(f"/a/{artifact_id}/v/2").text
    assert "One" in api.client.get(f"/a/{artifact_id}/v/1").text


def test_put_title_without_content_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}", json={"title": "Renamed"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422
    assert "title" in resp.json()["detail"]


def test_owner_submitted_version_is_live_immediately(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _submit_version(api, artifact_id, "# Owner two", note="second cut")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "live"
    assert body["note"] == "second cut"
    assert body["url"] == f"https://testserver/a/{artifact_id}/v/2"
    assert "Owner two" in api.client.get(f"/a/{artifact_id}").text


# --------------------------------------------------------------------------
# Versioning: moderated proposals
# --------------------------------------------------------------------------


def test_foreign_version_without_accept_versions_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Closed")
    resp = _submit_version(
        api, artifact_id, "# Hijack", headers=OTHER_AUTH_HEADERS
    )
    assert resp.status_code == 403
    assert "accept_versions" in resp.json()["detail"]


def test_foreign_proposal_is_private_until_promoted(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)

    proposal = _submit_version(
        api,
        artifact_id,
        "# Contributed two",
        note="typo fix",
        headers=OTHER_AUTH_HEADERS,
    )
    assert proposal.status_code == 201, proposal.text
    assert proposal.json()["status"] == "proposed"
    assert proposal.json()["version"] == 2

    # The head is untouched: /a/{id} still serves version 1.
    head = api.client.get(f"/a/{artifact_id}")
    assert head.status_code == 200
    assert "Open one" in head.text
    assert "Contributed two" not in head.text

    # The proposal's *content* is private...
    anonymous = api.client.get(f"/a/{artifact_id}/v/2")
    assert anonymous.status_code == 403
    assert "proposed" in anonymous.json()["error"]

    # ...to everyone but its author and the artifact owner.
    author = api.client.get(f"/a/{artifact_id}/v/2", headers=OTHER_AUTH_HEADERS)
    assert author.status_code == 200
    assert "Contributed two" in author.text

    owner = api.client.get(f"/a/{artifact_id}/v/2", headers=AUTH_HEADERS)
    assert owner.status_code == 200

    # Its *metadata* is listed for anyone holding the capability URL.
    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    proposed = next(v for v in listing["versions"] if v["version"] == 2)
    assert proposed["status"] == "proposed"
    assert proposed["note"] == "typo fix"
    assert proposed["author"]["project_id"] == 999
    assert listing["accept_versions"] is True


def test_promote_is_owner_only_and_moves_the_head(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Promoted two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    foreign = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote",
        headers=OTHER_AUTH_HEADERS,
    )
    assert foreign.status_code == 403

    promoted = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote", headers=AUTH_HEADERS
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "live"
    assert promoted.json()["head_version"] == 2

    head = api.client.get(f"/a/{artifact_id}")
    assert head.status_code == 200
    assert "Promoted two" in head.text

    again = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/2/promote", headers=AUTH_HEADERS
    )
    assert again.status_code == 409

    unknown = api.client.post(
        f"/api/artifacts/{artifact_id}/versions/77/promote", headers=AUTH_HEADERS
    )
    assert unknown.status_code == 404


def test_contributor_withdraws_own_proposal(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Withdrawn", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    withdrawn = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/2", headers=OTHER_AUTH_HEADERS
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["deleted"] is True

    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    assert [v["version"] for v in listing["versions"]] == [1]


def test_contributor_cannot_delete_someone_elses_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=OTHER_AUTH_HEADERS
    )
    assert resp.status_code == 403


def test_owner_cannot_delete_the_only_live_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Only one")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 409
    assert "only live version" in resp.json()["error"]
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_version_submissions_are_rate_limited_per_day(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_versions_per_day=2),
    )

    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    assert _submit_version(api, artifact_id, "# Three").status_code == 201

    limited = _submit_version(api, artifact_id, "# Four")
    assert limited.status_code == 429
    assert limited.json()["limit"] == 2


# --------------------------------------------------------------------------
# Diffs
# --------------------------------------------------------------------------


def _publish_and_update(api: Api) -> str:
    artifact_id = _publish_markdown(api, "# Report\n\nOld line\n")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Report\n\nNew line\n"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return artifact_id


def test_diff_json_reports_stats(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["from"] == 1 and body["to"] == 2
    assert body["kind"] == "markdown"
    assert body["stats"]["added"] >= 1
    assert body["stats"]["removed"] >= 1
    assert "New line" in body["unified"]


def test_diff_html_and_unified_render(api: Api) -> None:
    artifact_id = _publish_and_update(api)

    html_diff = api.client.get(f"/a/{artifact_id}/diff/1..2")
    assert html_diff.status_code == 200
    assert html_diff.headers["content-type"].startswith("text/html")
    assert "New line" in html_diff.text

    unified = api.client.get(f"/a/{artifact_id}/diff/1..2?format=unified")
    assert unified.status_code == 200
    assert unified.headers["content-type"].startswith("text/plain")
    assert "+New line" in unified.text


def test_diff_unknown_format_is_400(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    resp = api.client.get(f"/a/{artifact_id}/diff/1..2?format=pdf")
    assert resp.status_code == 400
    assert "format" in resp.json()["error"].lower()


def test_diff_malformed_spec_is_400_and_missing_version_is_404(api: Api) -> None:
    artifact_id = _publish_and_update(api)
    assert api.client.get(f"/a/{artifact_id}/diff/1-2").status_code == 400
    assert api.client.get(f"/a/{artifact_id}/diff/1..9").status_code == 404


# --------------------------------------------------------------------------
# Head pointer
# --------------------------------------------------------------------------


def test_pinning_the_head_freezes_what_is_served(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Pinned one")
    assert _submit_version(api, artifact_id, "# Latest two").status_code == 201
    assert "Latest two" in api.client.get(f"/a/{artifact_id}").text

    pinned = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 1},
        headers=AUTH_HEADERS,
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == {
        "id": artifact_id,
        "head_mode": "pinned",
        "head_version_served": 1,
    }

    served = api.client.get(f"/a/{artifact_id}")
    assert "Pinned one" in served.text
    assert "Latest two" not in served.text

    listing = api.client.get(f"/a/{artifact_id}/versions").json()
    assert listing["head_version"] == 1
    by_number = {v["version"]: v for v in listing["versions"]}
    assert by_number[1]["is_head"] is True
    assert by_number[2]["is_head"] is False

    back = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "latest"},
        headers=AUTH_HEADERS,
    )
    assert back.status_code == 200
    assert back.json()["head_version_served"] == 2


def test_head_rejects_unknown_mode_and_missing_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    bad_mode = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "whatever"},
        headers=AUTH_HEADERS,
    )
    assert bad_mode.status_code == 422

    no_version = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned"},
        headers=AUTH_HEADERS,
    )
    assert no_version.status_code == 422

    missing = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 42},
        headers=AUTH_HEADERS,
    )
    assert missing.status_code == 422

    foreign = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "latest"},
        headers=OTHER_AUTH_HEADERS,
    )
    assert foreign.status_code == 403


def test_head_cannot_be_pinned_to_a_proposal(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Open one", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Proposed two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}/head",
        json={"mode": "pinned", "version": 2},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert "proposed" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Version history page
# --------------------------------------------------------------------------


def test_versions_html_page_lists_every_version(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert _submit_version(api, artifact_id, "# Two", note="second cut").status_code == 201

    resp = api.client.get(f"/a/{artifact_id}/versions?format=html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"

    text = resp.text
    assert "Version history" in text
    assert ">v1<" in text and ">v2<" in text
    assert f"/a/{artifact_id}/v/1" in text
    assert f"/a/{artifact_id}/v/2" in text
    assert f"/a/{artifact_id}/diff/1..2" in text
    assert "second cut" in text
    # accept_versions is on, so the page teaches how to contribute.
    assert f"/api/artifacts/{artifact_id}/versions" in text


def test_versions_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/versions").status_code == 404
    assert api.client.get("/a/nope/v/1").status_code == 404


def test_protected_artifact_gates_versions_and_diff(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret one", password="hunter2")
    assert _submit_version(api, artifact_id, "# Secret two").status_code == 201

    assert api.client.get(f"/a/{artifact_id}/versions").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/diff/1..2").status_code == 401

    unlocked = {"X-Artifact-Password": "hunter2"}
    assert api.client.get(f"/a/{artifact_id}/versions", headers=unlocked).status_code == 200
    assert api.client.get(f"/a/{artifact_id}/diff/1..2", headers=unlocked).status_code == 200


# --------------------------------------------------------------------------
# Metadata with versions
# --------------------------------------------------------------------------


def test_meta_reports_version_counts(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    assert (
        _submit_version(
            api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS
        ).status_code
        == 201
    )

    body = api.client.get(f"/a/{artifact_id}/meta").json()
    _assert_no_sensitive_keys(body)
    assert body["head_version"] == 1
    assert body["versions_count"] == 2
    assert body["proposed_count"] == 1
    assert body["accept_versions"] is True
    assert body["versions_url"] == f"https://testserver/a/{artifact_id}/versions"


# --------------------------------------------------------------------------
# Inline comments
# --------------------------------------------------------------------------

#: Owner keys of the two fake tokens, as src.auth.Owner.key builds them from
#: the "us" alias (https://connection.keboola.com).
OWNER_KEY = "123@connection.keboola.com"
OTHER_KEY = "999@connection.keboola.com"


def _comment(
    api: Api,
    artifact_id: str,
    *,
    version: int = 1,
    exact: str = "Body text",
    prefix: str = "",
    suffix: str = "",
    body: str = "Is this still true?",
    headers: dict[str, str] = AUTH_HEADERS,
):
    """POST one comment thread; returns the raw response."""
    return api.client.post(
        f"/api/artifacts/{artifact_id}/comments",
        json={
            "version": version,
            "exact": exact,
            "prefix": prefix,
            "suffix": suffix,
            "body": body,
        },
        headers=headers,
    )


def _policy(
    api: Api, artifact_id: str, headers: dict[str, str] = AUTH_HEADERS, **fields: Any
):
    """PUT artifact-level policy fields (comments_mode, status, ...)."""
    return api.client.put(
        f"/api/artifacts/{artifact_id}", json=fields, headers=headers
    )


def _assert_no_credentials(obj: Any) -> None:
    """Recursively assert no internal identity field is exposed publicly.

    ``stack_url`` would hand a reader the address of the author's stack and
    ``key`` the exact string an allowlist is matched against; the public
    projection reduces both to a bare stack hostname.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key not in ("stack_url", "key"), f"found internal key {key!r}"
            _assert_no_credentials(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_credentials(item)


def test_comment_create_and_list_round_trips_the_selector(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")

    created = _comment(
        api,
        artifact_id,
        exact="Body text",
        prefix="Title\n\n",
        suffix=" here.",
        body="Which body?",
    )
    assert created.status_code == 201, created.text
    thread = created.json()
    assert thread["thread_id"] == thread["id"]
    assert thread["artifact_id"] == artifact_id
    assert thread["version"] == 1
    assert thread["selector"] == {
        "exact": "Body text",
        "prefix": "Title\n\n",
        "suffix": " here.",
    }
    assert thread["body"] == "Which body?"
    assert thread["resolved"] is False
    assert thread["replies"] == []
    assert thread["author"] == {
        "project_id": 123,
        "project_name": "Test",
        "stack_host": "connection.keboola.com",
    }
    _assert_no_credentials(thread)

    listing = api.client.get(f"/a/{artifact_id}/comments")
    assert listing.status_code == 200
    body = listing.json()
    assert body["id"] == artifact_id
    assert body["comments_mode"] == "anyone"
    assert body["status"] == "draft"
    assert [t["id"] for t in body["threads"]] == [thread["id"]]
    assert body["threads"][0]["selector"]["exact"] == "Body text"
    _assert_no_credentials(body)


def test_comments_are_listed_oldest_first(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One\n\nTwo three")
    # Thread ids are random and the real clock has second precision, so the
    # ordering is only observable with distinct, controlled timestamps.
    stamps = iter(["2026-01-02T09:00:00+00:00", "2026-01-01T09:00:00+00:00"])
    monkeypatch.setattr(main, "_now", lambda: next(stamps))

    newer = _comment(api, artifact_id, exact="One").json()["id"]
    older = _comment(api, artifact_id, exact="Two").json()["id"]

    ids = [t["id"] for t in api.client.get(f"/a/{artifact_id}/comments").json()["threads"]]
    assert ids == [older, newer]


def test_comments_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/comments").status_code == 404
    assert _comment(api, "nope").status_code == 404


def test_comments_mode_off_closes_the_artifact_to_other_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Closed")
    assert _policy(api, artifact_id, comments_mode="off").status_code == 200

    stranger = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert stranger.status_code == 403
    assert "closed" in stranger.json()["detail"]

    # Only "final" locks the owner out of their own artifact; "off" does not.
    assert _comment(api, artifact_id).status_code == 201


def test_comments_allowlist_admits_only_listed_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Allowlisted")
    assert (
        _policy(
            api,
            artifact_id,
            comments_mode="allowlist",
            contributors=[OTHER_KEY],
        ).status_code
        == 200
    )

    contributor = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert contributor.status_code == 201, contributor.text
    # The owner is never locked out of their own artifact.
    assert _comment(api, artifact_id).status_code == 201

    assert _policy(api, artifact_id, contributors=[]).status_code == 200
    stranger = _comment(api, artifact_id, headers=OTHER_AUTH_HEADERS)
    assert stranger.status_code == 403
    assert "allowlist" in stranger.json()["detail"]


def test_contributor_keys_are_validated(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Allowlisted")
    bad = _policy(api, artifact_id, contributors=["not-a-key"])
    assert bad.status_code == 422
    assert "project key" in bad.json()["detail"]

    too_many = _policy(
        api, artifact_id, contributors=[f"{n}@connection.keboola.com" for n in range(51)]
    )
    assert too_many.status_code == 422
    assert "too many contributors" in too_many.json()["detail"]


def test_accept_versions_and_mode_together_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _policy(api, artifact_id, accept_versions=True, accept_versions_mode="anyone")
    assert resp.status_code == 422
    assert "accept_versions_mode" in resp.json()["detail"]


def test_versions_allowlist_admits_only_listed_projects(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert (
        _policy(
            api,
            artifact_id,
            accept_versions_mode="allowlist",
            contributors=[OTHER_KEY],
        ).status_code
        == 200
    )
    allowed = _submit_version(api, artifact_id, "# Two", headers=OTHER_AUTH_HEADERS)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["status"] == "proposed"

    assert _policy(api, artifact_id, contributors=[]).status_code == 200
    denied = _submit_version(api, artifact_id, "# Three", headers=OTHER_AUTH_HEADERS)
    assert denied.status_code == 403


def test_comment_on_unknown_version_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = _comment(api, artifact_id, version=42)
    assert resp.status_code == 422
    assert "version 42" in resp.json()["detail"]


def test_comment_body_and_quote_validation_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")

    empty_body = _comment(api, artifact_id, body="   ")
    assert empty_body.status_code == 422
    assert "body" in empty_body.json()["detail"].lower()

    empty_quote = _comment(api, artifact_id, exact="   ")
    assert empty_quote.status_code == 422
    assert "selection" in empty_quote.json()["detail"].lower()

    long_quote = _comment(api, artifact_id, exact="x" * 2001)
    assert long_quote.status_code == 422
    assert "too long" in long_quote.json()["detail"]


def test_comments_are_rate_limited_per_day(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_comments_per_day=2)
    )

    assert _comment(api, artifact_id, exact="One").status_code == 201
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]

    limited = _comment(api, artifact_id, exact="One")
    assert limited.status_code == 429
    assert limited.json()["limit"] == 2

    # Replies share the same bucket.
    reply = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "also blocked"},
        headers=AUTH_HEADERS,
    )
    assert reply.status_code == 429


def test_reply_appends_to_the_thread(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]

    replied = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "Answering."},
        headers=OTHER_AUTH_HEADERS,
    )
    assert replied.status_code == 201, replied.text
    thread = replied.json()
    assert len(thread["replies"]) == 1
    assert thread["replies"][0]["body"] == "Answering."
    assert thread["replies"][0]["author"]["project_id"] == 999
    _assert_no_credentials(thread)

    listed = api.client.get(f"/a/{artifact_id}/comments").json()["threads"][0]
    assert len(listed["replies"]) == 1


def test_reply_to_unknown_thread_is_404(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/nope/replies",
        json={"body": "hello"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404


def test_empty_reply_body_is_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": " "},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_resolve_then_conflict_then_reopen(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One", accept_versions=True)
    thread_id = _comment(
        api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    path = f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve"

    # The artifact owner may resolve a thread they did not write.
    resolved = api.client.post(path, headers=AUTH_HEADERS)
    assert resolved.status_code == 200, resolved.text
    thread = resolved.json()
    assert thread["resolved"] is True
    assert thread["resolved_by"]["project_id"] == 123
    _assert_no_credentials(thread)

    again = api.client.post(path, headers=AUTH_HEADERS)
    assert again.status_code == 409
    assert "already resolved" in again.json()["error"]

    # The thread's own author may reopen it.
    reopened = api.client.post(
        path, json={"resolved": False}, headers=OTHER_AUTH_HEADERS
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["resolved"] is False
    assert reopened.json()["resolved_by"] is None

    already_open = api.client.post(
        path, json={"resolved": False}, headers=AUTH_HEADERS
    )
    assert already_open.status_code == 409
    assert "already open" in already_open.json()["error"]


def test_resolve_by_an_unrelated_project_is_403(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    thread_id = _comment(api, artifact_id, exact="One").json()["id"]
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
        headers=OTHER_AUTH_HEADERS,
    )
    assert resp.status_code == 403


def test_delete_thread_by_author_by_owner_and_by_a_stranger(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One\n\nTwo")

    # The author withdraws their own thread.
    own = _comment(
        api, artifact_id, exact="One", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    withdrawn = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{own}", headers=OTHER_AUTH_HEADERS
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["deleted"] is True

    # The artifact owner moderates someone else's thread.
    foreign = _comment(
        api, artifact_id, exact="Two", headers=OTHER_AUTH_HEADERS
    ).json()["id"]
    moderated = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{foreign}", headers=AUTH_HEADERS
    )
    assert moderated.status_code == 200

    # A project that is neither may not.
    owners_own = _comment(api, artifact_id, exact="One").json()["id"]
    stranger = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/{owners_own}",
        headers=OTHER_AUTH_HEADERS,
    )
    assert stranger.status_code == 403

    remaining = api.client.get(f"/a/{artifact_id}/comments").json()["threads"]
    assert [t["id"] for t in remaining] == [owners_own]


def test_delete_unknown_thread_is_404(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/comments/nope", headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


def test_deleting_an_artifact_removes_its_comment_threads(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    _comment(api, artifact_id, exact="One")
    _comment(api, artifact_id, exact="One")

    deleted = api.client.delete(f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS)
    assert deleted.status_code == 200
    assert deleted.json()["comment_threads_deleted"] == 2

    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 404
    # Nothing is left behind in Storage either.
    assert main.app.state.comments.list_for(artifact_id) == []


# --------------------------------------------------------------------------
# Final status
# --------------------------------------------------------------------------


def test_final_status_freezes_versions_comments_and_content(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Draft", accept_versions=True)

    final = _policy(api, artifact_id, status="final")
    assert final.status_code == 200, final.text
    assert final.json()["artifact_status"] == "final"
    assert api.client.get(f"/a/{artifact_id}/comments").json()["status"] == "final"

    version = _submit_version(api, artifact_id, "# Frozen")
    assert version.status_code == 409
    assert version.json()["error"] == "document is final"

    foreign_version = _submit_version(
        api, artifact_id, "# Frozen", headers=OTHER_AUTH_HEADERS
    )
    assert foreign_version.status_code == 409

    comment = _comment(api, artifact_id, exact="Draft")
    assert comment.status_code == 409
    assert comment.json()["error"] == "document is final"

    content = _policy(api, artifact_id, markdown="# Frozen")
    assert content.status_code == 409

    # Artifact-level settings still work — that is how the owner reopens it.
    reopened = _policy(api, artifact_id, status="draft")
    assert reopened.status_code == 200
    assert reopened.json()["artifact_status"] == "draft"
    assert _submit_version(api, artifact_id, "# Thawed").status_code == 201
    assert _comment(api, artifact_id, exact="Thawed").status_code == 201


def test_final_artifact_can_be_reopened_in_the_same_call_as_new_content(
    api: Api,
) -> None:
    artifact_id = _publish_markdown(api, "# Draft")
    assert _policy(api, artifact_id, status="final").status_code == 200

    resp = _policy(api, artifact_id, status="draft", markdown="# Reopened")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["artifact_status"] == "draft"


def test_unknown_policy_values_are_422(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _policy(api, artifact_id, status="archived").status_code == 422
    assert _policy(api, artifact_id, comments_mode="sometimes").status_code == 422
    assert _policy(api, artifact_id, accept_versions_mode="maybe").status_code == 422


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


def test_export_markdown_returns_the_source_as_an_attachment(api: Api) -> None:
    markdown = "# Q3 review\n\nShipped the new ingest path.\n"
    artifact_id = _publish_markdown(api, markdown, title="Q3 review")

    resp = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="q3-review.md"'
    )
    assert resp.text == markdown


def test_export_markdown_of_an_html_artifact_returns_html(api: Api) -> None:
    document = "<html><head><title>Notes</title></head><body><p>Hi</p></body></html>"
    published = api.client.post(
        "/api/artifacts", json={"html": document, "title": "Notes"}, headers=AUTH_HEADERS
    )
    assert published.status_code == 201
    artifact_id = published.json()["id"]

    resp = api.client.get(f"/a/{artifact_id}/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-disposition"] == 'attachment; filename="notes.html"'
    assert resp.text == document


def test_export_markdown_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/export/markdown").status_code == 404
    assert api.client.get("/a/nope/export/vault").status_code == 404


def test_export_vault_is_a_zip_holding_the_whole_story(api: Api) -> None:
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

    resp = api.client.get(f"/a/{artifact_id}/export/vault")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == (
        'attachment; filename="q3-review-vault.zip"'
    )

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = archive.namelist()
        assert "q3-review/INDEX.md" in names
        assert "q3-review/document.md" in names
        assert "q3-review/reasoning.md" in names
        # Live versions only: the anonymous export must not leak the
        # still-moderated proposal (v3).
        assert "q3-review/versions/v1.md" in names
        assert "q3-review/versions/v2.md" in names
        assert "q3-review/versions/v3.md" not in names
        assert any(name.startswith("q3-review/comments/") for name in names)
        assert "New line" in archive.read("q3-review/document.md").decode("utf-8")

    # The owner's authenticated download carries the proposal as well.
    owner_resp = api.client.get(
        f"/a/{artifact_id}/export/vault", headers=AUTH_HEADERS
    )
    with zipfile.ZipFile(io.BytesIO(owner_resp.content)) as archive:
        assert "q3-review/versions/v3.md" in archive.namelist()


def test_exports_honour_the_password_gate(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret\n\nBody", password="hunter2")

    assert api.client.get(f"/a/{artifact_id}/export/markdown").status_code == 401
    assert api.client.get(f"/a/{artifact_id}/export/vault").status_code == 401

    unlocked = {"X-Artifact-Password": "hunter2"}
    assert (
        api.client.get(
            f"/a/{artifact_id}/export/markdown", headers=unlocked
        ).status_code
        == 200
    )
    vault = api.client.get(f"/a/{artifact_id}/export/vault", headers=unlocked)
    assert vault.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(vault.content)).namelist()


def test_comments_honour_the_password_gate(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret\n\nBody", password="hunter2")
    assert api.client.get(f"/a/{artifact_id}/comments").status_code == 401
    assert (
        api.client.get(
            f"/a/{artifact_id}/comments", headers={"X-Artifact-Password": "hunter2"}
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------
# Review UI
# --------------------------------------------------------------------------


def test_review_page_sandboxes_the_artifact_and_keeps_the_token_in_the_tab(
    api: Api,
) -> None:
    """The security story of the review UI, asserted mechanically.

    The artifact renders in a srcdoc iframe *without* allow-same-origin, so its
    scripts run in an opaque origin; and the visitor's token lives in
    sessionStorage (shared with /admin), never in anything durable.
    """
    artifact_id = _publish_markdown(api, "# Reviewed\n\nBody text")
    resp = api.client.get(f"/a/{artifact_id}/review")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"

    text = resp.text
    assert 'sandbox="allow-scripts allow-popups"' in text
    sandboxes = re.findall(r'sandbox="([^"]*)"', text)
    assert sandboxes, "the page must render the artifact in a sandboxed iframe"
    for value in sandboxes:
        assert "allow-same-origin" not in value, value
    assert "localStorage" not in text
    assert "sessionStorage" in text
    assert "hub_admin_auth" in text
    assert api.settings.hub_storage_token not in text
    assert "good-token" not in text
    # The annotation script and the postMessage protocol it speaks.
    assert "ah-select" in text
    assert "ah-anchors" in text
    assert "ah-anchored" in text


def test_review_page_of_unknown_artifact_is_404(api: Api) -> None:
    assert api.client.get("/a/nope/review").status_code == 404


def test_review_page_of_a_protected_artifact_shows_the_unlock_form(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    locked = api.client.get(f"/a/{artifact_id}/review")
    assert locked.status_code == 401
    assert "Password required" in locked.text

    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock", data={"password": "hunter2"}, follow_redirects=False
    )
    assert unlocked.status_code == 303
    assert api.client.get(f"/a/{artifact_id}/review").status_code == 200


def test_versions_page_and_context_link_the_review_ui(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# One")
    page = api.client.get(f"/a/{artifact_id}/versions?format=html")
    assert f"/a/{artifact_id}/review" in page.text

    paths = {e["path"] for e in api.client.get("/context").json()["endpoints"]}
    assert "/a/{id}/review" in paths
    assert "/a/{id}/comments" in paths
    assert "/a/{id}/export/vault" in paths


def test_admin_studio_offers_a_review_link(api: Api) -> None:
    assert '"/review"' in api.client.get("/admin").text


def test_landing_page_lists_the_phase_three_read_endpoints(api: Api) -> None:
    text = api.client.get("/").text
    for path in ("/review", "/comments", "/export/markdown", "/export/vault"):
        assert path in text


# --------------------------------------------------------------------------
# Restart survival: comments
# --------------------------------------------------------------------------


def test_comment_threads_survive_a_restart(api: Api, tmp_path) -> None:
    """A fresh CommentStore over the same backend must find every thread."""
    artifact_id = _publish_markdown(api, "# One\n\nBody text")
    thread_id = _comment(api, artifact_id, exact="Body text").json()["id"]

    second = CommentStore(
        backend=api.backend,
        cache_dir=tmp_path / "restart-comments",
        cache_max_entries=api.settings.cache_max_entries,
    )
    assert second.hydrate() == 1
    threads = second.list_for(artifact_id)
    assert [t.id for t in threads] == [thread_id]
    assert threads[0].selector.exact == "Body text"


# --------------------------------------------------------------------------
# Hardening: unlock-cookie revocation and password-attempt throttling
# --------------------------------------------------------------------------


def test_unlock_cookie_is_revoked_by_a_password_change(api: Api) -> None:
    """A cookie issued under the old password must stop working (sec-authn)."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")

    unlocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert unlocked.status_code == 303
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    changed = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"password": "new-password"},
        headers=AUTH_HEADERS,
    )
    assert changed.status_code == 200

    # Same browser, same cookie — no longer valid for the new password.
    assert api.client.get(f"/a/{artifact_id}").status_code == 401
    # ...and the new password still unlocks normally.
    again = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "new-password"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_unlock_cookie_is_revoked_by_clearing_the_password(api: Api) -> None:
    """Clearing then re-setting a password must not resurrect an old cookie."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    assert (
        api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "hunter2"},
            follow_redirects=False,
        ).status_code
        == 303
    )

    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"clear_password": True},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    # No password at all: readable by anyone, cookie or not.
    assert api.client.get(f"/a/{artifact_id}").status_code == 200

    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"password": "hunter2"},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    # The re-set password produces a different record, so the old cookie —
    # even for the very same password string — does not verify.
    assert api.client.get(f"/a/{artifact_id}").status_code == 401


def _low_unlock_limit(api: Api, monkeypatch, limit: int = 2) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_unlock_attempts_per_hour=limit),
    )


def test_unlock_form_throttles_failed_attempts(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    for _ in range(2):
        wrong = api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "nope"},
            follow_redirects=False,
        )
        assert wrong.status_code == 401

    blocked = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "nope"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "Too many attempts" in blocked.text

    # The throttle covers the correct password too, once the budget is gone.
    correct = api.client.post(
        f"/a/{artifact_id}/unlock",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert correct.status_code == 429


def test_unlock_succeeds_while_the_budget_lasts(api: Api, monkeypatch) -> None:
    """Only failures count, so an honest reader is never throttled."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    assert (
        api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "nope"},
            follow_redirects=False,
        ).status_code
        == 401
    )
    for _ in range(5):
        ok = api.client.post(
            f"/a/{artifact_id}/unlock",
            data={"password": "hunter2"},
            follow_redirects=False,
        )
        assert ok.status_code == 303


def test_password_header_path_is_throttled_too(api: Api, monkeypatch) -> None:
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=2)

    for _ in range(2):
        assert (
            api.client.get(
                f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
            ).status_code
            == 401
        )

    blocked = api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong"}
    )
    assert blocked.status_code == 429
    assert "too many wrong passwords" in blocked.json()["detail"]


def test_unlock_throttle_buckets_per_client_address(api: Api, monkeypatch) -> None:
    """A different client address gets its own budget (X-Real-IP from nginx)."""
    artifact_id = _publish_markdown(api, "# Secret", password="hunter2")
    _low_unlock_limit(api, monkeypatch, limit=1)

    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.1"},
        ).status_code
        == 401
    )
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.1"},
        ).status_code
        == 429
    )
    # A second address is unaffected.
    assert (
        api.client.get(
            f"/a/{artifact_id}/raw",
            headers={"X-Artifact-Password": "wrong", "X-Real-IP": "10.0.0.2"},
        ).status_code
        == 401
    )


# --------------------------------------------------------------------------
# Hardening: configuration and logging
# --------------------------------------------------------------------------


def test_load_settings_rejects_a_short_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("HUB_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="HUB_SECRET_KEY"):
        load_settings()


def test_load_settings_accepts_a_long_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("HUB_SECRET_KEY", "x" * 32)
    assert load_settings().secret_key == "x" * 32


def test_json_log_formatter_cannot_be_forged_by_a_log_message() -> None:
    """Newlines and quotes in user-controlled text must not forge a record."""
    hostile = 'evil", "level": "CRITICAL", "note": "x\nfake line'
    record = logging.LogRecord(
        name="src.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="published artifact %s",
        args=(hostile,),
        exc_info=None,
    )
    line = main.JSONFormatter().format(record)

    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "src.main"
    assert parsed["message"] == f"published artifact {hostile}"


# --------------------------------------------------------------------------
# Hardening: git URL handling
# --------------------------------------------------------------------------


def test_publish_strips_userinfo_from_the_git_url(api: Api, monkeypatch) -> None:
    captured = _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={"git_url": "https://someone:s3cr3t@github.com/org/repo"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert "s3cr3t" not in resp.text

    # Stripped before the builder ever sees it...
    assert captured["git_url"] == "https://github.com/org/repo"

    artifact_id = resp.json()["id"]
    envelope = main.app.state.store.get_head(artifact_id)
    assert envelope is not None
    # ...before storage...
    assert envelope.source["git"]["url"] == "https://github.com/org/repo"
    assert "s3cr3t" not in envelope.to_json().decode("utf-8")
    # ...and in every public read surface.
    for path in (f"/a/{artifact_id}/meta", f"/a/{artifact_id}/versions"):
        read = api.client.get(path)
        assert "s3cr3t" not in read.text, path
        assert "someone" not in read.text, path


def test_update_and_version_strip_userinfo_from_the_git_url(
    api: Api, monkeypatch
) -> None:
    captured = _stub_build_from_git(monkeypatch)
    artifact_id = _publish_markdown(api, "# One")

    updated = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"git_url": "https://bot:tok3n@gitlab.com/org/repo"},
        headers=AUTH_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert captured["git_url"] == "https://gitlab.com/org/repo"
    assert "tok3n" not in updated.text

    submitted = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"git_url": "https://bot:tok3n@gitlab.com/org/other"},
        headers=AUTH_HEADERS,
    )
    assert submitted.status_code == 201, submitted.text
    assert captured["git_url"] == "https://gitlab.com/org/other"
    assert "tok3n" not in submitted.text
    assert "tok3n" not in api.client.get(f"/a/{artifact_id}/versions").text


def test_private_git_url_is_withheld_from_public_metadata(
    api: Api, monkeypatch
) -> None:
    """A private repo's address is not public provenance (sec-authz)."""
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={
            "git_url": "https://github.com/org/private-repo",
            "git_ref": "main",
            "git_token": GIT_TOKEN,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["id"]

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["git"]["private"] is True
    assert "url" not in meta["git"]
    assert meta["git"]["ref"] == "main"
    assert meta["git"]["commit"]
    assert "private-repo" not in api.client.get(f"/a/{artifact_id}/meta").text

    listing = api.client.get(f"/a/{artifact_id}/versions")
    row = listing.json()["versions"][0]
    assert row["git"]["private"] is True
    assert "url" not in row["git"]
    assert "private-repo" not in listing.text


def test_public_git_url_is_still_reported(api: Api, monkeypatch) -> None:
    """Regression: only *private* provenance is redacted."""
    _stub_build_from_git(monkeypatch)
    resp = api.client.post(
        "/api/artifacts",
        json={"git_url": "https://github.com/org/public-repo"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["id"]

    meta = api.client.get(f"/a/{artifact_id}/meta").json()
    assert meta["git"]["url"] == "https://github.com/org/public-repo"
    assert "private" not in meta["git"]

    row = api.client.get(f"/a/{artifact_id}/versions").json()["versions"][0]
    assert row["git"]["url"] == "https://github.com/org/public-repo"


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_publish_rejects_git_fields_without_a_git_url(api: Api, field: str) -> None:
    resp = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Hi", field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]
    assert "git_url" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_update_rejects_git_fields_without_a_git_url(api: Api, field: str) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]


@pytest.mark.parametrize("field", ["git_ref", "git_path"])
def test_version_submission_rejects_git_fields_without_a_git_url(
    api: Api, field: str
) -> None:
    artifact_id = _publish_markdown(api, "# One")
    resp = api.client.post(
        f"/api/artifacts/{artifact_id}/versions",
        json={"markdown": "# Two", field: "whatever"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert field in resp.json()["detail"]


# --------------------------------------------------------------------------
# Hardening: write-path limits, ordering and partial failures
# --------------------------------------------------------------------------


def test_owner_content_updates_count_against_the_daily_budget(
    api: Api, monkeypatch
) -> None:
    """sec-abuse-limits: PUT with content is a submission like any other."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(api.settings, max_versions_per_day=1),
    )

    first = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Two"},
        headers=AUTH_HEADERS,
    )
    assert first.status_code == 200, first.text

    second = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={"markdown": "# Three"},
        headers=AUTH_HEADERS,
    )
    assert second.status_code == 429
    assert second.json()["limit"] == 1

    # Nothing was persisted by the throttled call.
    assert "Three" not in api.client.get(f"/a/{artifact_id}/raw").text
    # A settings-only update is not a submission and still goes through.
    assert (
        api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"accept_versions": True},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )


def test_update_does_not_apply_settings_when_the_build_fails(
    api: Api, monkeypatch
) -> None:
    """concept-error-handling: no half-applied update."""
    artifact_id = _publish_markdown(api, "# One")
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, max_html_bytes=10)
    )

    resp = api.client.put(
        f"/api/artifacts/{artifact_id}",
        json={
            "html": "<html><body>" + "x" * 500 + "</body></html>",
            "title": "t",
            # Settings carried by the *same* call, which used to be saved
            # before the build ran and therefore survived its failure.
            "accept_versions": True,
            "password": "should-not-stick",
            "comments_mode": "off",
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 413

    meta = main.app.state.store.get_meta(artifact_id)
    assert meta is not None
    assert meta.accept_versions is False
    assert meta.comments_mode == "anyone"
    assert meta.password is None
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_publish_rolls_back_when_the_canonical_upload_fails(
    api: Api, monkeypatch
) -> None:
    """concept-error-handling: no artifact survives a failed canonical copy."""
    monkeypatch.setattr(main, "new_artifact_id", lambda: "rollback-probe-id")
    hub_backend = api.backend

    class _FailingCanonical:
        def upload(self, name: str, content: bytes, tags: list[str]) -> int:
            raise BackendError("the caller's project storage is down")

    def factory(stack_url: str, token: str):
        if (
            stack_url == api.settings.hub_stack_url
            and token == api.settings.hub_storage_token
        ):
            return hub_backend
        return _FailingCanonical()

    monkeypatch.setattr(main, "KbcFilesBackend", factory)

    resp = api.client.post(
        "/api/artifacts", json={"markdown": "# Doomed"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 502
    assert "canonical" in resp.json()["detail"]

    assert api.client.get("/a/rollback-probe-id").status_code == 404
    assert api.backend.search_by_tag("artifact-hub") == []
    assert main.app.state.store.count() == 0


def _break_backend_delete(api: Api, monkeypatch) -> None:
    def failing_delete(file_id: int) -> None:
        raise BackendError("storage refused the delete")

    monkeypatch.setattr(api.backend, "delete", failing_delete)


def test_delete_artifact_reports_partial_failure_as_502(
    api: Api, monkeypatch
) -> None:
    """Never claim 'deleted' over files that are still being served."""
    artifact_id = _publish_markdown(api, "# Mine")
    _break_backend_delete(api, monkeypatch)

    resp = api.client.delete(f"/api/artifacts/{artifact_id}", headers=AUTH_HEADERS)
    assert resp.status_code == 502
    assert resp.json()["error"] == "artifact not fully deleted"
    assert "retry" in resp.json()["detail"].lower()

    # The artifact really is still there, which is exactly why 200 would lie.
    assert api.client.get(f"/a/{artifact_id}").status_code == 200


def test_delete_version_reports_partial_failure_as_502(
    api: Api, monkeypatch
) -> None:
    artifact_id = _publish_markdown(api, "# One")
    assert _submit_version(api, artifact_id, "# Two").status_code == 201
    _break_backend_delete(api, monkeypatch)

    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "version not fully deleted"
    assert api.client.get(f"/a/{artifact_id}/v/1").status_code == 200


def test_delete_only_live_version_is_still_409(api: Api) -> None:
    """Regression: the policy refusal must not be confused with a 502."""
    artifact_id = _publish_markdown(api, "# Only one")
    resp = api.client.delete(
        f"/api/artifacts/{artifact_id}/versions/1", headers=AUTH_HEADERS
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "cannot delete the only live version"


def test_context_documents_the_health_headers_diagnostic(api: Api) -> None:
    by_path = {e["path"]: e for e in api.client.get("/context").json()["endpoints"]}
    entry = by_path["/health/headers"]
    assert entry["method"] == "GET"
    assert entry["auth"] == "none"
    assert "header" in entry["purpose"].lower()
    # It really exists, and OpenAPI knows about it too.
    assert api.client.get("/health/headers").status_code == 200
    assert "/health/headers" in api.client.get("/openapi.json").json()["paths"]


class TestVaultProposalPrivacy:
    """The public vault export must not leak moderated proposals."""

    def _seed(self, api):
        r = api.client.post(
            "/api/artifacts",
            json={"markdown": "# Doc\n\nbody\n", "accept_versions": True},
            headers=AUTH_HEADERS,
        )
        aid = r.json()["id"]
        r = api.client.post(
            f"/api/artifacts/{aid}/versions",
            json={"markdown": "# Doc\n\nproposed change\n", "note": "outside idea"},
            headers=OTHER_AUTH_HEADERS,
        )
        assert r.json()["status"] == "proposed"
        return aid

    def test_anonymous_vault_excludes_proposals(self, api):
        aid = self._seed(api)
        import io
        import zipfile

        payload = api.client.get(f"/a/{aid}/export/vault").content
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        assert any(n.endswith("versions/v1.md") for n in names)
        assert not any(n.endswith("versions/v2.md") for n in names)

    def test_owner_vault_includes_proposals(self, api):
        aid = self._seed(api)
        import io
        import zipfile

        payload = api.client.get(
            f"/a/{aid}/export/vault", headers=AUTH_HEADERS
        ).content
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        assert any(n.endswith("versions/v2.md") for n in names)
