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

Two documents and one page are served straight from the repository: ``/skill``
and ``/agent`` hand an agent its instructions, and ``/admin`` is a completely
client-side moderation studio — a static HTML page whose JavaScript drives this
same API with a token the visitor pastes into their own browser, so the server
never sees a studio session.

Startup is deliberately tolerant: if Storage is unreachable while hydrating the
index, the process still boots and retries hydration on the next request, so a
transient Storage outage cannot put the app into a crash loop.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import tomllib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field

from src import builder, export
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
from src.comments import CommentStore, CommentThread, Reply, Selector
from src.config import Settings, load_settings
from src.diff import DiffError, compute_diff
from src.kbc import BackendError, KbcFilesBackend
from src.pages import (
    admin_page,
    artifact_frame_page,
    landing_page,
    review_page,
    unlock_page,
    versions_page,
)
from src.security import (
    CookieSigner,
    check_password,
    hash_password,
    new_artifact_id,
)
from src.store import (
    ACCEPT_MODES,
    ARTIFACT_DRAFT,
    ARTIFACT_STATUSES,
    COMMENTS_MODES,
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

#: Path of the ready-to-install Claude Code subagent definition, served at
#: ``/agent``. Resolved exactly like :data:`SKILL_PATH`.
AGENT_PATH = (
    Path(__file__).resolve().parent.parent / "skills/artifact-hub-agent/AGENT.md"
)

#: Path of the repository changelog, served at ``/changelog`` (rendered) and
#: ``/changelog.md`` (raw). Resolved exactly like :data:`SKILL_PATH`. Read
#: fresh from disk on every request rather than cached at import, since it is
#: expected to be rewritten by other tooling while this process is running.
CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

#: ``/a/{id}/diff/{spec}`` accepts exactly ``<older>..<newer>``.
_DIFF_SPEC = re.compile(r"^(\d+)\.\.(\d+)$")

#: Longest contributor note accepted on a submitted version.
MAX_NOTE_CHARS = 500

#: Largest contributor allowlist an owner may set on one artifact.
MAX_CONTRIBUTORS = 50

#: Shape of an owner key: ``{project_id}@{stack hostname}`` (see
#: :meth:`src.auth.Owner.key`). Used to validate the contributor allowlist so a
#: typo becomes a 422 instead of an entry that can never match anybody.
_CONTRIBUTOR_KEY = re.compile(r"^[0-9]+@[A-Za-z0-9._-]+$")

# --------------------------------------------------------------------------
# OpenAPI documentation constants
#
# Every route documents its parameters through these, so one wording change
# stays one edit and no operation can quietly ship an undescribed parameter.
# --------------------------------------------------------------------------

#: Description attached to every ``artifact_id`` path parameter.
ARTIFACT_ID_DESC = (
    "Artifact identifier from the publish response — the capability part of "
    "the URL. Unguessable by design; there is no public listing."
)

#: Description attached to every ``version`` path parameter.
VERSION_DESC = (
    "Version number as listed by GET /a/{id}/versions (1 for the first "
    "published version, counting up; numbers are never reused)."
)

#: Description attached to every ``thread_id`` path parameter.
THREAD_ID_DESC = (
    "Comment thread identifier, as returned by POST "
    "/api/artifacts/{id}/comments and listed by GET /a/{id}/comments."
)

#: Description of the ``spec`` path parameter of the diff endpoint.
SPEC_DESC = (
    "Two version numbers in the form OLD..NEW, for example 1..2. Anything "
    "else is a 400."
)

#: Sentence appended to the description of reads that honour the password gate.
PASSWORD_GATE_NOTE = (
    "When the artifact is password-protected, machine clients send the "
    "password in the X-Artifact-Password header (Swagger UI cannot send it "
    "from this page); browsers use the unlock form at POST /a/{id}/unlock, "
    "which sets a signed cookie scoped to the artifact path and to the "
    "current password. Failed attempts are rate-limited per artifact and "
    "client address, so a wrong password may answer 429 instead of 401."
)

#: Reused ``responses`` entries, so the same failure never gets two wordings.
RESP_STACK_400 = {"description": "Unknown or disallowed X-Storage-Stack value."}
RESP_TOKEN_401 = {"description": "Storage token missing or rejected by the stack."}
RESP_STACK_502 = {
    "description": (
        "The caller's Keboola stack could not be reached to verify the token."
    )
}
RESP_HUB_502 = {
    "description": (
        "The hub's own Keboola Storage backend is unavailable; the artifact "
        "could not be read or written."
    )
}
RESP_NOT_FOUND = {"description": "No artifact exists with this id."}
RESP_UNLOCK_429 = {
    "description": (
        "Too many failed password attempts for this artifact from this "
        "client address in the current hour (HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR)."
    )
}
RESP_VERSIONS_429 = {
    "description": (
        "This project reached HUB_MAX_VERSIONS_PER_DAY submitted versions for "
        "this artifact today; owner updates that add content count too."
    )
}
RESP_THREAD_404 = {
    "description": "No artifact with this id, or no such comment thread."
}
RESP_FINAL_409 = {
    "description": (
        "The artifact's status is 'final', which freezes new versions and new "
        "comments for everyone. Its owner can reopen it with PUT "
        "/api/artifacts/{id} and {\"status\": \"draft\"}."
    )
}

#: ``content`` blocks for the non-JSON responses, so /docs stops implying JSON.
CONTENT_HTML = {"text/html": {"schema": {"type": "string"}}}
CONTENT_MARKDOWN = {"text/markdown": {"schema": {"type": "string"}}}
CONTENT_TEXT = {"text/plain": {"schema": {"type": "string"}}}


class MarkdownResponse(Response):
    """A ``text/markdown`` response.

    Used only as a route's ``response_class`` so the generated OpenAPI document
    advertises the real content type of ``/skill`` and ``/agent``. The handlers
    still build their own :class:`Response`, so runtime behavior is unchanged.
    """

    media_type = "text/markdown; charset=utf-8"

class JSONFormatter(logging.Formatter):
    """Render each log record as one real JSON object.

    The previous format string interpolated ``%(message)s`` straight into a
    JSON-shaped template, so any log line carrying user-controlled text (an
    artifact id, a stack URL, a backend error) could inject a quote or a
    newline and either forge an extra record or make the line unparseable.
    ``json.dumps`` escapes quotes, backslashes and control characters,
    including newlines, so a message is always exactly one JSON string in
    exactly one line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
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

#: The same, for inline comments (threads and replies alike).
_comment_counts: dict[tuple[str, str, str], int] = {}
_comment_lock = threading.Lock()

#: Failed unlock attempts, ``{(artifact id, client ip, UTC hour): count}``.
#: Only *failures* are counted, and the hour in the key makes the window reset
#: itself. In-memory and therefore per-replica, like the counters above.
_unlock_failures: dict[tuple[str, str, str], int] = {}
_unlock_lock = threading.Lock()

#: Above this many live buckets, stale days are swept on the next submission.
_SUBMISSION_SWEEP_AT = 1000


def _now() -> str:
    """Current UTC timestamp, ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_day() -> str:
    """Current UTC calendar day, used as the rate-limit bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_hour() -> str:
    """Current UTC hour, used as the unlock-throttle bucket."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _claim_slot(
    counts: dict[tuple[str, str, str], int],
    lock: threading.Lock,
    artifact_id: str,
    contributor_key: str,
    limit: int,
) -> bool:
    """Count one write against a per-(artifact, project, UTC day) bucket.

    Returns False when the bucket is exhausted. The counters are in-memory and
    therefore per-replica — a soft cap against accidental floods, not a
    security control.
    """
    day = _utc_day()
    bucket = (artifact_id, contributor_key, day)
    with lock:
        if len(counts) > _SUBMISSION_SWEEP_AT:
            for stale in [key for key in counts if key[2] != day]:
                del counts[stale]
        count = counts.get(bucket, 0)
        if count >= limit:
            return False
        counts[bucket] = count + 1
        return True


def _claim_submission_slot(artifact_id: str, contributor_key: str) -> bool:
    """Count one version submission; False when the daily cap is exhausted."""
    return _claim_slot(
        _submission_counts,
        _submission_lock,
        artifact_id,
        contributor_key,
        settings.max_versions_per_day,
    )


def _claim_comment_slot(artifact_id: str, contributor_key: str) -> bool:
    """Count one comment or reply; False when the daily cap is exhausted."""
    return _claim_slot(
        _comment_counts,
        _comment_lock,
        artifact_id,
        contributor_key,
        settings.max_comments_per_day,
    )


def _unlock_throttled(artifact_id: str, client_ip: str) -> bool:
    """True when this (artifact, client) burnt its failed-attempt budget.

    Verifying an artifact password runs a full PBKDF2 (200k iterations), so an
    unthrottled gate is both a password oracle and a cheap way to burn the
    hub's CPU. Only failures are counted, so a legitimate reader who types the
    password correctly is never affected, and the hour in the bucket key makes
    the window reset on its own.
    """
    with _unlock_lock:
        count = _unlock_failures.get((artifact_id, client_ip, _utc_hour()), 0)
    return count >= settings.max_unlock_attempts_per_hour


def _record_unlock_failure(artifact_id: str, client_ip: str) -> None:
    """Count one *failed* password attempt against the hourly budget."""
    hour = _utc_hour()
    bucket = (artifact_id, client_ip, hour)
    with _unlock_lock:
        if len(_unlock_failures) > _SUBMISSION_SWEEP_AT:
            for stale in [key for key in _unlock_failures if key[2] != hour]:
                del _unlock_failures[stale]
        _unlock_failures[bucket] = _unlock_failures.get(bucket, 0) + 1


def _client_ip(request: Request) -> str:
    """Best-effort client address, used only as a rate-limit bucket key.

    ``X-Real-IP`` is what our own nginx sets. It is deliberately *not* trusted
    for anything but bucketing: a forged value can only split an attacker's
    own budget, never grant access or identify anybody.
    """
    real_ip = _first_forwarded(request.headers.get("x-real-ip"))
    if real_ip:
        return real_ip
    client = request.client
    return client.host if client is not None else "unknown"


def _version_rate_limited() -> JSONResponse:
    """429 shared by every route that adds a version (owner updates included)."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build both stores and try (but do not require) an initial hydration."""
    app.state.settings = settings
    # One backend instance serves both stores: they read the same host project
    # and only differ in the tags they list (``artifact-hub`` vs.
    # ``artifact-hub-cmt``), so neither ever sees the other's files.
    backend = KbcFilesBackend(settings.hub_stack_url, settings.hub_storage_token)
    app.state.store = ArtifactStore(
        backend,
        settings.cache_dir,
        settings.cache_max_entries,
        settings.max_versions,
        settings.max_envelope_bytes,
        settings.max_proposed_versions,
    )
    app.state.comments = CommentStore(
        backend,
        settings.cache_dir,
        settings.cache_max_entries,
    )
    app.state.signer = CookieSigner(settings.secret_key)
    app.state.hydrated = False
    try:
        artifacts, threads = _hydrate(app)
        app.state.hydrated = True
        logger.info(
            "Startup hydration complete: %d artifact(s), %d comment thread(s)",
            artifacts,
            threads,
        )
    except BackendError as exc:
        logger.error(
            "Startup hydration failed, serving in degraded mode: %s", exc
        )
    yield


def _hydrate(app_obj: FastAPI) -> tuple[int, int]:
    """Rebuild both indexes from Storage; returns (artifacts, threads)."""
    artifacts = app_obj.state.store.hydrate()
    threads = app_obj.state.comments.hydrate()
    return artifacts, threads


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

- [`/admin`](/admin) — browser studio for artifact owners: review, diff,
  promote, reject, pin and delete versions. Your token stays in the tab.
- [`/agent`](/agent) — a ready-to-install Claude Code subagent definition
  (`install -d ~/.claude/agents && curl -fsSL {base}/agent -o
  ~/.claude/agents/artifact-hub.md`).
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
            "name": "comments",
            "description": (
                "Authenticated inline comment threads: create, reply, resolve "
                "and delete."
            ),
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
    """Retry index hydration once per call while the indexes are not hydrated.

    Covers both the artifact index and the comment-thread index; the single
    flag flips only when both rebuilt. A failure here is not fatal: both stores
    fall back to a per-artifact Storage lookup, so individual reads still work
    while the full index is missing.
    """
    if getattr(app_obj.state, "hydrated", False):
        return
    with _hydrate_lock:
        if getattr(app_obj.state, "hydrated", False):
            return
        try:
            artifacts, threads = _hydrate(app_obj)
        except BackendError as exc:
            logger.warning("Deferred hydration attempt failed: %s", exc)
            return
        app_obj.state.hydrated = True
        logger.info(
            "Deferred hydration complete: %d artifact(s), %d comment thread(s)",
            artifacts,
            threads,
        )


# --------------------------------------------------------------------------
# Middleware and error handling
# --------------------------------------------------------------------------


#: Forwarded schemes we are willing to trust; anything else is ignored.
_PUBLIC_SCHEMES = frozenset({"http", "https"})

#: A ``host[:port]`` we are willing to write into the request scope: a
#: registered name or IPv4 literal, or a bracketed IPv6 literal, each with an
#: optional port. Deliberately strict — a malformed forwarded value must be
#: dropped rather than turned into a broken absolute URL.
_PUBLIC_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?$")


def _first_forwarded(raw: str | None) -> str:
    """First entry of a comma-separated ``X-Forwarded-*`` value, stripped."""
    if not raw:
        return ""
    return raw.split(",", 1)[0].strip()


def _forwarded_trusted() -> bool:
    """True when ``X-Forwarded-Host``/``-Proto`` may name the public origin.

    Only when no explicit ``HUB_PUBLIC_BASE_URL`` is configured (that one wins
    outright) *and* the deployment opted in with
    ``HUB_TRUST_FORWARDED_HEADERS``. Otherwise the headers are client-supplied
    and unverifiable, so they are ignored entirely.
    """
    return not settings.public_base_url and settings.trust_forwarded_headers


@lru_cache(maxsize=8)
def _public_origin(public_base_url: str | None) -> tuple[str, str] | None:
    """``(scheme, netloc)`` of ``HUB_PUBLIC_BASE_URL``, or None when unusable.

    Memoized, so the configured value is parsed once instead of on every
    request. Keying the cache on the value rather than parsing at import time
    keeps the helper honest when the module-level settings object is swapped
    (as the test suite does).
    """
    if not public_base_url:
        return None
    parsed = urlsplit(public_base_url)
    scheme = parsed.scheme.lower()
    if scheme not in _PUBLIC_SCHEMES or not _PUBLIC_HOST.match(parsed.netloc):
        return None
    return scheme, parsed.netloc


def _set_host_header(scope: dict[str, Any], host: str) -> None:
    """Replace (or insert) the ``host`` entry of an ASGI scope's header list.

    Every other header is carried over untouched and in order; duplicate
    ``Host`` headers collapse into the single normalized one.
    """
    encoded = host.encode("latin-1")
    headers: list[tuple[bytes, bytes]] = []
    replaced = False
    for name, value in scope["headers"]:
        if name == b"host":
            if replaced:
                continue
            headers.append((name, encoded))
            replaced = True
        else:
            headers.append((name, value))
    if not replaced:
        headers.append((b"host", encoded))
    scope["headers"] = headers


@app.middleware("http")
async def public_origin(request: Request, call_next):
    """Normalize the request scope to the origin the client actually used.

    The Keboola data-app platform proxy terminates TLS and rewrites ``Host`` to
    the internal cluster service name (``app-*.sandbox.svc.cluster.local``),
    forwarding the real values in ``X-Forwarded-Proto`` / ``X-Forwarded-Host``.
    Starlette builds *absolute* URLs straight from the ASGI scope, so without
    this normalization a request for ``/a/{id}/`` gets a 307 whose ``Location``
    names the internal hostname — both a leak and unreachable for the client.

    Rewriting the scheme and the ``Host`` header here, before routing, fixes
    every absolute URL Starlette generates (trailing-slash redirects today,
    ``url_for`` tomorrow). :func:`base_url` never had this bug because it reads
    the forwarded headers itself, which is why JSON payload URLs were correct
    while redirects were not.

    Precedence: an explicitly configured ``HUB_PUBLIC_BASE_URL`` wins, then —
    *only* when ``HUB_TRUST_FORWARDED_HEADERS`` is on — the forwarded headers;
    when neither yields a usable value the scope is left exactly as it
    arrived. Forwarded headers are off by default because any direct client
    can send them: honoring them unconditionally let an attacker choose the
    host in our redirects and generated links. Production sets
    HUB_PUBLIC_BASE_URL, so nothing changes there; a developer running behind
    a local proxy opts in.
    """
    origin = _public_origin(settings.public_base_url)
    if origin is not None:
        scheme, host = origin
    elif _forwarded_trusted():
        scheme = _first_forwarded(request.headers.get("x-forwarded-proto")).lower()
        host = _first_forwarded(request.headers.get("x-forwarded-host"))
        if scheme not in _PUBLIC_SCHEMES:
            scheme = ""
        if not _PUBLIC_HOST.match(host):
            host = ""
    else:
        scheme = ""
        host = ""
    if scheme:
        request.scope["scheme"] = scheme
    if host:
        _set_host_header(request.scope, host)
    return await call_next(request)


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
    """Absolute base URL of this service as seen by the client.

    Same precedence as the :func:`public_origin` middleware: the configured
    public base URL, then the forwarded headers but only when they are trusted
    (see :func:`_forwarded_trusted`), then the request as it arrived.
    """
    if settings.public_base_url:
        return settings.public_base_url
    scheme = request.url.scheme
    host = request.url.netloc
    if _forwarded_trusted():
        scheme = request.headers.get("x-forwarded-proto") or scheme
        host = request.headers.get("x-forwarded-host") or host
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


def password_scope(meta: ArtifactMeta) -> str:
    """Cookie scope binding an unlock cookie to the *current* password record.

    A cookie signed under the previous password no longer verifies once the
    owner replaces or clears the password, because the scope mixed into the
    signature changed. Only a prefix of the stored PBKDF2 digest is used: it
    is already a one-way hash, never leaves the server, and a prefix is enough
    to change whenever the record does.
    """
    record = meta.password
    if not isinstance(record, dict):
        return "open"
    digest = record.get("hash")
    if not isinstance(digest, str) or not digest:
        return "open"
    return digest[:16]


def reader_allowed(meta: ArtifactMeta, request: Request) -> bool:
    """True when the caller may read a (possibly password-protected) artifact.

    Raises ``HTTPException`` 429 when this client has burnt its hourly budget
    of failed password attempts on this artifact — a wrong password is cheap
    to send and expensive (PBKDF2) to check.
    """
    if not meta.password:
        return True
    # The cookie is checked first: it is a cheap HMAC, and a reader who
    # already unlocked must never be caught by the brute-force throttle.
    cookie = request.cookies.get(f"art_{meta.id}")
    if cookie and request.app.state.signer.check(
        meta.id, cookie, settings.unlock_cookie_max_age_s, password_scope(meta)
    ):
        return True
    supplied = request.headers.get("x-artifact-password")
    if not supplied:
        return False
    client_ip = _client_ip(request)
    if _unlock_throttled(meta.id, client_ip):
        raise HTTPException(
            status_code=429,
            detail=(
                "too many wrong passwords for this artifact from your "
                f"address; at most {settings.max_unlock_attempts_per_hour} "
                "failed attempts are allowed per hour"
            ),
        )
    if check_password(supplied, meta.password):
        return True
    _record_unlock_failure(meta.id, client_ip)
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
    responses={
        200: {
            "description": (
                "JSON object with 'received_header_names': the sorted header "
                "names of this request, values omitted."
            )
        }
    },
)
def health_headers(request: Request) -> dict[str, Any]:
    """Diagnostic: names of request headers that reached the app.

    Values are never echoed — this exists to detect reverse proxies that strip
    custom headers (which would silently break header-based authentication).
    """
    return {"received_header_names": sorted(request.headers.keys())}


def _framed(envelope: Envelope) -> HTMLResponse:
    """One version, wrapped in the zero-chrome sandboxed-iframe page.

    The browser-facing read paths never hand a publisher's document to the
    hub's own origin any more: the artifact runs inside an iframe sandboxed
    without ``allow-same-origin``, i.e. in an opaque origin, so its scripts
    cannot reach the ``sessionStorage`` that ``/admin`` and ``/a/{id}/review``
    use for a visitor's Storage token. ``frame-ancestors 'self'`` keeps the
    wrapper itself from being embedded elsewhere; no other CSP directive is
    set, because the artifact inside ``srcdoc`` must keep rendering exactly as
    published. Machines that need the bytes use ``/a/{id}/raw``.
    """
    return HTMLResponse(
        artifact_frame_page(envelope.title, envelope.html),
        headers={"Content-Security-Policy": "frame-ancestors 'self'"},
    )


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


def _thread_not_found(artifact_id: str, thread_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "comment thread not found",
            "id": artifact_id,
            "thread_id": thread_id,
        },
    )


def _document_is_final(artifact_id: str, what: str) -> JSONResponse:
    """409 for any write frozen by ``status: final``."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "document is final",
            "detail": (
                f"This artifact is marked final, so {what} are frozen. Its "
                "owner can reopen it with PUT /api/artifacts/{id} and "
                '{"status": "draft"}.'
            ),
            "id": artifact_id,
        },
    )


def _comments_closed(meta: ArtifactMeta) -> JSONResponse:
    """403 for a caller the comment policy does not admit."""
    if meta.comments_mode == "off":
        detail = (
            "commenting is closed on this artifact; its owner can reopen it "
            "with comments_mode 'anyone' or 'allowlist'"
        )
    else:
        detail = (
            "this artifact only accepts comments from projects on its "
            "contributor allowlist; ask its owner to add your project"
        )
    return JSONResponse(
        status_code=403,
        content={"error": "comments not allowed", "detail": detail, "id": meta.id},
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

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": (
                        "# Q3 review\n\n"
                        "Shipped the new ingest path.\n\n"
                        "| metric | Q2 | Q3 |\n|---|---|---|\n"
                        "| runs | 120 | 184 |\n"
                    ),
                    "title": "Q3 review",
                    "accept_versions": True,
                }
            ]
        }
    }

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

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": "# Q3 review\n\nCorrected the Q3 run count.\n",
                    "title": "Q3 review (corrected)",
                    "accept_versions": True,
                }
            ]
        }
    }

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
            "Legacy two-state switch for version contributions: true means "
            "'anyone', false means 'off'. Prefer 'accept_versions_mode'; "
            "sending both is a 422. Omit to leave unchanged."
        ),
    )
    accept_versions_mode: str | None = Field(
        None,
        description=(
            "Who may submit versions: 'off' (owner only), 'anyone' (any "
            "verified Keboola project, moderated as proposals) or "
            "'allowlist' (only the projects in 'contributors'). Omit to "
            "leave unchanged."
        ),
    )
    contributors: list[str] | None = Field(
        None,
        description=(
            "Owner keys allowed by the 'allowlist' modes, each shaped "
            "'{project_id}@{stack hostname}' (for example "
            f"'123@connection.keboola.com'). At most {MAX_CONTRIBUTORS} "
            "entries. Replaces the whole list; omit to leave unchanged."
        ),
    )
    comments_mode: str | None = Field(
        None,
        description=(
            "Who may open inline comment threads: 'anyone' (the default), "
            "'allowlist' (only the projects in 'contributors') or 'off'. "
            "The owner may always comment unless the artifact is final. "
            "Omit to leave unchanged."
        ),
    )
    status: str | None = Field(
        None,
        description=(
            "'draft' (the default) or 'final'. Marking an artifact final "
            "freezes it: new versions and new comments answer 409 for "
            "everyone, the owner included. Set it back to 'draft' to reopen. "
            "Omit to leave unchanged."
        ),
    )


class CommentBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments``.

    The anchor is a W3C-style TextQuoteSelector captured from the *rendered*
    text of one version: the quote itself plus a little surrounding context so
    a repeated quote can still be told apart.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "version": 2,
                    "exact": "runs grew to 184",
                    "prefix": "In Q3 the ",
                    "suffix": " across every project.",
                    "body": "Is this the deduplicated count?",
                }
            ]
        }
    }

    version: int = Field(
        ...,
        description=(
            "Version the quote was taken from. The thread stays bound to it — "
            "there is no cross-version re-anchoring."
        ),
    )
    exact: str = Field(
        ...,
        description=(
            "The quoted text itself, exactly as rendered. Must not be blank."
        ),
    )
    prefix: str = Field(
        "",
        description=(
            "Rendered text immediately before the quote (about 32 "
            "characters). Used to disambiguate a quote that occurs more than "
            "once."
        ),
    )
    suffix: str = Field(
        "",
        description="Rendered text immediately after the quote (about 32 characters).",
    )
    body: str = Field(
        ..., description="The comment itself, as plain text. Must not be blank."
    )


class ReplyBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments/{tid}/replies``."""

    model_config = {
        "json_schema_extra": {
            "examples": [{"body": "Yes — deduplicated, same as Q2."}]
        }
    }

    body: str = Field(
        ..., description="The reply itself, as plain text. Must not be blank."
    )


class ResolveBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/comments/{tid}/resolve``."""

    model_config = {"json_schema_extra": {"examples": [{"resolved": True}]}}

    resolved: bool = Field(
        True,
        description=(
            "true (the default, and what an empty body means) resolves the "
            "thread; false reopens a resolved one. Both are available to the "
            "artifact owner and to the thread's author."
        ),
    )


class VersionBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/versions``.

    Same content shape as publishing: exactly one of ``html``, ``markdown`` or
    ``git_url``, plus an optional note describing what changed.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": "# Q3 review\n\nFixed the Q3 totals.\n",
                    "note": "fix Q3 totals",
                }
            ]
        }
    }

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

    model_config = {
        "json_schema_extra": {"examples": [{"mode": "pinned", "version": 2}]}
    }

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


def strip_git_userinfo(git_url: str) -> str:
    """Remove ``user:password@`` userinfo from a git URL.

    A submitted ``https://user:token@github.com/org/repo`` used to be stored
    verbatim in the version envelope and echoed back through public metadata
    and version history, leaking the credential to every capability-URL
    holder. The builder already scrubs it out of clone output, but the
    envelope kept the original — so it is stripped here, before validation,
    storage or any response. Private clones do not need it: they authenticate
    with the separate, request-scoped ``git_token``/``git_username`` fields.
    """
    parsed = urlsplit(git_url)
    if not parsed.netloc or "@" not in parsed.netloc:
        return git_url
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


def _normalize_git_url(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Strip userinfo from ``body.git_url`` in place, before anything uses it."""
    if body.git_url is not None:
        body.git_url = strip_git_userinfo(str(body.git_url))


def _check_git_credentials(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Reject git-only fields that were sent without a ``git_url``.

    Covers both the clone credentials and ``git_ref``/``git_path``: silently
    ignoring any of them would leave the caller believing a token was used, or
    a branch checked out, when it was not. So this is a hard 422.
    """
    if body.git_url is not None:
        return
    stray = [
        name
        for name in ("git_ref", "git_path", "git_username", "git_token")
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


def _validate_contributors(keys: list[str]) -> list[str]:
    """Clean and validate a contributor allowlist, or raise 422.

    Entries are owner keys (``{project_id}@{stack hostname}``); a typo would
    otherwise be stored as an entry that can never match anybody.
    """
    cleaned: list[str] = []
    for raw in keys:
        key = str(raw).strip()
        if not key:
            continue
        if not _CONTRIBUTOR_KEY.match(key):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"contributor {key!r} is not a project key; use "
                    "'{project_id}@{stack hostname}', for example "
                    "'123@connection.keboola.com'"
                ),
            )
        if key not in cleaned:
            cleaned.append(key)
    if len(cleaned) > MAX_CONTRIBUTORS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many contributors ({len(cleaned)}); the limit is "
                f"{MAX_CONTRIBUTORS}"
            ),
        )
    return cleaned


def _apply_policy(meta: ArtifactMeta, body: UpdateBody) -> None:
    """Apply the contribution/comment/status fields of a PUT onto ``meta``.

    Every unknown value is a 422 rather than a silent fallback, so an owner is
    never told "saved" about a policy the hub did not actually adopt.
    """
    if body.accept_versions is not None and body.accept_versions_mode is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "provide either 'accept_versions' (legacy boolean) or "
                "'accept_versions_mode', not both"
            ),
        )
    if body.accept_versions_mode is not None:
        if body.accept_versions_mode not in ACCEPT_MODES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "accept_versions_mode must be one of "
                    f"{', '.join(ACCEPT_MODES)}"
                ),
            )
        meta.accept_versions_mode = body.accept_versions_mode
    elif body.accept_versions is not None:
        meta.accept_versions = bool(body.accept_versions)

    if body.contributors is not None:
        meta.contributors = _validate_contributors(body.contributors)

    if body.comments_mode is not None:
        if body.comments_mode not in COMMENTS_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"comments_mode must be one of {', '.join(COMMENTS_MODES)}",
            )
        meta.comments_mode = body.comments_mode

    if body.status is not None:
        if body.status not in ARTIFACT_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {', '.join(ARTIFACT_STATUSES)}",
            )
        meta.status = body.status


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
        # transient and must not reach the stored envelope. "private" records
        # *that* a credential was needed (never the credential itself), so the
        # public read path can withhold the repository URL — the name of a
        # private repo is itself information its owner did not publish.
        source = {
            "git": {
                "url": str(body.git_url),
                "ref": body.git_ref,
                "path": body.git_path,
                "commit": built.git_commit,
            }
        }
        if body.git_token:
            source["git"]["private"] = True
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


def _public_version_meta(row: dict) -> dict:
    """One version's public metadata with a private repo's URL withheld.

    ``Envelope.public_meta`` lives in the store and hands out the whole ``git``
    dict; it cannot tell a public repository from one that needed a token. A
    private repository's URL names infrastructure its owner never published,
    so it is dropped here, at response time, while ref/path/commit and the
    ``private`` flag itself stay — provenance without the address.
    """
    git = row.get("git")
    if not isinstance(git, dict) or not git.get("private"):
        return row
    return {**row, "git": {k: v for k, v in git.items() if k != "url"}}


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
        "accept_versions_mode": meta.accept_versions_mode,
        "contributors": list(meta.contributors),
        "comments_mode": meta.comments_mode,
        "artifact_status": meta.status,
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
    description=(
        "Human-facing HTML documentation of the hub: what it does, how to "
        "authenticate, copy-pasteable curl examples, and links to the admin "
        "studio (/admin), the agent definition (/agent) and the skill "
        "(/skill). Needs no credentials and returns a complete HTML document."
    ),
    responses={
        200: {"description": "The landing page.", "content": CONTENT_HTML}
    },
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
    description=(
        "Always returns 200 with the plain-text body 'OK'. Used by the Keboola "
        "Data App platform proxy to check that the process is up; it takes no "
        "body, no parameters and no credentials, and is not meant to be called "
        "by clients."
    ),
    responses={
        200: {"description": "Always; body is 'OK'.", "content": CONTENT_TEXT}
    },
)
def landing_probe() -> PlainTextResponse:
    """Platform startup check — the proxy POSTs to ``/`` to see if we are up."""
    return PlainTextResponse("OK")


@app.get(
    "/health",
    tags=["service"],
    summary="Liveness and index statistics",
    description=(
        "Reports process liveness, the running service version, and whether "
        "the in-memory artifact index has finished hydrating from Storage. "
        "'hydrated': false means Storage was unreachable at startup and the "
        "hub is serving in degraded mode (individual artifacts still resolve, "
        "one Storage lookup at a time). Unauthenticated."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'status', 'version', 'artifacts' (indexed count) "
                "and 'hydrated'."
            )
        }
    },
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
    description=(
        "Full manifest for agents: endpoint catalog, auth model, stack "
        "aliases, publish body schema, the versioning model, and the limits "
        "this deployment is actually configured with. Intended to be fetched "
        "once before scripting against the API — it is the authoritative "
        "source when this document and the running service disagree. "
        "Unauthenticated; contains no owner or token details."
    ),
    responses={
        200: {
            "description": (
                "The manifest: 'service', 'version', 'base_url', 'auth', "
                "'endpoints', 'publish_body', 'versioning', 'limits', 'notes'."
            )
        }
    },
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
                "path": "/health/headers",
                "auth": "none",
                "purpose": (
                    "diagnostic: the names (never the values) of the request "
                    "headers that reached the app, to detect a proxy that "
                    "strips X-StorageApi-Token or X-Storage-Stack"
                ),
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
                "path": "/agent",
                "auth": "none",
                "purpose": (
                    "a ready-to-install Claude Code subagent definition "
                    "(text/markdown); install with 'install -d "
                    "~/.claude/agents && curl -fsSL {base}/agent -o "
                    "~/.claude/agents/artifact-hub.md'"
                ),
            },
            {
                "method": "GET",
                "path": "/changelog",
                "auth": "none",
                "purpose": (
                    "rendered CHANGELOG.md, through the standard artifact "
                    "template (HTML); read fresh from disk on every request"
                ),
            },
            {
                "method": "GET",
                "path": "/changelog.md",
                "auth": "none",
                "purpose": (
                    "raw CHANGELOG.md source (text/markdown), for machines; "
                    "read fresh from disk on every request"
                ),
            },
            {
                "method": "GET",
                "path": "/admin",
                "auth": (
                    "none to load the page; the visitor's own storage token, "
                    "entered in the browser, for every call it makes"
                ),
                "purpose": (
                    "owner/moderation studio (HTML): list your artifacts, "
                    "review and diff proposals, promote, reject, pin or "
                    "delete versions. The token stays in the browser tab "
                    "(sessionStorage) and is only sent as the usual "
                    "management headers."
                ),
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
                "purpose": (
                    "rendered head version, inside a full-viewport iframe "
                    "sandboxed without allow-same-origin (the document runs "
                    "in an opaque origin); use /a/{id}/raw for the bytes"
                ),
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
                "method": "GET",
                "path": "/a/{id}/comments",
                "auth": "url capability",
                "purpose": "every inline comment thread as JSON, oldest first",
            },
            {
                "method": "GET",
                "path": "/a/{id}/review",
                "auth": (
                    "url capability to read; the visitor's own storage token, "
                    "entered in the browser, to comment"
                ),
                "purpose": (
                    "two-pane review UI (HTML): the document in a sandboxed "
                    "iframe beside its comment threads. Select text to open a "
                    "thread, click a highlight to jump to one, reply and "
                    "resolve in place."
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/export/markdown",
                "auth": "url capability",
                "purpose": (
                    "head version's markdown source (or its HTML document) as "
                    "a file attachment"
                ),
            },
            {
                "method": "GET",
                "path": "/a/{id}/export/vault",
                "auth": "url capability",
                "purpose": (
                    "the whole artifact as a ready-to-open Obsidian vault "
                    "(application/zip): INDEX.md, document.md, versions/, "
                    "comments/ and reasoning.md"
                ),
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
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments",
                "auth": "storage token (subject to comments_mode)",
                "purpose": (
                    "open an inline comment thread on a quoted passage of one "
                    "version"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments/{tid}/replies",
                "auth": "storage token (subject to comments_mode)",
                "purpose": "reply in an existing thread",
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/comments/{tid}/resolve",
                "auth": "storage token (owner, or the thread's author)",
                "purpose": (
                    "resolve a thread, or reopen it with {'resolved': false}"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/comments/{tid}",
                "auth": "storage token (owner, or the thread's author)",
                "purpose": "delete a thread and its replies",
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
                "git_ref, git_path, git_token and git_username are only valid "
                "together with git_url (422 otherwise)",
                "userinfo in git_url (https://user:pass@host/...) is stripped "
                "before the URL is validated, cloned, stored or returned",
                "PUT accepts the same fields, all optional, plus clear_password, "
                "accept_versions/accept_versions_mode, contributors, "
                "comments_mode and status; a title is only valid together "
                "with new content, because a title lives on a version",
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
                "project per artifact per UTC day (429 afterwards); owner "
                "content updates through PUT /api/artifacts/{id} count "
                "against the same budget"
            ),
            "deletion": (
                "The owner may delete any version except the last live one "
                "(409); a contributor may withdraw their own proposal."
            ),
        },
        "collaboration": {
            "accept_versions_mode": (
                "'off' (owner only, the default), 'anyone' (any verified "
                "project, moderated as proposals) or 'allowlist' (only the "
                "projects listed in contributors). The legacy boolean "
                "accept_versions still works: false maps to 'off', true to "
                "'anyone'."
            ),
            "comments_mode": (
                "'anyone' (the default), 'allowlist' or 'off'. The owner may "
                "always comment unless the artifact is final."
            ),
            "contributors": (
                "list of owner keys shaped '{project_id}@{stack hostname}', "
                f"at most {MAX_CONTRIBUTORS}; used by both allowlist modes"
            ),
            "status": (
                "'draft' or 'final'. 'final' freezes the artifact: new "
                "versions, new comments and content updates all answer 409 "
                "for everyone, the owner included. The owner reopens it with "
                "PUT /api/artifacts/{id} and {'status': 'draft'}."
            ),
            "comment_anchoring": (
                "A thread carries a W3C-style TextQuoteSelector (exact + "
                "prefix + suffix) captured from the rendered text of one "
                "version, and stays bound to that version: there is no "
                "cross-version re-anchoring, so a thread made on an older "
                "version may not highlight anything on the current one."
            ),
            "comment_visibility": (
                "Threads are readable by anyone holding the capability URL "
                "(GET /a/{id}/comments); identities are reduced to project "
                "id, project name and stack hostname."
            ),
            "comment_rate_limit": (
                f"{settings.max_comments_per_day} comments and replies per "
                "project per artifact per UTC day (429 afterwards)"
            ),
            "export": (
                "GET /a/{id}/export/markdown downloads the served version's "
                "source; GET /a/{id}/export/vault downloads a deterministic "
                "Obsidian vault ZIP of the whole history and discussion."
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
            "max_comments_per_day": settings.max_comments_per_day,
            "max_contributors": MAX_CONTRIBUTORS,
            "max_unlock_attempts_per_hour": settings.max_unlock_attempts_per_hour,
        },
        "notes": [
            "GET /a/{id} and /a/{id}/v/{n} return a wrapper page whose "
            "full-viewport iframe carries the artifact as srcdoc, sandboxed "
            "without allow-same-origin: the document runs in an opaque origin "
            "and cannot touch this origin's storage or cookies. "
            "GET /a/{id}/raw returns the same bytes unwrapped, for machines — "
            "anything that renders them does so in its own context.",
            "The reader password gate is throttled: after "
            f"{settings.max_unlock_attempts_per_hour} failed attempts per "
            "artifact per client address per hour it answers 429. Successful "
            "unlocks never count. An unlock cookie is bound to the password "
            "that issued it, so changing or clearing the password revokes "
            "every cookie immediately.",
            "A version published from a private repository (one that needed "
            "git_token) reports git.private true in public metadata and "
            "history, and its git.url is withheld.",
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
    response_class=MarkdownResponse,
    summary="Agent-facing SKILL.md",
    description=(
        "Serves skills/artifact-publisher/SKILL.md verbatim as text/markdown, "
        "teaching an AI agent how to authenticate, publish artifacts and "
        "contribute versions unassisted. Unauthenticated, and identical for "
        "every caller. Use /agent instead for a ready-to-install Claude Code "
        "subagent definition."
    ),
    responses={
        200: {
            "description": "The SKILL.md document.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "SKILL.md is not readable on this deployment."},
    },
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
    "/agent",
    tags=["service"],
    response_class=MarkdownResponse,
    summary="Ready-to-install Claude Code subagent definition",
    description=(
        "Serves skills/artifact-hub-agent/AGENT.md verbatim as text/markdown: "
        "a self-contained Claude Code subagent definition (YAML front matter "
        "plus instructions) that knows how to publish, update and moderate "
        "artifacts on this hub. Install it with:\n\n"
        "`install -d ~/.claude/agents && curl -fsSL {base}/agent -o "
        "~/.claude/agents/artifact-hub.md`\n\n"
        "Unauthenticated, and identical for every caller."
    ),
    responses={
        200: {
            "description": "The AGENT.md subagent definition.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "AGENT.md is not readable on this deployment."},
    },
)
def agent() -> Response:
    """Serve the ready-to-install Claude Code subagent definition."""
    try:
        text = AGENT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("AGENT.md not readable at %s", AGENT_PATH)
        return JSONResponse(
            status_code=404, content={"error": "agent definition not available"}
        )
    return Response(content=text, media_type="text/markdown; charset=utf-8")


def _read_changelog() -> str | None:
    """Read CHANGELOG.md fresh from disk; None when it cannot be read.

    Deliberately not cached at import or module scope: another process may
    rewrite the file while this one keeps running, and both /changelog and
    /changelog.md must reflect the current content on every request.
    """
    try:
        return CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("CHANGELOG.md not readable at %s", CHANGELOG_PATH)
        return None


def _changelog_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"error": "changelog not available"}
    )


@app.get(
    "/changelog",
    tags=["service"],
    response_class=HTMLResponse,
    summary="Rendered changelog",
    description=(
        "Serves CHANGELOG.md from the repository root, rendered through the "
        "same Markdown template used for published artifacts (headings, "
        "tables, dark mode). Read fresh from disk on every request, so "
        "changes to the file appear immediately. Unauthenticated, and "
        "identical for every caller. Use /changelog.md for the raw source."
    ),
    responses={
        200: {"description": "The rendered changelog page.", "content": CONTENT_HTML},
        404: {"description": "CHANGELOG.md is not readable on this deployment."},
    },
)
def changelog() -> Response:
    """Serve CHANGELOG.md rendered through the standard artifact template."""
    text = _read_changelog()
    if text is None:
        return _changelog_not_found()
    built = builder.build_from_markdown(text)
    return HTMLResponse(built.html)


@app.get(
    "/changelog.md",
    tags=["service"],
    response_class=MarkdownResponse,
    summary="Raw changelog source",
    description=(
        "Serves CHANGELOG.md from the repository root verbatim as "
        "text/markdown, for machines that want the source rather than the "
        "rendered page. Read fresh from disk on every request. "
        "Unauthenticated, and identical for every caller. Use /changelog for "
        "the rendered HTML page."
    ),
    responses={
        200: {
            "description": "The CHANGELOG.md document.",
            "content": CONTENT_MARKDOWN,
        },
        404: {"description": "CHANGELOG.md is not readable on this deployment."},
    },
)
def changelog_md() -> Response:
    """Serve CHANGELOG.md verbatim as text/markdown."""
    text = _read_changelog()
    if text is None:
        return _changelog_not_found()
    return Response(content=text, media_type="text/markdown; charset=utf-8")


@app.get(
    "/admin",
    tags=["service"],
    response_class=HTMLResponse,
    summary="Owner and moderation studio (browser UI)",
    description=(
        "A single self-contained HTML page for artifact owners: list the "
        "artifacts your project owns, open a version history, read a pending "
        "proposal, diff it against the head, then promote, reject, pin or "
        "delete it, and toggle accept_versions.\n\n"
        "The page itself is public and needs no credentials — authentication "
        "happens entirely in the browser. The visitor pastes their Storage "
        "token and picks a stack; the page keeps them in a JavaScript variable "
        "and in sessionStorage (per-tab, cleared when the tab closes) and "
        "sends them as the usual X-StorageApi-Token / X-Storage-Stack headers "
        "on the very same API calls curl would make. The server never sees or "
        "stores the token beyond serving those ordinary API requests."
    ),
    responses={
        200: {"description": "The admin studio page.", "content": CONTENT_HTML}
    },
)
def admin(request: Request) -> HTMLResponse:
    """Serve the client-side moderation studio.

    Deliberately a static document: no server-side session, no token handling,
    nothing to log. Everything it does, it does from the visitor's browser
    against the public API.
    """
    return HTMLResponse(
        admin_page(base_url(request), SERVICE_VERSION, GITHUB_REPO_URL)
    )


@app.get(
    "/a/{artifact_id}",
    tags=["public"],
    response_class=HTMLResponse,
    summary="Rendered artifact page (head version)",
    description=(
        "Serves the head version — the newest live version, or the one the "
        "owner pinned. No token is needed: the unguessable id in the URL is "
        "the access control.\n\n"
        "The document is returned inside a minimal wrapper page: a "
        "full-viewport iframe carrying the artifact as srcdoc, sandboxed "
        "without allow-same-origin, so the artifact's own scripts run in an "
        "opaque origin and cannot reach this origin's storage or cookies. "
        "Readers see no difference; machines that want the bytes themselves "
        "use GET /a/{id}/raw.\n\n"
        + PASSWORD_GATE_NOTE
        + "\n\nUntil the caller is unlocked, this returns 401 with the unlock "
        "form as HTML rather than the artifact."
    ),
    responses={
        200: {
            "description": (
                "The wrapper page embedding the head version's HTML document "
                "in a sandboxed iframe."
            ),
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password-protected artifact; the HTML unlock form is returned."
            ),
            "content": CONTENT_HTML,
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
    """Serve the head version sandboxed, or the unlock form when protected."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    envelope = request.app.state.store.get_head(artifact_id)
    if envelope is None:
        return _not_found(artifact_id)
    return _framed(envelope)


@app.post(
    "/a/{artifact_id}/unlock",
    tags=["public"],
    status_code=303,
    summary="Unlock a password-protected artifact",
    description=(
        "Target of the HTML unlock form; the password is sent as an "
        "application/x-www-form-urlencoded 'password' field. On a correct "
        "password this responds 303 to the artifact page and sets a signed, "
        "HttpOnly cookie scoped to /a/{id}, so later visits from that browser "
        "skip the form until the cookie expires. The cookie is bound to the "
        "password that issued it: changing or clearing the artifact's "
        "password revokes every cookie already handed out.\n\n"
        "Failed attempts are throttled per artifact and client address "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR per hour, 429 afterwards); a "
        "correct password never counts against the budget. Machine clients do "
        "not need this endpoint at all — they send X-Artifact-Password on "
        "each read, under the same throttle."
    ),
    responses={
        303: {
            "description": (
                "Correct password; redirects to the artifact page with an "
                "unlock cookie set."
            )
        },
        401: {
            "description": (
                "Wrong password; the unlock form is returned with an error "
                "message."
            ),
            "content": CONTENT_HTML,
        },
        404: RESP_NOT_FOUND,
        429: {
            **RESP_UNLOCK_429,
            "content": CONTENT_HTML,
        },
        502: RESP_HUB_502,
    },
)
def unlock_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    password: str = Form(
        "", description="The artifact's reader password, from the unlock form."
    ),
) -> Response:
    """Password form target: on success set a signed, path-scoped cookie."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if meta.password:
        # Each attempt costs a full PBKDF2, so the budget is checked *before*
        # the hash runs; only failures are counted, so a reader who gets it
        # right on the first try is never throttled.
        client_ip = _client_ip(request)
        if _unlock_throttled(artifact_id, client_ip):
            return HTMLResponse(
                unlock_page(
                    artifact_id,
                    "Too many attempts — wait an hour and try again",
                ),
                status_code=429,
            )
        if not check_password(password, meta.password):
            _record_unlock_failure(artifact_id, client_ip)
            return HTMLResponse(
                unlock_page(artifact_id, "Wrong password"), status_code=401
            )
    response = RedirectResponse(f"/a/{artifact_id}", status_code=303)
    response.set_cookie(
        key=f"art_{artifact_id}",
        # Bound to the current password record: changing or clearing the
        # password invalidates every cookie issued under the old one.
        value=request.app.state.signer.make(artifact_id, password_scope(meta)),
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
    response_class=HTMLResponse,
    summary="Raw artifact HTML (head version)",
    description=(
        "The exact built HTML of the head version, with no unlock-form "
        "fallback — meant for machine consumption. These are the bytes GET "
        "/a/{id} embeds in its sandboxed iframe; served here unwrapped, so "
        "anything that renders them does so in its own context.\n\n"
        + PASSWORD_GATE_NOTE
        + "\n\nA protected "
        "artifact without a valid password answers 401 as JSON, never as a "
        "form."
    ),
    responses={
        200: {
            "description": "The head version's HTML document.",
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password required or wrong; JSON hint pointing at the "
                "X-Artifact-Password header."
            )
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_raw(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
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
    description=(
        "Returns the head version's original Markdown (as text/markdown) for "
        "markdown-sourced artifacts, or the original HTML (as text/html) for "
        "html and git-html artifacts. Markdown rendered from a git repository "
        "has no retained source: that answers 404 with a JSON pointer back to "
        "the repository, ref and commit.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The retained source: Markdown for markdown artifacts, HTML "
                "otherwise."
            ),
            "content": {**CONTENT_MARKDOWN, **CONTENT_HTML},
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: {
            "description": (
                "No artifact with this id, it has no live version, or its "
                "source was not retained (git-sourced Markdown)."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_source(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
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
    description=(
        "JSON metadata of the artifact and its head version: title, source "
        "type, timestamps, size, head version, total and proposed version "
        "counts, the 'protected' and 'accept_versions' flags, and every "
        "public URL. Deliberately carries no owner identity and no password "
        "record. Unlike the content endpoints this is readable even when the "
        "artifact is password-protected — it is metadata only."
    ),
    responses={
        200: {"description": "The artifact's public metadata."},
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        502: RESP_HUB_502,
    },
)
def read_meta(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
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
            **_public_version_meta(head.public_meta(is_head=True)),
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
    response_class=HTMLResponse,
    summary="One specific version",
    description=(
        "Serves a single version, live or proposed, in the same sandboxed "
        "wrapper page as GET /a/{id}. A "
        "*proposed* version is private: only the artifact owner and the "
        "version's own author may read it, and they identify themselves by "
        "sending the two management headers (X-StorageApi-Token and "
        "X-Storage-Stack) on this otherwise unauthenticated read. Anyone else "
        "gets 403.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": "That version's HTML document.",
            "content": CONTENT_HTML,
        },
        401: {
            "description": (
                "Password-protected artifact; the HTML unlock form is returned."
            ),
            "content": CONTENT_HTML,
        },
        403: {
            "description": (
                "This version is a proposal and the caller is neither the "
                "owner nor its author."
            )
        },
        404: {"description": "No artifact, or no such version."},
        422: {"description": "'version' is not an integer."},
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_version(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
) -> Response:
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
    return _framed(envelope)


@app.get(
    "/a/{artifact_id}/versions",
    tags=["public"],
    summary="Version history",
    description=(
        "Lists every version (live and proposed) newest first, with its "
        "number, title, status, author project, note, size, source type and "
        "creation time, plus the current head version and the artifact's "
        "'protected' and 'accept_versions' flags.\n\n"
        "Proposal *metadata* is public to capability-URL holders; proposal "
        "*content* is not — fetching it still requires being the owner or the "
        "author (see GET /a/{id}/v/{n}).\n\n"
        "The 'format' query parameter selects the rendering: 'json' (the "
        "default) returns the machine-readable history; 'html' returns a "
        "styled picker page with links to each version and to the diff of "
        "every adjacent pair. Any other value is treated as 'json'.\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The version history as JSON, or the picker page when "
                "format=html."
            ),
            "content": {"application/json": {}, **CONTENT_HTML},
        },
        401: {
            "description": (
                "Password required or wrong: JSON for format=json, the HTML "
                "unlock form for format=html."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_versions(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    format: str = Query(
        "json",
        description=(
            "Rendering of the history: 'json' (default, machine-readable) or "
            "'html' (a human-readable picker page). Any other value falls "
            "back to 'json'."
        ),
        # Documentation only: the runtime deliberately accepts anything and
        # falls back to JSON, so declaring a Literal here would turn today's
        # silent fallback into a 422.
        json_schema_extra={"enum": ["json", "html"]},
    ),
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
    versions = [
        _public_version_meta(row) for row in store.list_versions(artifact_id)
    ]
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
    description=(
        "Compares two versions of one artifact, given in the path as "
        "'{older}..{newer}' (for example 3..5). Markdown is compared when "
        "both versions carry Markdown source, otherwise the built HTML is.\n\n"
        "The 'format' query parameter selects the rendering: 'html' (the "
        "default) is a side-by-side page for humans, 'unified' is a "
        "text/plain unified diff, and 'json' returns the unified diff plus "
        "added/removed line counts. An unknown value is a 400.\n\n"
        "Either side may be a proposal, in which case the same rule as GET "
        "/a/{id}/v/{n} applies: send the two management headers to read it as "
        "the owner or the author, otherwise 403.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "The diff in the requested rendering: HTML page, plain-text "
                "unified diff, or JSON."
            ),
            "content": {
                **CONTENT_HTML,
                **CONTENT_TEXT,
                "application/json": {},
            },
        },
        400: {
            "description": (
                "Malformed diff spec (not OLD..NEW) or an unknown 'format'."
            )
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        403: {"description": "One side is a proposal the caller may not read."},
        404: {
            "description": "No artifact, or one of the versions does not exist."
        },
        413: {
            "description": (
                "One side is larger than the configured HUB_DIFF_MAX_BYTES."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_diff(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    spec: str = PathParam(..., description=SPEC_DESC),
    format: str = Query(
        "html",
        description=(
            "Rendering of the diff: 'html' (default, side-by-side page), "
            "'unified' (text/plain unified diff) or 'json' (unified diff plus "
            "added/removed counts). Anything else is a 400."
        ),
        # Documentation only — validation stays in compute_diff so an unknown
        # format keeps answering 400 rather than FastAPI's 422.
        json_schema_extra={"enum": ["html", "unified", "json"]},
    ),
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


@app.get(
    "/a/{artifact_id}/comments",
    tags=["public"],
    summary="Inline comment threads",
    description=(
        "Every inline comment thread of this artifact, oldest first, together "
        "with the artifact's current 'comments_mode' and draft/final "
        "'status'. Each thread carries its TextQuoteSelector (the quote plus "
        "a little surrounding context), the comment body, its replies, the "
        "resolved flag and the *project identity* of everyone who spoke — "
        "project id, project name and stack hostname only, never a full stack "
        "URL and never an internal owner key.\n\n"
        "Threads are public to capability-URL holders; writing one needs a "
        "Storage token (POST /api/artifacts/{id}/comments).\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id', 'comments_mode', 'status' and the 'threads' "
                "array (possibly empty)."
            )
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_comments(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
    """List every comment thread of one artifact, oldest first."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()
    threads = request.app.state.comments.list_for(artifact_id)
    return JSONResponse(
        {
            "id": artifact_id,
            "comments_mode": meta.comments_mode,
            "status": meta.status,
            "threads": [thread.public_dict() for thread in threads],
        }
    )


@app.get(
    "/a/{artifact_id}/export/markdown",
    tags=["public"],
    response_class=MarkdownResponse,
    summary="Download the head version's source",
    description=(
        "Downloads the served version as a single file: the original Markdown "
        "for markdown-authored artifacts (text/markdown), or the built HTML "
        "document otherwise (text/html). The response carries a "
        "Content-Disposition attachment filename derived from the version's "
        "title, so a browser 'Save as' lands on something readable.\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": "The head version's source, as a file attachment.",
            "content": {**CONTENT_MARKDOWN, **CONTENT_HTML},
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: {
            "description": (
                "No artifact exists with this id, or it has no live version."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def export_markdown(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
    """The head version's source as a downloadable file."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()
    head = request.app.state.store.get_head(artifact_id)
    if head is None:
        return _not_found(artifact_id)

    filename, content_type, body = export.head_source(head)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/a/{artifact_id}/export/vault",
    tags=["public"],
    summary="Download an Obsidian vault of the whole artifact",
    description=(
        "Builds an in-memory ZIP holding a ready-to-open Obsidian vault: "
        "INDEX.md (a wikilink hub), document.md (the served version), "
        "versions/v{n}.md (one note per version — proposals included — with "
        "author, date, status, note and a diff against its predecessor), "
        "comments/{tid}.md (one note per thread: the quote, the discussion "
        "and the resolution) and reasoning.md, a chronological trail of how "
        "the document ended up this way.\n\n"
        "The archive is deterministic: the same artifact state always "
        "produces byte-identical bytes.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": "The vault, as a ZIP file attachment.",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        },
        401: {
            "description": (
                "Password required or wrong (see the X-Artifact-Password "
                "header)."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def export_vault(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
    """The whole artifact — versions, comments and timeline — as a vault ZIP."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        return _password_required()

    store = request.app.state.store
    # Proposals are moderated content: the public vault must not leak them.
    # Only the owner or a proposal's author (authenticated via the standard
    # token headers) gets them included in their download.
    caller = optional_caller(request)
    envelopes: list[Envelope] = []
    for row in store.list_versions(artifact_id):
        envelope = store.get_version(artifact_id, row["version"])
        if envelope is not None and may_see(meta, envelope, caller):
            envelopes.append(envelope)
    threads = request.app.state.comments.list_for(artifact_id)

    filename, payload = export.build_vault(meta, envelopes, threads)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/a/{artifact_id}/review",
    tags=["public"],
    response_class=HTMLResponse,
    summary="Review UI (document plus inline comments)",
    description=(
        "A two-pane review page: the artifact on the left, its comment "
        "threads on the right. Select any text to open a thread on that "
        "quote, click a highlight to jump to its thread, and reply or resolve "
        "in place.\n\n"
        "The page itself is public and carries no credential. Its JavaScript "
        "fetches this artifact's /raw, /versions and /comments, injects a "
        "small annotation script into the fetched HTML and renders it in a "
        "srcdoc iframe sandboxed *without* allow-same-origin — so the "
        "artifact's own scripts run in an opaque origin and can reach neither "
        "the page nor the Storage token a visitor may sign in with. "
        "Commenting requires signing in inside the page; the token lives in "
        "sessionStorage (shared with /admin) and is only ever sent to this "
        "hub's own API.\n\n" + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {"description": "The review page.", "content": CONTENT_HTML},
        401: {
            "description": (
                "Password-protected artifact; the HTML unlock form is returned."
            ),
            "content": CONTENT_HTML,
        },
        404: RESP_NOT_FOUND,
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_review(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
) -> Response:
    """Serve the review shell; all of its data is fetched client-side."""
    meta = _meta_of(request, artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if not reader_allowed(meta, request):
        # Unlocking here sets the same path-scoped cookie the page's own
        # same-origin fetches then ride on.
        return HTMLResponse(unlock_page(artifact_id, None), status_code=401)
    return HTMLResponse(
        review_page(base_url(request), artifact_id, SERVICE_VERSION)
    )


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
        201: {
            "description": (
                "Artifact published as version 1; returns its id, title, "
                "flags, head version and every public URL."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: not exactly one content field, git credentials "
                "without git_url, or a build failure (bad repo, no entry "
                "file, markdown render error)."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the canonical upload into the caller's project "
                "failed, or the hub's own Storage is unavailable."
            )
        },
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

    _normalize_git_url(body)
    _check_git_credentials(body)
    _require_exactly_one_content(body)

    built, source = _build(body)
    artifact_id = new_artifact_id()

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
    store = request.app.state.store
    # Ordering, and what it buys:
    #
    #   1. the hub-side meta record — the thing store.delete() can roll back,
    #   2. the canonical copy in the *caller's* project, which only needs the
    #      artifact id, and
    #   3. the version envelope, which needs the canonical file id.
    #
    # Uploading the canonical copy first (as this used to) meant a later
    # failure left a stray artifact-* file in someone else's project with
    # nothing on the hub pointing at it. Now a failed canonical upload rolls
    # the meta record back, so nothing half-published survives the request.
    #
    # Residual risk, deliberately not solved here: if the rollback delete
    # itself fails (logged at ERROR below), or the process dies between steps
    # 1 and 3, a meta record with no version can be left behind. It is inert —
    # every read path answers 404 for it, because get_head finds nothing — but
    # it does need reaping by hand. Real transactionality needs a two-phase
    # marker in the store and is out of scope.
    store.save_meta(meta)
    try:
        canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)
    except Exception:
        rolled_back = False
        try:
            rolled_back = store.delete(artifact_id)
        except Exception:  # noqa: BLE001 - the original failure must win
            rolled_back = False
        if not rolled_back:
            logger.error(
                "Rollback of artifact %s failed after its canonical upload "
                "failed; a meta record with no version may remain in Storage "
                "and needs to be reaped by hand",
                artifact_id,
            )
        raise
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
    store.add_version(envelope)
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
    description=(
        "Owner-only. A content field ('html', 'markdown', 'git_url') adds a "
        "new live version; everything else changes artifact-level settings: "
        "'password' / 'clear_password', who may contribute versions "
        "('accept_versions_mode' or the legacy 'accept_versions', plus "
        "'contributors'), who may comment ('comments_mode'), and the "
        "draft/final 'status'. A title is only valid together with new "
        "content, because a title lives on a version.\n\n"
        "Marking the artifact 'final' freezes it: new versions and new "
        "comments answer 409 for everyone, the owner included, and so does a "
        "content update. Setting 'status' back to 'draft' reopens it — "
        "including in the same call that carries the new content."
    ),
    responses={
        200: {
            "description": (
                "Artifact updated; returns its current state, including the "
                "version this call produced and the version now served."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Token is valid but not from the project that owns this "
                "artifact."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: more than one content field, git credentials "
                "without git_url, a title without content, both "
                "'accept_versions' and 'accept_versions_mode', an unknown "
                "mode or status, a malformed contributor key, or a build "
                "failure."
            )
        },
        429: RESP_VERSIONS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the canonical upload failed, or the hub's own "
                "Storage is unavailable."
            )
        },
    },
)
def update_artifact(
    body: UpdateBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
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

    _normalize_git_url(body)
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

    # A final artifact is frozen for content. The owner may reopen it in this
    # very call by sending status="draft" alongside the new content.
    if present and meta.is_final() and body.status != ARTIFACT_DRAFT:
        return _document_is_final(artifact_id, "new versions")

    # An owner update that carries content is a version submission like any
    # other, so it draws on the same per-project daily budget contributors
    # have. Checked before anything is persisted, so a throttled call changes
    # nothing at all.
    if present and not _claim_submission_slot(artifact_id, owner.key):
        return _version_rate_limited()

    now = _now()
    # Build first. A 413/422 from _build used to arrive *after* the settings
    # change had already been saved, leaving an update the caller was told
    # failed but which had half happened. Nothing is persisted until the new
    # content actually exists.
    built = source = None
    if present:
        built, source = _build(body)

    if body.clear_password:
        meta.password = None
    elif body.password:
        meta.password = hash_password(body.password)
    _apply_policy(meta, body)
    meta.updated_at = now
    store.save_meta(meta)

    if built is not None:
        # Residual risk (documented, not fixed here): the settings above are
        # already committed, so a failure in the canonical upload or the
        # version write leaves them applied while the content update did not
        # land. The call answers 502, and the artifact stays readable at its
        # previous version — no half-written version is ever served.
        canonical_file_id = _store_canonical(owner, token, artifact_id, built.html)
        envelope = Envelope(
            id=artifact_id,
            # Replaced by add_version_next, which allocates atomically.
            version=0,
            title=built.title,
            html=built.html,
            source_type=built.source_type,
            source=source or {},
            author=_identity(owner),
            status=STATUS_LIVE,
            canonical_file_id=canonical_file_id,
            created_at=now,
        )
        envelope.version = store.add_version_next(envelope)
    else:
        envelope = store.get_head(artifact_id)
        if envelope is None:
            return _not_found(artifact_id)

    logger.info(
        "Updated artifact %s (owner project %s, version %d, source %s, "
        "%d bytes, protected=%s, versions=%s, comments=%s, status=%s)",
        artifact_id,
        owner.project_id,
        envelope.version,
        envelope.source_type,
        len(envelope.html.encode("utf-8")),
        bool(meta.password),
        meta.accept_versions_mode,
        meta.comments_mode,
        meta.status,
    )
    return _artifact_response(request, meta, envelope, 200)


@app.get(
    "/api/artifacts",
    tags=["artifacts"],
    summary="List the caller's artifacts",
    description=(
        "Lists the artifacts owned by the caller's own project — ownership is "
        "the pair (stack, project id) derived from the Storage token, so the "
        "listing depends only on the credentials, never on a query. Each row "
        "carries the title, timestamps, 'protected' and 'accept_versions' "
        "flags, head version, total version count, pending 'proposed_count', "
        "and every public URL. Newest 'updated_at' first. This is the "
        "endpoint the /admin studio calls to sign a visitor in."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'project_id' and the 'artifacts' array (possibly "
                "empty)."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
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
    description=(
        "Deletes every version, every comment thread and the meta record from "
        "the hub's project, so the artifact and all its URLs stop resolving. "
        "Irreversible. The canonical copies in the authors' own Keboola "
        "projects are left untouched. Requires a Storage token from the "
        "owning project; another project's valid token is a 403."
    ),
    responses={
        200: {
            "description": (
                "Deleted; JSON reporting how many comment threads went with "
                "it and confirming the canonical copies were kept."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Token is valid but not from the project that owns this "
                "artifact."
            )
        },
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — some stored files could "
                "not be removed, so the artifact is still readable and the "
                "call must be retried."
            )
        },
    },
)
def delete_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
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

    if not store.delete(artifact_id):
        # store.delete only reports success when *every* authoritative file is
        # confirmed gone; on a partial failure it keeps the index so the
        # artifact stays readable. Reporting "deleted": true here would tell an
        # owner their content is unreachable while it is still being served.
        logger.error(
            "Partial delete of artifact %s: some Storage files remain",
            artifact_id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "artifact not fully deleted",
                "detail": (
                    "Some stored files could not be removed; the artifact is "
                    "still readable. Retry the delete."
                ),
                "id": artifact_id,
            },
        )
    # Comment threads live in their own files; without this they would outlive
    # the artifact as orphaned Storage files nothing can ever reach again.
    threads = request.app.state.comments.delete_all_for(artifact_id)
    logger.info(
        "Deleted artifact %s and %d comment thread(s) (owner project %s)",
        artifact_id,
        threads,
        owner.project_id,
    )
    return JSONResponse(
        {
            "deleted": True,
            "comment_threads_deleted": threads,
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
    description=(
        "Adds a version to an existing artifact. The owning project's "
        "submissions go live immediately; another project may submit only "
        "when the owner opened the artifact (accept_versions_mode 'anyone', "
        "or 'allowlist' with that project on the contributor list), and its "
        "submission lands as a moderated proposal that stays private until "
        "the owner promotes it. A 'final' artifact accepts nothing from "
        "anybody (409). The canonical copy is stored in the *caller's* own "
        "project with the caller's token."
    ),
    responses={
        201: {
            "description": (
                "Version stored; returns its number, its status ('live' for "
                "the owner, 'proposed' otherwise), the note and its URL."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "This artifact does not accept versions from the caller's "
                "project (accept_versions is off, or the project is not on "
                "the contributor allowlist)."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        413: {"description": "Built HTML exceeds the configured size limit."},
        422: {
            "description": (
                "Invalid body: not exactly one content field, git credentials "
                "without git_url, an over-long note, or a build failure."
            )
        },
        429: RESP_VERSIONS_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached, the "
                "canonical upload failed, or the hub's own Storage is "
                "unavailable."
            )
        },
    },
)
def submit_version(
    body: VersionBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Submit one version: live for the owner, a moderated proposal otherwise."""
    caller, token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)

    # "final" freezes the artifact for everyone, so it gets its own, clearer
    # answer instead of the generic "you may not contribute" 403.
    if meta.is_final():
        return _document_is_final(artifact_id, "new versions")

    is_owner = meta.owner_key == caller.key
    if not meta.allows_versions_from(caller.key):
        raise HTTPException(
            status_code=403,
            detail=(
                "this artifact does not accept versions from your project; "
                "its owner can open it with accept_versions (or "
                "accept_versions_mode 'anyone'), or add your project to the "
                "contributor allowlist"
            ),
        )

    _normalize_git_url(body)
    _check_git_credentials(body)
    _require_exactly_one_content(body)

    if not _claim_submission_slot(artifact_id, caller.key):
        return _version_rate_limited()

    built, source = _build(body)
    # The canonical copy always goes to the *submitter's* project: whoever
    # wrote a version keeps its source of truth.
    canonical_file_id = _store_canonical(caller, token, artifact_id, built.html)

    status = STATUS_LIVE if is_owner else STATUS_PROPOSED
    envelope = Envelope(
        id=artifact_id,
        # Replaced by add_version_next, which allocates atomically.
        version=0,
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
    envelope.version = store.add_version_next(envelope)
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
        200: {
            "description": (
                "Version promoted; reports its new status, the head mode and "
                "the version now served as head."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {"description": "Only the owning project may promote a version."},
        404: {"description": "No artifact, or no such version."},
        409: {"description": "That version is already live."},
        422: {"description": "'version' is not an integer."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def promote_version(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
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
        200: {
            "description": (
                "Version deleted; reports the version now served as head."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": "Not the owner, and not the author of this proposal."
        },
        404: {"description": "No artifact, or no such version."},
        409: {
            "description": (
                "This is the only live version; an artifact must keep one."
            )
        },
        422: {"description": "'version' is not an integer."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — the stored file could not "
                "be removed, so the version is still readable and the call "
                "must be retried."
            )
        },
    },
)
def delete_version(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
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

    # store.delete_version answers False for two very different reasons: the
    # policy refusal below, and a backend delete that did not confirm. The
    # refusal is decided here so the remaining False can only mean "the files
    # are still in Storage", which is a 502, not a 409.
    other_live = [
        row
        for row in store.list_versions(artifact_id)
        if row.get("status") == STATUS_LIVE and row.get("version") != version
    ]
    if envelope.status == STATUS_LIVE and not other_live:
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

    if not store.delete_version(artifact_id, version):
        logger.error(
            "Partial delete of version %d of artifact %s: the Storage file "
            "could not be removed",
            version,
            artifact_id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "version not fully deleted",
                "detail": (
                    "Some stored files could not be removed; the version is "
                    "still readable. Retry the delete."
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
        200: {
            "description": (
                "Head pointer updated; reports the new head mode and the "
                "version now served."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": "Only the owning project may move the head pointer."
        },
        404: RESP_NOT_FOUND,
        422: {
            "description": (
                "Unknown mode, missing 'version' for 'pinned', or a version "
                "that does not exist or is not live."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def set_head(
    body: HeadBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
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


# --------------------------------------------------------------------------
# Inline comments
#
# Reading threads is public (GET /a/{id}/comments); writing one needs any
# verified Storage token, which is what ``require_owner`` returns — despite the
# name it authenticates *a* caller, and each handler below decides for itself
# whether that caller is the artifact owner, a thread author, or neither.
# --------------------------------------------------------------------------


def _thread_response(thread: CommentThread, status_code: int) -> JSONResponse:
    """One thread as the public projection, plus its id under 'thread_id'."""
    return JSONResponse(
        status_code=status_code,
        content={**thread.public_dict(), "thread_id": thread.id},
    )


def _comment_rate_limited() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": "too many comments today",
            "detail": (
                f"Your project may write {settings.max_comments_per_day} "
                "comments and replies on one artifact per UTC day."
            ),
            "limit": settings.max_comments_per_day,
        },
    )


@app.post(
    "/api/artifacts/{artifact_id}/comments",
    status_code=201,
    tags=["comments"],
    summary="Open an inline comment thread",
    description=(
        "Anchors a new thread to a quoted passage of one version, W3C "
        "annotation style: 'exact' is the quote as rendered, 'prefix' and "
        "'suffix' are about 32 characters of surrounding text so a repeated "
        "quote can still be told apart. The thread stays bound to the version "
        "it was made on — there is no cross-version re-anchoring — so a "
        "thread opened on an older version may no longer highlight anything "
        "on the current one.\n\n"
        "Any verified Keboola project may comment while 'comments_mode' is "
        "'anyone' (the default); 'allowlist' restricts it to the artifact's "
        "contributors and 'off' closes it. The owner may always comment "
        "unless the artifact is final."
    ),
    responses={
        201: {
            "description": (
                "Thread created; returns the whole thread (selector, body, "
                "author project, timestamps) plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Commenting is closed on this artifact, or the caller's "
                "project is not on its contributor allowlist."
            )
        },
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        422: {
            "description": (
                "The referenced version does not exist, the quote is empty or "
                "too long, or the comment body is empty or too long."
            )
        },
        429: {
            "description": (
                "This project reached HUB_MAX_COMMENTS_PER_DAY comments and "
                "replies on this artifact today."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def create_comment(
    body: CommentBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Open a thread anchored to a quoted passage of one version."""
    caller, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if meta.is_final():
        return _document_is_final(artifact_id, "new comments")
    if not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    if store.get_version(artifact_id, body.version) is None:
        raise HTTPException(
            status_code=422,
            detail=f"artifact {artifact_id} has no version {body.version}",
        )

    if not _claim_comment_slot(artifact_id, caller.key):
        return _comment_rate_limited()

    thread = CommentThread(
        id=new_artifact_id(),
        artifact_id=artifact_id,
        version=body.version,
        selector=Selector(
            exact=body.exact, prefix=body.prefix, suffix=body.suffix
        ),
        body=body.body,
        author=_identity(caller),
        created_at=_now(),
    )
    try:
        request.app.state.comments.create(thread)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info(
        "Artifact %s got comment thread %s on v%d from project %s",
        artifact_id,
        thread.id,
        thread.version,
        caller.project_id,
    )
    return _thread_response(thread, 201)


@app.post(
    "/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
    status_code=201,
    tags=["comments"],
    summary="Reply in a comment thread",
    description=(
        "Appends a reply to an existing thread. The same policy as opening a "
        "thread applies: 'comments_mode' decides who may write, the owner may "
        "always reply unless the artifact is final, and replies count against "
        "the same per-project daily cap."
    ),
    responses={
        201: {
            "description": (
                "Reply appended; returns the whole updated thread plus "
                "'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Commenting is closed on this artifact, or the caller's "
                "project is not on its contributor allowlist."
            )
        },
        404: RESP_THREAD_404,
        409: RESP_FINAL_409,
        422: {"description": "The reply body is empty or too long."},
        429: {
            "description": (
                "This project reached HUB_MAX_COMMENTS_PER_DAY comments and "
                "replies on this artifact today."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def reply_to_comment(
    body: ReplyBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Append one reply to an existing thread."""
    caller, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    if meta.is_final():
        return _document_is_final(artifact_id, "new comments")
    if not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    comments = request.app.state.comments
    thread = comments.get(artifact_id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if not _claim_comment_slot(artifact_id, caller.key):
        return _comment_rate_limited()

    thread.replies.append(
        Reply(author=_identity(caller), body=body.body, created_at=_now())
    )
    try:
        comments.update(thread)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info(
        "Comment thread %s of artifact %s got a reply from project %s",
        thread_id,
        artifact_id,
        caller.project_id,
    )
    return _thread_response(thread, 201)


@app.post(
    "/api/artifacts/{artifact_id}/comments/{thread_id}/resolve",
    tags=["comments"],
    summary="Resolve or reopen a comment thread",
    description=(
        "Marks a thread resolved and records who resolved it. Available to "
        "the artifact owner and to the thread's own author; anyone else gets "
        "a 403.\n\n"
        "The body is optional: sending nothing (or {\"resolved\": true}) "
        "resolves the thread, and {\"resolved\": false} reopens a resolved "
        "one — the same two principals may do either. Asking for the state "
        "the thread is already in is a 409."
    ),
    responses={
        200: {
            "description": (
                "Thread resolved or reopened; returns the whole updated "
                "thread plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Only the artifact owner and the thread's author may resolve "
                "or reopen it."
            )
        },
        404: RESP_THREAD_404,
        409: {
            "description": (
                "The thread is already resolved (or already open, when "
                "reopening)."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def resolve_comment(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
    body: ResolveBody | None = None,
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner or thread author closes a thread — or reopens it."""
    caller, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)

    comments = request.app.state.comments
    thread = comments.get(artifact_id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if caller.key not in (meta.owner_key, thread.author_key):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner or the thread's author can resolve "
                "or reopen it"
            ),
        )

    wanted = True if body is None else bool(body.resolved)
    if thread.resolved == wanted:
        return JSONResponse(
            status_code=409,
            content={
                "error": (
                    "thread is already resolved" if wanted else "thread is already open"
                ),
                "id": artifact_id,
                "thread_id": thread_id,
            },
        )

    thread.resolved = wanted
    thread.resolved_by = _identity(caller) if wanted else None
    comments.update(thread)

    logger.info(
        "Comment thread %s of artifact %s %s by project %s",
        thread_id,
        artifact_id,
        "resolved" if wanted else "reopened",
        caller.project_id,
    )
    return _thread_response(thread, 200)


@app.delete(
    "/api/artifacts/{artifact_id}/comments/{thread_id}",
    tags=["comments"],
    summary="Delete a comment thread",
    description=(
        "Removes a thread and all its replies from Storage. Available to the "
        "artifact owner (moderation) and to the thread's own author "
        "(withdrawing a comment); anyone else gets a 403. Irreversible."
    ),
    responses={
        200: {"description": "Thread deleted."},
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: {
            "description": (
                "Only the artifact owner or the thread's author may delete it."
            )
        },
        404: RESP_THREAD_404,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def delete_comment(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Owner moderates, or an author withdraws their own thread."""
    caller, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)

    comments = request.app.state.comments
    thread = comments.get(artifact_id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if caller.key not in (meta.owner_key, thread.author_key):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner or the thread's author can delete "
                "this comment"
            ),
        )

    comments.delete(artifact_id, thread_id)
    logger.info(
        "Deleted comment thread %s of artifact %s (by project %s)",
        thread_id,
        artifact_id,
        caller.project_id,
    )
    return JSONResponse(
        {"deleted": True, "id": artifact_id, "thread_id": thread_id}
    )
