"""FastAPI wiring for the KBC Artifact Hub.

Read path (public, unauthenticated): ``/a/{id}`` and friends serve artifacts
straight from the serving store. Write path (``/api/artifacts``): authenticated
with the caller's own Keboola Storage token, which is used once — to store the
canonical copy in the caller's project — and never persisted.

Since phase 2 an artifact is a *series of versions* plus an artifact-level meta
record. ``/a/{id}`` serves the head version (newest live, or a pinned one).
Owners add live versions; other projects may submit **proposals** when the owner
opted in with ``accept_versions`` — a proposal is readable only by the owner or
its author until the owner promotes it.

Startup is deliberately tolerant: if Storage is unreachable while hydrating the
index, the process still boots and retries hydration on the next request, so a
transient Storage outage cannot put the app into a crash loop.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import tomllib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
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
from src.diff import DiffError, compute_diff
from src.kbc import BackendError, KbcFilesBackend
from src.pages import landing_page, unlock_page, versions_page
from src.security import (
    CookieSigner,
    check_password,
    hash_password,
    new_artifact_id,
)
from src.store import (
    HEAD_LATEST,
    HEAD_PINNED,
    STATUS_LIVE,
    STATUS_PROPOSED,
    ArtifactMeta,
    ArtifactStore,
    Envelope,
    tag_for_id,
)

SERVICE_NAME = "kbc-artifact-hub"

#: Public source repository, surfaced on the landing page and in /context.
GITHUB_REPO_URL = "https://github.com/padak/kbc_ai_artifact"

#: Storage tags put on the canonical copy in the author's own project.
CANONICAL_TAG = "kbc-artifact"

#: Path of the agent-facing skill document, relative to the repository root.
SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "skills/artifact-publisher/SKILL.md"
)

#: ``/a/{id}/diff/{spec}`` accepts exactly ``<older>..<newer>``.
_DIFF_SPEC = re.compile(r"^(\d+)\.\.(\d+)$")

#: Longest contributor note accepted on a submitted version.
MAX_NOTE_CHARS = 500

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format=(
        '{"time": "%(asctime)s", "level": "%(levelname)s", '
        '"logger": "%(name)s", "message": "%(message)s"}'
    ),
)
logger = logging.getLogger(__name__)


def _read_service_version() -> str:
    """The single source of truth for the service version.

    Prefers the installed package metadata; falls back to parsing the
    repository's ``pyproject.toml`` when the app runs from a source checkout
    that was never installed. Never invents a placeholder — a build that can
    supply neither is misconfigured and fails fast.
    """
    try:
        return package_version("kbc-artifact-hub")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(
            "Cannot determine the service version: the 'kbc-artifact-hub' "
            f"package is not installed and {pyproject} is unreadable ({exc})"
        ) from exc


SERVICE_VERSION = _read_service_version()

# Settings are read once at import time so a misconfigured deployment fails
# before the server starts accepting traffic.
settings: Settings = load_settings()

#: Guards the lazy re-hydration retry so concurrent requests do not all hammer
#: Storage at once after a failed startup hydration.
_hydrate_lock = threading.Lock()

#: Per-contributor version-submission counters, ``{(id, key, UTC day): count}``.
#: In-memory and therefore per-replica — a soft cap against accidental floods,
#: not a security control.
_submission_counts: dict[tuple[str, str, str], int] = {}
_submission_lock = threading.Lock()

#: Above this many live buckets, stale days are swept on the next submission.
_SUBMISSION_SWEEP_AT = 1000


def _now() -> str:
    """Current UTC timestamp, ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_day() -> str:
    """Current UTC calendar day, used as the rate-limit bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _claim_submission_slot(artifact_id: str, contributor_key: str) -> bool:
    """Count one version submission; False when the daily cap is exhausted."""
    limit = settings.max_versions_per_day
    day = _utc_day()
    bucket = (artifact_id, contributor_key, day)
    with _submission_lock:
        if len(_submission_counts) > _SUBMISSION_SWEEP_AT:
            for stale in [key for key in _submission_counts if key[2] != day]:
                del _submission_counts[stale]
        count = _submission_counts.get(bucket, 0)
        if count >= limit:
            return False
        _submission_counts[bucket] = count + 1
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the store and try (but do not require) an initial hydration."""
    app.state.settings = settings
    app.state.store = ArtifactStore(
        KbcFilesBackend(settings.hub_stack_url, settings.hub_storage_token),
        settings.cache_dir,
        settings.cache_max_entries,
        settings.max_versions,
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

## Versioning

Updates never overwrite: each one adds a version. `/a/{id}` serves the head
(newest live version, or a pinned one). When the owner sets
`accept_versions`, any other project may submit a version — it lands as a
**proposal**, readable only by the owner and its author until the owner
promotes it.
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
            "description": "Unauthenticated reads: artifact pages, version history, diffs.",
        },
        {
            "name": "artifacts",
            "description": "Authenticated artifact management (publish, update, list, delete).",
        },
        {
            "name": "versions",
            "description": "Authenticated community versioning: submit, promote, withdraw, pin.",
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
        "versions_url": f"{root}/versions",
    }


def require_owner(request: Request) -> tuple[Owner, str]:
    """Authenticate the caller and return (caller identity, raw token).

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


def optional_caller(request: Request) -> Owner | None:
    """Identify the caller from the management headers when they are present.

    Used by the read path to decide whether a *proposed* version may be shown.
    Anonymous reads are the norm here, so a missing or unusable credential is
    simply "no identity" rather than an error.
    """
    token = request.headers.get("x-storageapi-token", "")
    raw_stack = request.headers.get("x-storage-stack", "") or request.headers.get(
        "x-kbc-stack", ""
    )
    if not token or not raw_stack:
        return None
    try:
        stack_url = resolve_stack(raw_stack, settings.extra_stacks)
        return verify_token(stack_url, token, settings.token_verify_timeout_s)
    except (StackError, AuthError, StackUnreachableError) as exc:
        logger.info("Ignoring unusable read credentials: %s", exc)
        return None


def reader_allowed(meta: ArtifactMeta, request: Request) -> bool:
    """True when the caller may read a (possibly password-protected) artifact."""
    if not meta.password:
        return True
    supplied = request.headers.get("x-artifact-password")
    if supplied and check_password(supplied, meta.password):
        return True
    cookie = request.cookies.get(f"art_{meta.id}")
    if cookie and request.app.state.signer.check(
        meta.id, cookie, settings.unlock_cookie_max_age_s
    ):
        return True
    return False


def may_see(meta: ArtifactMeta, envelope: Envelope, caller: Owner | None) -> bool:
    """True unless this is someone else's proposal.

    Proposals are moderated content: only the artifact owner and the version's
    own author may read them until the owner promotes them.
    """
    if envelope.status != STATUS_PROPOSED:
        return True
    if caller is None:
        return False
    return caller.key in (meta.owner_key, envelope.author_key)


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


def _version_not_found(artifact_id: str, version: int) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "version not found",
            "id": artifact_id,
            "version": version,
        },
    )


def _password_required() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "password required",
            "hint": "send X-Artifact-Password header",
        },
    )


def _proposal_hidden(artifact_id: str, version: int) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": "proposed version is not public",
            "detail": (
                "Proposed versions stay private until the artifact owner "
                "promotes them. Only the owner and the version's author can "
                "read one, by sending X-StorageApi-Token and X-Storage-Stack."
            ),
            "id": artifact_id,
            "version": version,
        },
    )


def _meta_of(request: Request, artifact_id: str) -> ArtifactMeta | None:
    """Fetch an artifact's meta record, hydrating the index first when needed."""
    ensure_hydrated(request.app)
    return request.app.state.store.get_meta(artifact_id)


def _owner_only(meta: ArtifactMeta, caller: Owner) -> None:
    """Raise 403 unless the caller's project owns the artifact."""
    if meta.owner_key != caller.key:
        raise HTTPException(
            status_code=403, detail="this artifact belongs to another project"
        )


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
    accept_versions: bool = Field(
        False,
        description=(
            "When true, any other Keboola project may submit versions of this "
            "artifact. Submissions from other projects always land as "
            "moderated proposals that only you can promote. Default false: "
            "only the owning project may add versions."
        ),
    )


class UpdateBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}``: every field is optional.

    At most one content field (``html``, ``markdown``, ``git_url``) may be
    given; omit all of them to leave the content unchanged. A content field
    adds a new live version — nothing is ever overwritten.
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
    title: str | None = Field(
        None,
        description=(
            "Title of the new version. A title lives on a version, so this "
            "is only valid together with a content field (422 otherwise)."
        ),
    )
    password: str | None = Field(
        None, description="Set or replace the reader password."
    )
    clear_password: bool = Field(
        False, description="When true, remove any existing reader password."
    )
    accept_versions: bool | None = Field(
        None,
        description=(
            "Open the artifact to versions submitted by other projects "
            "(true), or close it again (false). Omit to leave unchanged."
        ),
    )


class VersionBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/versions``.

    Same content shape as publishing: exactly one of ``html``, ``markdown`` or
    ``git_url``, plus an optional note describing what changed.
    """

    html: str | None = Field(None, description="Complete HTML document, served as-is.")
    markdown: str | None = Field(
        None, description="Markdown source, rendered by the built-in template."
    )
    git_url: str | None = Field(None, description="HTTPS git repository to clone.")
    git_ref: str | None = Field(None, description="Optional branch, tag or commit.")
    git_path: str | None = Field(
        None, description="Optional entry file or directory inside the repository."
    )
    git_username: str | None = Field(
        None, description="Only valid together with 'git_url' (422 otherwise)."
    )
    git_token: str | None = Field(
        None,
        description=(
            "Transient clone credential; only valid together with 'git_url'."
        ),
    )
    title: str | None = Field(
        None, description="Title of this version; derived from the content when omitted."
    )
    note: str | None = Field(
        None,
        max_length=MAX_NOTE_CHARS,
        description=(
            "Short description of what changed, shown in the version history. "
            f"At most {MAX_NOTE_CHARS} characters."
        ),
    )


class HeadBody(BaseModel):
    """Body of ``PUT /api/artifacts/{id}/head``."""

    mode: str = Field(
        ...,
        description=(
            "'latest' to always serve the newest live version, or 'pinned' to "
            "serve one specific live version."
        ),
    )
    version: int | None = Field(
        None, description="Required (and must be a live version) when mode is 'pinned'."
    )


def _content_fields(body: PublishBody | UpdateBody | VersionBody) -> list[str]:
    """Names of the content fields present in a request body."""
    return [
        name
        for name in ("html", "markdown", "git_url")
        if getattr(body, name) is not None
    ]


def _check_git_credentials(body: PublishBody | UpdateBody | VersionBody) -> None:
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


def _require_exactly_one_content(body: PublishBody | VersionBody) -> None:
    present = _content_fields(body)
    if len(present) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide exactly one of 'html', 'markdown' or 'git_url' "
                f"(got: {', '.join(present) if present else 'none'})"
            ),
        )


def _build(
    body: PublishBody | UpdateBody | VersionBody,
) -> tuple[BuiltArtifact, dict[str, Any]]:
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


def _identity(owner: Owner) -> dict[str, Any]:
    """The project identity recorded on a meta record or a version envelope."""
    return {
        "stack_url": owner.stack_url,
        "project_id": owner.project_id,
        "project_name": owner.project_name,
        "key": owner.key,
    }


def _head_version_of(request: Request, artifact_id: str) -> int | None:
    head = request.app.state.store.get_head(artifact_id)
    return head.version if head is not None else None


def _artifact_response(
    request: Request,
    meta: ArtifactMeta,
    envelope: Envelope,
    status_code: int,
) -> JSONResponse:
    """Standard management-API response describing one artifact."""
    payload = {
        "id": meta.id,
        "title": envelope.title,
        "protected": bool(meta.password),
        "accept_versions": meta.accept_versions,
        "version": envelope.version,
        "status": envelope.status,
        "head_version": _head_version_of(request, meta.id),
        "owner_project_id": meta.owner.get("project_id"),
        "canonical_file_id": envelope.canonical_file_id,
        **artifact_urls(base_url(request), meta.id),
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
    description="Human-facing HTML documentation of the hub: what it does, how to authenticate, and copy-pasteable curl examples.",
)
def landing(request: Request) -> HTMLResponse:
    """Human-facing documentation page."""
    return HTMLResponse(
        landing_page(base_url(request), SERVICE_VERSION, GITHUB_REPO_URL)
    )


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
    description="Reports process liveness, the running service version, and whether the in-memory artifact index has finished hydrating from Storage.",
)
def health(request: Request) -> dict:
    """Liveness plus index statistics."""
    return {
        "status": "ok",
        "version": SERVICE_VERSION,
        "artifacts": request.app.state.store.count(),
        "hydrated": bool(getattr(request.app.state, "hydrated", False)),
    }


@app.get(
    "/context",
    tags=["service"],
    summary="Machine-readable service manifest",
    description="Full manifest for agents: endpoints, auth model, stack aliases, publish body schema, the versioning model, and configured limits. Intended to be fetched once before scripting against the API.",
)
def context(request: Request) -> dict:
    """Machine-readable manifest of the service, for agents."""
    base = base_url(request)
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "base_url": base,
        "repository": GITHUB_REPO_URL,
        "description": (
            "Public hosting for self-contained HTML artifacts, backed by "
            "Keboola Storage. Any Keboola Storage API token from any stack can "
            "publish; the canonical copy is stored in the caller's own project "
            "and a serving copy in the hub's project. Updates add versions "
            "rather than overwriting, and an artifact can accept moderated "
            "version proposals from other projects."
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
                "(normalized stack, project id); update, delete, promote and "
                "head pinning require a token from the owning project"
            ),
            "token_storage": "never persisted; used only during the request",
            "reader_password_header": "X-Artifact-Password",
            "reading_proposals": (
                "the same two management headers may be sent on /a/{id}/v/{n} "
                "and /a/{id}/diff/{a}..{b} to read a proposed version as its "
                "author or as the artifact owner"
            ),
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
                "purpose": "liveness, service version and index statistics",
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
                "purpose": "rendered head version",
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
                "purpose": "the head version's HTML itself",
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
                "method": "GET",
                "path": "/a/{id}/v/{n}",
                "auth": "url capability; owner or author for proposals",
                "purpose": "one specific version",
            },
            {
                "method": "GET",
                "path": "/a/{id}/versions",
                "auth": "url capability",
                "purpose": "version history JSON, or ?format=html for a picker page",
            },
            {
                "method": "GET",
                "path": "/a/{id}/diff/{a}..{b}",
                "auth": "url capability; owner or author for proposals",
                "purpose": "diff two versions (?format=html|unified|json)",
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
                "purpose": "add a live version and/or change password and accept_versions",
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
                "purpose": "delete every version and the meta record",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/versions",
                "auth": "storage token (any project when accept_versions)",
                "purpose": "submit a version; live for the owner, proposed otherwise",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/versions/{n}/promote",
                "auth": "storage token (owner project)",
                "purpose": "promote a proposal to live",
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/versions/{n}",
                "auth": "storage token (owner, or the proposal's author)",
                "purpose": "delete a version, or withdraw your own proposal",
            },
            {
                "method": "PUT",
                "path": "/api/artifacts/{id}/head",
                "auth": "storage token (owner project)",
                "purpose": "serve the latest live version, or pin one",
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
            "accept_versions": (
                "bool, default false; when true other projects may submit "
                "moderated version proposals"
            ),
            "rules": [
                "exactly one of html, markdown, git_url",
                "git_token and git_username are only valid together with git_url "
                "(422 otherwise)",
                "PUT accepts the same fields, all optional, plus clear_password "
                "and accept_versions; a title is only valid together with new "
                "content, because a title lives on a version",
                "POST /api/artifacts/{id}/versions accepts the same content "
                f"fields plus an optional note (max {MAX_NOTE_CHARS} chars)",
            ],
        },
        "versioning": {
            "model": (
                "Updates never overwrite. Every submission becomes version "
                "next_version and is stored as its own Storage file with a "
                "verified author."
            ),
            "statuses": {
                "live": "servable; the head is chosen among live versions",
                "proposed": (
                    "moderated; readable only by the artifact owner and the "
                    "version's author until the owner promotes it"
                ),
            },
            "moderation": (
                "Owner submissions are always live. Other projects need "
                "accept_versions=true (403 otherwise) and always land as "
                "proposals; the owner promotes one with POST "
                "/api/artifacts/{id}/versions/{n}/promote."
            ),
            "head_pointer": (
                "PUT /api/artifacts/{id}/head with {'mode': 'latest'} serves the "
                "newest live version; {'mode': 'pinned', 'version': n} freezes "
                "/a/{id} on one live version."
            ),
            "diff": (
                "GET /a/{id}/diff/{older}..{newer} renders a side-by-side page; "
                "?format=unified returns text/plain and ?format=json returns the "
                "unified diff plus added/removed counts. Markdown is compared "
                "when both versions carry it, otherwise the built HTML."
            ),
            "retention": (
                f"at most {settings.max_versions} live versions per artifact; "
                "the oldest live versions that are neither the head nor pinned "
                "are pruned. Proposals are never pruned."
            ),
            "rate_limit": (
                f"{settings.max_versions_per_day} submitted versions per "
                "contributor project per artifact per UTC day (429 afterwards)"
            ),
            "deletion": (
                "The owner may delete any version except the last live one "
                "(409); a contributor may withdraw their own proposal."
            ),
        },
        "limits": {
            "max_html_bytes": settings.max_html_bytes,
            "max_inline_image_bytes": settings.max_inline_image_bytes,
            "max_inline_total_bytes": settings.max_inline_total_bytes,
            "git_clone_timeout_s": settings.git_clone_timeout_s,
            "git_max_repo_bytes": settings.git_max_repo_bytes,
            "unlock_cookie_max_age_s": settings.unlock_cookie_max_age_s,
            "max_versions": settings.max_versions,
            "max_versions_per_day": settings.max_versions_per_day,
            "diff_max_bytes": settings.diff_max_bytes,
            "max_note_chars": MAX_NOTE_CHARS,
        },
        "notes": [
            "Artifact URLs are capabilities: the unguessable id is the only "
            "access control by default, there is no public listing, and every "
            "/a/* response carries X-Robots-Tag: noindex, nofollow.",
            "An optional password adds a second layer; readers unlock in the "
            "browser (signed cookie scoped to the artifact path) or send the "
            "X-Artifact-Password header.",
            "Version history is visible to anyone holding the capability URL, "
            "but the content of a proposed version is not: only the owner and "
            "its author can read it.",
            "git_token follows the same rule as the Storage token: it is used "
            "only for the clone inside the request and is never written to the "
            "stored artifact, the logs, or any response.",
        ],
    }


@app.get(
    "/skill",
    tags=["service"],
    summary="Agent-facing SKILL.md",
    description="Serves skills/artifact-publisher/SKILL.md verbatim, teaching an AI agent how to authenticate, publish artifacts and contribute versions unassisted.",
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
    summary="Rendered artifact page (head version)",
    description="Serves the head version's built HTML as-is — the newest live version, or the one the owner pinned. When the artifact is password-protected and the caller has not unlocked it, returns the HTML unlock form instead.",
    responses={
        401: {"description": "Password-protected artifact; the unlock form is returned."},
        404: {"description": "No artifact exists with this id, or it has no live version."},
    },
)
def read_artifact(artifact_id: str, request: Request) -> Response:
    """Serve the head version, or the unlock form when protected."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    envelope = request.app.state.store.get_head(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
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
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if meta.password and not check_password(password, meta.password):
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
    summary="Raw artifact HTML (head version)",
    description="The exact built HTML of the head version, with no unlock-form fallback — meant for machine consumption. When protected, send the password via the X-Artifact-Password header.",
    responses={
        401: {"description": "Password required or wrong (see X-Artifact-Password header)."},
        404: {"description": "No artifact exists with this id, or it has no live version."},
    },
)
def read_raw(artifact_id: str, request: Request) -> Response:
    """The head version's HTML itself, for machines."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
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
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)

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
    description="JSON metadata of the artifact and its head version (title, timestamps, version counts, protection and contribution flags, size) with no owner details. Available even when the artifact is password-protected.",
    responses={404: {"description": "No artifact exists with this id, or it has no live version."}},
)
def read_meta(artifact_id: str, request: Request) -> Response:
    """Public metadata; available even when the artifact is password-protected."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    store = request.app.state.store
    head = store.get_head(artifact_id)
    if head is None:
        return _not_found(artifact_id)
    versions = store.list_versions(artifact_id)
    return JSONResponse(
        {
            **head.public_meta(is_head=True),
            "id": artifact_id,
            "protected": bool(meta.password),
            "accept_versions": meta.accept_versions,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "head_version": head.version,
            "versions_count": len(versions),
            "proposed_count": sum(
                1 for row in versions if row.get("status") == STATUS_PROPOSED
            ),
            **artifact_urls(base_url(request), artifact_id),
        }
    )


@app.get(
    "/a/{artifact_id}/v/{version}",
    tags=["public"],
    summary="One specific version",
    description="Serves the built HTML of a single version. A proposed version is private: only the artifact owner and the version's author can read it, by sending X-StorageApi-Token and X-Storage-Stack.",
    responses={
        401: {"description": "Password-protected artifact; the unlock form is returned."},
        403: {"description": "This version is a proposal and the caller is neither the owner nor its author."},
        404: {"description": "No artifact or no such version."},
    },
)
def read_version(artifact_id: str, version: int, request: Request) -> Response:
    """Serve one version, honoring both the password gate and proposal privacy."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    envelope = request.app.state.store.get_version(artifact_id, version)
    if envelope is None:
        return _version_not_found(artifact_id, version)
    if not may_see(meta, envelope, optional_caller(request)):
        return _proposal_hidden(artifact_id, version)
    return HTMLResponse(envelope.html)


@app.get(
    "/a/{artifact_id}/versions",
    tags=["public"],
    summary="Version history",
    description="Lists every version (live and proposed) newest first, with its author, status, note and size. Add ?format=html for a human-readable picker page with links to each version and to the diff of every adjacent pair. Proposal *metadata* is public to capability-URL holders; proposal *content* is not.",
    responses={
        401: {"description": "Password required or wrong (see X-Artifact-Password header)."},
        404: {"description": "No artifact exists with this id."},
    },
)
def read_versions(
    artifact_id: str, request: Request, format: str = "json"
) -> Response:
    """Version history as JSON, or a styled picker page with ?format=html."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    wants_html = format == "html"
    if not reader_allowed(meta, request):
        if wants_html:
            return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
        return _password_required()

    store = request.app.state.store
    versions = store.list_versions(artifact_id)
    head_version = _head_version_of(request, artifact_id)
    base = base_url(request)

    if wants_html:
        return HTMLResponse(
            versions_page(
                base,
                artifact_id,
                versions,
                head_version,
                meta.accept_versions,
                bool(meta.password),
            )
        )

    root = f"{base.rstrip('/')}/a/{artifact_id}"
    return JSONResponse(
        {
            "id": artifact_id,
            "head_version": head_version,
            "accept_versions": meta.accept_versions,
            "protected": bool(meta.password),
            "versions": [
                {**row, "url": f"{root}/v/{row['version']}"} for row in versions
            ],
        }
    )


@app.get(
    "/a/{artifact_id}/diff/{spec}",
    tags=["public"],
    summary="Diff two versions",
    description="Compares two versions given as '{older}..{newer}' (for example 3..5). Renders a side-by-side HTML page by default; ?format=unified returns text/plain and ?format=json returns the unified diff plus added/removed counts. Markdown is compared when both versions carry it, otherwise the built HTML.",
    responses={
        400: {"description": "Malformed diff spec or unknown format."},
        401: {"description": "Password required or wrong (see X-Artifact-Password header)."},
        403: {"description": "One side is a proposal the caller may not read."},
        404: {"description": "No artifact, or one of the versions does not exist."},
        413: {"description": "One side exceeds HUB_DIFF_MAX_BYTES."},
    },
)
def read_diff(
    artifact_id: str, spec: str, request: Request, format: str = "html"
) -> Response:
    """Diff two versions of one artifact in the requested rendering."""
    match = _DIFF_SPEC.match(spec)
    if match is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "malformed diff spec",
                "detail": "Use {older}..{newer}, for example 3..5",
                "spec": spec,
            },
        )
    older, newer = int(match.group(1)), int(match.group(2))

    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()

    store = request.app.state.store
    caller = None
    envelopes: list[Envelope] = []
    for number in (older, newer):
        envelope = store.get_version(artifact_id, number)
        if envelope is None:
            return _version_not_found(artifact_id, number)
        if envelope.status == STATUS_PROPOSED:
            if caller is None:
                caller = optional_caller(request)
            if not may_see(meta, envelope, caller):
                return _proposal_hidden(artifact_id, number)
        envelopes.append(envelope)

    try:
        content_type, body = compute_diff(
            envelopes[0], envelopes[1], format, settings.diff_max_bytes
        )
    except DiffError as exc:
        status_code = 413 if "too large" in str(exc).lower() else 400
        return JSONResponse(status_code=status_code, content={"error": str(exc)})
    return Response(content=body, media_type=content_type)


# --------------------------------------------------------------------------
# Management API
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts",
    status_code=201,
    tags=["artifacts"],
    summary="Publish a new artifact",
    description="Publish HTML, Markdown, or a git repository as a new artifact. Exactly one of 'html', 'markdown', 'git_url' must be provided. Requires a Storage token (X-StorageApi-Token) and stack (X-Storage-Stack); the token establishes the owning project and is used once to store a canonical copy there. Set 'accept_versions' to let other projects submit moderated version proposals.",
    responses={
        201: {"description": "Artifact published as version 1; returns its id and URLs."},
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
    _require_exactly_one_content(body)

    built, source = _build(body)
    artifact_id = new_artifact_id()
    canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)

    now = _now()
    identity = _identity(owner)
    meta = ArtifactMeta(
        id=artifact_id,
        owner=identity,
        password=hash_password(body.password) if body.password else None,
        accept_versions=bool(body.accept_versions),
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at=now,
        updated_at=now,
    )
    envelope = Envelope(
        id=artifact_id,
        version=1,
        title=built.title,
        html=built.html,
        source_type=built.source_type,
        source=source,
        author=identity,
        status=STATUS_LIVE,
        canonical_file_id=canonical_file_id,
        created_at=now,
    )
    request.app.state.store.create(meta, envelope)
    logger.info(
        "Published artifact %s (owner project %s, source %s, %d bytes, "
        "protected=%s, accept_versions=%s, canonical file %s)",
        artifact_id,
        owner.project_id,
        built.source_type,
        len(built.html.encode("utf-8")),
        bool(meta.password),
        meta.accept_versions,
        canonical_file_id,
    )
    return _artifact_response(request, meta, envelope, 201)


@app.put(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Update an artifact",
    description="Owner-only. A content field ('html', 'markdown', 'git_url') adds a new live version; 'password', 'clear_password' and 'accept_versions' change artifact-level settings. A title is only valid together with new content, because a title lives on a version.",
    responses={
        200: {"description": "Artifact updated; returns its current state."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Token is valid but not from the project that owns this artifact."},
        404: {"description": "No artifact exists with this id."},
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {"description": "Invalid body: more than one content field, git credentials without git_url, a title without content, or a build failure."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token, or the canonical upload failed."},
    },
)
def update_artifact(
    artifact_id: str,
    body: UpdateBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Add a live version and/or change the artifact-level settings."""
    owner, token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

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
    if body.title is not None and not present:
        raise HTTPException(
            status_code=422,
            detail=(
                "'title' belongs to a version, so it can only be changed "
                "together with new content ('html', 'markdown' or 'git_url')"
            ),
        )

    now = _now()
    if body.clear_password:
        meta.password = None
    elif body.password:
        meta.password = hash_password(body.password)
    if body.accept_versions is not None:
        meta.accept_versions = bool(body.accept_versions)
    meta.updated_at = now
    store.save_meta(meta)

    if present:
        built, source = _build(body)
        canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)
        envelope = Envelope(
            id=artifact_id,
            version=store.next_version(artifact_id),
            title=built.title,
            html=built.html,
            source_type=built.source_type,
            source=source,
            author=_identity(owner),
            status=STATUS_LIVE,
            canonical_file_id=canonical_file_id,
            created_at=now,
        )
        store.add_version(envelope)
    else:
        envelope = store.get_head(artifact_id)
        if envelope is None:
            return _not_found(artifact_id)

    logger.info(
        "Updated artifact %s (owner project %s, version %d, source %s, "
        "%d bytes, protected=%s, accept_versions=%s)",
        artifact_id,
        owner.project_id,
        envelope.version,
        envelope.source_type,
        len(envelope.html.encode("utf-8")),
        bool(meta.password),
        meta.accept_versions,
    )
    return _artifact_response(request, meta, envelope, 200)


@app.get(
    "/api/artifacts",
    tags=["artifacts"],
    summary="List the caller's artifacts",
    description="Lists artifacts owned by the caller's own project (identified by the Storage token's owning project), with version counts and URLs merged in.",
    responses={
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def list_artifacts(
    request: Request, auth: tuple[Owner, str] = Depends(require_owner)
) -> dict:
    """List the artifacts owned by the caller's project."""
    owner, _token = auth
    ensure_hydrated(request.app)
    base = base_url(request)
    rows = request.app.state.store.list_owner(owner.key)
    return {
        "project_id": owner.project_id,
        "artifacts": [{**row, **artifact_urls(base, row["id"])} for row in rows],
    }


@app.delete(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Delete an artifact",
    description="Deletes every version and the meta record from the hub's project. The canonical copies in the authors' own projects are left untouched. Requires a Storage token from the owning project.",
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
    """Delete every version; the authors' canonical copies are left untouched."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    store.delete(artifact_id)
    logger.info("Deleted artifact %s (owner project %s)", artifact_id, owner.project_id)
    return JSONResponse(
        {
            "deleted": True,
            "note": "canonical copies in the authors' projects were not touched",
        }
    )


# --------------------------------------------------------------------------
# Community versioning
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts/{artifact_id}/versions",
    status_code=201,
    tags=["versions"],
    summary="Submit a new version",
    description="Adds a version to an existing artifact. The owning project's submissions go live immediately; another project may submit only when the owner set 'accept_versions', and its submission lands as a moderated proposal that stays private until the owner promotes it. The canonical copy is stored in the *caller's* own project with the caller's token.",
    responses={
        201: {"description": "Version stored; returns its number, status and URL."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "This artifact does not accept versions from other projects."},
        404: {"description": "No artifact exists with this id."},
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {"description": "Invalid body: not exactly one content field, git credentials without git_url, an over-long note, or a build failure."},
        429: {"description": "This project reached HUB_MAX_VERSIONS_PER_DAY submissions for this artifact today."},
        502: {"description": "The caller's Keboola stack could not be reached, or the canonical upload failed."},
    },
)
def submit_version(
    artifact_id: str,
    body: VersionBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Submit one version: live for the owner, a moderated proposal otherwise."""
    caller, token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)

    is_owner = meta.owner_key == caller.key
    if not is_owner and not meta.accept_versions:
        raise HTTPException(
            status_code=403,
            detail=(
                "this artifact does not accept versions from other projects; "
                "its owner can enable that with accept_versions"
            ),
        )

    _check_git_credentials(body)
    _require_exactly_one_content(body)

    if not _claim_submission_slot(artifact_id, caller.key):
        return JSONResponse(
            status_code=429,
            content={
                "error": "too many versions today",
                "detail": (
                    f"Your project may submit {settings.max_versions_per_day} "
                    "versions of one artifact per UTC day."
                ),
                "limit": settings.max_versions_per_day,
            },
        )

    built, source = _build(body)
    # The canonical copy always goes to the *submitter's* project: whoever
    # wrote a version keeps its source of truth.
    canonical_file_id = _store_canonical(caller, token, artifact_id, built.html)

    status = STATUS_LIVE if is_owner else STATUS_PROPOSED
    envelope = Envelope(
        id=artifact_id,
        version=store.next_version(artifact_id),
        title=built.title,
        html=built.html,
        source_type=built.source_type,
        source=source,
        author=_identity(caller),
        status=status,
        note=body.note or None,
        canonical_file_id=canonical_file_id,
        created_at=_now(),
    )
    store.add_version(envelope)
    logger.info(
        "Artifact %s got version %d (%s) from project %s",
        artifact_id,
        envelope.version,
        status,
        caller.project_id,
    )
    return JSONResponse(
        status_code=201,
        content={
            "id": artifact_id,
            "version": envelope.version,
            "status": envelope.status,
            "note": envelope.note,
            "url": f"{base_url(request).rstrip('/')}/a/{artifact_id}/v/{envelope.version}",
        },
    )


@app.post(
    "/api/artifacts/{artifact_id}/versions/{version}/promote",
    tags=["versions"],
    summary="Promote a proposal to live",
    description="Owner-only. Marks a proposed version live so it can be served. With the default head mode ('latest') the promoted version immediately becomes what /a/{id} serves.",
    responses={
        200: {"description": "Version promoted; reports the version now served as head."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Only the owning project may promote a version."},
        404: {"description": "No artifact or no such version."},
        409: {"description": "That version is already live."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def promote_version(
    artifact_id: str,
    version: int,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner approves a proposal: its status becomes live."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    envelope = store.get_version(artifact_id, version)
    if envelope is None:
        return _version_not_found(artifact_id, version)
    if envelope.status == STATUS_LIVE:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version is already live",
                "id": artifact_id,
                "version": version,
            },
        )

    if not store.set_status(artifact_id, version, STATUS_LIVE):
        return _version_not_found(artifact_id, version)

    head_version = _head_version_of(request, artifact_id)
    logger.info(
        "Promoted version %d of artifact %s (head is now v%s)",
        version,
        artifact_id,
        head_version,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "version": version,
            "status": STATUS_LIVE,
            "head_mode": meta.head_mode,
            "head_version": head_version,
            "url": f"{base_url(request).rstrip('/')}/a/{artifact_id}/v/{version}",
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/versions/{version}",
    tags=["versions"],
    summary="Delete a version or withdraw a proposal",
    description="The owning project may delete any version except the last live one. A contributor may delete only their own proposal — withdrawing a submission they no longer stand behind.",
    responses={
        200: {"description": "Version deleted."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Not the owner, and not the author of this proposal."},
        404: {"description": "No artifact or no such version."},
        409: {"description": "This is the only live version; an artifact must keep one."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def delete_version(
    artifact_id: str,
    version: int,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner deletes any version; a contributor withdraws their own proposal."""
    caller, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    envelope = store.get_version(artifact_id, version)
    if envelope is None:
        return _version_not_found(artifact_id, version)

    if meta.owner_key != caller.key and not (
        envelope.status == STATUS_PROPOSED and envelope.author_key == caller.key
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner can delete this version; a "
                "contributor may withdraw their own proposal"
            ),
        )

    if not store.delete_version(artifact_id, version):
        return JSONResponse(
            status_code=409,
            content={
                "error": "cannot delete the only live version",
                "detail": (
                    "An artifact must keep at least one live version. Publish "
                    "a replacement first, or delete the whole artifact."
                ),
                "id": artifact_id,
                "version": version,
            },
        )

    logger.info(
        "Deleted version %d of artifact %s (by project %s)",
        version,
        artifact_id,
        caller.project_id,
    )
    return JSONResponse(
        {
            "deleted": True,
            "id": artifact_id,
            "version": version,
            "head_version": _head_version_of(request, artifact_id),
        }
    )


@app.put(
    "/api/artifacts/{artifact_id}/head",
    tags=["versions"],
    summary="Choose what /a/{id} serves",
    description="Owner-only. {'mode': 'latest'} always serves the newest live version; {'mode': 'pinned', 'version': n} freezes the artifact on one live version, which is also protected from retention pruning.",
    responses={
        200: {"description": "Head pointer updated; reports the version now served."},
        400: {"description": "Unknown or disallowed X-Storage-Stack value."},
        401: {"description": "Storage token missing or rejected by the stack."},
        403: {"description": "Only the owning project may move the head pointer."},
        404: {"description": "No artifact exists with this id."},
        422: {"description": "Unknown mode, missing version for 'pinned', or a version that does not exist or is not live."},
        502: {"description": "The caller's Keboola stack could not be reached to verify the token."},
    },
)
def set_head(
    artifact_id: str,
    body: HeadBody,
    request: Request,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Point the head at the newest live version, or pin it to one version."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    if body.mode not in (HEAD_LATEST, HEAD_PINNED):
        raise HTTPException(
            status_code=422,
            detail=f"mode must be '{HEAD_LATEST}' or '{HEAD_PINNED}'",
        )

    if body.mode == HEAD_PINNED:
        if body.version is None:
            raise HTTPException(
                status_code=422, detail="'version' is required when mode is 'pinned'"
            )
        pinned = store.get_version(artifact_id, body.version)
        if pinned is None:
            raise HTTPException(
                status_code=422,
                detail=f"artifact {artifact_id} has no version {body.version}",
            )
        if pinned.status != STATUS_LIVE:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"version {body.version} is {pinned.status}; only a live "
                    "version can be pinned as head"
                ),
            )
        meta.head_mode = HEAD_PINNED
        meta.head_version = body.version
    else:
        meta.head_mode = HEAD_LATEST
        meta.head_version = None

    meta.updated_at = _now()
    store.save_meta(meta)
    head_version = _head_version_of(request, artifact_id)
    logger.info(
        "Artifact %s head set to %s (serving v%s)",
        artifact_id,
        meta.head_mode,
        head_version,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "head_mode": meta.head_mode,
            "head_version_served": head_version,
        }
    )
