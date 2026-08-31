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
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

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


app = FastAPI(
    title="KBC Artifact Hub",
    version=SERVICE_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


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


@app.get("/health/headers")
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
    """Body of ``POST /api/artifacts``: exactly one content field is required."""

    html: str | None = None
    markdown: str | None = None
    git_url: str | None = None
    git_ref: str | None = None
    git_path: str | None = None
    title: str | None = None
    password: str | None = None


class UpdateBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}``: every field is optional."""

    html: str | None = None
    markdown: str | None = None
    git_url: str | None = None
    git_ref: str | None = None
    git_path: str | None = None
    title: str | None = None
    password: str | None = None
    clear_password: bool = False


def _content_fields(body: PublishBody | UpdateBody) -> list[str]:
    """Names of the content fields present in a request body."""
    return [
        name
        for name in ("html", "markdown", "git_url")
        if getattr(body, name) is not None
    ]


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
            str(body.git_url), body.git_ref, body.git_path, body.title, settings
        )
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


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    """Human-facing documentation page."""
    return HTMLResponse(landing_page(base_url(request)))


@app.post("/", response_class=PlainTextResponse)
def landing_probe() -> PlainTextResponse:
    """Platform startup check — the proxy POSTs to ``/`` to see if we are up."""
    return PlainTextResponse("OK")


@app.get("/health")
def health(request: Request) -> dict:
    """Liveness plus index statistics."""
    return {
        "status": "ok",
        "artifacts": request.app.state.store.count(),
        "hydrated": bool(getattr(request.app.state, "hydrated", False)),
    }


@app.get("/context")
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
            "git_url": "string, public https git repository to clone",
            "git_ref": "string, optional branch/tag/commit for git_url",
            "git_path": (
                "string, optional entry file or directory inside the repository; "
                "defaults to index.html, then README.md, then a single root *.html"
            ),
            "title": "string, optional; derived from the content when omitted",
            "password": "string, optional reader password",
            "rules": [
                "exactly one of html, markdown, git_url",
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
        ],
    }


@app.get("/skill")
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


@app.get("/a/{artifact_id}")
def read_artifact(artifact_id: str, request: Request) -> Response:
    """Serve the rendered artifact, or the unlock form when protected."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if not reader_allowed(envelope, request):
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    return HTMLResponse(envelope.html)


@app.post("/a/{artifact_id}/unlock")
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


@app.get("/a/{artifact_id}/raw")
def read_raw(artifact_id: str, request: Request) -> Response:
    """The artifact HTML itself, for machines."""
    envelope = _load(request, artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    if not reader_allowed(envelope, request):
        return _password_required()
    return HTMLResponse(envelope.html)


@app.get("/a/{artifact_id}/source")
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


@app.get("/a/{artifact_id}/meta")
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


@app.post("/api/artifacts", status_code=201)
def publish_artifact(
    body: PublishBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Publish a new artifact from HTML, Markdown or a git repository."""
    owner, token = auth
    ensure_hydrated(request.app)

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


@app.put("/api/artifacts/{artifact_id}")
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


@app.get("/api/artifacts")
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


@app.delete("/api/artifacts/{artifact_id}")
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
