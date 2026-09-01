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
from src.comments import (
    CommentStore,
    CommentThread,
    Reply,
    Selector,
    author_key_of,
    guest_author,
)
from src.config import Settings, load_settings
from src.diff import DiffError, compute_diff
from src.kbc import BackendError, KbcFilesBackend
from src.pages import (
    admin_page,
    artifact_frame_page,
    changelog_page,
    landing_page,
    review_page,
    unlock_page,
    versions_page,
    visual_diff_page,
)
from src.security import (
    KEY_LABEL_UNLOCK_COOKIE,
    KEY_LABEL_WEBHOOK,
    CookieSigner,
    check_password,
    derive_key,
    hash_password,
    new_artifact_id,
)
from src.statedb import StateDB
from src.store import (
    ACCEPT_MODES,
    ARTIFACT_DRAFT,
    ARTIFACT_FINAL,
    ARTIFACT_SETTABLE_STATUSES,
    ARTIFACT_TRASHED,
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
from src.webhooks import WebhookDispatcher, WebhookEvent, validate_webhook_url

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

#: Longest display name an invitation may carry.
MAX_INVITATION_NAME_CHARS = 80

#: Header carrying a guest's invitation credential, ``{invitation_id}.{secret}``.
#: The secret half rides the *fragment* of the review URL and is only ever put
#: in this header — never in a path, a query string or a cookie — so it stays
#: out of access logs, referrers and browser history on the server side.
GUEST_HEADER = "X-Artifact-Guest"

# --------------------------------------------------------------------------
# OpenAPI documentation constants
#
# Every route documents its parameters through these, so one wording change
# stays one edit and no operation can quietly ship an undescribed parameter.
# --------------------------------------------------------------------------

#: Description attached to every ``artifact_id`` path parameter of an
#: authenticated ``/api/*`` route: the *internal* handle, which never changes.
ARTIFACT_ID_DESC = (
    "Internal artifact identifier from the publish response ('id'). Stable for "
    "the life of the artifact and unaffected by link rotation, so it stays the "
    "handle for every authenticated operation. Unguessable by design; there is "
    "no public listing."
)

#: Description attached to the identifier in every public ``/a/{...}`` route.
#: The parameter is still *named* ``artifact_id`` (it identifies an artifact,
#: and renaming it would churn every documented path), but what belongs there
#: is the **share id**.
SHARE_ID_DESC = (
    "Public share identifier of the artifact — the capability part of the URL, "
    "as returned in 'share_id' and in every '*_url' field. Equal to the "
    "internal artifact id until the owner rotates the link with POST "
    "/api/artifacts/{id}/rotate-link, after which only the new share id "
    "resolves here and the old one (and the bare artifact id) answer 404. "
    "Unguessable by design; there is no public listing."
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

#: Description of the identifier in the path of a comment *write*. These four
#: routes are the only ones that accept either half of the identity pair,
#: because the review UI and everyone holding a capability URL know an artifact
#: by its share id, while agents and owners address it by the internal one.
COMMENT_TARGET_ID_DESC = (
    "Either identifier of the artifact: the public share id that appears in "
    "its /a/{...} URLs, or — for the artifact's own owner, authenticated with "
    "a Storage token — the internal id from the publish response. The share id "
    "is resolved first and exactly as every other public path resolves one: a "
    "share id that has been rotated away no longer works, and neither does the "
    "bare internal id of an artifact whose link was rotated, except for that "
    "owner. Everyone else gets 404 for a revoked identifier, so rotating the "
    "link revokes comment writes too."
)

#: Description attached to every ``invitation_id`` path parameter.
INVITATION_ID_DESC = (
    "Invitation identifier, as returned by POST "
    "/api/artifacts/{id}/invitations and listed by GET "
    "/api/artifacts/{id}/invitations. This is the half of the invitation "
    "credential that is *not* secret."
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

#: Paragraph appended to every public route that describes an artifact.
#:
#: An artifact carries two independent notions of "status" and they are easy to
#: confuse, so every payload that names one says which it is: a *version's*
#: status is 'live' or 'proposed', while the *document's* status is 'draft' or
#: 'final'. The document status is always reported under the unambiguous
#: 'document_status' key, on every endpoint.
STATUS_VS_DOCUMENT_STATUS_NOTE = (
    "**Two kinds of status.** A *version* has a status of 'live' or "
    "'proposed' (whether that version is served or is still a proposal); the "
    "*document* has a status of 'draft' or 'final' ('final' freezes new "
    "versions and new comments). The document's status is always reported "
    "under 'document_status', so it can be read the same way on every "
    "endpoint, and 'contributions_frozen' is the derived answer to \"may I "
    "still contribute?\" — true when the document is final (or trashed). "
    "'accept_versions'/'accept_versions_mode' remain the owner's raw setting "
    "and are not rewritten by the freeze, so check 'contributions_frozen' "
    "before submitting a version or a comment."
)

#: Paragraph appended to every comment-write route: the guest alternative.
GUEST_WRITE_NOTE = (
    "**Guests.** Instead of a Storage token, this route also accepts a guest "
    "invitation in the X-Artifact-Guest header, shaped "
    "'{invitation_id}.{secret}' — what the #invite= fragment of an invitation "
    "review URL carries. A guest is one invited human without a Keboola "
    "account: they may open threads, reply, and resolve or delete threads "
    "they opened themselves, and nothing else. 'comments_mode' does not gate "
    "them (the invitation is the grant, and the owner issued it), but a final "
    "or trashed artifact freezes them like everybody else, and they draw on "
    "the same daily comment budget, counted per invitation. Their comments "
    "are published as {'kind': 'guest', 'name': ...} — the invitation id "
    "never appears in a public response."
)

#: Paragraph appended to every comment-write route: which id the path takes.
COMMENT_TARGET_NOTE = (
    "**Either identifier works in the path** on this route, unlike the rest of "
    "/api/*: the public share id that /a/{...} URLs carry (resolved first) or "
    "the internal artifact id. Capability-URL holders, the review UI and "
    "invited guests only ever saw the share id, so refusing it here would "
    "make them unable to address the artifact they are looking at. The share "
    "id is resolved with the same rules as every public path, so rotating the "
    "link (POST /api/artifacts/{id}/rotate-link) revokes writes through the "
    "old link as well as reads; the internal id keeps working only for the "
    "artifact's own owner, authenticated with a Storage token."
)

#: Sentence appended to every comment-write route: the reader password applies.
COMMENT_PASSWORD_NOTE = (
    "**Password-protected artifacts.** The discussion is part of the "
    "protected document, so a write is gated exactly like a read: send the "
    "X-Artifact-Password header (or hold the unlock cookie from POST "
    "/a/{id}/unlock), or the answer is 401. This holds for guests too — an "
    "invitation is a grant to comment, never a way around the reader password "
    "— and for the owner, who reads through the same gate."
)

#: Reused ``responses`` entries, so the same failure never gets two wordings.
RESP_STACK_400 = {"description": "Unknown or disallowed X-Storage-Stack value."}
RESP_COMMENT_401 = {
    "description": (
        "Storage token missing or rejected by the stack — or, when an "
        "X-Artifact-Guest header was sent, an invitation that is unknown to "
        "this artifact, revoked, or whose secret does not verify (the three "
        "guest cases are deliberately indistinguishable) — or the artifact is "
        "password-protected and no valid X-Artifact-Password header or unlock "
        "cookie came with the request."
    )
}
RESP_COMMENTS_429 = {
    "description": (
        "A budget for this artifact is spent: either this project or this "
        "invitation reached HUB_MAX_COMMENTS_PER_DAY comments and replies on "
        "it today, or this client address made "
        "HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR failed password or invitation "
        "attempts on it this hour."
    )
}
RESP_COMMENT_MOD_429 = {
    "description": (
        "This client address made HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR failed "
        "password or invitation attempts on this artifact in the current hour."
    )
}
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
RESP_GUEST_429 = {
    "description": (
        "Too many failed password *or* invitation attempts for this artifact "
        "from this client address in the current hour "
        "(HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR, counted separately for each). "
        "Verifying an invitation secret is as expensive as verifying a "
        "password, so rejected credentials are budgeted the same way."
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
        "The artifact is frozen, so this write is refused. Either its status "
        "is 'final' (error 'document is final'; the owner reopens it with PUT "
        "/api/artifacts/{id} and {\"status\": \"draft\"}) or it is in the "
        "trash (error 'document is trashed'; the owner brings it back with "
        "POST /api/artifacts/{id}/restore)."
    )
}
RESP_OWNER_403 = {
    "description": (
        "Token is valid but not from the project that owns this artifact."
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

# --------------------------------------------------------------------------
# Rate-limit counters
#
# Since 0.7.0 every counter lives in the SQLite sidecar (``src.statedb``), which
# snapshots itself into Storage Files: a redeploy no longer hands everybody a
# fresh daily budget. The three scopes and their key conventions are fixed by
# statedb's contract:
#
#   scope "submissions"     key "{artifact_id}:{contributor_key}"  bucket UTC day
#   scope "comments"        key "{artifact_id}:{contributor_key}"  bucket UTC day
#   scope "unlock_failures" key "{artifact_id}:{client_ip}"        bucket UTC hour
#
# The key always starts with the *internal* artifact id, which is what
# ``StateDB.forget_artifact`` purges by — so purging an artifact takes its
# counters with it.
#
# The dicts below are a fallback for the window where no StateDB is attached to
# the app (an unstarted app object, or a Storage failure that made the sidecar
# unusable). Limits must keep being enforced then, so the counters degrade to
# the pre-0.7.0 per-process behavior rather than to "no limit at all".
# --------------------------------------------------------------------------

COUNTER_SUBMISSIONS = "submissions"
COUNTER_COMMENTS = "comments"
COUNTER_UNLOCK_FAILURES = "unlock_failures"
COUNTER_GUEST_FAILURES = "guest_failures"

_fallback_counts: dict[tuple[str, str, str], int] = {}
_fallback_lock = threading.Lock()

#: Above this many live fallback buckets, stale ones are swept on the next bump.
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


def _counter_key(artifact_id: str, who: str) -> str:
    """Counter key: the internal artifact id first, so purging can find it."""
    return f"{artifact_id}:{who}"


def _statedb(app_obj: FastAPI | None) -> StateDB | None:
    """The app's state sidecar, or None when there is none to use."""
    if app_obj is None:
        return None
    return getattr(app_obj.state, "statedb", None)


def _fallback_bump(scope: str, key: str, bucket: str) -> int:
    """Per-process counter used when the state sidecar is unavailable."""
    entry = (scope, key, bucket)
    with _fallback_lock:
        if len(_fallback_counts) > _SUBMISSION_SWEEP_AT:
            for stale in [k for k in _fallback_counts if k[2] != bucket]:
                del _fallback_counts[stale]
        value = _fallback_counts.get(entry, 0) + 1
        _fallback_counts[entry] = value
        return value


def _bump_counter(app_obj: FastAPI | None, scope: str, key: str, bucket: str) -> int:
    """Add one to a counter and return its new value, sidecar or fallback."""
    database = _statedb(app_obj)
    if database is not None:
        try:
            return database.bump(scope, key, bucket)
        except Exception as exc:  # noqa: BLE001 - a limit must survive a bad DB
            logger.warning("State counter bump failed (%s/%s): %s", scope, key, exc)
    return _fallback_bump(scope, key, bucket)


def _read_counter(app_obj: FastAPI | None, scope: str, key: str, bucket: str) -> int:
    """Current value of a counter, sidecar or fallback."""
    database = _statedb(app_obj)
    if database is not None:
        try:
            return database.count(scope, key, bucket)
        except Exception as exc:  # noqa: BLE001 - a limit must survive a bad DB
            logger.warning("State counter read failed (%s/%s): %s", scope, key, exc)
    with _fallback_lock:
        return _fallback_counts.get((scope, key, bucket), 0)


def _claim_slot(
    app_obj: FastAPI | None,
    scope: str,
    artifact_id: str,
    contributor_key: str,
    limit: int,
) -> bool:
    """Count one write against a per-(artifact, project, UTC day) bucket.

    Returns False when the bucket is exhausted. The bump happens either way —
    a caller who keeps hammering a spent budget keeps being counted, which is
    what makes the window self-limiting rather than a retry loop.
    """
    used = _bump_counter(
        app_obj, scope, _counter_key(artifact_id, contributor_key), _utc_day()
    )
    return used <= limit


def _claim_submission_slot(
    app_obj: FastAPI | None, artifact_id: str, contributor_key: str
) -> bool:
    """Count one version submission; False when the daily cap is exhausted."""
    return _claim_slot(
        app_obj,
        COUNTER_SUBMISSIONS,
        artifact_id,
        contributor_key,
        settings.max_versions_per_day,
    )


def _claim_comment_slot(
    app_obj: FastAPI | None, artifact_id: str, contributor_key: str
) -> bool:
    """Count one comment or reply; False when the daily cap is exhausted."""
    return _claim_slot(
        app_obj,
        COUNTER_COMMENTS,
        artifact_id,
        contributor_key,
        settings.max_comments_per_day,
    )


def _unlock_throttled(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> bool:
    """True when this (artifact, client) burnt its failed-attempt budget.

    Verifying an artifact password runs a full PBKDF2 (200k iterations), so an
    unthrottled gate is both a password oracle and a cheap way to burn the
    hub's CPU. Only failures are counted, so a legitimate reader who types the
    password correctly is never affected, and the hour in the bucket key makes
    the window reset on its own.
    """
    count = _read_counter(
        app_obj,
        COUNTER_UNLOCK_FAILURES,
        _counter_key(artifact_id, client_ip),
        _utc_hour(),
    )
    return count >= settings.max_unlock_attempts_per_hour


def _record_unlock_failure(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> None:
    """Count one *failed* password attempt against the hourly budget."""
    _bump_counter(
        app_obj,
        COUNTER_UNLOCK_FAILURES,
        _counter_key(artifact_id, client_ip),
        _utc_hour(),
    )


def _guest_throttled(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> bool:
    """True when this (artifact, client) burnt its failed-invitation budget.

    Checking an invitation secret runs the same full PBKDF2 as a password, and
    ``GET /a/{id}/guest`` is public, so a known invitation id would otherwise
    be a free CPU-exhaustion primitive as well as an offline-free oracle. The
    budget is the unlock budget (``HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR``) in its
    own scope, so guest probing and password guessing cannot spend each
    other's allowance.
    """
    count = _read_counter(
        app_obj,
        COUNTER_GUEST_FAILURES,
        _counter_key(artifact_id, client_ip),
        _utc_hour(),
    )
    return count >= settings.max_unlock_attempts_per_hour


def _record_guest_failure(
    app_obj: FastAPI | None, artifact_id: str, client_ip: str
) -> None:
    """Count one *failed* guest verification against the hourly budget."""
    _bump_counter(
        app_obj,
        COUNTER_GUEST_FAILURES,
        _counter_key(artifact_id, client_ip),
        _utc_hour(),
    )


def _record_view(app_obj: FastAPI | None, artifact_id: str, kind: str) -> None:
    """Count one successful read of an artifact. Never raises into serving.

    Analytics are strictly best effort: a broken or unstarted state sidecar
    must degrade to "no numbers", never to a failed page load.
    """
    database = _statedb(app_obj)
    if database is None:
        return
    try:
        database.record_view(artifact_id, _utc_day(), kind)
    except Exception as exc:  # noqa: BLE001 - analytics never break serving
        logger.warning("Could not record a %s view of %s: %s", kind, artifact_id, exc)


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
    """Build the stores and sidecars, and try (not require) an initial hydration.

    Four pieces of state hang off ``app.state``: the two content stores, the
    operational-state sidecar (:class:`~src.statedb.StateDB`: rate-limit
    counters and view analytics, snapshotted into Storage Files) and the
    outbound webhook dispatcher. The sidecar is started *after* hydration, so a
    slow Storage listing never delays the counters becoming usable, and both
    background workers are stopped on the way out — the sidecar's ``stop()``
    takes a final snapshot, so a clean shutdown loses nothing.
    """
    app.state.settings = settings
    # One backend instance serves both stores: they read the same host project
    # and only differ in the tags they list (``artifact-hub`` vs.
    # ``artifact-hub-cmt``), so neither ever sees the other's files. The state
    # sidecar shares it too, under its own disjoint ``artifact-hub-state`` tag.
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
    # Neither consumer of the master secret ever sees it raw: each gets its own
    # key derived under a distinct label (see security.derive_key). A webhook
    # receiver necessarily learns the key it verifies signatures with, and must
    # not thereby be able to mint unlock cookies. Switching to a derived key
    # invalidates unlock cookies issued by an older build — that is expected and
    # harmless: they are short-lived, and a reader simply unlocks once more.
    app.state.signer = CookieSigner(
        derive_key(settings.secret_key, KEY_LABEL_UNLOCK_COOKIE)
    )
    app.state.hydrated = False
    statedb = StateDB(
        backend,
        settings.cache_dir / settings.state_db_filename,
        settings.state_snapshot_interval_s,
        settings.state_max_snapshot_bytes,
    )
    app.state.statedb = statedb
    webhooks = WebhookDispatcher(
        settings.webhook_timeout_s,
        settings.webhook_max_attempts,
        derive_key(settings.secret_key, KEY_LABEL_WEBHOOK),
    )
    app.state.webhooks = webhooks
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
    statedb.start()
    # Buckets are ISO-ish strings, so lexicographic order is chronological
    # order and today's day string sorts before today's hour strings
    # ("2026-09-01" < "2026-09-01T14"). Pruning at that boundary drops every
    # window that has already closed and keeps today's intact.
    try:
        removed = statedb.prune_counters_before(_utc_day())
        if removed:
            logger.info("Pruned %d expired rate-limit counter(s)", removed)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not block boot
        logger.warning("Could not prune expired rate-limit counters: %s", exc)
    webhooks.start()
    try:
        yield
    finally:
        webhooks.stop()
        statedb.stop()


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


def artifact_urls(base: str, share_id: str) -> dict[str, str]:
    """Every public URL of one artifact, built from its **share id**.

    Since 0.7.0 ``/a/{...}`` addresses an artifact by the public share id its
    meta record publishes, not by the internal artifact id the ``/api/*``
    routes use. The two are equal until the owner rotates the link, after which
    only the share id resolves publicly — so every URL the API hands out has to
    be built from :attr:`~src.store.ArtifactMeta.share_id`.
    """
    root = f"{base.rstrip('/')}/a/{share_id}"
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


def unlock_cookie_name(meta: ArtifactMeta) -> str:
    """Name of the unlock cookie for one artifact.

    Keyed by the **share id**, because a cookie is only sent back when its path
    prefixes the request path — and the path the browser sees is
    ``/a/{share_id}``. The signed *value* still carries the internal artifact
    id (see :func:`unlock_artifact`), so the cookie identifies an artifact, not
    a URL. A rotated link therefore also drops every unlock cookie: the new
    path has no cookie yet, and readers unlock again.
    """
    return f"art_{meta.share_id}"


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
    cookie = request.cookies.get(unlock_cookie_name(meta))
    if cookie and request.app.state.signer.check(
        meta.id, cookie, settings.unlock_cookie_max_age_s, password_scope(meta)
    ):
        return True
    supplied = request.headers.get("x-artifact-password")
    if not supplied:
        return False
    client_ip = _client_ip(request)
    # Throttle buckets are keyed by the *internal* id, so rotating the link
    # does not hand an attacker a fresh budget.
    if _unlock_throttled(request.app, meta.id, client_ip):
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
    _record_unlock_failure(request.app, meta.id, client_ip)
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


def _document_frozen(meta: ArtifactMeta, what: str) -> JSONResponse:
    """409 for any write frozen by the artifact's status.

    Two very different states freeze an artifact, and telling them apart is the
    whole point of this answer: ``final`` is a deliberate "this is done", while
    ``trashed`` means the artifact is in the trash and its public link is dead.
    The caller needs to know which, because the way out differs — reopen with
    ``PUT /api/artifacts/{id}`` versus restore with
    ``POST /api/artifacts/{id}/restore``.
    """
    if meta.is_trashed():
        return JSONResponse(
            status_code=409,
            content={
                "error": "document is trashed",
                "detail": (
                    f"This artifact is in the trash, so {what} are frozen and "
                    "its public link no longer resolves. Its owner can bring "
                    "it back with POST /api/artifacts/{id}/restore."
                ),
                "id": meta.id,
            },
        )
    return JSONResponse(
        status_code=409,
        content={
            "error": "document is final",
            "detail": (
                f"This artifact is marked final, so {what} are frozen. Its "
                "owner can reopen it with PUT /api/artifacts/{id} and "
                '{"status": "draft"}.'
            ),
            "id": meta.id,
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
    """Fetch an artifact's meta record, hydrating the index first when needed.

    Takes an **internal** artifact id — the ``/api/*`` handle. Public routes go
    through :func:`_public_meta_of`, which resolves the share id first.
    """
    ensure_hydrated(request.app)
    return request.app.state.store.get_meta(artifact_id)


def _public_meta_of(request: Request, public_id: str) -> ArtifactMeta | None:
    """Resolve a public ``/a/{...}`` identifier and load its meta record.

    ``public_id`` is a *share id*: what the URL a reader holds actually
    carries. :meth:`~src.store.ArtifactStore.resolve_share` maps it to the
    internal artifact id, and answers ``None`` for everything that must not
    resolve publicly — a rotated-away link, the bare artifact id of a rotated
    artifact, an artifact in the trash, or an identifier naming nothing. All
    four are the same 404 to the caller, deliberately: distinguishing them
    would turn the endpoint into an oracle for revoked links.

    Every public handler starts here and then works with ``meta.id``; nothing
    downstream ever sees the share id again except URL building.
    """
    ensure_hydrated(request.app)
    artifact_id = request.app.state.store.resolve_share(public_id)
    if artifact_id is None:
        return None
    return request.app.state.store.get_meta(artifact_id)


def _comment_target_of(
    request: Request, path_id: str, caller: Owner | None = None
) -> ArtifactMeta | None:
    """Resolve the identifier in a comment-write path, *share id first*.

    The comment write routes are the one place where both halves of an
    artifact's identity pair are legitimate. The review UI, a capability-URL
    holder and every invited guest only ever saw the **share id**, because that
    is what ``/a/{...}`` carries; an owner also holds the internal id (it is
    what the publish response and ``/api/artifacts`` hand them).

    The share id is therefore resolved the way every other public ``/a/`` path
    resolves one — through :meth:`~src.store.ArtifactStore.resolve_share`, which
    answers ``None`` for a rotated-away link, for the bare internal id of a
    rotated artifact, and for an artifact in the trash. Looking the internal id
    up first (as this helper used to) quietly defeated that: because a fresh
    artifact's share id *is* its internal id, the original public identifier
    kept working for comment writes forever, and rotation stopped being
    revocation for anybody who had seen the link.

    The internal id survives only as an **owner** fallback, and only for a
    token-authenticated caller who owns the artifact — the API ergonomics an
    agent depends on, with none of the public reach. A guest, a stranger's
    token and an anonymous caller all get ``None`` (a 404) for it.
    """
    ensure_hydrated(request.app)
    store = request.app.state.store
    internal_id = store.resolve_share(path_id)
    if internal_id is not None:
        return store.get_meta(internal_id)
    if caller is None:
        return None
    meta = store.get_meta(path_id)
    if meta is None or meta.owner_key != caller.key:
        return None
    return meta


# ------------------------------------------------------------ guest invitations


def _guest_credential(request: Request) -> tuple[str, str] | None:
    """Split the ``X-Artifact-Guest`` header into ``(invitation_id, secret)``.

    ``None`` means "this caller is not presenting a guest credential at all" —
    the header is absent or blank — which is what sends the request down the
    ordinary token-authenticated path. A header that is *present* but
    unparseable is a guest whose link got mangled in a chat client, so it
    raises the guest 401 rather than falling through to token auth and
    answering something about Storage stacks they have never heard of.
    """
    raw = request.headers.get(GUEST_HEADER.lower(), "").strip()
    if not raw:
        return None
    invitation_id, separator, secret = raw.partition(".")
    if not separator or not invitation_id or not secret:
        raise _guest_refused()
    return invitation_id, secret


def _guest_refused() -> HTTPException:
    """The single 401 every guest-credential failure answers with.

    Malformed, unknown, revoked and wrong-secret are deliberately one answer:
    telling them apart would turn the header into an oracle over other
    people's invitations.
    """
    return HTTPException(
        status_code=401,
        detail=(
            "this invitation link is not valid for this artifact; it may have "
            "been revoked, or the link may be incomplete — ask whoever "
            "invited you for a fresh one"
        ),
    )


def _verify_guest(meta: ArtifactMeta, credential: tuple[str, str]) -> dict:
    """Return the invitation this credential proves, or raise 401.

    Three things have to hold: the invitation still exists on this artifact, it
    has not been revoked, and the secret verifies against its stored PBKDF2
    record. All three failures answer :func:`_guest_refused` — a guest must not
    be able to tell "revoked" from "never existed" from "wrong secret".
    """
    invitation_id, secret = credential
    for invitation in meta.invitations or []:
        if invitation.get("id") != invitation_id:
            continue
        if invitation.get("revoked"):
            break
        record = invitation.get("secret")
        if isinstance(record, dict) and check_password(secret, record):
            return invitation
        break
    raise _guest_refused()


def _verify_guest_checked(
    request: Request, meta: ArtifactMeta, credential: tuple[str, str]
) -> dict:
    """:func:`_verify_guest` behind the same failed-attempt budget as unlock.

    Every route that accepts an ``X-Artifact-Guest`` credential goes through
    here rather than calling :func:`_verify_guest` directly. Only *failures*
    are counted, so an invited guest working normally is never affected, while
    somebody grinding secrets against a known invitation id gets 429 long
    before they have spent much of the hub's CPU on PBKDF2.

    Buckets are keyed by the *internal* artifact id, so rotating the link (or
    addressing the artifact by its other identifier) does not hand an attacker
    a fresh budget.
    """
    client_ip = _client_ip(request)
    if _guest_throttled(request.app, meta.id, client_ip):
        raise HTTPException(
            status_code=429,
            detail=(
                "too many rejected invitation credentials for this artifact "
                f"from your address; at most "
                f"{settings.max_unlock_attempts_per_hour} failed attempts are "
                "allowed per hour"
            ),
        )
    try:
        return _verify_guest(meta, credential)
    except HTTPException:
        _record_guest_failure(request.app, meta.id, client_ip)
        raise


def _guest_identity(invitation: dict) -> dict:
    """The author record stored beside a guest's comment."""
    return guest_author(
        str(invitation.get("id") or ""), str(invitation.get("name") or "")
    )


def _guest_actor(invitation: dict) -> str:
    """How a guest is named in a webhook notification."""
    name = str(invitation.get("name") or "").strip() or "someone"
    return f"{name} (guest)"


def _public_invitation(invitation: dict) -> dict:
    """One invitation as its owner may see it — never including the secret."""
    return {
        "id": str(invitation.get("id") or ""),
        "name": str(invitation.get("name") or ""),
        "created_at": str(invitation.get("created_at") or ""),
        "revoked": bool(invitation.get("revoked")),
    }


def _make_room_for_invitation(meta: ArtifactMeta) -> None:
    """Ensure one more invitation fits, or raise 422.

    The cap bounds the meta record, which is a single Storage File rewritten on
    every artifact change — an unbounded list would eventually make the
    artifact itself expensive to load. Revoked invitations are tombstones with
    no remaining use (their secret can never verify again), so the oldest of
    them are dropped to make room before the cap is enforced. That is what
    makes "revoke somebody, invite somebody else" work indefinitely while the
    stored list stays bounded.
    """
    limit = settings.max_invitations_per_artifact
    if len(meta.invitations) < limit:
        return
    live = [inv for inv in meta.invitations if not inv.get("revoked")]
    # Oldest revoked first — the list keeps creation order.
    droppable = [str(inv.get("id")) for inv in meta.invitations if inv.get("revoked")]
    room_needed = len(meta.invitations) - limit + 1
    if room_needed > len(droppable):
        raise HTTPException(
            status_code=422,
            detail=(
                f"this artifact already has {len(live)} live invitations, "
                f"which is the limit of {limit} per artifact; revoke one "
                "before inviting somebody else"
            ),
        )
    dropped = set(droppable[:room_needed])
    meta.invitations = [
        inv for inv in meta.invitations if str(inv.get("id")) not in dropped
    ]


def _owner_only(meta: ArtifactMeta, caller: Owner) -> None:
    """Raise 403 unless the caller's project owns the artifact."""
    if meta.owner_key != caller.key:
        raise HTTPException(
            status_code=403, detail="this artifact belongs to another project"
        )


def _emit_webhook(
    request: Request,
    meta: ArtifactMeta,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: Owner | None = None,
    actor_name: str | None = None,
) -> None:
    """Queue one webhook delivery per URL this artifact registered.

    A no-op when the artifact registered nothing, which is the common case, so
    the cost of the feature on an ordinary publish is one attribute read.

    What goes into the payload is deliberately narrow: the *internal* artifact
    id (the handle its owner already knows), the title, whatever version or
    thread the event is about, the acting project's **name** — never a token,
    never an owner key, never a password record — and the public ``url``, built
    from the share id. That last one is the only capability in the envelope,
    and it is the very link whose owner configured the receiver.

    Never raises into the request path: a webhook is a notification about
    something that already happened and is already durable in Storage.
    """
    urls = list(meta.webhooks or [])
    if not urls:
        return
    dispatcher = getattr(request.app.state, "webhooks", None)
    if dispatcher is None:
        return
    body: dict[str, Any] = {
        "artifact_id": meta.id,
        "url": f"{base_url(request).rstrip('/')}/a/{meta.share_id}",
        **(payload or {}),
    }
    if actor is not None:
        body["actor"] = actor.project_name
    elif actor_name:
        # A guest has no project to name, so the receiver gets the display name
        # their inviter chose, marked as a guest. Still not a capability: it is
        # a label, and it is the one the artifact's own owner typed.
        body["actor"] = actor_name
    try:
        dispatcher.emit(
            urls,
            WebhookEvent(
                artifact_id=meta.id, kind=kind, payload=body, created_at=_now()
            ),
        )
    except Exception as exc:  # noqa: BLE001 - notifications never break a write
        logger.warning(
            "Could not queue the %s webhook for artifact %s: %s", kind, meta.id, exc
        )


def _validate_webhooks(urls: list[str]) -> list[str]:
    """Clean and validate a webhook URL list, or raise 422.

    Each entry goes through :func:`src.webhooks.validate_webhook_url`, which
    enforces https and refuses hosts resolving into private, loopback,
    link-local, reserved or cloud-metadata ranges — the same SSRF guard git
    clones get. A refusal is a 422 carrying the validator's own sentence, so
    the owner learns *why* rather than just "invalid".
    """
    cleaned: list[str] = []
    for raw in urls:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            normalized = validate_webhook_url(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if normalized not in cleaned:
            cleaned.append(normalized)
    if len(cleaned) > settings.max_webhooks_per_artifact:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many webhooks ({len(cleaned)}); the limit is "
                f"{settings.max_webhooks_per_artifact} per artifact"
            ),
        )
    return cleaned


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
            "'trashed' is deliberately not settable here — use DELETE "
            "/api/artifacts/{id} and POST /api/artifacts/{id}/restore, which "
            "also record when it was trashed and what to restore it to. Omit "
            "to leave unchanged."
        ),
    )
    webhooks: list[str] | None = Field(
        None,
        description=(
            "https URLs notified when something happens to this artifact "
            "(version published or proposed, proposal promoted, comment or "
            "reply, finalized, trashed, restored, link rotated). Each delivery "
            "is a small JSON envelope signed with X-Hub-Signature-256; a "
            "hooks.slack.com URL gets Slack's {\"text\": ...} shape instead. "
            "Replaces the whole list; [] clears it; omit to leave unchanged. "
            "URLs must be https and must not resolve to a private, loopback, "
            "link-local or metadata address. Treated as semi-secret: they are "
            "returned in this response only, never in GET /api/artifacts, "
            "which reports 'webhooks_count' instead."
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


class InvitationBody(BaseModel):
    """Body of ``POST /api/artifacts/{id}/invitations``."""

    model_config = {"json_schema_extra": {"examples": [{"name": "Jana (legal)"}]}}

    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_INVITATION_NAME_CHARS,
        description=(
            "Who this invitation is for, as it will appear beside their "
            "comments. Purely a label chosen by the owner — the hub never "
            "verifies it and never emails it anywhere — but it is the only "
            "thing readers see about a guest, so make it recognisable."
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
    base_version: int | None = Field(
        None,
        description=(
            "The version this submission was written against. Must name a "
            "version that exists (422 otherwise). Recorded on the version and "
            "reported in the history, where a proposal whose base_version is "
            "no longer the head is flagged 'outdated': true — so a reviewer "
            "can see that the document moved on while the proposal was being "
            "written. Omit when you did not start from a specific version."
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
    """Remove every credential-bearing part of a git URL: userinfo, query, fragment.

    A submitted ``https://user:token@github.com/org/repo`` used to be stored
    verbatim in the version envelope and echoed back through public metadata
    and version history, leaking the credential to every capability-URL
    holder. The builder already scrubs it out of clone output, but the
    envelope kept the original — so it is stripped here, before validation,
    storage or any response. Private clones do not need it: they authenticate
    with the separate, request-scoped ``git_token``/``git_username`` fields.

    The query string and fragment go the same way, and for the same reason: a
    secret hides just as well in ``?token=...`` (or ``#token=...``) as it does
    in the userinfo, and the stored envelope and the public git provenance
    (``/a/{id}/meta``, ``/a/{id}/versions``) would carry it verbatim. A clone
    URL needs neither component, so dropping both costs nothing and closes the
    remaining leak.
    """
    parsed = urlsplit(git_url)
    if not parsed.netloc:
        # Not a URL with an authority (e.g. an scp-style or relative form);
        # there is nothing to reliably split off, so leave it to validation.
        return git_url
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _normalize_git_url(body: PublishBody | UpdateBody | VersionBody) -> None:
    """Scrub ``body.git_url`` in place, before anything uses it.

    Runs before validation, cloning, storage and every response, so no route
    ever sees the userinfo, query or fragment the caller submitted.
    """
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

    if body.webhooks is not None:
        meta.webhooks = _validate_webhooks(body.webhooks)

    if body.status is not None:
        # ARTIFACT_SETTABLE_STATUSES, not ARTIFACT_STATUSES: "trashed" is
        # reachable only through DELETE (which also stamps trashed_at and the
        # status to restore to) and left only through the restore route.
        # Accepting it here would produce a meta record that claims to be in
        # the trash without either of those, so it is a 422.
        if body.status not in ARTIFACT_SETTABLE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "status must be one of "
                    f"{', '.join(ARTIFACT_SETTABLE_STATUSES)}"
                ),
            )
        if meta.is_trashed():
            raise HTTPException(
                status_code=409,
                detail=(
                    "this artifact is in the trash; restore it with POST "
                    "/api/artifacts/{id}/restore before changing its status"
                ),
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


def _outdated_flag(row: dict, head_version: int | None) -> dict:
    """``{"outdated": bool}`` for a proposal, or nothing for any other row.

    A proposal is *outdated* when its author told us which version they wrote
    it against (``base_version``) and the head has moved on since. That is the
    one thing a reviewer cannot see from the numbers alone: a proposal based on
    v3 while the head is v5 was written without seeing two live versions, and
    promoting it silently reverts them.

    Only proposals carry the key: for a live version — already promoted, or the
    head itself — "outdated" would be meaningless rather than false.
    """
    if row.get("status") != STATUS_PROPOSED:
        return {}
    base = row.get("base_version")
    return {"outdated": base is not None and base != head_version}


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
        # The public half of the identity pair. Equal to "id" until the link is
        # rotated; every URL below is built from it.
        "share_id": meta.share_id,
        "title": envelope.title,
        "protected": bool(meta.password),
        "accept_versions": meta.accept_versions,
        "accept_versions_mode": meta.accept_versions_mode,
        "contributors": list(meta.contributors),
        "comments_mode": meta.comments_mode,
        "artifact_status": meta.status,
        # Full URLs, not a count: this response only ever reaches the owning
        # project, which is who set them. The listing endpoint reports a count.
        "webhooks": list(meta.webhooks),
        "version": envelope.version,
        "status": envelope.status,
        "head_version": _head_version_of(request, meta.id),
        "owner_project_id": meta.owner.get("project_id"),
        "canonical_file_id": envelope.canonical_file_id,
        **artifact_urls(base_url(request), meta.share_id),
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
        landing_page(
            base_url(request),
            SERVICE_VERSION,
            GITHUB_REPO_URL,
            settings.demo_url,
        )
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
            "guest_header": (
                "X-Artifact-Guest: '{invitation_id}.{secret}' — an alternative "
                "to the storage token on the four comment-write routes and on "
                "GET /a/{id}/guest, for an invited human without a Keboola "
                "account. See the 'guests' section."
            ),
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
                "path": "/a/{id}/guest",
                "auth": "X-Artifact-Guest invitation credential",
                "purpose": (
                    "resolve a guest invitation to the display name it "
                    "carries, so a client can say who it is commenting as "
                    "before it writes anything; 401 for a missing, revoked or "
                    "wrong credential"
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
                "purpose": (
                    "move the artifact to the trash: reversible soft delete "
                    "that freezes it and kills its public link"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/restore",
                "auth": "storage token (owner project)",
                "purpose": (
                    "bring a trashed artifact back on the same share id and "
                    "URL"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/purge",
                "auth": "storage token (owner project)",
                "purpose": (
                    "irreversibly erase every version, comment thread, meta "
                    "record and view statistic"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/rotate-link",
                "auth": "storage token (owner project)",
                "purpose": (
                    "mint a new share id; the previous public link stops "
                    "resolving immediately"
                ),
            },
            {
                "method": "GET",
                "path": "/api/artifacts/{id}/stats",
                "auth": "storage token (owner project)",
                "purpose": (
                    "view counts for one artifact: total, by day and by "
                    "surface"
                ),
            },
            {
                "method": "POST",
                "path": "/api/artifacts/{id}/invitations",
                "auth": "storage token (owner project)",
                "purpose": (
                    "invite one person without a Keboola account to comment; "
                    "returns a review URL carrying a one-time secret in its "
                    "fragment"
                ),
            },
            {
                "method": "GET",
                "path": "/api/artifacts/{id}/invitations",
                "auth": "storage token (owner project)",
                "purpose": (
                    "list this artifact's guest invitations (id, name, "
                    "created_at, revoked); never the secrets"
                ),
            },
            {
                "method": "DELETE",
                "path": "/api/artifacts/{id}/invitations/{iid}",
                "auth": "storage token (owner project)",
                "purpose": (
                    "revoke one guest's invitation, leaving every other "
                    "invitation working"
                ),
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
                "userinfo (https://user:pass@host/...), the query string and "
                "the fragment are stripped from git_url before the URL is "
                "validated, cloned, stored or returned — a clone URL needs "
                "none of them, and each can carry a secret",
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
                "?format=unified returns text/plain, ?format=json returns the "
                "unified diff plus added/removed counts, and ?format=visual "
                "shows the two rendered documents themselves side by side in "
                "sandboxed iframes with synchronized scrolling. Markdown is "
                "compared when both versions carry it, otherwise the built "
                "HTML."
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
                "id, project name and stack hostname, and a guest to "
                "{'kind': 'guest', 'name': ...}."
            ),
            "comment_addressing": (
                "The four comment-write routes accept either the public share "
                "id or the internal artifact id in their path (share id "
                "first), because the review UI and every capability-URL "
                "holder only ever saw the share id. The share id resolves "
                "under the public rules, so a rotated-away link cannot write "
                "either; the internal id works only for the artifact's own "
                "owner. Every other /api/* route takes the internal id only."
            ),
            "comment_password_gate": (
                "On a password-protected artifact, writing a comment needs "
                "the same X-Artifact-Password header (or unlock cookie) that "
                "reading it does — for guests and for the owner alike."
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
        "guests": {
            "model": (
                "A guest is one invited human without a Keboola account. An "
                "invitation is a named, revocable capability on one artifact: "
                "POST /api/artifacts/{id}/invitations mints it and returns a "
                "review URL of the form "
                "/a/{share_id}/review#invite={invitation_id}.{secret}."
            ),
            "secret": (
                "Shown exactly once and stored only as a PBKDF2 record, like a "
                "reader password. It rides the URL *fragment*, which browsers "
                "never send to a server, and reaches the API only in the "
                "X-Artifact-Guest header shaped '{invitation_id}.{secret}' — "
                "so it stays out of access logs and Referer headers. A lost "
                "link is replaced by revoking and minting another."
            ),
            "grants": (
                "Open a comment thread, reply, and resolve or delete threads "
                "the guest opened themselves. Never a version, never any "
                "/api/* management call, never another artifact. "
                "comments_mode does not gate a guest (the invitation is the "
                "grant); 'final' and 'trashed' freeze them like everybody "
                "else."
            ),
            "revocation": (
                "DELETE /api/artifacts/{id}/invitations/{iid} turns off one "
                "person's link immediately and leaves every other guest's "
                "working. Comments they already wrote stay."
            ),
            "identity": (
                "A guest's comments are published as {'kind': 'guest', "
                "'name': ...} — the name their inviter chose, and nothing "
                "else. Their daily comment budget is counted per invitation. "
                "GET /a/{id}/guest resolves a credential to that name."
            ),
        },
        "sharing": {
            "model": (
                "An artifact has two identifiers: the internal 'id' used by "
                "every /api/* call, and the public 'share_id' that appears in "
                "/a/{...} URLs. They are equal until the link is rotated."
            ),
            "rotation": (
                "POST /api/artifacts/{id}/rotate-link mints a new share_id. "
                "The previous share id stops resolving immediately, and so "
                "does the bare artifact id once the two differ — there is no "
                "grace period and no way to un-rotate. Unlock cookies are "
                "scoped to the old path and go with it."
            ),
            "trash": (
                "DELETE /api/artifacts/{id} is a reversible soft delete: the "
                "status becomes 'trashed', the public link 404s and versions "
                "and comments freeze (409), but the owner still sees the row "
                "in GET /api/artifacts with a 'trashed_at'. POST "
                "/api/artifacts/{id}/restore undoes it on the same URL; "
                "DELETE /api/artifacts/{id}/purge is the irreversible erase. "
                "'trashed' cannot be set through PUT status."
            ),
            "base_version": (
                "POST /api/artifacts/{id}/versions accepts an optional "
                "base_version naming the version the submission was written "
                "against (422 when it does not exist). GET /a/{id}/versions "
                "flags a proposal 'outdated': true when its base_version is no "
                "longer the head — the document moved on while it was being "
                "written."
            ),
        },
        "webhooks": {
            "registering": (
                "PUT /api/artifacts/{id} with 'webhooks': [...] replaces the "
                "artifact's list ([] clears it); at most "
                f"{settings.max_webhooks_per_artifact} URLs, https only, and "
                "hosts resolving to private, loopback, link-local or metadata "
                "addresses are refused with 422."
            ),
            "events": (
                "version.published, version.proposed, version.promoted, "
                "comment.created, comment.replied, artifact.finalized, "
                "artifact.trashed, artifact.restored, link.rotated. The "
                "initial publish (v1) emits nothing — the owner just did it."
            ),
            "delivery": (
                "POST of {'event', 'artifact_id', 'payload', 'created_at'} "
                "signed with X-Hub-Signature-256: sha256=<hmac>; a "
                "hooks.slack.com URL gets Slack's {'text': ...} shape "
                f"instead. Up to {settings.webhook_max_attempts} attempts with "
                "backoff, best effort: the queue is in memory and a restart "
                "drops what was pending."
            ),
            "secrecy": (
                "A webhook URL is itself a capability, so it is returned only "
                "in the owner PUT response that set it; GET /api/artifacts "
                "reports 'webhooks_count' instead."
            ),
        },
        "analytics": {
            "views": (
                "GET /api/artifacts/{id}/stats (owner only) reports total, "
                "per-day (30 days) and per-surface view counts. Surfaces are "
                "'page', 'raw', 'source' and 'version'."
            ),
            "privacy": (
                "Counts only — no reader identity, address or referrer is "
                "recorded. Purging an artifact forgets its numbers."
            ),
            "durability": (
                "Counters and view rows live in a SQLite sidecar snapshotted "
                "into the host project's Storage Files, so rate limits survive "
                "a redeploy; a crash can lose the last few minutes."
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
            "max_webhooks_per_artifact": settings.max_webhooks_per_artifact,
            "webhook_timeout_s": settings.webhook_timeout_s,
            "webhook_max_attempts": settings.webhook_max_attempts,
            "max_invitations_per_artifact": settings.max_invitations_per_artifact,
            "max_invitation_name_chars": MAX_INVITATION_NAME_CHARS,
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
            "Guest invitation credentials (X-Artifact-Guest) are throttled "
            "the same way and on the same budget size: after "
            f"{settings.max_unlock_attempts_per_hour} rejected credentials "
            "per artifact per client address per hour the answer is 429. "
            "Verifying an invitation secret costs a full PBKDF2, so an "
            "unthrottled public probe would be a CPU-exhaustion primitive.",
            "A version published from a private repository (one that needed "
            "git_token) reports git.private true in public metadata and "
            "history, and its git.url is withheld.",
            "Artifact URLs are capabilities: the unguessable id is the only "
            "access control by default, there is no public listing, and every "
            "/a/* response carries X-Robots-Tag: noindex, nofollow.",
            "/a/{...} addresses an artifact by its share_id, not by the "
            "internal artifact id the /api/* routes use. They start out equal; "
            "after POST /api/artifacts/{id}/rotate-link only the new share id "
            "resolves publicly, and the old link — plus the bare artifact id — "
            "answers 404 from the next request on.",
            "DELETE /api/artifacts/{id} moves an artifact to the trash "
            "(reversible with POST /api/artifacts/{id}/restore); DELETE "
            "/api/artifacts/{id}/purge is the irreversible erase. A trashed "
            "artifact 404s publicly but still appears in its owner's listing.",
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
        "Serves CHANGELOG.md from the repository root, rendered as a page of "
        "this service rather than as a published artifact: the hub's own "
        "shell — graph-paper grid, monospace headings, the same footer as the "
        "landing page — wrapped around the rendered Markdown. Read fresh from "
        "disk on every request, so changes to the file appear immediately. "
        "Unauthenticated, and identical for every caller. Use /changelog.md "
        "for the raw source."
    ),
    responses={
        200: {"description": "The rendered changelog page.", "content": CONTENT_HTML},
        404: {"description": "CHANGELOG.md is not readable on this deployment."},
    },
)
def changelog() -> Response:
    """Serve CHANGELOG.md inside the service's own shell design system.

    The Markdown is rendered by the *builder's* configured markdown-it instance
    (tables, task lists, anchors — the same dialect a published artifact gets)
    but only down to a body fragment: the full artifact page template would
    bring its own standalone look, and the changelog is a page of this service,
    not somebody's document. ``src.pages`` supplies the chrome.
    """
    text = _read_changelog()
    if text is None:
        return _changelog_not_found()
    body_html = builder._render_markdown_body(text)
    return HTMLResponse(
        changelog_page(body_html, SERVICE_VERSION, GITHUB_REPO_URL)
    )


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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Serve the head version sandboxed, or the unlock form when protected."""
    # The path carries the *public* share id; everything past resolution works
    # with the internal artifact id (``meta.id``).
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(public_id, None), status_code=401)
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)
    _record_view(request.app, meta.id, "page")
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    password: str = Form(
        "", description="The artifact's reader password, from the unlock form."
    ),
) -> Response:
    """Password form target: on success set a signed, path-scoped cookie."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if meta.password:
        # Each attempt costs a full PBKDF2, so the budget is checked *before*
        # the hash runs; only failures are counted, so a reader who gets it
        # right on the first try is never throttled. The bucket is keyed by the
        # internal id, so rotating the link gives no fresh budget.
        client_ip = _client_ip(request)
        if _unlock_throttled(request.app, meta.id, client_ip):
            return HTMLResponse(
                unlock_page(
                    public_id,
                    "Too many attempts — wait an hour and try again",
                ),
                status_code=429,
            )
        if not check_password(password, meta.password):
            _record_unlock_failure(request.app, meta.id, client_ip)
            return HTMLResponse(
                unlock_page(public_id, "Wrong password"), status_code=401
            )
    response = RedirectResponse(f"/a/{meta.share_id}", status_code=303)
    response.set_cookie(
        # Name and path both carry the share id: a cookie only comes back when
        # its path prefixes the request path, and what the browser sees is
        # /a/{share_id}. The signed *value* carries the internal artifact id,
        # so the cookie identifies the artifact rather than the URL.
        key=unlock_cookie_name(meta),
        # Bound to the current password record: changing or clearing the
        # password invalidates every cookie issued under the old one.
        value=request.app.state.signer.make(meta.id, password_scope(meta)),
        path=f"/a/{meta.share_id}",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.unlock_cookie_max_age_s,
    )
    return response


# Publisher-controlled HTML served as a *top-level* document, not inside the
# wrapper page's sandboxed iframe. GET /a/{id} isolates artifact HTML with
# <iframe sandbox="allow-scripts ..."> (no allow-same-origin), so its scripts
# never touch the hub's origin. /raw and /source have no such wrapper, so the
# equivalent isolation has to come from the response itself: the CSP `sandbox`
# directive applies the very same sandbox flags to a top-level document,
# dropping it into a unique opaque origin. That is what stops a malicious
# artifact opened by a signed-in admin/reviewer from reading the hub-origin
# sessionStorage where their Keboola Storage token lives, or its cookies.
#
# Deliberately WITHOUT allow-same-origin — granting it would hand the document
# the hub's origin back and undo the whole protection.
#
# The body is never touched: machine clients (curl, fetch, agents) don't
# execute JavaScript and ignore these headers, so they still get the exact
# stored bytes.
SANDBOX_CSP = "sandbox allow-scripts allow-popups allow-forms allow-downloads"


def _sandboxed_html(html: str) -> HTMLResponse:
    """Serve publisher HTML as an opaque-origin, non-sniffed document."""
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": SANDBOX_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )


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
        "Because this is publisher-controlled HTML served as a top-level "
        "document, the response carries `Content-Security-Policy: sandbox "
        "allow-scripts allow-popups allow-forms allow-downloads` (plus "
        "`X-Content-Type-Options: nosniff`). A browser therefore renders it in "
        "a unique opaque origin — the same isolation the wrapper page gets "
        "from its sandboxed iframe — so artifact scripts cannot reach hub "
        "origin storage or cookies. Machine clients ignore these headers and "
        "receive the exact stored bytes, unchanged.\n\n"
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The head version's HTML itself, for machines."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)
    _record_view(request.app, meta.id, "raw")
    return _sandboxed_html(envelope.html)


@app.get(
    "/a/{artifact_id}/source",
    tags=["public"],
    summary="Original submitted source",
    description=(
        "Returns the head version's original Markdown (as text/markdown) for "
        "markdown-sourced artifacts, or the original HTML (as text/html) for "
        "html and git-html artifacts. Markdown rendered from a git repository "
        "has no retained source: that answers 404 with a JSON pointer back to "
        "the repository, ref and commit.\n\n"
        "The HTML answer is publisher-controlled markup served as a top-level "
        "document, so — exactly like /a/{id}/raw — it carries "
        "`Content-Security-Policy: sandbox allow-scripts allow-popups "
        "allow-forms allow-downloads` and `X-Content-Type-Options: nosniff`. "
        "Browsers render it in a unique opaque origin (the isolation the "
        "wrapper page gets from its sandboxed iframe), while machine clients "
        "ignore the headers and receive the exact stored bytes. The Markdown "
        "answer is not executable and is served without that CSP.\n\n"
        + PASSWORD_GATE_NOTE
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The original source: markdown for markdown artifacts, HTML otherwise."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    envelope = request.app.state.store.get_head(meta.id)
    if envelope is None:
        return _not_found(public_id)

    markdown = envelope.source.get("markdown")
    if isinstance(markdown, str):
        _record_view(request.app, meta.id, "source")
        return PlainTextResponse(
            markdown, media_type="text/markdown; charset=utf-8"
        )
    if envelope.source_type in ("html", "git-html"):
        _record_view(request.app, meta.id, "source")
        # Executable publisher HTML at top level — same opaque-origin sandbox
        # as /raw. (The Markdown branch above needs none: it is inert text.)
        return _sandboxed_html(envelope.html)
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
        "counts, the 'protected', 'accept_versions' and "
        "'accept_versions_mode' settings, the derived 'contributions_frozen' "
        "flag, and every public URL. Deliberately carries no owner identity "
        "and no password record. Unlike the content endpoints this is "
        "readable even when the artifact is password-protected — it is "
        "metadata only.\n\n" + STATUS_VS_DOCUMENT_STATUS_NOTE
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Public metadata; available even when the artifact is password-protected."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    store = request.app.state.store
    head = store.get_head(meta.id)
    if head is None:
        return _not_found(public_id)
    versions = store.list_versions(meta.id)
    return JSONResponse(
        {
            **_public_version_meta(head.public_meta(is_head=True)),
            # Public metadata names the artifact by its public identifier: a
            # capability-URL holder is not entitled to the internal id.
            "id": meta.share_id,
            "protected": bool(meta.password),
            # The owner's raw contribution setting, kept raw: it is what the
            # owner configured, not what is currently possible.
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            # ... while these two describe the *document*. The spread above
            # carries the head *version's* "status" ('live'/'proposed'), which
            # is a different axis entirely - see
            # STATUS_VS_DOCUMENT_STATUS_NOTE.
            "document_status": meta.status,
            "contributions_frozen": meta.is_frozen(),
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "head_version": head.version,
            "versions_count": len(versions),
            "proposed_count": sum(
                1 for row in versions if row.get("status") == STATUS_PROPOSED
            ),
            **artifact_urls(base_url(request), meta.share_id),
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    version: int = PathParam(..., description=VERSION_DESC),
) -> Response:
    """Serve one version, honoring both the password gate and proposal privacy."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return HTMLResponse(unlock_page(public_id, None), status_code=401)
    envelope = request.app.state.store.get_version(meta.id, version)
    if envelope is None:
        return _version_not_found(public_id, version)
    if not may_see(meta, envelope, optional_caller(request)):
        return _proposal_hidden(public_id, version)
    _record_view(request.app, meta.id, "version")
    return _framed(envelope)


@app.get(
    "/a/{artifact_id}/versions",
    tags=["public"],
    summary="Version history",
    description=(
        "Lists every version (live and proposed) newest first, with its "
        "number, title, status, author project, note, size, source type and "
        "creation time, plus the current head version, the artifact's "
        "'protected', 'accept_versions' and 'accept_versions_mode' settings, "
        "its 'document_status' and the derived 'contributions_frozen' "
        "flag.\n\n" + STATUS_VS_DOCUMENT_STATUS_NOTE + "\n\n"
        "Each row's own 'status' is therefore the version's ('live' or "
        "'proposed'), never the document's.\n\n"
        "Proposal *metadata* is public to capability-URL holders; proposal "
        "*content* is not — fetching it still requires being the owner or the "
        "author (see GET /a/{id}/v/{n}).\n\n"
        "Every proposed row also carries 'outdated': true when the submitter "
        "declared a 'base_version' and the head has moved on since — the "
        "proposal was written without seeing the versions published after it. "
        "Live rows carry no 'outdated' key at all.\n\n"
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
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
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    wants_html = format == "html"
    if not reader_allowed(meta, request):
        if wants_html:
            return HTMLResponse(unlock_page(public_id, None), status_code=401)
        return _password_required()

    store = request.app.state.store
    versions = [_public_version_meta(row) for row in store.list_versions(meta.id)]
    head_version = _head_version_of(request, meta.id)
    base = base_url(request)

    if wants_html:
        return HTMLResponse(
            versions_page(
                base,
                public_id,
                versions,
                head_version,
                meta.accept_versions,
                bool(meta.password),
            )
        )

    root = f"{base.rstrip('/')}/a/{meta.share_id}"
    return JSONResponse(
        {
            "id": meta.share_id,
            "head_version": head_version,
            # Raw owner setting (see /a/{id}/meta) ...
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            # ... and the document-level facts. Each row below carries its own
            # "status", which is the *version's* ('live'/'proposed').
            "document_status": meta.status,
            "contributions_frozen": meta.is_frozen(),
            "protected": bool(meta.password),
            "versions": [
                {
                    **row,
                    **_outdated_flag(row, head_version),
                    "url": f"{root}/v/{row['version']}",
                }
                for row in versions
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
        "text/plain unified diff, 'json' returns the unified diff plus "
        "added/removed line counts, and 'visual' renders the two versions "
        "themselves side by side — each in its own iframe sandboxed without "
        "allow-same-origin, with the panes' scrolling synchronized — for "
        "comparing what a reader actually sees rather than the source that "
        "produced it. An unknown value is a 400.\n\n"
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
                "One side is larger than the configured HUB_DIFF_MAX_BYTES. "
                "For 'visual' the rendered HTML of each side is what is "
                "measured, since that is what the page has to carry."
            )
        },
        429: RESP_UNLOCK_429,
        502: RESP_HUB_502,
    },
)
def read_diff(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
    spec: str = PathParam(..., description=SPEC_DESC),
    format: str = Query(
        "html",
        description=(
            "Rendering of the diff: 'html' (default, side-by-side page), "
            "'unified' (text/plain unified diff), 'json' (unified diff plus "
            "added/removed counts) or 'visual' (the two rendered documents "
            "side by side in sandboxed iframes, scrolling in step). Anything "
            "else is a 400."
        ),
        # Documentation only — validation stays in compute_diff so an unknown
        # format keeps answering 400 rather than FastAPI's 422.
        json_schema_extra={"enum": ["html", "unified", "json", "visual"]},
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

    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()

    store = request.app.state.store
    caller = None
    envelopes: list[Envelope] = []
    for number in (older, newer):
        envelope = store.get_version(meta.id, number)
        if envelope is None:
            return _version_not_found(public_id, number)
        if envelope.status == STATUS_PROPOSED:
            if caller is None:
                caller = optional_caller(request)
            if not may_see(meta, envelope, caller):
                return _proposal_hidden(public_id, number)
        envelopes.append(envelope)

    if format == "visual":
        return _visual_diff(request, envelopes[0], envelopes[1])

    try:
        content_type, body = compute_diff(
            envelopes[0], envelopes[1], format, settings.diff_max_bytes
        )
    except DiffError as exc:
        status_code = 413 if "too large" in str(exc).lower() else 400
        return JSONResponse(status_code=status_code, content={"error": str(exc)})
    return Response(content=body, media_type=content_type)


def _visual_diff(request: Request, older: Envelope, newer: Envelope) -> Response:
    """Render the ``format=visual`` page: two documents, side by side.

    Unlike the other three formats this one carries the *rendered* documents
    rather than a comparison of their source, so the size guard is applied to
    the HTML each pane has to hold — one 10 MB artifact would otherwise arrive
    as a 20 MB page. The add/remove counts in the header still come from
    :func:`~src.diff.compute_diff`, so the numbers a reader sees here and on
    ``?format=json`` can never drift apart.
    """
    limit = settings.diff_max_bytes
    for env in (older, newer):
        size = len((env.html or "").encode("utf-8"))
        if size > limit:
            return JSONResponse(
                status_code=413,
                content={
                    "error": (
                        f"v{env.version} is too large to show side by side "
                        f"({size} bytes > {limit})"
                    )
                },
            )

    added = removed = None
    try:
        _content_type, payload = compute_diff(older, newer, "json", limit)
        stats = json.loads(payload).get("stats") or {}
        added, removed = stats.get("added"), stats.get("removed")
    except DiffError as exc:
        # The rendered sides fit but their comparable text does not (or cannot
        # be compared). The page is still the useful answer — it just says
        # nothing about how many lines moved.
        logger.info(
            "No diff statistics for the visual diff of %s (v%d..v%d): %s",
            older.id,
            older.version,
            newer.version,
            exc,
        )
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        logger.warning("Unreadable diff statistics: %s", exc)

    return HTMLResponse(
        visual_diff_page(older, newer, added=added, removed=removed),
        headers={"Content-Security-Policy": "frame-ancestors 'self'"},
    )


@app.get(
    "/a/{artifact_id}/comments",
    tags=["public"],
    summary="Inline comment threads",
    description=(
        "Every inline comment thread of this artifact, oldest first, together "
        "with the artifact's current 'comments_mode' and its draft/final "
        "document status — reported both as 'document_status' (the key every "
        "other endpoint uses) and, unchanged for backwards compatibility, as "
        "'status'. On this endpoint 'status' has always meant the "
        "*document's* status, not a version's; both keys carry the same "
        "value. Each thread carries its TextQuoteSelector (the quote plus "
        "a little surrounding context), the comment body, its replies, the "
        "resolved flag and the *project identity* of everyone who spoke — "
        "project id, project name and stack hostname only, never a full stack "
        "URL and never an internal owner key.\n\n"
        "Threads are public to capability-URL holders; writing one needs a "
        "Storage token (POST /api/artifacts/{id}/comments).\n\n"
        + STATUS_VS_DOCUMENT_STATUS_NOTE
        + "\n\n"
        + PASSWORD_GATE_NOTE
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id', 'comments_mode', 'document_status' (also "
                "echoed as the legacy 'status') and the 'threads' array "
                "(possibly empty)."
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """List every comment thread of one artifact, oldest first."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    threads = request.app.state.comments.list_for(meta.id)
    return JSONResponse(
        {
            "id": meta.share_id,
            "comments_mode": meta.comments_mode,
            # "status" here has always meant the *document's* status, unlike
            # every other public payload where it is a version's. It stays
            # exactly as it is for existing callers (the review page reads
            # it), and "document_status" is the name to use going forward.
            "status": meta.status,
            "document_status": meta.status,
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The head version's source as a downloadable file."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()
    head = request.app.state.store.get_head(meta.id)
    if head is None:
        return _not_found(public_id)

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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """The whole artifact — versions, comments and timeline — as a vault ZIP."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        return _password_required()

    store = request.app.state.store
    # Proposals are moderated content: the public vault must not leak them.
    # Only the owner or a proposal's author (authenticated via the standard
    # token headers) gets them included in their download.
    caller = optional_caller(request)
    envelopes: list[Envelope] = []
    for row in store.list_versions(meta.id):
        envelope = store.get_version(meta.id, row["version"])
        if envelope is not None and may_see(meta, envelope, caller):
            envelopes.append(envelope)
    threads = request.app.state.comments.list_for(meta.id)

    filename, payload = export.build_vault(meta, envelopes, threads)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/a/{artifact_id}/guest",
    tags=["public"],
    summary="Who am I, as an invited guest?",
    description=(
        "Checks a guest invitation and answers with the name it carries, so a "
        "client can say 'commenting as Jana (guest)' before anybody writes "
        "anything. Send the credential in the X-Artifact-Guest header, shaped "
        "'{invitation_id}.{secret}' — the two halves of what the review URL's "
        "#invite= fragment contains.\n\n"
        "The review UI calls this on load; an agent holding an invitation can "
        "use it the same way, as a cheap 'is this link still good?' probe. "
        "Nothing is recorded and nothing is returned but the display name and "
        "the (non-secret) invitation id.\n\n"
        "A missing, malformed, revoked or wrong credential all answer the same "
        "401, deliberately: telling them apart would make this an oracle over "
        "somebody else's invitation. Checking a credential runs a full PBKDF2, "
        "so *failed* checks are rate-limited per artifact and client address "
        "the way failed passwords are — grinding secrets against a known "
        "invitation id answers 429, not 401."
    ),
    responses={
        200: {
            "description": (
                "The invitation is valid; JSON with 'name', 'invitation_id' "
                "and the artifact's share 'id'."
            )
        },
        401: {
            "description": (
                "No usable X-Artifact-Guest credential for this artifact."
            )
        },
        404: RESP_NOT_FOUND,
        429: RESP_GUEST_429,
        502: RESP_HUB_502,
    },
)
def guest_identity(
    request: Request,
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Resolve an invitation credential to the guest's display name."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    # A password-protected artifact still gates its guests: an invitation is a
    # grant to *comment*, not a way around the reader password.
    if not reader_allowed(meta, request):
        return _password_required()

    credential = _guest_credential(request)
    if credential is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "send the invitation credential in the "
                f"{GUEST_HEADER} header, shaped '{{invitation_id}}.{{secret}}'"
            ),
        )
    invitation = _verify_guest_checked(request, meta, credential)
    return JSONResponse(
        {
            "id": meta.share_id,
            "invitation_id": str(invitation.get("id") or ""),
            "name": str(invitation.get("name") or ""),
        }
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
        "hub's own API.\n\n"
        "**Guest mode.** Opened through an invitation link — the URL POST "
        "/api/artifacts/{id}/invitations returns, ending in "
        "'#invite={invitation_id}.{secret}' — the page reads that fragment, "
        "clears it from the address bar, checks it against GET /a/{id}/guest "
        "and shows 'Commenting as {name} (guest)'. The composer then works "
        "with no Keboola account at all, sending the credential in the "
        "X-Artifact-Guest header. Browsers never send a fragment to a server, "
        "and the page never puts it in a URL, so the secret stays out of the "
        "hub's logs.\n\n" + PASSWORD_GATE_NOTE
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
    artifact_id: str = PathParam(..., description=SHARE_ID_DESC),
) -> Response:
    """Serve the review shell; all of its data is fetched client-side."""
    public_id = artifact_id
    meta = _public_meta_of(request, public_id)
    if meta is None:
        return _not_found(public_id)
    if not reader_allowed(meta, request):
        # Unlocking here sets the same path-scoped cookie the page's own
        # same-origin fetches then ride on.
        return HTMLResponse(unlock_page(public_id, None), status_code=401)
    # The page's own fetches are all /a/{...} reads, so it gets the *public*
    # identifier — the one those URLs have to carry.
    return HTMLResponse(
        review_page(base_url(request), public_id, SERVICE_VERSION)
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

    # A frozen artifact takes no new content. "final" has an escape hatch: the
    # owner may reopen it in this very call by sending status="draft" alongside
    # the new content. "trashed" has none — restoring is its own route, so that
    # bringing an artifact back is never a side effect of editing it.
    if present and meta.is_frozen():
        if not (meta.is_final() and body.status == ARTIFACT_DRAFT):
            return _document_frozen(meta, "new versions")

    was_final = meta.is_final()

    # An owner update that carries content is a version submission like any
    # other, so it draws on the same per-project daily budget contributors
    # have. Checked before anything is persisted, so a throttled call changes
    # nothing at all.
    if present and not _claim_submission_slot(request.app, artifact_id, owner.key):
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
        # v1 is the publish itself, which the owner just did by hand and does
        # not need a notification about; every later live version is news.
        if envelope.version > 1:
            _emit_webhook(
                request,
                meta,
                "version.published",
                {"title": envelope.title, "version": envelope.version},
                actor=owner,
            )
    else:
        envelope = store.get_head(artifact_id)
        if envelope is None:
            return _not_found(artifact_id)

    if meta.status == ARTIFACT_FINAL and not was_final:
        _emit_webhook(
            request,
            meta,
            "artifact.finalized",
            {"title": envelope.title, "version": envelope.version},
            actor=owner,
        )

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
        "carries the internal 'id' and the public 'share_id', the title, "
        "timestamps, 'protected' and 'accept_versions' flags, head version, "
        "total version count, pending 'proposed_count', the document status "
        "(as 'document_status', and unchanged as the original 'status') with "
        "'trashed_at' and the derived 'contributions_frozen' flag, a "
        "'webhooks_count' (never the URLs themselves), and every public URL "
        "built from the share id. Newest 'updated_at' first.\n\n"
        "Every row's 'status'/'document_status' here is the *document's* "
        "('draft', 'final' or 'trashed'), not a version's ('live' or "
        "'proposed').\n\n"
        "Trashed artifacts are listed too, with status 'trashed' and a "
        "'trashed_at' timestamp: this is the owner's own view, so it is also "
        "their trash can. Their public URLs answer 404 until they are "
        "restored. This is the endpoint the /admin studio calls to sign a "
        "visitor in."
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
    """List the artifacts owned by the caller's project, trash included."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store
    base = base_url(request)
    rows = store.list_owner(owner.key)
    artifacts = []
    for row in rows:
        meta = store.get_meta(row["id"])
        artifacts.append(
            {
                **row,
                # The row's "status" from the store is already the *document's*
                # status; mirror it under the name every other endpoint uses so
                # a consumer can read one key everywhere. "status" is kept.
                "document_status": row.get("status"),
                "contributions_frozen": row.get("status")
                in (ARTIFACT_FINAL, ARTIFACT_TRASHED),
                # A count, never the URLs: a webhook URL is a capability (a
                # Slack hook's path *is* its credential), so the only response
                # that echoes them is the owner PUT that set them.
                "webhooks_count": len(meta.webhooks) if meta is not None else 0,
                # Built from share_id, so a rotated link is reflected here.
                **artifact_urls(base, row["share_id"]),
            }
        )
    return {"project_id": owner.project_id, "artifacts": artifacts}


@app.delete(
    "/api/artifacts/{artifact_id}",
    tags=["artifacts"],
    summary="Move an artifact to the trash",
    description=(
        "Owner-only **soft** delete, reversible by design. Nothing is removed "
        "from Storage: the artifact's status becomes 'trashed', its public "
        "link stops resolving (every /a/{share_id} route answers 404, exactly "
        "as if it had never existed) and it is frozen — new versions and new "
        "comments answer 409.\n\n"
        "The artifact keeps appearing in GET /api/artifacts with status "
        "'trashed' and a 'trashed_at' timestamp, so its owner still sees it. "
        "POST /api/artifacts/{id}/restore brings it back to whatever status "
        "it had before, on the same share id and the same URL.\n\n"
        "To erase it for good — versions, comment threads, view statistics and "
        "counters — use DELETE /api/artifacts/{id}/purge, which is the "
        "irreversible one. The canonical copies in the authors' own Keboola "
        "projects are never touched by either route."
    ),
    responses={
        200: {
            "description": (
                "Moved to the trash (or already there — trashing twice is a "
                "successful no-op); JSON with 'trashed': true, the artifact "
                "id, the timestamp and a 'restore_hint' naming the way back."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
def delete_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Soft-delete: freeze the artifact and kill its public link."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    now = _now()
    if not store.trash(artifact_id, now):
        # trash() only answers False for an artifact it cannot find, and we
        # just loaded its meta — so this is a race with a concurrent purge.
        return _not_found(artifact_id)

    _emit_webhook(request, meta, "artifact.trashed", {"trashed_at": now}, actor=owner)
    logger.info(
        "Artifact %s moved to the trash (owner project %s)",
        artifact_id,
        owner.project_id,
    )
    return JSONResponse(
        {
            "trashed": True,
            "id": artifact_id,
            "trashed_at": meta.trashed_at or now,
            "restore_hint": (
                f"POST /api/artifacts/{artifact_id}/restore brings it back on "
                "the same URL; DELETE /api/artifacts/"
                f"{artifact_id}/purge erases it for good."
            ),
        }
    )


@app.post(
    "/api/artifacts/{artifact_id}/restore",
    tags=["artifacts"],
    summary="Restore an artifact from the trash",
    description=(
        "Owner-only. Undoes DELETE /api/artifacts/{id}: the artifact returns "
        "to the status it was trashed from ('draft' or 'final'), its public "
        "link resolves again on the *same* share id, and versions and comments "
        "unfreeze. Restoring an artifact that is not in the trash is a 409 — "
        "there is nothing to undo."
    ),
    responses={
        200: {
            "description": (
                "Restored; JSON with 'restored': true, the status it came back "
                "to, and its public URLs."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        409: {"description": "This artifact is not in the trash."},
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def restore_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Bring a trashed artifact back to the status it was trashed from."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    if not meta.is_trashed():
        return JSONResponse(
            status_code=409,
            content={
                "error": "artifact is not in the trash",
                "detail": (
                    f"Its status is {meta.status!r}, so there is nothing to "
                    "restore."
                ),
                "id": artifact_id,
            },
        )

    if not store.restore(artifact_id, _now()):
        return _not_found(artifact_id)

    restored = store.get_meta(artifact_id) or meta
    _emit_webhook(
        request, restored, "artifact.restored", {"status": restored.status}, actor=owner
    )
    logger.info(
        "Artifact %s restored from the trash to %s (owner project %s)",
        artifact_id,
        restored.status,
        owner.project_id,
    )
    return JSONResponse(
        {
            "restored": True,
            "id": artifact_id,
            "share_id": restored.share_id,
            "artifact_status": restored.status,
            **artifact_urls(base_url(request), restored.share_id),
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/purge",
    tags=["artifacts"],
    summary="Permanently erase an artifact",
    description=(
        "Owner-only, and **irreversible** — there is no undo and no trash to "
        "fall back on. Deletes every version file, every comment thread and "
        "the meta record from the hub's project, and forgets the artifact's "
        "view statistics and rate-limit counters. All of its URLs stop "
        "resolving for good.\n\n"
        "An artifact does not have to be in the trash first, but the gentler "
        "path is DELETE /api/artifacts/{id} (soft, reversible) followed by "
        "this once you are sure. The canonical copies in the authors' own "
        "Keboola projects are left untouched — this only erases the hub's."
    ),
    responses={
        200: {
            "description": (
                "Purged; JSON reporting how many comment threads went with it "
                "and confirming the canonical copies were kept."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — some stored files could "
                "not be removed, so the artifact is still readable and the "
                "call must be retried. The same 502 covers a partial comment "
                "erasure: the artifact went, but at least one comment thread "
                "still has files in Storage ('comment_threads_failed' says "
                "how many); retry the purge to finish the job."
            )
        },
    },
)
def purge_artifact(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Erase every version; the authors' canonical copies are left untouched."""
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
            "Partial purge of artifact %s: some Storage files remain",
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
    threads, failed_threads = request.app.state.comments.delete_all_for(artifact_id)
    if failed_threads:
        # Same honesty rule as the artifact files above: some comment files are
        # still in Storage, so the erasure the owner asked for did not fully
        # happen. Reporting success would tell them a discussion is gone while
        # it is still readable by anything that can reach those files.
        logger.error(
            "Partial purge of artifact %s: %d comment thread(s) still have "
            "Storage files",
            artifact_id,
            failed_threads,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "comment threads not fully deleted",
                "detail": (
                    f"The artifact was erased, but {failed_threads} of its "
                    "comment threads could not be: some stored comment files "
                    "remain. Retry the purge to finish erasing them."
                ),
                "id": artifact_id,
                "comment_threads_deleted": threads,
                "comment_threads_failed": failed_threads,
            },
        )
    # Same reasoning for the state sidecar: view rows and rate-limit counters
    # keyed by this artifact have nothing left to describe. Best effort — a
    # sidecar failure must not turn a completed purge into an error.
    database = _statedb(request.app)
    if database is not None:
        try:
            database.forget_artifact(artifact_id)
        except Exception as exc:  # noqa: BLE001 - the purge itself succeeded
            logger.warning(
                "Could not forget the state rows of artifact %s: %s",
                artifact_id,
                exc,
            )
    logger.info(
        "Purged artifact %s and %d comment thread(s) (owner project %s)",
        artifact_id,
        threads,
        owner.project_id,
    )
    return JSONResponse(
        {
            "deleted": True,
            "purged": True,
            "comment_threads_deleted": threads,
            "note": "canonical copies in the authors' projects were not touched",
        }
    )


@app.post(
    "/api/artifacts/{artifact_id}/rotate-link",
    tags=["artifacts"],
    summary="Rotate the public link",
    description=(
        "Owner-only. Mints a fresh share id for the artifact and returns its "
        "new URLs.\n\n"
        "**The old link stops working immediately.** Anyone holding the "
        "previous /a/{share_id} URL — and anyone who noted the bare artifact "
        "id, which stops resolving publicly the moment it differs from the "
        "share id — gets a 404 from the next request on. That revocation is "
        "the entire point: a capability URL sent to the wrong person can be "
        "taken back. There is no grace period and no way to un-rotate, so "
        "reshare the new URL with everyone who should still have it.\n\n"
        "Unlock cookies handed out under the old link go with it (they are "
        "scoped to the old path), so readers of a password-protected artifact "
        "unlock once more. The internal artifact id, the content, the version "
        "history and the comment threads are all unchanged — this rotates the "
        "address, not the artifact."
    ),
    responses={
        200: {
            "description": (
                "Rotated; JSON with the new 'share_id', every public URL "
                "rebuilt from it, the previous share id and a warning that it "
                "no longer resolves."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
def rotate_link(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Mint a new public share id and revoke the previous link."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    previous = meta.share_id
    new_share = store.rotate_share(artifact_id, when_iso=_now())
    if new_share is None:
        return _not_found(artifact_id)

    # rotate_share persisted the new meta, so this reload sees the new share
    # id; the fallback only guards an impossible read failure right after it.
    rotated = store.get_meta(artifact_id) or meta
    _emit_webhook(
        request, rotated, "link.rotated", {"share_id": new_share}, actor=owner
    )
    logger.info(
        "Rotated the public link of artifact %s (owner project %s)",
        artifact_id,
        owner.project_id,
    )
    return JSONResponse(
        {
            "id": artifact_id,
            "share_id": new_share,
            "previous_share_id": previous,
            "warning": (
                "The previous link stopped working immediately: "
                f"/a/{previous} now answers 404, and so does the bare "
                "artifact id. Reshare the new URL with everyone who should "
                "still have access."
            ),
            **artifact_urls(base_url(request), new_share),
        }
    )


@app.get(
    "/api/artifacts/{artifact_id}/stats",
    tags=["artifacts"],
    summary="View statistics for one artifact",
    description=(
        "Owner-only. Reports how often the artifact was read: 'total' across "
        "all of recorded history, 'by_kind' (which surface — 'page' for the "
        "rendered wrapper, 'raw', 'source', 'version') and 'by_day' for the "
        "most recent 30 UTC days, oldest first so it charts as-is.\n\n"
        "Counts come from the hub's operational-state sidecar, which "
        "snapshots itself into Storage periodically: a crash can lose the last "
        "few minutes, and a purge forgets an artifact's numbers entirely. "
        "These are traffic figures, not an audit log — no reader identity, no "
        "address and no referrer is recorded anywhere."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id', 'share_id', 'total', 'by_day' and 'by_kind'. "
                "An artifact nobody has read yet reports zeros, not a 404."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def artifact_stats(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Per-artifact view counts, for the owning project only."""
    owner, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    database = _statedb(request.app)
    empty = {"total": 0, "by_day": [], "by_kind": {}}
    if database is None:
        views = empty
    else:
        try:
            views = database.views(artifact_id)
        except Exception as exc:  # noqa: BLE001 - stats never 500 the owner
            logger.warning(
                "Could not read view statistics of artifact %s: %s",
                artifact_id,
                exc,
            )
            views = empty
    return JSONResponse({**views, "id": artifact_id, "share_id": meta.share_id})


# --------------------------------------------------------------------------
# Guest invitations
#
# An invitation is a named, revocable capability that lets one human without a
# Keboola account comment on one artifact. It is a pair: a public
# ``invitation_id`` stored in the artifact's meta record, and a secret that is
# shown **once**, hashed exactly like a reader password and never stored in the
# clear.
#
# The secret travels in the *fragment* of the review URL
# (``/a/{share}/review#invite={id}.{secret}``), which browsers never send to
# the server, and from there only ever into the ``X-Artifact-Guest`` request
# header. It therefore stays out of access logs, out of ``Referer`` and out of
# anything a proxy records — the same discipline the reader password gets.
# --------------------------------------------------------------------------


@app.post(
    "/api/artifacts/{artifact_id}/invitations",
    status_code=201,
    tags=["artifacts"],
    summary="Invite a guest to comment",
    description=(
        "Owner-only. Mints a named, revocable invitation that lets one person "
        "*without* a Keboola account comment on this artifact, and returns "
        "the review URL that carries it.\n\n"
        "**The secret is shown exactly once.** It is hashed before it is "
        "stored (PBKDF2, like a reader password), so neither this hub nor its "
        "owner can ever show it again — a lost link is replaced by revoking "
        "the invitation and minting another one. It rides the URL *fragment* "
        "(after the #), which browsers never send to a server, and reaches "
        "this API only in the X-Artifact-Guest header.\n\n"
        "What the invitation grants is narrow and only grows narrower: open a "
        "comment thread, reply, and resolve or delete threads the guest "
        "themselves opened. It never grants a version submission, never any "
        "/api/* management call, and never access to another artifact. "
        "'comments_mode' does not gate a guest — the invitation *is* the "
        "grant — but a final or trashed artifact is frozen for them exactly "
        "as it is for everybody else."
    ),
    responses={
        201: {
            "description": (
                "Invitation created; JSON with 'invitation_id', 'name' and "
                "the one-time 'review_url'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        409: RESP_FINAL_409,
        422: {
            "description": (
                "The name is empty or longer than "
                f"{MAX_INVITATION_NAME_CHARS} characters, or the artifact "
                "already holds HUB_MAX_INVITATIONS_PER_ARTIFACT live "
                "invitations."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
def create_invitation(
    body: InvitationBody,
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Mint a one-time guest invitation and return its review URL."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)
    if meta.is_frozen():
        return _document_frozen(meta, "new invitations")

    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=422, detail="an invitation needs a name for the guest"
        )
    _make_room_for_invitation(meta)

    # Same generator as an artifact id: 18 random bytes, urlsafe — unguessable,
    # and free of the "." that separates the two halves of the credential.
    secret = new_artifact_id()
    invitation_id = new_artifact_id()
    meta.invitations = list(meta.invitations) + [
        {
            "id": invitation_id,
            "name": name,
            "secret": hash_password(secret),
            "created_at": _now(),
            "revoked": False,
        }
    ]
    meta.updated_at = _now()
    store.save_meta(meta)

    base = base_url(request).rstrip("/")
    logger.info(
        "Artifact %s invited a guest (%s, owner project %s)",
        artifact_id,
        invitation_id,
        owner.project_id,
    )
    return JSONResponse(
        status_code=201,
        content={
            "invitation_id": invitation_id,
            "name": name,
            "review_url": (
                f"{base}/a/{meta.share_id}/review#invite={invitation_id}.{secret}"
            ),
            "warning": (
                "This link is shown once and cannot be recovered — the secret "
                "is stored hashed. Send it to the person it names; anyone "
                "holding it can comment as them until you revoke it."
            ),
        },
    )


@app.get(
    "/api/artifacts/{artifact_id}/invitations",
    tags=["artifacts"],
    summary="List guest invitations",
    description=(
        "Owner-only. Lists this artifact's guest invitations: id, the name "
        "the owner gave, when it was minted and whether it has been revoked.\n\n"
        "The secrets are **not** here and cannot be recovered from anywhere — "
        "they are stored hashed and were shown once, when each invitation was "
        "created. To give somebody a working link again, revoke theirs and "
        "mint a new one."
    ),
    responses={
        200: {
            "description": (
                "JSON with 'id' and the 'invitations' array (possibly empty); "
                "no secrets."
            )
        },
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: RESP_NOT_FOUND,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable."
            )
        },
    },
)
def list_invitations(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Every invitation of one artifact, secrets omitted."""
    owner, _token = auth
    ensure_hydrated(request.app)

    meta = request.app.state.store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    return JSONResponse(
        {
            "id": artifact_id,
            "invitations": [
                _public_invitation(invitation) for invitation in meta.invitations
            ],
        }
    )


@app.delete(
    "/api/artifacts/{artifact_id}/invitations/{invitation_id}",
    tags=["artifacts"],
    summary="Revoke a guest invitation",
    description=(
        "Owner-only, and per person: the named invitation stops working "
        "immediately while every other guest's link keeps working. Comments "
        "the guest already wrote stay — revoking withdraws the capability, "
        "not the contribution.\n\n"
        "Revoking is idempotent: revoking an already-revoked invitation is a "
        "successful no-op."
    ),
    responses={
        200: {"description": "Revoked; JSON with 'revoked': true."},
        400: RESP_STACK_400,
        401: RESP_TOKEN_401,
        403: RESP_OWNER_403,
        404: {
            "description": (
                "No artifact with this id, or it has no such invitation."
            )
        },
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, or the hub's own Storage is unavailable so the "
                "meta record could not be rewritten."
            )
        },
    },
)
def revoke_invitation(
    request: Request,
    artifact_id: str = PathParam(..., description=ARTIFACT_ID_DESC),
    invitation_id: str = PathParam(..., description=INVITATION_ID_DESC),
    auth: tuple[Owner, str] = Depends(require_owner),
) -> Response:
    """Turn off one person's invitation without touching anybody else's."""
    owner, _token = auth
    ensure_hydrated(request.app)
    store = request.app.state.store

    meta = store.get_meta(artifact_id)
    if meta is None:
        return _not_found(artifact_id)
    _owner_only(meta, owner)

    target = None
    for invitation in meta.invitations:
        if invitation.get("id") == invitation_id:
            target = invitation
            break
    if target is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "invitation not found",
                "id": artifact_id,
                "invitation_id": invitation_id,
            },
        )

    if not target.get("revoked"):
        target["revoked"] = True
        meta.updated_at = _now()
        store.save_meta(meta)
        logger.info(
            "Artifact %s revoked guest invitation %s (owner project %s)",
            artifact_id,
            invitation_id,
            owner.project_id,
        )
    return JSONResponse(
        {
            "revoked": True,
            "id": artifact_id,
            "invitation_id": invitation_id,
            "name": str(target.get("name") or ""),
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

    # A frozen artifact ("final", or in the trash) takes nothing from anybody,
    # so it gets its own, clearer answer instead of the generic "you may not
    # contribute" 403 that ``allows_versions_from`` would otherwise produce.
    if meta.is_frozen():
        return _document_frozen(meta, "new versions")

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

    # A base_version that names nothing would render the "outdated" flag
    # meaningless (and quietly mislead a reviewer), so it is a 422 rather than
    # a value we store and hope about.
    if body.base_version is not None:
        if store.get_version(artifact_id, body.base_version) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"artifact {artifact_id} has no version "
                    f"{body.base_version}, so it cannot be a base_version"
                ),
            )

    if not _claim_submission_slot(request.app, artifact_id, caller.key):
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
        base_version=body.base_version,
        canonical_file_id=canonical_file_id,
        created_at=_now(),
    )
    envelope.version = store.add_version_next(envelope)
    # A proposal is news for the owner; an owner's own live version is news for
    # everybody watching, except the v1 that publishing already produced.
    if status == STATUS_PROPOSED:
        _emit_webhook(
            request,
            meta,
            "version.proposed",
            {
                "title": envelope.title,
                "version": envelope.version,
                "note": envelope.note,
                "base_version": envelope.base_version,
            },
            actor=caller,
        )
    elif envelope.version > 1:
        _emit_webhook(
            request,
            meta,
            "version.published",
            {
                "title": envelope.title,
                "version": envelope.version,
                "note": envelope.note,
            },
            actor=caller,
        )
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
            "base_version": envelope.base_version,
            # Built from the share id: the version has to be readable at the
            # artifact's *public* address, not its internal handle.
            "url": (
                f"{base_url(request).rstrip('/')}/a/{meta.share_id}"
                f"/v/{envelope.version}"
            ),
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
    _emit_webhook(
        request,
        meta,
        "version.promoted",
        {
            "title": envelope.title,
            "version": version,
            "head_version": head_version,
        },
        actor=owner,
    )
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
            "url": (
                f"{base_url(request).rstrip('/')}/a/{meta.share_id}/v/{version}"
            ),
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


def _comment_rate_limited(guest: bool = False) -> JSONResponse:
    who = "Your invitation" if guest else "Your project"
    return JSONResponse(
        status_code=429,
        content={
            "error": "too many comments today",
            "detail": (
                f"{who} may write {settings.max_comments_per_day} comments "
                "and replies on one artifact per UTC day."
            ),
            "limit": settings.max_comments_per_day,
        },
    )


def _comment_writer(request: Request) -> tuple[Owner | None, tuple[str, str] | None]:
    """Authenticate whoever is about to write a comment.

    Two credentials are accepted, and exactly one is used per request:

    * a Keboola Storage token (the usual two management headers), which
      identifies a verified *project*, or
    * an ``X-Artifact-Guest`` invitation credential, which identifies one
      invited *person* who has no Keboola account at all.

    When both arrive, the guest header wins: sending it is an unambiguous
    "act as this invitation", and a caller who wants their project's identity
    simply does not send it. (The review UI never sends both — it prefers the
    token it holds and drops the invitation for that request.)

    Returns ``(owner, None)`` or ``(None, credential)``. The guest credential
    comes back unverified on purpose: proving it needs the artifact's meta
    record, and that lookup belongs after this function so a bad *token* still
    answers 401 before the artifact is even looked up — the ordering every
    existing caller of these routes already depends on.
    """
    credential = _guest_credential(request)
    if credential is not None:
        return None, credential
    owner, _token = require_owner(request)
    return owner, None


def _comment_gate(request: Request, meta: ArtifactMeta) -> JSONResponse | None:
    """The reader gate a comment write must clear, or ``None`` when it did.

    The review surface of a password-protected artifact is part of that
    artifact: ``GET /a/{id}/comments`` needs the password to be read, and
    ``GET /a/{id}/guest`` already says in so many words that an invitation is
    a grant to comment, *not* a way around the reader password. Writing was
    the hole — a guest credential alone let somebody read and write the whole
    discussion of a protected document.

    The policy is exactly the read policy of ``/a/{id}/raw``: the password (or
    an unlock cookie) is required from *everyone*, with no exemption for a
    token-authenticated owner, because the read path grants none either. It
    answers the read path's 401, and ``reader_allowed`` itself raises the
    read path's 429 once the failed-attempt budget is spent.
    """
    if reader_allowed(meta, request):
        return None
    return _password_required()


def _comment_author(
    caller: Owner | None, invitation: dict | None
) -> tuple[dict, str]:
    """``(author record, rate-limit key)`` for a verified comment writer.

    The key is what the daily budget is counted against: an owner key for a
    project, ``guest:{invitation_id}`` for a guest. The two namespaces cannot
    collide, so one person's invitation can never spend a project's budget or
    vice versa.
    """
    if caller is not None:
        return _identity(caller), caller.key
    author = _guest_identity(invitation or {})
    return author, author_key_of(author)


def _may_moderate_thread(
    meta: ArtifactMeta,
    thread: CommentThread,
    caller: Owner | None,
    invitation: dict | None,
) -> bool:
    """May this writer resolve, reopen or delete ``thread``?

    The artifact owner may moderate anything on their own artifact, and any
    author may act on their own thread. A guest is only ever the second of
    those: an invitation grants a voice in the discussion, never authority over
    somebody else's part of it.
    """
    if caller is not None:
        return caller.key in (meta.owner_key, thread.author_key)
    author, key = _comment_author(None, invitation)
    del author
    return bool(key) and key == thread.author_key


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
        "unless the artifact is final.\n\n" + GUEST_WRITE_NOTE + "\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        201: {
            "description": (
                "Thread created; returns the whole thread (selector, body, "
                "author project or guest name, timestamps) plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
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
        429: RESP_COMMENTS_429,
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
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
) -> Response:
    """Open a thread anchored to a quoted passage of one version."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    if meta.is_frozen():
        return _document_frozen(meta, "new comments")
    # A guest is not subject to comments_mode: their invitation *is* the grant,
    # and it was issued by the very owner that mode belongs to.
    if caller is not None and not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    store = request.app.state.store
    if store.get_version(meta.id, body.version) is None:
        raise HTTPException(
            status_code=422,
            detail=f"artifact {artifact_id} has no version {body.version}",
        )

    author, writer_key = _comment_author(caller, invitation)
    if not _claim_comment_slot(request.app, meta.id, writer_key):
        return _comment_rate_limited(guest=caller is None)

    thread = CommentThread(
        id=new_artifact_id(),
        artifact_id=meta.id,
        version=body.version,
        selector=Selector(
            exact=body.exact, prefix=body.prefix, suffix=body.suffix
        ),
        body=body.body,
        author=author,
        created_at=_now(),
    )
    try:
        request.app.state.comments.create(thread)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _emit_webhook(
        request,
        meta,
        "comment.created",
        {"thread_id": thread.id, "version": thread.version, "quote": body.exact},
        actor=caller,
        actor_name=None if caller is not None else _guest_actor(invitation or {}),
    )
    logger.info(
        "Artifact %s got comment thread %s on v%d from %s",
        meta.id,
        thread.id,
        thread.version,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
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
        "the same per-project daily cap.\n\n" + GUEST_WRITE_NOTE + "\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        201: {
            "description": (
                "Reply appended; returns the whole updated thread plus "
                "'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Commenting is closed on this artifact, or the caller's "
                "project is not on its contributor allowlist."
            )
        },
        404: RESP_THREAD_404,
        409: RESP_FINAL_409,
        422: {"description": "The reply body is empty or too long."},
        429: RESP_COMMENTS_429,
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
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
) -> Response:
    """Append one reply to an existing thread."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    if meta.is_frozen():
        return _document_frozen(meta, "new comments")
    if caller is not None and not meta.allows_comments_from(caller.key):
        return _comments_closed(meta)

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    author, writer_key = _comment_author(caller, invitation)
    if not _claim_comment_slot(request.app, meta.id, writer_key):
        return _comment_rate_limited(guest=caller is None)

    thread.replies.append(
        Reply(author=author, body=body.body, created_at=_now())
    )
    try:
        comments.update(thread)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _emit_webhook(
        request,
        meta,
        "comment.replied",
        {"thread_id": thread_id, "version": thread.version},
        actor=caller,
        actor_name=None if caller is not None else _guest_actor(invitation or {}),
    )
    logger.info(
        "Comment thread %s of artifact %s got a reply from %s",
        thread_id,
        meta.id,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
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
        "the thread is already in is a 409.\n\n"
        "A guest (X-Artifact-Guest) may resolve and reopen the threads they "
        "opened themselves, and only those — an invitation never carries "
        "moderation authority over anybody else's thread.\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        200: {
            "description": (
                "Thread resolved or reopened; returns the whole updated "
                "thread plus 'thread_id'."
            )
        },
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Only the artifact owner and the thread's author may resolve "
                "or reopen it; a guest only their own threads."
            )
        },
        404: RESP_THREAD_404,
        409: {
            "description": (
                "The thread is already resolved (or already open, when "
                "reopening)."
            )
        },
        429: RESP_COMMENT_MOD_429,
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
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
    body: ResolveBody | None = None,
) -> Response:
    """Owner or thread author closes a thread — or reopens it."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if not _may_moderate_thread(meta, thread, caller, invitation):
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

    author, writer_key = _comment_author(caller, invitation)
    thread.resolved = wanted
    thread.resolved_by = author if wanted else None
    comments.update(thread)

    logger.info(
        "Comment thread %s of artifact %s %s by %s",
        thread_id,
        meta.id,
        "resolved" if wanted else "reopened",
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return _thread_response(thread, 200)


@app.delete(
    "/api/artifacts/{artifact_id}/comments/{thread_id}",
    tags=["comments"],
    summary="Delete a comment thread",
    description=(
        "Removes a thread and all its replies from Storage. Available to the "
        "artifact owner (moderation) and to the thread's own author "
        "(withdrawing a comment); anyone else gets a 403. Irreversible.\n\n"
        "A guest (X-Artifact-Guest) may withdraw the threads they opened "
        "themselves, and only those.\n\n"
        + COMMENT_PASSWORD_NOTE + "\n\n" + COMMENT_TARGET_NOTE
    ),
    responses={
        200: {"description": "Thread deleted."},
        400: RESP_STACK_400,
        401: RESP_COMMENT_401,
        403: {
            "description": (
                "Only the artifact owner or the thread's author may delete "
                "it; a guest only their own threads."
            )
        },
        404: RESP_THREAD_404,
        429: RESP_COMMENT_MOD_429,
        502: {
            "description": (
                "The caller's Keboola stack could not be reached to verify "
                "the token, the hub's own Storage is unavailable, or the "
                "delete only partially succeeded — some stored files of the "
                "thread could not be removed, so it is still readable and the "
                "call must be retried."
            )
        },
    },
)
def delete_comment(
    request: Request,
    artifact_id: str = PathParam(..., description=COMMENT_TARGET_ID_DESC),
    thread_id: str = PathParam(..., description=THREAD_ID_DESC),
) -> Response:
    """Owner moderates, or an author withdraws their own thread."""
    caller, credential = _comment_writer(request)
    meta = _comment_target_of(request, artifact_id, caller)
    if meta is None:
        return _not_found(artifact_id)
    locked = _comment_gate(request, meta)
    if locked is not None:
        return locked
    invitation = (
        _verify_guest_checked(request, meta, credential) if credential else None
    )

    comments = request.app.state.comments
    thread = comments.get(meta.id, thread_id)
    if thread is None:
        return _thread_not_found(artifact_id, thread_id)

    if not _may_moderate_thread(meta, thread, caller, invitation):
        raise HTTPException(
            status_code=403,
            detail=(
                "only the artifact owner or the thread's author can delete "
                "this comment"
            ),
        )

    if not comments.delete(meta.id, thread_id):
        # delete() reports False both when nothing matched and when a backend
        # delete failed with files left behind. Re-reading tells the two apart:
        # a failed erasure keeps the thread listable, a concurrent delete does
        # not. Claiming "deleted" over a thread that is still readable is the
        # one answer this route must never give.
        if comments.get(meta.id, thread_id) is None:
            return _thread_not_found(artifact_id, thread_id)
        logger.error(
            "Partial delete of comment thread %s of artifact %s: some Storage "
            "files remain",
            thread_id,
            meta.id,
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "comment thread not fully deleted",
                "detail": (
                    "Some stored files of this comment thread could not be "
                    "removed; the thread is still readable. Retry the delete."
                ),
                "id": artifact_id,
                "thread_id": thread_id,
            },
        )
    _, writer_key = _comment_author(caller, invitation)
    logger.info(
        "Deleted comment thread %s of artifact %s (by %s)",
        thread_id,
        meta.id,
        f"project {caller.project_id}" if caller else f"guest {writer_key}",
    )
    return JSONResponse(
        {"deleted": True, "id": artifact_id, "thread_id": thread_id}
    )
