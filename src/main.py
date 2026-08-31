"""FastAPI wiring for the KBC Artifact Hub.

Read path (public, unauthenticated): ``/a/{id}`` and friends serve artifacts
straight from the serving store. Write path (``/api/artifacts``): authenticated
with the caller's own Keboola Storage token, which is used once — to store the
canonical copy in the caller's project — and never persisted.

Startup is deliberately tolerant: if Storage is unreachable while hydrating the
index, the process still boots and retries hydration on the next request, so a
transient Storage outage cannot put the app into a crash loop.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field

from src import builder
from src.auth import (
    STACK_ALIASES,
    AuthError,
    Owner,
    StackError,
    StackUnreachableError,
    resolve_stack,
    verify_token,
)
from src.builder import BuildError, BuiltArtifact
from src.config import Settings, load_settings
from src.kbc import BackendError, KbcFilesBackend
from src.pages import landing_page, unlock_page
from src.security import (
    CookieSigner,
    check_password,
    hash_password,
    new_artifact_id,
)
from src.store import ArtifactStore, Envelope, tag_for_id

SERVICE_NAME = "kbc-artifact-hub"
SERVICE_VERSION = "0.1.0"

#: Storage tags put on the canonical copy in the author's own project.
CANONICAL_TAG = "kbc-artifact"

#: Path of the agent-facing skill document, relative to the repository root.
SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "skills/artifact-publisher/SKILL.md"
)

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format=(
        '{"time": "%(asctime)s", "level": "%(levelname)s", '
        '"logger": "%(name)s", "message": "%(message)s"}'
    ),
)
logger = logging.getLogger(__name__)

# Settings are read once at import time so a misconfigured deployment fails
# before the server starts accepting traffic.
settings: Settings = load_settings()

#: Guards the lazy re-hydration retry so concurrent requests do not all hammer
#: Storage at once after a failed startup hydration.
_hydrate_lock = threading.Lock()


def _now() -> str:
    """Current UTC timestamp, ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the store and try (but do not require) an initial hydration."""
    app.state.settings = settings
    app.state.store = ArtifactStore(
        KbcFilesBackend(settings.hub_stack_url, settings.hub_storage_token),
        settings.cache_dir,
        settings.cache_max_entries,
    )
    app.state.signer = CookieSigner(settings.secret_key)
    app.state.hydrated = False
    try:
        count = app.state.store.hydrate()
        app.state.hydrated = True
        logger.info("Startup hydration complete: %d artifact(s)", count)
    except BackendError as exc:
        logger.error(
            "Startup hydration failed, serving in degraded mode: %s", exc
        )
    yield


#: Markdown shown at the top of the interactive docs (Swagger UI) and in the
#: generated OpenAPI document's ``info.description``.
API_DESCRIPTION = """\
Public hosting for self-contained HTML/Markdown artifacts, backed by Keboola
Storage. Anyone holding **any** Keboola Storage API token, on **any** Keboola
stack, can publish a document and get back an unguessable public URL.

## Authentication

Everything under `/api/artifacts` is authenticated with two headers instead
of a bearer token:

| Header | Meaning |
|---|---|
| `X-StorageApi-Token` | Any Keboola Storage API token |
| `X-Storage-Stack` | Stack alias (`us`, `gcp-us`, `eu`, `azure-eu`, `gcp-eu`) or a full `https://*.keboola.com` URL. `X-Kbc-Stack` is accepted as an alias for direct/local access. |

Use the **Authorize** button above to set both headers once for every request
made from this page.

## Learn more

- [`/skill`](/skill) — SKILL.md teaching an AI agent how to publish
  artifacts, unassisted.
- [`/context`](/context) — machine-readable manifest of endpoints, auth
  model and limits.

## Artifact URLs are capabilities

Reading an artifact (`/a/{id}` and friends) needs no token: the unguessable
id in the URL *is* the access control. There is no public listing. An
optional password adds a second layer on top.
"""

app = FastAPI(
    title="KBC Artifact Hub",
    version=SERVICE_VERSION,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    openapi_tags=[
        {
            "name": "public",
            "description": "Unauthenticated reads: artifact pages and service discovery.",
        },
        {
            "name": "artifacts",
            "description": "Authenticated artifact management (publish, update, list, delete).",
        },
        {
            "name": "service",
            "description": "Health, machine-readable manifest, and the agent-facing skill document.",
        },
    ],
)


#: Names of the two OpenAPI apiKey-in-header security schemes. Kept in one
#: place so the custom openapi() override and the per-route `security` lists
#: cannot drift apart.
_TOKEN_SECURITY = "StorageApiToken"
_STACK_SECURITY = "StorageStack"


def custom_openapi() -> dict[str, Any]:
    """Attach the two header-based auth schemes to every ``/api/*`` operation.

    FastAPI has no first-class concept of "two headers act together as
    credentials", so the schemes are declared as plain ``apiKey``-in-header
    security schemes and wired onto the relevant operations here. This is
    purely a documentation aid for Swagger UI's Authorize button — the actual
    runtime check stays in :func:`require_owner`, untouched.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes[_TOKEN_SECURITY] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-StorageApi-Token",
        "description": "Any Keboola Storage API token.",
    }
    security_schemes[_STACK_SECURITY] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Storage-Stack",
        "description": (
            "Stack alias (us, gcp-us, eu, azure-eu, gcp-eu) or a full "
            "https://*.keboola.com URL."
        ),
    }
    security_requirement = [{_TOKEN_SECURITY: [], _STACK_SECURITY: []}]
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for operation in methods.values():
            if isinstance(operation, dict):
                operation["security"] = security_requirement
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


def ensure_hydrated(app_obj: FastAPI) -> None:
    """Retry index hydration once per call while the index is not hydrated.

    A failure here is not fatal: the store falls back to a per-artifact Storage
    lookup, so individual reads still work while the full index is missing.
    """
    if getattr(app_obj.state, "hydrated", False):
        return
    with _hydrate_lock:
        if getattr(app_obj.state, "hydrated", False):
            return
        try:
            count = app_obj.state.store.hydrate()
        except BackendError as exc:
            logger.warning("Deferred hydration attempt failed: %s", exc)
            return
        app_obj.state.hydrated = True
        logger.info("Deferred hydration complete: %d artifact(s)", count)


# --------------------------------------------------------------------------
# Middleware and error handling
# --------------------------------------------------------------------------


@app.middleware("http")
async def artifact_headers(request: Request, call_next):
    """Keep artifact responses out of search indexes and shared caches."""
    response = await call_next(request)
    if request.url.path.startswith("/a/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(BackendError)
async def backend_error_handler(request: Request, exc: BackendError) -> Response:
    """Any unhandled Storage failure surfaces as 502, never as a 500."""
    logger.error("Storage backend failure on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=502,
        content={"error": "storage backend unavailable", "detail": str(exc)},
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def base_url(request: Request) -> str:
    """Absolute base URL of this service as seen by the client."""
    if settings.public_base_url:
        return settings.public_base_url
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{scheme.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def artifact_urls(base: str, artifact_id: str) -> dict[str, str]:
    """Every public URL of one artifact."""
    root = f"{base.rstrip('/')}/a/{artifact_id}"
    return {
        "url": root,
        "raw_url": f"{root}/raw",
        "source_url": f"{root}/source",
        "meta_url": f"{root}/meta",
    }


def require_owner(request: Request) -> tuple[Owner, str]:
    """Authenticate the caller and return (owner identity, raw token).

    The raw token is returned because the canonical copy of the artifact is
    uploaded to the caller's own project with it. It is never stored or logged.
    """
    token = request.headers.get("x-storageapi-token", "")
    # Primary header is X-Storage-Stack: the platform proxy in front of
    # deployed data apps strips X-Kbc-* headers, so that name never arrives.
    # X-Kbc-Stack is kept as an alias for direct/local access.
    raw_stack = request.headers.get("x-storage-stack", "") or request.headers.get(
        "x-kbc-stack", ""
    )
    try:
        stack_url = resolve_stack(raw_stack, settings.extra_stacks)
    except StackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        owner = verify_token(stack_url, token, settings.token_verify_timeout_s)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except StackUnreachableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return owner, token


def reader_allowed(envelope: Envelope, request: Request) -> bool:
    """True when the caller may read a (possibly protected) artifact."""
    if not envelope.password:
        return True
    supplied = request.headers.get("x-artifact-password")
    if supplied and check_password(supplied, envelope.password):
        return True
    cookie = request.cookies.get(f"art_{envelope.id}")
    if cookie and request.app.state.signer.check(
        envelope.id, cookie, settings.unlock_cookie_max_age_s
    ):
        return True
    return False


@app.get(
    "/health/headers",
    tags=["service"],
    summary="Diagnostic: header names received by the app",
    description=(
        "Lists the names (never the values) of request headers that reached "
        "this process. Exists to detect reverse proxies that silently strip "
        "custom headers such as X-StorageApi-Token or X-Storage-Stack, which "
        "would otherwise break header-based authentication in a way that is "
        "hard to diagnose from the outside."
    ),
)
def health_headers(request: Request) -> dict[str, Any]:
    """Diagnostic: names of request headers that reached the app.

    Values are never echoed — this exists to detect reverse proxies that strip
    custom headers (which would silently break header-based authentication).
    """
    return {"received_header_names": sorted(request.headers.keys())}


def _not_found(artifact_id: str) -> JSONResponse:
    """Identical answer whether the artifact never existed or was deleted."""
    return JSONResponse(
        status_code=404,
        content={"error": "artifact not found", "id": artifact_id},
    )


def _password_required() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "password required",
            "hint": "send X-Artifact-Password header",
        },
    )


def _load(request: Request, artifact_id: str) -> Envelope | None:
    """Fetch an envelope, hydrating the index first when needed."""
    ensure_hydrated(request.app)
    return request.app.state.store.get(artifact_id)


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class PublishBody(BaseModel):
    """Body of ``POST /api/artifacts``: exactly one content field is required.

    Provide exactly one of ``html``, ``markdown`` or ``git_url``.
    ``git_token``/``git_username`` are transient clone credentials for a
    private repository: like the Storage token, they are used during the
    request and never stored, logged or echoed back.
    """

    html: str | None = Field(
        None,
        description=(
            "Complete HTML document, served as-is. Mutually exclusive with "
            "'markdown' and 'git_url'."
        ),
    )
    markdown: str | None = Field(
        None,
        description=(
            "Markdown source, rendered by the hub's built-in template (GFM "
            "tables, task lists, mermaid fences, syntax highlighting). "
            "Mutually exclusive with 'html' and 'git_url'."
        ),
    )
    git_url: str | None = Field(
        None,
        description=(
            "HTTPS git repository to clone (public, or private with "
            "'git_token'). Mutually exclusive with 'html' and 'markdown'."
        ),
    )
    git_ref: str | None = Field(
        None, description="Optional branch, tag or commit to check out for 'git_url'."
    )
    git_path: str | None = Field(
        None,
        description=(
            "Optional entry file or directory inside the repository. "
            "Defaults to index.html, then README.md, then a single root "
            "*.html file."
        ),
    )
    git_username: str | None = Field(
        None,
        description=(
            "Username for git hosts that require one alongside 'git_token'. "
            "Defaults to 'x-access-token', which works for GitHub PATs and "
            "GitLab deploy tokens. Only valid together with 'git_url' (422 "
            "otherwise)."
        ),
    )
    git_token: str | None = Field(
        None,
        description=(
            "Personal access token for the git host (GitHub PAT, GitLab "
            "token, ...), used to clone a private repository. Transient: "
            "used only for the clone during this request, never stored, "
            "logged or returned. Only valid together with 'git_url' (422 "
            "otherwise)."
        ),
    )
    title: str | None = Field(
        None, description="Optional title; derived from the content when omitted."
    )
    password: str | None = Field(
        None,
        description=(
            "Optional reader password. Readers unlock via the "
            "X-Artifact-Password header or the web unlock form."
        ),
    )


class UpdateBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}``: every field is optional.

    At most one content field (``html``, ``markdown``, ``git_url``) may be
    given; omit all of them to leave the content unchanged.
    """

    html: str | None = Field(
        None,
        description=(
            "Complete HTML document, served as-is. At most one of 'html', "
            "'markdown', 'git_url' may be given."
        ),
    )
    markdown: str | None = Field(
        None,
        description=(
            "Markdown source, rendered by the hub's built-in template. At "
            "most one of 'html', 'markdown', 'git_url' may be given."
        ),
    )
    git_url: str | None = Field(
        None,
        description=(
            "HTTPS git repository to clone. At most one of 'html', "
            "'markdown', 'git_url' may be given."
        ),
    )
    git_ref: str | None = Field(
        None, description="Optional branch, tag or commit to check out for 'git_url'."
    )
    git_path: str | None = Field(
        None,
        description=(
            "Optional entry file or directory inside the repository; see "
            "the same field on the publish body for the resolution order."
        ),
    )
    git_username: str | None = Field(
        None,
        description=(
            "Username for git hosts that require one. Only valid together "
            "with 'git_url' (422 otherwise)."
        ),
    )
    git_token: str | None = Field(
        None,
        description=(
            "Personal access token for the git host; transient, never "
            "stored, logged or returned. Only valid together with "
            "'git_url' (422 otherwise). Must be resent on every update that "
            "re-publishes from a private repository."
        ),
    )
    title: str | None = Field(None, description="New title; leaves it unchanged when omitted.")
    password: str | None = Field(
        None, description="Set or replace the reader password."
    )
    clear_password: bool = Field(
        False, description="When true, remove any existing reader password."
    )


def _content_fields(body: PublishBody | UpdateBody) -> list[str]:
    """Names of the content fields present in a request body."""
    return [
        name
        for name in ("html", "markdown", "git_url")
        if getattr(body, name) is not None
    ]


def _check_git_credentials(body: PublishBody | UpdateBody) -> None:
    """Reject clone credentials that were sent without a ``git_url``.

    Silently ignoring them would leave the caller believing a token was used
    when it was not, so this is a hard 422.
    """
    if body.git_url is not None:
        return
    stray = [
        name
        for name in ("git_username", "git_token")
        if getattr(body, name) is not None
    ]
    if stray:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{' and '.join(repr(name) for name in stray)} "
                f"{'is' if len(stray) == 1 else 'are'} only valid together with "
                "'git_url'"
            ),
        )


def _build(body: PublishBody | UpdateBody) -> tuple[BuiltArtifact, dict[str, Any]]:
    """Build the HTML for a request body and the ``source`` dict to store.

    Raises :class:`HTTPException` 413 when the result exceeds the size limit
    and 422 when the input cannot be built.
    """
    try:
        if body.html is not None:
            if len(body.html.encode("utf-8")) > settings.max_html_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "HTML is too large; the limit is "
                        f"{settings.max_html_bytes} bytes"
                    ),
                )
            built = builder.build_from_html(body.html, body.title)
            return built, {}

        if body.markdown is not None:
            built = builder.build_from_markdown(body.markdown, body.title)
            if len(built.html.encode("utf-8")) > settings.max_html_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Rendered HTML is too large; the limit is "
                        f"{settings.max_html_bytes} bytes"
                    ),
                )
            return built, {"markdown": body.markdown}

        built = builder.build_from_git(
            str(body.git_url),
            body.git_ref,
            body.git_path,
            body.title,
            settings,
            git_username=body.git_username,
            git_token=body.git_token,
        )
        # Deliberately only url/ref/path/commit: the clone credentials are
        # transient and must not reach the stored envelope.
        source = {
            "git": {
                "url": str(body.git_url),
                "ref": body.git_ref,
                "path": body.git_path,
                "commit": built.git_commit,
            }
        }
        return built, source
    except BuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _store_canonical(owner: Owner, token: str, artifact_id: str, html: str) -> int:
    """Upload the canonical copy of the built HTML into the author's project."""
    backend = KbcFilesBackend(owner.stack_url, token)
    try:
        return backend.upload(
            f"artifact-{artifact_id}.html",
            html.encode("utf-8"),
            [CANONICAL_TAG, tag_for_id(artifact_id)],
        )
    except BackendError as exc:
        logger.error(
            "Canonical upload failed for artifact %s (project %s): %s",
            artifact_id,
            owner.project_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail="could not store canonical copy in your project",
        ) from exc


def _artifact_response(
    request: Request, envelope: Envelope, status_code: int
) -> JSONResponse:
    """Standard management-API response describing one artifact."""
    payload = {
        "id": envelope.id,
        "title": envelope.title,
        "protected": bool(envelope.password),
        "version": envelope.version,
        "owner_project_id": envelope.owner.get("project_id"),
        "canonical_file_id": envelope.canonical_file_id,
        **artifact_urls(base_url(request), envelope.id),
    }
    return JSONResponse(status_code=status_code, content=payload)


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------


@app.get(
    "/",
    response_class=HTMLResponse,
    tags=["public"],
    summary="Landing page",
    description="Human-facing HTML documentation of the hub: what it is, how to authenticate, and copy-pasteable curl examples.",
)
def landing(request: Request) -> HTMLResponse:
    """Human-facing documentation page."""
    return HTMLResponse(landing_page(base_url(request)))


@app.post(
    "/",
    response_class=PlainTextResponse,
    tags=["service"],
    summary="Platform startup probe",
    description="Always returns 200 'OK'. Used by the Keboola Data App platform to check the process is up; not meant to be called by clients.",
)
def landing_probe() -> PlainTextResponse:
    """Platform startup check — the proxy POSTs to ``/`` to see if we are up."""
    return PlainTextResponse("OK")


@app.get(
    "/health",
    tags=["service"],
    summary="Liveness and index statistics",
    description="Reports process liveness plus whether the in-memory artifact index has finished hydrating from Storage, and how many artifacts it holds.",
)
def health(request: Request) -> dict:
    """Liveness plus index statistics."""
    return {
        "status": "ok",
        "artifacts": request.app.state.store.count(),
        "hydrated": bool(getattr(request.app.state, "hydrated", False)),
    }


@app.get(
    "/context",
    tags=["service"],
    summary="Machine-readable service manifest",
    description="Full manifest for agents: endpoints, auth model, stack aliases, publish body schema, and configured limits. Intended to be fetched once before scripting against the API.",
)
def context(request: Request) -> dict:
    """Machine-readable manifest of the service, for agents."""
    base = base_url(request)
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "base_url": base,
        "description": (
            "Public hosting for self-contained HTML artifacts, backed by "
            "Keboola Storage. Any Keboola Storage API token from any stack can "
            "publish; the canonical copy is stored in the caller's own project "
            "and a serving copy in the hub's project."
        ),
        "auth": {
            "applies_to": "/api/*",
            "headers": {
                "X-StorageApi-Token": "any Keboola Storage API token",
                "X-Storage-Stack": "stack alias or full https URL (X-Kbc-Stack accepted as alias for direct access)",
            },
            "stack_aliases": dict(STACK_ALIASES),
            "stack_rule": (
                "a full stack URL is accepted when it is https and its hostname "
                "ends with .keboola.com (plus any host configured in "
                "HUB_EXTRA_STACKS)"
            ),
            "verification": "GET {stack}/v2/storage/tokens/verify",
            "ownership": (
                "(normalized stack, project id); update and delete require a "
                "token from the owning project"
            ),
            "token_storage": "never persisted; used only during the request",
            "reader_password_header": "X-Artifact-Password",
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/",
                "auth": "none",
                "purpose": "human-facing landing page (HTML)",
            },
            {
                "method": "POST",
                "path": "/",
                "auth": "none",
                "purpose": "platform startup check, returns OK",
            },
            {
                "method": "GET",
                "path": "/health",
                "auth": "none",
                "purpose": "liveness and index statistics",
            },
            {
                "method": "GET",
                "path": "/context",
                "auth": "none",
                "purpose": "this manifest",
            },
            {
                "method": "GET",
                "path": "/skill",
                "auth": "none",
                "purpose": "SKILL.md for agents (text/markdown)",
            },
            {
                "method": "GET",
                "path": "/docs",
                "auth": "none",
                "purpose": "interactive Swagger UI for this API",
            },
            {
                "method": "GET",
                "path": "/openapi.json",
                "auth": "none",
                "purpose": "machine-readable OpenAPI schema for this API",
            },
            {
                "method": "GET",
                "path": "/a/{id}",
                "auth": "url capability; password form when protected",
                "purpose": "rendered artifact page",
            },
            {
                "method": "POST",
                "path": "/a/{id}/unlock",
                "auth": "form field 'password'",
                "purpose": "unlock a protected artifact, sets a signed cookie",
            },
            {
                "method": "GET",
                "path": "/a/{id}/raw",
                "auth": "url capability; X-Artifact-Password when protected",
                "purpose": "the artifact HTML itself",
            },
            {
                "method": "GET",
                "path": "/a/{id}/source",
                "auth": "url capability; X-Artifact-Password when protected",
                "purpose": "original source (markdown or html)",
            },
            {
                "method": "GET",
                "path": "/a/{id}/meta",
                "auth": "url capability",
                "purpose": "public metadata, no owner details",
            },
            {
                "method": "POST",
                "path": "/api/artifacts",
                "auth": "storage token",
                "purpose": "publish a new artifact",
            },
            {
                "method": "PUT",
                "path": "/api/artifacts/{id}",
                "auth": "storage token (owner project)",
                "purpose": "update content and/or password",
            },
            {
                "method": "GET",
                "path": "/api/artifacts",
                "auth": "storage token",
                "purpose": "list the caller project's artifacts",
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}",
                "auth": "storage token (owner project)",
                "purpose": "delete the serving copy",
            },
        ],
        "publish_body": {
            "html": "string, complete HTML document, served as-is",
            "markdown": (
                "string, rendered by the built-in template (GFM tables, task "
                "lists, mermaid fences, syntax highlighting)"
            ),
            "git_url": "string, https git repository to clone (public, or private with git_token)",
            "git_ref": "string, optional branch/tag/commit for git_url",
            "git_path": (
                "string, optional entry file or directory inside the repository; "
                "defaults to index.html, then README.md, then a single root *.html"
            ),
            "git_token": (
                "string, optional personal access token for the git host "
                "(GitHub PAT, GitLab token, ...) used to clone a private "
                "repository; transient — used only for the clone during this "
                "request, never stored, logged or returned"
            ),
            "git_username": (
                "string, optional username for git hosts that require one; "
                "defaults to 'x-access-token', which works for GitHub PATs and "
                "GitLab deploy tokens"
            ),
            "title": "string, optional; derived from the content when omitted",
            "password": "string, optional reader password",
            "rules": [
                "exactly one of html, markdown, git_url",
                "git_token and git_username are only valid together with git_url "
                "(422 otherwise)",
                "PUT accepts the same fields, all optional, plus clear_password",
            ],
        },
        "limits": {
            "max_html_bytes": settings.max_html_bytes,
            "max_inline_image_bytes": settings.max_inline_image_bytes,
            "max_inline_total_bytes": settings.max_inline_total_bytes,
            "git_clone_timeout_s": settings.git_clone_timeout_s,
            "git_max_repo_bytes": settings.git_max_repo_bytes,
            "unlock_cookie_max_age_s": settings.unlock_cookie_max_age_s,
        },
        "notes": [
            "Artifact URLs are capabilities: the unguessable id is the only "
            "access control by default, there is no public listing, and every "
            "/a/* response carries X-Robots-Tag: noindex, nofollow.",
            "An optional password adds a second layer; readers unlock in the "
            "browser (signed cookie scoped to the artifact path) or send the "
            "X-Artifact-Password header.",
            "git_token follows the same rule as the Storage token: it is used "
            "only for the clone inside the request and is never written to the "
            "stored artifact, the logs, or any response.",
        ],
    }


@app.get(
    "/skill",
    tags=["service"],
    summary="Agent-facing SKILL.md",
    description="Serves skills/artifact-publisher/SKILL.md verbatim, teaching an AI agent how to authenticate and publish artifacts unassisted.",
    responses={404: {"description": "SKILL.md is not readable on this deployment."}},
)
def skill() -> Response:
    """Serve the agent-facing SKILL.md."""
    try:
        text = SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("SKILL.md not readable at %s", SKILL_PATH)
        return JSONResponse(
            status_code=404, content={"error": "skill document not available"}
        )
    return Response(content=text, media_type="text/markdown; charset=utf-8")


@app.get(
    "/a/{artifact_id}",
    tags=["public"],
    summary="Rendered artifact page",
    description="Serves the artifact's built HTML as-is. When the artifact is password-protected and the caller has not unlocked it, returns the HTML unlock form instead.",
    responses={
        401: {"description": "Password-protected artifact; the unlock form is returned."},
        404: {"description": "No artifact exists with this id."},
    },
)
def read_artifact(artifact_id: str, request: Request) -> Response:
    """Serve the rendered artifact, or the unlock form when protected."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if not reader_allowed(envelope, request):
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    return HTMLResponse(envelope.html)


@app.post(
    "/a/{artifact_id}/unlock",
    tags=["public"],
    summary="Unlock a password-protected artifact",
    description="Target of the HTML unlock form. On a correct password, redirects to the artifact page and sets a signed, path-scoped cookie so future browser visits do not need the password again.",
    responses={
        303: {"description": "Correct password; redirects to the artifact page with an unlock cookie set."},
        401: {"description": "Wrong password; the unlock form is returned with an error message."},
        404: {"description": "No artifact exists with this id."},
    },
)
def unlock_artifact(
    artifact_id: str, request: Request, password: str = Form("")
) -> Response:
    """Password form target: on success set a signed, path-scoped cookie."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if envelope.password and not check_password(password, envelope.password):
        return HTMLResponse(
            unlock_page(artifact_id, "Wrong password"), status_code=401
        )
    response = RedirectResponse(f"/a/{artifact_id}", status_code=303)
    response.set_cookie(
        key=f"art_{artifact_id}",
        value=request.app.state.signer.make(artifact_id),
        path=f"/a/{artifact_id}",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.unlock_cookie_max_age_s,
    )
    return response


@app.get(
    "/a/{artifact_id}/raw",
    tags=["public"],
    summary="Raw artifact HTML",
    description="The exact built HTML, with no unlock-form fallback — meant for machine consumption. When protected, send the password via the X-Artifact-Password header.",
    responses={
        401: {"description": "Password required or wrong (see X-Artifact-Password header)."},
        404: {"description": "No artifact exists with this id."},
    },
)
def read_raw(artifact_id: str, request: Request) -> Response:
    """The artifact HTML itself, for machines."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if not reader_allowed(envelope, request):
        return _password_required()
    return HTMLResponse(envelope.html)


@app.get(
    "/a/{artifact_id}/source",
    tags=["public"],
    summary="Original submitted source",
    description="Returns the original Markdown for markdown-sourced artifacts, or the original HTML for html/git-html artifacts. Markdown rendered from a git repository has no retained source and returns 404 with a pointer back to the repository.",
    responses={
        401: {"description": "Password required or wrong (see X-Artifact-Password header)."},
        404: {"description": "No artifact with this id, or its source was not retained (git-sourced Markdown)."},
    },
)
def read_source(artifact_id: str, request: Request) -> Response:
    """The original source: markdown for markdown artifacts, HTML otherwise."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if not reader_allowed(envelope, request):
        return _password_required()

    markdown = envelope.source.get("markdown")
    if isinstance(markdown, str):
        return PlainTextResponse(
            markdown, media_type="text/markdown; charset=utf-8"
        )
    if envelope.source_type in ("html", "git-html"):
        return HTMLResponse(envelope.html)
    return JSONResponse(
        status_code=404,
        content={
            "error": "source not retained",
            "detail": (
                "This artifact was rendered from Markdown in a git repository; "
                "only the built HTML is kept. Fetch the source from the "
                "repository instead."
            ),
            "git": envelope.source.get("git"),
        },
    )


@app.get(
    "/a/{artifact_id}/meta",
    tags=["public"],
    summary="Public artifact metadata",
    description="JSON metadata (title, timestamps, version, protection flag, size) with no owner details. Available even when the artifact is password-protected.",
    responses={404: {"description": "No artifact exists with this id."}},
)
def read_meta(artifact_id: str, request: Request) -> Response:
    """Public metadata; available even when the artifact is password-protected."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    return JSONResponse(
        {
            **envelope.public_meta(),
            **artifact_urls(base_url(request), artifact_id),
        }
    )


# --------------------------------------------------------------------------
# Management API
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts",
    status_code=201,
    tags=["artifacts"],
    summary="Publish a new artifact",
    description="Publish HTML, Markdown, or a git repository as a new artifact. Exactly one of 'html', 'markdown', 'git_url' must be provided. Requires a Storage token (X-StorageApi-Token) and stack (X-Storage-Stack); the token establishes the owning project and is used once to store a canonical copy there.",
    responses={
        201: {"description": "Artifact published; returns its id and URLs."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {"description": "Invalid body: not exactly one content field, git credentials without git_url, or a build failure (bad repo, no entry file, markdown render error)."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token, or the canonical upload failed."},
    },
)
def publish_artifact(
    body: PublishBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Publish a new artifact from HTML, Markdown or a git repository."""
    owner, token = auth
    ensure_hydrated(request.app)

    _check_git_credentials(body)
    present = _content_fields(body)
    if len(present) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide exactly one of 'html', 'markdown' or 'git_url' "
                f"(got: {', '.join(present) if present else 'none'})"
            ),
        )

    built, source = _build(body)
    artifact_id = new_artifact_id()
    canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)

    now = _now()
    envelope = Envelope(
        id=artifact_id,
        title=built.title,
        html=built.html,
        source_type=built.source_type,
        source=source,
        owner={
            "stack_url": owner.stack_url,
            "project_id": owner.project_id,
            "project_name": owner.project_name,
            "key": owner.key,
        },
        password=hash_password(body.password) if body.password else None,
        canonical_file_id=canonical_file_id,
        created_at=now,
        updated_at=now,
        version=1,
    )
    request.app.state.store.publish(envelope)
    logger.info(
        "Published artifact %s (owner project %s, source %s, %d bytes, "
        "protected=%s, canonical file %s)",
        artifact_id,
        owner.project_id,
        built.source_type,
        len(built.html.encode("utf-8")),
        bool(envelope.password),
        canonical_file_id,
    )
    return _artifact_response(request, envelope, 201)


@app.put(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Update an artifact",
    description="Update the content and/or password of an artifact owned by the caller's project. All fields are optional; at most one content field ('html', 'markdown', 'git_url') may be given. Requires a Storage token from the owning project.",
    responses={
        200: {"description": "Artifact updated; returns its current state."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Token is valid but not from the project that owns this artifact."},
        404: {"description": "No artifact exists with this id."},
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {"description": "Invalid body: more than one content field, git credentials without git_url, or a build failure."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token, or the canonical upload failed."},
    },
)
def update_artifact(
    artifact_id: str,
    body: UpdateBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Update the content and/or password of an artifact owned by the caller."""
    owner, token = auth
    ensure_hydrated(request.app)

    envelope = request.app.state.store.get(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if envelope.owner.get("key") != owner.key:
        raise HTTPException(
            status_code=403, detail="this artifact belongs to another project"
        )

    _check_git_credentials(body)
    present = _content_fields(body)
    if len(present) > 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide at most one of 'html', 'markdown' or 'git_url' "
                f"(got: {', '.join(present)})"
            ),
        )

    if present:
        built, source = _build(body)
        envelope.html = built.html
        envelope.title = built.title
        envelope.source_type = built.source_type
        envelope.source = source
        envelope.canonical_file_id = _store_canonical(
            owner, token, artifact_id, built.html
        )
    elif body.title is not None:
        envelope.title = body.title

    if body.clear_password:
        envelope.password = None
    elif body.password:
        envelope.password = hash_password(body.password)

    envelope.version += 1
    envelope.updated_at = _now()
    request.app.state.store.publish(envelope)
    logger.info(
        "Updated artifact %s to version %d (owner project %s, source %s, "
        "%d bytes, protected=%s)",
        artifact_id,
        envelope.version,
        owner.project_id,
        envelope.source_type,
        len(envelope.html.encode("utf-8")),
        bool(envelope.password),
    )
    return _artifact_response(request, envelope, 200)


@app.get(
    "/api/artifacts",
    tags=["artifacts"],
    summary="List the caller's artifacts",
    description="Lists artifacts published by the caller's own project (identified by the Storage token's owning project), with their URLs merged in.",
    responses={
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def list_artifacts(
    request: Request, auth: tuple[Owner, str] = Depends(require_owner)
) -> dict:
    """List the artifacts published by the caller's project."""
    owner, _token = auth
    ensure_hydrated(request.app)
    base = base_url(request)
    metas = request.app.state.store.list_owner(owner.key)
    return {
        "project_id": owner.project_id,
        "artifacts": [
            {**meta, **artifact_urls(base, meta["id"])} for meta in metas
        ],
    }


@app.delete(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Delete an artifact's serving copy",
    description="Deletes the serving copy in the hub's project. The canonical copy in the author's own project is left untouched. Requires a Storage token from the owning project.",
    responses={
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Token is valid but not from the project that owns this artifact."},
        404: {"description": "No artifact exists with this id."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def delete_artifact(
    artifact_id: str,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Delete the serving copy; the author's canonical copy is left untouched."""
    owner, _token = auth
    ensure_hydrated(request.app)

    envelope = request.app.state.store.get(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if envelope.owner.get("key") != owner.key:
        raise HTTPException(
            status_code=403, detail="this artifact belongs to another project"
        )

    request.app.state.store.delete(artifact_id)
    logger.info(
        "Deleted artifact %s (owner project %s, %d bytes)",
        artifact_id,
        owner.project_id,
        len(envelope.html.encode("utf-8")),
    )
    return JSONResponse(
        {
            "deleted": True,
            "note": "canonical copy in your project was not touched",
        }
    )
