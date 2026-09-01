"""Inline comment threads: anchoring, serialization and the thread store.

A comment thread is anchored to a *rendered text quote* of one specific version
of one artifact, W3C-annotation style: ``exact`` plus a short ``prefix`` and
``suffix`` of surrounding text so a highlight can be re-found in the rendered
document even when the quote itself occurs more than once. Threads stay bound to
the version they were made on — there is no cross-version re-anchoring.

Storage layout mirrors :mod:`src.store` but uses a disjoint tag namespace, so
``ArtifactStore.hydrate`` (which lists ``artifact-hub``) never sees a comment
file and :meth:`CommentStore.hydrate` (which lists ``artifact-hub-cmt``) never
sees an artifact file:

- one file per thread, ``comment-{artifact_id}-{thread_id}.json``, tagged
  :data:`TAG_CMT_ALL`, ``artifact-cmt-{artifact_id}`` and ``cmt-id-{thread_id}``.
- replying and resolving rewrite the whole file — the same "upload the new
  file, then retire the older ones carrying the same ID tag" pattern the
  version store uses for status changes.

Like :mod:`src.store` this module is pure: it never reads the clock, never
generates IDs and never touches the network directly. Timestamps and thread IDs
come from the caller (``security.new_artifact_id()`` produces suitable urlsafe
IDs); all Storage access goes through the injected
:class:`~src.kbc.FilesBackend`.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from src.kbc import BackendError, FilesBackend

logger = logging.getLogger(__name__)

__all__ = [
    "AUTHOR_GUEST",
    "MAX_BODY_CHARS",
    "MAX_QUOTE_CHARS",
    "SCHEMA_VERSION",
    "SELECTOR_CONTEXT_CHARS",
    "TAG_CMT_ALL",
    "CommentStore",
    "CommentThread",
    "Reply",
    "Selector",
    "author_key_of",
    "guest_author",
    "tag_cmt_artifact",
    "tag_cmt_id",
]

#: ``author["kind"]`` marking a comment written by an invited guest — a human
#: without a Keboola account, holding a per-person invitation URL — rather than
#: by a verified project. Any other author is a project identity.
AUTHOR_GUEST = "guest"

#: Prefix of the author key of a guest, so guest and project keys can never
#: collide (a project key is ``"{project_id}@{stack host}"``).
_GUEST_KEY_PREFIX = "guest:"

#: Tag on every comment file; drives :meth:`CommentStore.hydrate`.
TAG_CMT_ALL = "artifact-hub-cmt"
_TAG_CMT_ARTIFACT_PREFIX = "artifact-cmt-"
_TAG_CMT_ID_PREFIX = "cmt-id-"

#: Longest accepted comment/reply body, in characters.
MAX_BODY_CHARS = 10_000
#: Longest accepted quoted selection, in characters.
MAX_QUOTE_CHARS = 2_000
#: How much context the UI captures on each side of the quote. Advisory only —
#: this module accepts whatever prefix/suffix the caller recorded.
SELECTOR_CONTEXT_CHARS = 32

#: Schema version written by this module.
SCHEMA_VERSION = 1

#: Cache-file namespace. The dot cannot appear in an artifact ID (see
#: ``_SAFE_ID_CHARS``), so a comment cache file can never collide with — or be
#: swept up by — :class:`~src.store.ArtifactStore`'s ``{id}-{file_id}.json``
#: entries in the same cache directory.
_CACHE_PREFIX = "cmt."

# Same accepted set as the artifact store: IDs come from ``secrets.token_urlsafe``
# and anything outside this alphabet simply skips the disk cache.
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def tag_cmt_artifact(artifact_id: str) -> str:
    """Storage tag carrying every comment thread of one artifact."""
    return f"{_TAG_CMT_ARTIFACT_PREFIX}{artifact_id}"


def tag_cmt_id(thread_id: str) -> str:
    """Storage tag identifying the file(s) of one comment thread."""
    return f"{_TAG_CMT_ID_PREFIX}{thread_id}"


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _stack_host(stack_url: str) -> str:
    """Hostname of a stack URL — the only part of it we ever expose publicly."""
    if not stack_url:
        return ""
    return urlsplit(stack_url).hostname or ""


def _is_guest(identity: dict | None) -> bool:
    """True when this author record describes an invited guest."""
    return isinstance(identity, dict) and identity.get("kind") == AUTHOR_GUEST


def guest_author(invitation_id: str, name: str) -> dict:
    """The author record stored for a comment written by an invited guest.

    Deliberately tiny: the guest has no project, no stack and no token, and the
    invitation *secret* is never part of it — only the invitation id, which is
    what revocation and rate limiting key off.
    """
    return {
        "kind": AUTHOR_GUEST,
        "name": str(name or ""),
        "invitation_id": str(invitation_id or ""),
    }


def author_key_of(identity: dict | None) -> str:
    """Stable key identifying whoever wrote something, or "" when unknown.

    Two disjoint namespaces meet here: a verified project is keyed by its owner
    key (``"{project_id}@{stack host}"``, put there by the API layer) and a
    guest by ``"guest:{invitation_id}"``. Ownership checks — may this caller
    resolve or delete this thread? — compare these keys, so the prefix is what
    keeps an invitation id from ever matching a project.
    """
    if not isinstance(identity, dict):
        return ""
    if _is_guest(identity):
        invitation_id = str(identity.get("invitation_id") or "")
        return f"{_GUEST_KEY_PREFIX}{invitation_id}" if invitation_id else ""
    return str(identity.get("key") or "")


def _identity_summary(identity: dict | None) -> dict:
    """Public projection of an author: project + stack *host*, or a guest name.

    The full ``stack_url`` and the owner ``key`` stay internal; a reader holding
    the capability URL learns which project spoke, not how to address it. A
    guest is reduced even harder — to the name their inviter gave them and
    nothing else, so neither the invitation id nor anything derived from the
    invitation URL is ever published beside their comment.
    """
    identity = identity if isinstance(identity, dict) else {}
    if _is_guest(identity):
        return {"kind": AUTHOR_GUEST, "name": str(identity.get("name") or "")}
    return {
        "project_id": identity.get("project_id"),
        "project_name": identity.get("project_name"),
        "stack_host": _stack_host(str(identity.get("stack_url") or "")),
    }


def _load_object(raw: bytes, what: str) -> dict:
    """Decode UTF-8 JSON that must be an object, or raise ``ValueError``."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what} JSON is not an object")
    return data


@dataclass
class Selector:
    """W3C-style TextQuoteSelector: the quote plus its surrounding context."""

    exact: str
    prefix: str = ""
    suffix: str = ""

    def to_dict(self) -> dict:
        return {"exact": self.exact, "prefix": self.prefix, "suffix": self.suffix}

    @classmethod
    def from_dict(cls, data: object) -> "Selector":
        """Build a selector from an untrusted dict; missing parts become ""."""
        if not isinstance(data, dict):
            return cls(exact="")
        return cls(
            exact=str(data.get("exact") or ""),
            prefix=str(data.get("prefix") or ""),
            suffix=str(data.get("suffix") or ""),
        )


@dataclass
class Reply:
    """One reply inside a thread."""

    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    author: dict
    body: str
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str

    @property
    def author_key(self) -> str:
        """Author key: the project's, ``guest:{id}``, or "" when unknown."""
        return author_key_of(self.author)

    def to_dict(self) -> dict:
        return {
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "Reply":
        """Build a reply from an untrusted dict; missing parts get defaults."""
        if not isinstance(data, dict):
            return cls(author={}, body="", created_at="")
        author = data.get("author")
        return cls(
            author=author if isinstance(author, dict) else {},
            body=str(data.get("body") or ""),
            created_at=str(data.get("created_at") or ""),
        )

    def public_dict(self) -> dict:
        return {
            "author": _identity_summary(self.author),
            "body": self.body,
            "created_at": self.created_at,
        }


@dataclass
class CommentThread:
    """One inline comment thread, stored as JSON in a single Storage File."""

    id: str
    artifact_id: str
    # The version the selection was made on; the thread never re-anchors.
    version: int
    selector: Selector
    # Plain text — never HTML. Rendering escapes it.
    body: str
    # The verified submitter:
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    author: dict
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str
    resolved: bool = False
    # Identity of whoever resolved the thread, or None while it is open.
    resolved_by: dict | None = None
    replies: list[Reply] = field(default_factory=list)
    schema: int = SCHEMA_VERSION

    @property
    def author_key(self) -> str:
        """Author key: the project's, ``guest:{id}``, or "" when unknown."""
        return author_key_of(self.author)

    def to_json(self) -> bytes:
        """Serialize the thread to UTF-8 JSON bytes."""
        payload = {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "selector": self.selector.to_dict(),
            "body": self.body,
            "author": self.author,
            "created_at": self.created_at,
            "resolved": self.resolved,
            "resolved_by": self.resolved_by,
            "replies": [reply.to_dict() for reply in self.replies],
            "schema": self.schema,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "CommentThread":
        """Parse thread JSON tolerantly (unknown keys ignored, defaults applied).

        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable thread ID / artifact ID — callers treat that as corrupt.
        """
        data = _load_object(raw, "comment")

        thread_id = data.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("comment has no usable 'id'")
        artifact_id = data.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("comment has no usable 'artifact_id'")

        version = data.get("version")
        author = data.get("author")
        resolved_by = data.get("resolved_by")
        raw_replies = data.get("replies")
        schema = data.get("schema")

        return cls(
            id=thread_id,
            artifact_id=artifact_id,
            version=(
                version
                if isinstance(version, int)
                and not isinstance(version, bool)
                and version > 0
                else 1
            ),
            selector=Selector.from_dict(data.get("selector")),
            body=str(data.get("body") or ""),
            author=author if isinstance(author, dict) else {},
            created_at=str(data.get("created_at") or ""),
            resolved=bool(data.get("resolved")),
            resolved_by=resolved_by if isinstance(resolved_by, dict) else None,
            replies=(
                [Reply.from_dict(item) for item in raw_replies]
                if isinstance(raw_replies, list)
                else []
            ),
            schema=schema if isinstance(schema, int) else SCHEMA_VERSION,
        )

    def public_dict(self) -> dict:
        """The whole thread, safe to hand to any capability-URL holder.

        Project identities are reduced to ``project_id`` / ``project_name`` /
        ``stack_host``: neither the full ``stack_url`` nor the internal owner
        ``key`` ever leaves this method. A guest is reduced to
        ``{"kind": "guest", "name": ...}`` — their invitation id stays internal.
        """
        return {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "selector": self.selector.to_dict(),
            "body": self.body,
            "author": _identity_summary(self.author),
            "created_at": self.created_at,
            "resolved": self.resolved,
            "resolved_by": (
                _identity_summary(self.resolved_by)
                if self.resolved_by is not None
                else None
            ),
            "replies": [reply.public_dict() for reply in self.replies],
        }


def _validate(thread: CommentThread) -> None:
    """Reject an unusable thread with a message safe to show to the caller."""
    if not thread.id:
        raise ValueError("Comment thread has no id")
    if not thread.artifact_id:
        raise ValueError("Comment thread has no artifact id")
    _validate_body(thread.body, "Comment")
    exact = thread.selector.exact if thread.selector else ""
    if not exact.strip():
        raise ValueError("The quoted selection must not be empty")
    if len(exact) > MAX_QUOTE_CHARS:
        raise ValueError(
            f"The quoted selection is too long "
            f"({len(exact)} characters, maximum is {MAX_QUOTE_CHARS})"
        )
    for reply in thread.replies:
        _validate_body(reply.body, "Reply")


def _validate_body(body: str, what: str) -> None:
    if not (body or "").strip():
        raise ValueError(f"{what} body must not be empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(
            f"{what} body is too long "
            f"({len(body)} characters, maximum is {MAX_BODY_CHARS})"
        )


class CommentStore:
    """Comment threads backed by Storage Files, with memory + disk caches.

    Thread safety mirrors :class:`~src.store.ArtifactStore`: the index and the
    in-process LRU are guarded by a lock, while backend calls happen outside it
    so a slow Storage request never blocks another one.
    """

    def __init__(
        self,
        backend: FilesBackend,
        cache_dir: Path,
        cache_max_entries: int,
    ) -> None:
        self._backend = backend
        self._cache_dir = Path(cache_dir)
        self._cache_max_entries = max(0, int(cache_max_entries))
        #: ``{artifact_id: {thread_id: file_id}}`` — newest file per thread.
        self._index: dict[str, dict[str, int]] = {}
        self._memory: OrderedDict[tuple[str, int], CommentThread] = OrderedDict()
        self._lock = threading.Lock()
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # Disk cache is optional — degrade gracefully.
            logger.warning("Cannot create cache dir %s: %s", self._cache_dir, exc)

    # ---------------------------------------------------------------- index

    def hydrate(self) -> int:
        """Rebuild the index from Storage. Returns the number of threads indexed.

        When several files describe the same thread (an interrupted rewrite left
        an older one behind) the highest file ID — the newest upload — wins.
        """
        files = self._backend.search_by_tag(TAG_CMT_ALL)
        index: dict[str, dict[str, int]] = {}
        for info in files:
            artifact_id = _tag_value(info.tags, _TAG_CMT_ARTIFACT_PREFIX)
            thread_id = _tag_value(info.tags, _TAG_CMT_ID_PREFIX)
            if not artifact_id or not thread_id:
                logger.warning(
                    "Skipping comment file %s (%s): missing artifact/thread tag",
                    info.id,
                    info.name,
                )
                continue
            threads = index.setdefault(artifact_id, {})
            if threads.get(thread_id, -1) < info.id:
                threads[thread_id] = info.id

        count = sum(len(threads) for threads in index.values())
        with self._lock:
            self._index = index
        logger.info("Hydrated comment index with %d thread(s)", count)
        return count

    def count(self) -> int:
        """Number of threads currently indexed."""
        with self._lock:
            return sum(len(threads) for threads in self._index.values())

    # ----------------------------------------------------------------- write

    def create(self, thread: CommentThread) -> None:
        """Validate and upload a new thread, then index and cache it."""
        _validate(thread)
        self._put(thread, retire_older=False)

    def update(self, thread: CommentThread) -> None:
        """Rewrite a thread (reply, resolve, edit) and retire its older files.

        Storage Files are immutable, so an update is a fresh upload followed by
        a best-effort delete of every other file carrying the same ``cmt-id``
        tag. The backend therefore ends up holding exactly one file per thread.
        """
        _validate(thread)
        self._put(thread, retire_older=True)

    def _put(self, thread: CommentThread, retire_older: bool) -> None:
        artifact_id = thread.artifact_id
        thread_id = thread.id
        raw = thread.to_json()
        tags = [TAG_CMT_ALL, tag_cmt_artifact(artifact_id), tag_cmt_id(thread_id)]

        # Upload first: a failure here leaves the index untouched.
        file_id = self._backend.upload(
            f"comment-{artifact_id}-{thread_id}.json", raw, tags
        )

        with self._lock:
            threads = self._index.setdefault(artifact_id, {})
            previous = threads.get(thread_id)
            threads[thread_id] = file_id
            if previous is not None:
                self._memory.pop((artifact_id, previous), None)
            self._remember_locked(artifact_id, file_id, thread)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous != file_id:
            self._delete_disk_cache(artifact_id, previous)
        if retire_older:
            self._delete_superseded(artifact_id, thread_id, keep_file_id=file_id)

    # ------------------------------------------------------------------ read

    def get(self, artifact_id: str, thread_id: str) -> CommentThread | None:
        """One thread by ID, or None. An index miss falls back to a tag search.

        Another replica may have written the thread after our last hydrate, so
        "not in the index" is re-checked against Storage before answering None.
        """
        with self._lock:
            file_id = self._index.get(artifact_id, {}).get(thread_id)
        if file_id is not None:
            thread = self._load(artifact_id, file_id)
            if thread is not None:
                return thread

        file_id = self._newest_file_id(tag_cmt_id(thread_id))
        if file_id is None:
            return None
        thread = self._load(artifact_id, file_id)
        if thread is None or thread.artifact_id != artifact_id:
            return None
        with self._lock:
            self._index.setdefault(artifact_id, {})[thread_id] = file_id
        return thread

    def list_for(self, artifact_id: str) -> list[CommentThread]:
        """Every thread of one artifact, oldest first.

        Driven by the artifact tag rather than the index, so a cold process — or
        one whose index predates another replica's write — still sees everything.
        A thread whose file is corrupt is skipped with a warning; a Storage
        failure propagates as :class:`~src.kbc.BackendError`.
        """
        newest: dict[str, int] = {}
        for info in self._backend.search_by_tag(tag_cmt_artifact(artifact_id)):
            thread_id = _tag_value(info.tags, _TAG_CMT_ID_PREFIX)
            if not thread_id:
                logger.warning(
                    "Skipping comment file %s (%s): no %s* tag",
                    info.id,
                    info.name,
                    _TAG_CMT_ID_PREFIX,
                )
                continue
            if newest.get(thread_id, -1) < info.id:
                newest[thread_id] = info.id

        with self._lock:
            if newest:
                self._index.setdefault(artifact_id, {}).update(newest)
            elif artifact_id in self._index:
                self._index.pop(artifact_id, None)

        threads: list[CommentThread] = []
        for thread_id, file_id in newest.items():
            thread = self._load(artifact_id, file_id)
            if thread is None:
                continue
            if thread.id != thread_id or thread.artifact_id != artifact_id:
                logger.warning(
                    "Comment file %s disagrees with its tags (%s/%s); skipping",
                    file_id,
                    thread.artifact_id,
                    thread.id,
                )
                continue
            threads.append(thread)

        threads.sort(key=lambda thread: (thread.created_at or "", thread.id))
        return threads

    def count_open_for(self, artifact_id: str) -> int:
        """How many threads of an artifact are still unresolved."""
        return sum(1 for thread in self.list_for(artifact_id) if not thread.resolved)

    # ---------------------------------------------------------------- delete

    def delete(self, artifact_id: str, thread_id: str) -> bool:
        """Delete every file of one thread. False when there was nothing to delete."""
        files = self._backend.search_by_tag(tag_cmt_id(thread_id))
        deleted = False
        for info in files:
            if _tag_value(info.tags, _TAG_CMT_ARTIFACT_PREFIX) != artifact_id:
                # A thread ID belongs to exactly one artifact; a mismatch means
                # the caller asked about the wrong artifact.
                continue
            self._delete_file(artifact_id, info.id)
            deleted = True

        with self._lock:
            threads = self._index.get(artifact_id)
            if threads is not None:
                file_id = threads.pop(thread_id, None)
                if file_id is not None:
                    self._memory.pop((artifact_id, file_id), None)
                if not threads:
                    self._index.pop(artifact_id, None)
        return deleted

    def delete_all_for(self, artifact_id: str) -> int:
        """Delete every thread of an artifact. Returns the thread count removed.

        Called when the artifact itself is deleted, so its comments do not
        outlive it as orphaned Storage files.
        """
        files = self._backend.search_by_tag(tag_cmt_artifact(artifact_id))
        thread_ids: set[str] = set()
        for info in files:
            thread_id = _tag_value(info.tags, _TAG_CMT_ID_PREFIX)
            if thread_id:
                thread_ids.add(thread_id)
            self._delete_file(artifact_id, info.id)

        with self._lock:
            self._index.pop(artifact_id, None)
            self._forget_memory_locked(artifact_id)
        self._purge_disk_cache(artifact_id)
        return len(thread_ids)

    def _delete_superseded(
        self, artifact_id: str, thread_id: str, keep_file_id: int
    ) -> None:
        """Best-effort removal of older files of one thread. Never raises."""
        try:
            files = self._backend.search_by_tag(tag_cmt_id(thread_id))
        except BackendError as exc:
            logger.warning(
                "Cannot list files of comment thread %s: %s", thread_id, exc
            )
            return
        for info in files:
            if info.id == keep_file_id:
                continue
            self._delete_file(artifact_id, info.id)

    def _delete_file(self, artifact_id: str, file_id: int) -> None:
        """Delete one Storage file and its cache entry. Never raises."""
        try:
            self._backend.delete(file_id)
        except BackendError as exc:
            logger.warning(
                "Cannot delete comment file %s of artifact %s: %s",
                file_id,
                artifact_id,
                exc,
            )
        self._delete_disk_cache(artifact_id, file_id)

    # ------------------------------------------------------------------ load

    def _newest_file_id(self, tag: str) -> int | None:
        files = self._backend.search_by_tag(tag)
        if not files:
            return None
        return max(info.id for info in files)

    def _load(self, artifact_id: str, file_id: int) -> CommentThread | None:
        key = (artifact_id, file_id)
        with self._lock:
            thread = self._memory.get(key)
            if thread is not None:
                self._memory.move_to_end(key)
                return thread

        raw = self._read_raw(artifact_id, file_id)
        if raw is None:
            return None
        try:
            thread = CommentThread.from_json(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt comment in Storage for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        with self._lock:
            self._remember_locked(artifact_id, file_id, thread)
        return thread

    def _read_raw(self, artifact_id: str, file_id: int) -> bytes | None:
        """Disk cache -> Storage download. None when the file is unreadable.

        ``BackendError`` from Storage reads propagates (the API maps it to 502).
        """
        raw = self._read_disk_cache(artifact_id, file_id)
        if raw is not None:
            return raw
        raw = self._backend.download(file_id)
        if raw is None:
            return None
        self._write_disk_cache(artifact_id, file_id, raw)
        return raw

    # ----------------------------------------------------------- memory LRU

    def _remember_locked(
        self, artifact_id: str, file_id: int, thread: CommentThread
    ) -> None:
        """Insert into the thread LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._memory[key] = thread
        self._memory.move_to_end(key)
        while len(self._memory) > self._cache_max_entries:
            self._memory.popitem(last=False)

    def _forget_memory_locked(self, artifact_id: str) -> None:
        """Drop every LRU entry of an artifact. Caller must hold the lock."""
        for key in [k for k in self._memory if k[0] == artifact_id]:
            self._memory.pop(key, None)

    # ----------------------------------------------------------- disk cache

    def _cache_path(self, artifact_id: str, file_id: int) -> Path | None:
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return None
        return self._cache_dir / f"{_CACHE_PREFIX}{artifact_id}-{file_id}.json"

    def _read_disk_cache(self, artifact_id: str, file_id: int) -> bytes | None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return None
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Cannot read disk cache %s: %s", path, exc)
            return None
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt comment disk cache for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            self._delete_disk_cache(artifact_id, file_id)
            return None
        return raw

    def _write_disk_cache(self, artifact_id: str, file_id: int, raw: bytes) -> None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return
        tmp = path.parent / f"{path.name}.tmp"
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Cannot write disk cache %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_disk_cache(self, artifact_id: str, file_id: int) -> None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Cannot delete disk cache %s: %s", path, exc)

    def _purge_disk_cache(self, artifact_id: str) -> None:
        """Remove every cached comment file of an artifact."""
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return
        try:
            candidates = list(
                self._cache_dir.glob(f"{_CACHE_PREFIX}{artifact_id}-*.json")
            )
        except OSError as exc:
            logger.warning("Cannot scan cache dir %s: %s", self._cache_dir, exc)
            return
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Cannot prune disk cache %s: %s", path, exc)
