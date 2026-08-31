"""Artifact envelopes, artifact meta records and the versioned serving store.

The single source of truth for served artifacts is Storage Files of the *host*
project. Since phase 2 (community versioning) one artifact is spread over
several files:

- one **version** file per version, ``artifact-{id}-v{n}.json``, tagged
  ``artifact-hub``, ``artifact-id-{id}`` and ``artifact-ver-{n}``. It holds the
  content (html + source), the verified ``author``, a ``status`` ("live" or
  "proposed") and an optional ``note``.
- one **meta** file ``artifact-{id}-meta.json``, tagged ``artifact-hub``,
  ``artifact-id-{id}``, ``artifact-meta`` and ``artifact-owner-{key}``. It holds
  artifact-level state: owner, password record, contribution and comment
  policy (``accept_versions_mode``, ``contributors``, ``comments_mode``), the
  draft/final ``status`` and the head pointer. The owner tag lives on the meta
  file so listing an owner's artifacts stays a single tag search.

Inline comment threads live in their own files with their own tags and their
own store — see :mod:`src.comments`. The artifact index never sees them.

Schema-1 artifacts (one ``artifact-{id}.json`` envelope carrying owner and
password inline, tagged with the owner tag and no version/meta tag) are still
readable: they are treated as version 1 "live" and their meta record is
synthesized on read (:func:`migrate_legacy`) and materialized on the next write.

Because the app container has no permanent disk, the process keeps only:

- an in-memory index ``{artifact_id: _ArtEntry}`` rebuilt on startup by listing
  files tagged ``artifact-hub`` (:meth:`ArtifactStore.hydrate`),
- small in-process LRUs of parsed :class:`Envelope` / :class:`ArtifactMeta`
  objects keyed by ``(artifact_id, file_id)``, and
- a disk cache of raw JSON under ``cache_dir`` (pure cache — safe to wipe).

This module is deliberately pure: it never reads the clock, never generates IDs
and never touches the network directly. Timestamps and IDs are produced by the
caller; all Storage access goes through the injected
:class:`~src.kbc.FilesBackend`.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from src.kbc import BackendError, FileInfo, FilesBackend

logger = logging.getLogger(__name__)

TAG_ALL = "artifact-hub"
TAG_META = "artifact-meta"
_TAG_ID_PREFIX = "artifact-id-"
_TAG_OWNER_PREFIX = "artifact-owner-"
_TAG_VER_PREFIX = "artifact-ver-"

# Characters accepted in an artifact ID when it is used to build a cache file
# name. Artifact IDs come from ``secrets.token_urlsafe`` so this is a superset
# of what we generate; anything else simply skips the disk cache.
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

_SOURCE_TYPES = ("html", "markdown", "git-html", "git-markdown")

#: Version status values. "proposed" content is moderated: readable only by the
#: artifact owner or the submitting author, never served as the head.
STATUS_LIVE = "live"
STATUS_PROPOSED = "proposed"
_STATUSES = (STATUS_LIVE, STATUS_PROPOSED)

#: Head pointer modes of :class:`ArtifactMeta`.
HEAD_LATEST = "latest"
HEAD_PINNED = "pinned"
_HEAD_MODES = (HEAD_LATEST, HEAD_PINNED)

#: Who may submit a version (:attr:`ArtifactMeta.accept_versions_mode`).
ACCEPT_OFF = "off"
ACCEPT_ANYONE = "anyone"
ACCEPT_ALLOWLIST = "allowlist"
ACCEPT_MODES = (ACCEPT_OFF, ACCEPT_ANYONE, ACCEPT_ALLOWLIST)
#: Modes that mean "someone other than the owner may contribute".
_ACCEPT_ON_MODES = (ACCEPT_ANYONE, ACCEPT_ALLOWLIST)

#: Who may comment (:attr:`ArtifactMeta.comments_mode`).
COMMENTS_ANYONE = "anyone"
COMMENTS_ALLOWLIST = "allowlist"
COMMENTS_OFF = "off"
COMMENTS_MODES = (COMMENTS_ANYONE, COMMENTS_ALLOWLIST, COMMENTS_OFF)

#: Artifact lifecycle status. "final" freezes new versions and new comments for
#: *everyone*, the owner included; reopening is an owner action in the API layer
#: (set the status back to "draft").
ARTIFACT_DRAFT = "draft"
ARTIFACT_FINAL = "final"
ARTIFACT_STATUSES = (ARTIFACT_DRAFT, ARTIFACT_FINAL)

#: Schema version written by this module.
SCHEMA_VERSION = 2


def tag_for_id(artifact_id: str) -> str:
    """Storage tag identifying every file of a single artifact."""
    return f"{_TAG_ID_PREFIX}{artifact_id}"


def tag_for_owner(owner_key: str) -> str:
    """Storage tag identifying all artifacts of one owner (stack + project)."""
    return f"{_TAG_OWNER_PREFIX}{owner_key}"


def tag_for_version(version: int) -> str:
    """Storage tag identifying one version file of an artifact."""
    return f"{_TAG_VER_PREFIX}{version}"


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _version_from_tags(tags: list[str]) -> int | None:
    """Version number encoded in an ``artifact-ver-N`` tag, if any."""
    raw = _tag_value(tags, _TAG_VER_PREFIX)
    if raw is None:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if number > 0 else None


def _stack_host(stack_url: str) -> str:
    """Hostname of a stack URL — the only part of it we expose publicly."""
    if not stack_url:
        return ""
    return urlsplit(stack_url).hostname or ""


@dataclass
class ArtifactMeta:
    """Artifact-level state: who owns it, who may contribute, where head points.

    **``accept_versions`` vs ``accept_versions_mode``.** Phase 3 turned the
    old boolean "anyone may submit a version" switch into a three-way mode
    (``off`` / ``anyone`` / ``allowlist``). To keep every existing caller and
    every meta file already in Storage working, the two live side by side:

    * :attr:`accept_versions_mode` is the **source of truth** — one of
      :data:`ACCEPT_MODES`, default :data:`ACCEPT_OFF`.
    * ``accept_versions`` is a **derived compatibility field**: a ``bool``
      property that reads ``True`` iff the mode is not ``off``. Assigning to it
      (``meta.accept_versions = True``) flips the mode between ``off`` and
      ``anyone``, and is a no-op when the mode already agrees — so turning an
      allowlisted artifact "on" does not silently widen it to ``anyone``.

    Both keys are written by :meth:`to_json`; :meth:`from_json` prefers
    ``accept_versions_mode`` when it carries a known mode and otherwise maps the
    legacy boolean (``false`` -> ``off``, ``true`` -> ``anyone``).

    Precedence when a caller passes both: the explicit ``accept_versions``
    boolean wins, because the generated ``__init__`` assigns the mode first and
    the compatibility setter second. Pass one or the other, not two
    contradictory values.

    The property is attached right after the class body (see
    ``_accept_versions_get``/``_accept_versions_set`` below) because a dataclass
    field and a property cannot be declared under the same name. The field is
    declared *after* ``accept_versions_mode`` so the generated ``__init__``
    assigns the mode first and the setter then sees the real mode; its default
    is ``None``, meaning "the caller said nothing, leave the mode alone".
    """

    id: str
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    owner: dict = field(default_factory=dict)
    # security.hash_password() record, or None when the artifact is public.
    password: dict | None = None
    # Source of truth for version contributions; one of ACCEPT_MODES.
    accept_versions_mode: str = ACCEPT_OFF
    # Compatibility alias for accept_versions_mode; see the class docstring.
    # Replaced by a property below — never read this annotation as storage.
    accept_versions: bool | None = None
    # Owner keys ("project@stackhost") allowed when a mode is "allowlist".
    contributors: list[str] = field(default_factory=list)
    # One of COMMENTS_MODES; comments are open by default.
    comments_mode: str = COMMENTS_ANYONE
    # ARTIFACT_DRAFT or ARTIFACT_FINAL; "final" freezes versions and comments.
    status: str = ARTIFACT_DRAFT
    # HEAD_LATEST -> serve the newest live version; HEAD_PINNED -> head_version.
    head_mode: str = HEAD_LATEST
    head_version: int | None = None
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str = ""
    updated_at: str = ""
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Normalize the enum-ish fields so an unknown value can never leak in."""
        if self.accept_versions_mode not in ACCEPT_MODES:
            self.accept_versions_mode = ACCEPT_OFF
        if self.comments_mode not in COMMENTS_MODES:
            self.comments_mode = COMMENTS_ANYONE
        if self.status not in ARTIFACT_STATUSES:
            self.status = ARTIFACT_DRAFT
        if isinstance(self.contributors, list):
            self.contributors = [str(key) for key in self.contributors if key]
        else:
            self.contributors = []

    @property
    def owner_key(self) -> str:
        """Owner key (stack + project), or "" when the record has no owner."""
        return str(self.owner.get("key") or "")

    def is_final(self) -> bool:
        """True when the artifact is frozen: no new versions, no new comments."""
        return self.status == ARTIFACT_FINAL

    def allows_versions_from(self, key: str) -> bool:
        """May the project identified by ``key`` submit a version?

        A "final" artifact is frozen for everyone, the owner included — the API
        layer offers the owner a reopen path instead. Otherwise the owner may
        always contribute, ``anyone`` opens it up to every verified project and
        ``allowlist`` restricts it to :attr:`contributors`.
        """
        if self.is_final():
            return False
        if key and key == self.owner_key:
            return True
        if self.accept_versions_mode == ACCEPT_ANYONE:
            return True
        if self.accept_versions_mode == ACCEPT_ALLOWLIST:
            return bool(key) and key in self.contributors
        return False

    def allows_comments_from(self, key: str) -> bool:
        """May the project identified by ``key`` comment? Same shape as versions."""
        if self.is_final():
            return False
        if key and key == self.owner_key:
            return True
        if self.comments_mode == COMMENTS_ANYONE:
            return True
        if self.comments_mode == COMMENTS_ALLOWLIST:
            return bool(key) and key in self.contributors
        return False

    def to_json(self) -> bytes:
        """Serialize the meta record to UTF-8 JSON bytes.

        Both ``accept_versions`` (legacy boolean) and ``accept_versions_mode``
        are written, so a rollback to a phase-2 build still reads the file.
        """
        payload = {
            "id": self.id,
            "owner": self.owner,
            "password": self.password,
            "accept_versions": self.accept_versions,
            "accept_versions_mode": self.accept_versions_mode,
            "contributors": self.contributors,
            "comments_mode": self.comments_mode,
            "status": self.status,
            "head_mode": self.head_mode,
            "head_version": self.head_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema": self.schema,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "ArtifactMeta":
        """Parse meta JSON tolerantly (unknown keys ignored, defaults applied).

        ``accept_versions_mode`` wins when it names a known mode; a file written
        before phase 3 has only the boolean ``accept_versions`` and maps to
        ``anyone``/``off``.

        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable artifact ID — callers treat that as a corrupt record.
        """
        data = _load_object(raw, "meta")

        artifact_id = data.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("meta has no usable 'id'")

        owner = data.get("owner")
        password = data.get("password")
        head_mode = data.get("head_mode")
        head_version = data.get("head_version")
        schema = data.get("schema")

        raw_mode = data.get("accept_versions_mode")
        if raw_mode in ACCEPT_MODES:
            accept_mode = str(raw_mode)
        else:
            accept_mode = ACCEPT_ANYONE if data.get("accept_versions") else ACCEPT_OFF

        contributors = data.get("contributors")
        comments_mode = data.get("comments_mode")
        status = data.get("status")

        return cls(
            id=artifact_id,
            owner=owner if isinstance(owner, dict) else {},
            password=password if isinstance(password, dict) else None,
            accept_versions_mode=accept_mode,
            contributors=(
                [str(key) for key in contributors if key]
                if isinstance(contributors, list)
                else []
            ),
            comments_mode=(
                comments_mode if comments_mode in COMMENTS_MODES else COMMENTS_ANYONE
            ),
            status=status if status in ARTIFACT_STATUSES else ARTIFACT_DRAFT,
            head_mode=head_mode if head_mode in _HEAD_MODES else HEAD_LATEST,
            head_version=(
                head_version
                if isinstance(head_version, int) and not isinstance(head_version, bool)
                else None
            ),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            schema=schema if isinstance(schema, int) else SCHEMA_VERSION,
        )


def _accept_versions_get(self: ArtifactMeta) -> bool:
    """``True`` iff someone other than the owner may submit versions."""
    return self.accept_versions_mode in _ACCEPT_ON_MODES


def _accept_versions_set(self: ArtifactMeta, value: bool | None) -> None:
    """Flip the mode on/off, preserving ``allowlist`` when it already agrees.

    ``None`` means "not specified" — used by the generated ``__init__`` when a
    caller passes only ``accept_versions_mode``.
    """
    if value is None:
        return
    wanted = bool(value)
    if wanted == (self.accept_versions_mode in _ACCEPT_ON_MODES):
        return
    self.accept_versions_mode = ACCEPT_ANYONE if wanted else ACCEPT_OFF


# Attached post-hoc: a dataclass field and a property cannot share a name, but a
# property installed after the fact is what the generated ``__init__`` assigns
# through, so ``ArtifactMeta(accept_versions=True)`` and
# ``meta.accept_versions = False`` both keep the mode in sync.
ArtifactMeta.accept_versions = property(  # type: ignore[assignment]
    _accept_versions_get, _accept_versions_set
)


@dataclass
class Envelope:
    """One *version* of an artifact, stored as JSON in Storage Files."""

    id: str
    version: int
    title: str
    html: str
    # One of _SOURCE_TYPES.
    source_type: str
    # Original input: {"markdown": "..."} / {"git": {...}} / {} for raw HTML.
    source: dict = field(default_factory=dict)
    # The verified submitter of this version:
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    author: dict = field(default_factory=dict)
    # STATUS_LIVE or STATUS_PROPOSED.
    status: str = STATUS_LIVE
    # Free-form contributor note ("what changed").
    note: str | None = None
    # File ID of the canonical copy in the *author's* project (may be absent).
    canonical_file_id: int | None = None
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str = ""
    schema: int = SCHEMA_VERSION

    @property
    def author_key(self) -> str:
        """Author key (stack + project), or "" when the record has no author."""
        return str(self.author.get("key") or "")

    def to_json(self) -> bytes:
        """Serialize the version envelope to UTF-8 JSON bytes."""
        payload = {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "html": self.html,
            "source_type": self.source_type,
            "source": self.source,
            "author": self.author,
            "status": self.status,
            "note": self.note,
            "canonical_file_id": self.canonical_file_id,
            "created_at": self.created_at,
            "schema": self.schema,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "Envelope":
        """Parse version-envelope JSON, accepting legacy schema-1 payloads.

        Unknown keys are ignored and missing optional fields fall back to their
        defaults, so envelopes written by older/newer versions still load. In a
        schema-1 payload the submitter lived under ``owner`` (there was no
        distinction between owner and author yet) and there was no ``status``;
        such a record loads as a live version authored by its owner. The
        schema-1 ``password`` and ``updated_at`` keys are artifact-level state
        and are ignored here — see :func:`migrate_legacy`.

        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable artifact ID.
        """
        data = _load_object(raw, "envelope")

        artifact_id = data.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("envelope has no usable 'id'")

        source = data.get("source")
        author = data.get("author")
        if not isinstance(author, dict):
            legacy_owner = data.get("owner")
            author = legacy_owner if isinstance(legacy_owner, dict) else {}
        status = data.get("status")
        note = data.get("note")
        canonical_file_id = data.get("canonical_file_id")
        version = data.get("version")
        schema = data.get("schema")

        return cls(
            id=artifact_id,
            version=(
                version
                if isinstance(version, int)
                and not isinstance(version, bool)
                and version > 0
                else 1
            ),
            title=data.get("title") or "",
            html=data.get("html") or "",
            source_type=data.get("source_type") or "html",
            source=source if isinstance(source, dict) else {},
            author=author,
            status=status if status in _STATUSES else STATUS_LIVE,
            note=note if isinstance(note, str) and note else None,
            canonical_file_id=(
                canonical_file_id
                if isinstance(canonical_file_id, int)
                and not isinstance(canonical_file_id, bool)
                else None
            ),
            created_at=data.get("created_at") or "",
            schema=schema if isinstance(schema, int) else 1,
        )

    def public_meta(self, is_head: bool = False) -> dict:
        """Version metadata safe to expose to any capability-URL holder.

        Only the author's project identity and stack *hostname* are exposed —
        never a token, a full stack URL or the password record.
        """
        git = self.source.get("git") if self.source_type.startswith("git") else None
        return {
            "version": self.version,
            "title": self.title,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at,
            "is_head": bool(is_head),
            "size_bytes": len(self.html.encode("utf-8")),
            "source_type": self.source_type,
            "author": {
                "project_id": self.author.get("project_id"),
                "project_name": self.author.get("project_name"),
                "stack_host": _stack_host(str(self.author.get("stack_url") or "")),
            },
            "git": git,
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


def migrate_legacy(raw: bytes) -> tuple[Envelope, ArtifactMeta]:
    """Split a legacy schema-1 envelope into (version 1 envelope, meta record).

    Nothing is written: the caller decides whether to materialize the meta file.
    """
    envelope = Envelope.from_json(raw)
    envelope.version = 1
    envelope.status = STATUS_LIVE

    data = _load_object(raw, "envelope")
    owner = data.get("owner")
    password = data.get("password")
    meta = ArtifactMeta(
        id=envelope.id,
        owner=owner if isinstance(owner, dict) else dict(envelope.author),
        password=password if isinstance(password, dict) else None,
        accept_versions_mode=ACCEPT_OFF,
        head_mode=HEAD_LATEST,
        head_version=None,
        created_at=envelope.created_at,
        updated_at=data.get("updated_at") or envelope.created_at,
        schema=SCHEMA_VERSION,
    )
    return envelope, meta


@dataclass
class _VerEntry:
    """Where one version file lives, plus its status once we have loaded it.

    ``status`` is not derivable from tags, so it stays ``None`` until the
    envelope has been read at least once (cheap thanks to the LRU).
    """

    file_id: int
    status: str | None = None


@dataclass
class _ArtEntry:
    """Every host-project file belonging to one artifact."""

    meta_file_id: int | None = None
    # A schema-1 ``artifact-{id}.json`` envelope, if the artifact predates v2.
    legacy_file_id: int | None = None
    versions: dict[int, _VerEntry] = field(default_factory=dict)

    def version_numbers(self) -> list[int]:
        """All known version numbers, ascending; a legacy file counts as v1."""
        numbers = set(self.versions)
        if self.legacy_file_id is not None:
            numbers.add(1)
        return sorted(numbers)


class ArtifactStore:
    """Versioned serving store backed by Storage Files, with memory + disk caches.

    Thread safety: the index and the in-process LRUs are guarded by a lock;
    backend calls happen outside the lock so slow Storage requests never block
    other requests.
    """

    def __init__(
        self,
        backend: FilesBackend,
        cache_dir: Path,
        cache_max_entries: int,
        max_versions: int,
    ) -> None:
        self._backend = backend
        self._cache_dir = Path(cache_dir)
        self._cache_max_entries = max(0, int(cache_max_entries))
        self._max_versions = max(1, int(max_versions))
        self._index: dict[str, _ArtEntry] = {}
        self._memory: OrderedDict[tuple[str, int], Envelope] = OrderedDict()
        self._meta_memory: OrderedDict[tuple[str, int], ArtifactMeta] = OrderedDict()
        self._lock = threading.Lock()
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # Disk cache is optional — degrade gracefully.
            logger.warning("Cannot create cache dir %s: %s", self._cache_dir, exc)

    # ---------------------------------------------------------------- index

    def hydrate(self) -> int:
        """Rebuild the in-memory index from Storage. Returns artifact count.

        When several files describe the same thing (an interrupted write left an
        older file behind) the highest file ID — the newest upload — wins.
        """
        files = self._backend.search_by_tag(TAG_ALL)
        index: dict[str, _ArtEntry] = {}
        for info in files:
            artifact_id = _tag_value(info.tags, _TAG_ID_PREFIX)
            if not artifact_id:
                logger.warning(
                    "Skipping file %s (%s): no %s* tag",
                    info.id,
                    info.name,
                    _TAG_ID_PREFIX,
                )
                continue
            entry = index.setdefault(artifact_id, _ArtEntry())
            _absorb_file(entry, info)

        with self._lock:
            self._index = index
        logger.info("Hydrated index with %d artifact(s)", len(index))
        return len(index)

    def count(self) -> int:
        """Number of artifacts currently indexed."""
        with self._lock:
            return len(self._index)

    def _resolve(self, artifact_id: str) -> _ArtEntry | None:
        """Index lookup with a Storage fallback for artifacts we never saw.

        Another replica may have written after our last hydrate, so an index
        miss is re-checked against Storage before we answer "not found".
        """
        with self._lock:
            entry = self._index.get(artifact_id)
        if entry is not None:
            return entry

        files = self._backend.search_by_tag(tag_for_id(artifact_id))
        if not files:
            return None
        fresh = _ArtEntry()
        for info in files:
            _absorb_file(fresh, info)

        with self._lock:
            current = self._index.get(artifact_id)
            if current is None:
                self._index[artifact_id] = fresh
                return fresh
            return current

    # ----------------------------------------------------------------- meta

    def get_meta(self, artifact_id: str) -> ArtifactMeta | None:
        """Artifact-level state, synthesized for legacy artifacts, else None."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        if entry.meta_file_id is not None:
            meta = self._load_meta(artifact_id, entry.meta_file_id)
            if meta is not None:
                return meta
        if entry.legacy_file_id is not None:
            migrated = self._load_legacy(artifact_id, entry.legacy_file_id)
            if migrated is not None:
                return migrated[1]
        return None

    def save_meta(self, meta: ArtifactMeta) -> None:
        """Upload the meta file, retire older meta files and refresh caches."""
        artifact_id = meta.id
        owner_key = meta.owner_key
        if not owner_key:
            logger.warning("Saving meta of artifact %s without an owner key", artifact_id)
        raw = meta.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), TAG_META, tag_for_owner(owner_key)]

        file_id = self._backend.upload(f"artifact-{artifact_id}-meta.json", raw, tags)

        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            previous = entry.meta_file_id
            entry.meta_file_id = file_id
            self._forget_meta_locked(artifact_id)
            self._remember_meta_locked(artifact_id, file_id, meta)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous != file_id:
            self._delete_disk_cache(artifact_id, previous)
        self._delete_stale_meta_files(artifact_id, keep_file_id=file_id)

    def owner_key_of(self, artifact_id: str) -> str | None:
        """Owner key of an artifact, or None when the artifact is unknown."""
        meta = self.get_meta(artifact_id)
        return meta.owner_key if meta is not None else None

    # ------------------------------------------------------------- versions

    def create(self, meta: ArtifactMeta, first: Envelope) -> None:
        """Publish a brand-new artifact: its meta record plus its first version."""
        self.save_meta(meta)
        self.add_version(first)

    def add_version(self, env: Envelope) -> None:
        """Upload one version file, index it and apply the retention policy."""
        artifact_id = env.id
        version = env.version
        raw = env.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), tag_for_version(version)]

        # Upload first: a failure here leaves the index untouched.
        file_id = self._backend.upload(
            f"artifact-{artifact_id}-v{version}.json", raw, tags
        )

        with self._lock:
            entry = self._index.setdefault(artifact_id, _ArtEntry())
            previous = entry.versions.get(version)
            entry.versions[version] = _VerEntry(file_id=file_id, status=env.status)
            if previous is not None:
                self._memory.pop((artifact_id, previous.file_id), None)
            self._remember_locked(artifact_id, file_id, env)
        self._write_disk_cache(artifact_id, file_id, raw)
        if previous is not None and previous.file_id != file_id:
            self._delete_file(artifact_id, previous.file_id)
        self._prune_versions(artifact_id)

    def get_version(self, artifact_id: str, version: int) -> Envelope | None:
        """One version by number, regardless of its status; None when missing."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        return self._load_version(artifact_id, entry, version)

    def get_head(self, artifact_id: str) -> Envelope | None:
        """The version ``/a/{id}`` serves: the pinned one, else the newest live."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return None

        meta = self.get_meta(artifact_id)
        if (
            meta is not None
            and meta.head_mode == HEAD_PINNED
            and meta.head_version is not None
        ):
            pinned = self._load_version(artifact_id, entry, meta.head_version)
            if pinned is not None and pinned.status == STATUS_LIVE:
                return pinned
            logger.warning(
                "Artifact %s pins version %s which is not live; "
                "falling back to the newest live version",
                artifact_id,
                meta.head_version,
            )

        for number in reversed(entry.version_numbers()):
            env = self._load_version(artifact_id, entry, number)
            if env is not None and env.status == STATUS_LIVE:
                return env
        return None

    def list_versions(self, artifact_id: str) -> list[dict]:
        """Public metadata of every version (live and proposed), newest first."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return []
        head = self.get_head(artifact_id)
        head_version = head.version if head is not None else None

        metas: list[dict] = []
        for number in reversed(entry.version_numbers()):
            try:
                env = self._load_version(artifact_id, entry, number)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break the whole history.
                logger.warning(
                    "Cannot load version %s of artifact %s: %s",
                    number,
                    artifact_id,
                    exc,
                )
                continue
            if env is None:
                continue
            metas.append(env.public_meta(is_head=number == head_version))
        return metas

    def next_version(self, artifact_id: str) -> int:
        """The number the next submitted version gets (proposals included)."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return 1
        numbers = entry.version_numbers()
        return (max(numbers) + 1) if numbers else 1

    def set_status(self, artifact_id: str, version: int, status: str) -> bool:
        """Rewrite one version file with a new status. False when not found."""
        if status not in _STATUSES:
            raise ValueError(f"Unknown status: {status!r}")
        entry = self._resolve(artifact_id)
        if entry is None:
            return False
        env = self._load_version(artifact_id, entry, version)
        if env is None:
            return False
        if env.status == status and version in entry.versions:
            return True

        # Never mutate the cached object: rewrite a copy.
        self.add_version(replace(env, status=status, version=version))
        # A legacy artifact now has a real version file; the stale schema-1
        # envelope would otherwise shadow it as "version 1".
        self._retire_legacy_if_covered(artifact_id, version)
        return True

    def delete_version(self, artifact_id: str, version: int) -> bool:
        """Delete one version. Refuses to remove the last live version."""
        entry = self._resolve(artifact_id)
        if entry is None:
            return False
        numbers = entry.version_numbers()
        if version not in numbers:
            return False

        target = self._load_version(artifact_id, entry, version)
        if target is None:
            return False
        if target.status == STATUS_LIVE:
            live = [
                number
                for number in numbers
                if number != version and self._is_live(artifact_id, entry, number)
            ]
            if not live:
                logger.info(
                    "Refusing to delete the only live version %s of artifact %s",
                    version,
                    artifact_id,
                )
                return False

        with self._lock:
            ver_entry = entry.versions.pop(version, None)
            legacy_file_id = None
            if version == 1 and entry.legacy_file_id is not None:
                legacy_file_id = entry.legacy_file_id
                entry.legacy_file_id = None
            if ver_entry is not None:
                self._memory.pop((artifact_id, ver_entry.file_id), None)
            if legacy_file_id is not None:
                self._memory.pop((artifact_id, legacy_file_id), None)

        if ver_entry is not None:
            self._delete_file(artifact_id, ver_entry.file_id)
        if legacy_file_id is not None:
            self._delete_file(artifact_id, legacy_file_id)
        return True

    # --------------------------------------------------------------- delete

    def delete(self, artifact_id: str) -> bool:
        """Delete every Storage file of an artifact and purge the caches."""
        files: list[FileInfo] = self._backend.search_by_tag(tag_for_id(artifact_id))
        deleted = False
        for info in files:
            try:
                self._backend.delete(info.id)
                deleted = True
            except BackendError as exc:
                logger.warning(
                    "Cannot delete file %s of artifact %s: %s",
                    info.id,
                    artifact_id,
                    exc,
                )

        with self._lock:
            self._index.pop(artifact_id, None)
            self._forget_memory_locked(artifact_id)
            self._forget_meta_locked(artifact_id)
        self._purge_disk_cache(artifact_id)
        return deleted

    # ----------------------------------------------------------------- list

    def list_owner(self, owner_key: str) -> list[dict]:
        """One owner's artifacts, newest ``updated_at`` first.

        Driven by the owner tag, which lives on the meta file (and, for legacy
        artifacts, on the schema-1 envelope), so a cold process answers without
        a full hydrate.
        """
        files = self._backend.search_by_tag(tag_for_owner(owner_key))
        artifact_ids: list[str] = []
        for info in files:
            artifact_id = _tag_value(info.tags, _TAG_ID_PREFIX)
            if artifact_id and artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)

        rows: list[dict] = []
        for artifact_id in artifact_ids:
            try:
                row = self._owner_row(artifact_id)
            except BackendError:
                # Storage is unhealthy — the caller maps this to 502.
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break the whole listing.
                logger.warning(
                    "Cannot load artifact %s for listing: %s", artifact_id, exc
                )
                continue
            if row is not None:
                rows.append(row)

        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        return rows

    def _owner_row(self, artifact_id: str) -> dict | None:
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        meta = self.get_meta(artifact_id)
        if meta is None:
            logger.warning("Artifact %s has no meta record; skipping", artifact_id)
            return None
        head = self.get_head(artifact_id)

        versions_count = 0
        proposed_count = 0
        for number in entry.version_numbers():
            env = self._load_version(artifact_id, entry, number)
            if env is None:
                continue
            versions_count += 1
            if env.status == STATUS_PROPOSED:
                proposed_count += 1

        return {
            "id": artifact_id,
            "title": head.title if head is not None else "",
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "accept_versions": meta.accept_versions,
            "accept_versions_mode": meta.accept_versions_mode,
            "comments_mode": meta.comments_mode,
            "status": meta.status,
            "protected": bool(meta.password),
            "head_version": head.version if head is not None else None,
            "versions_count": versions_count,
            "proposed_count": proposed_count,
        }

    # ------------------------------------------------------------ retention

    def _prune_versions(self, artifact_id: str) -> None:
        """Drop the oldest prunable live versions above the retention limit.

        Never prunes a proposal, the version currently served as head, or the
        pinned version. Best effort: Storage failures are logged, not raised.
        """
        entry = self._resolve(artifact_id)
        if entry is None:
            return

        meta = self.get_meta(artifact_id)
        protected: set[int] = set()
        if meta is not None and meta.head_version is not None:
            protected.add(meta.head_version)
        head = self.get_head(artifact_id)
        if head is not None:
            protected.add(head.version)

        live: list[int] = []
        for number in entry.version_numbers():
            env = self._load_version(artifact_id, entry, number)
            if env is not None and env.status == STATUS_LIVE:
                live.append(number)

        prunable = [number for number in live if number not in protected]
        excess = len(live) - self._max_versions
        for number in prunable:
            if excess <= 0:
                break
            with self._lock:
                ver_entry = entry.versions.pop(number, None)
                legacy_file_id = None
                if number == 1 and entry.legacy_file_id is not None:
                    legacy_file_id = entry.legacy_file_id
                    entry.legacy_file_id = None
                if ver_entry is not None:
                    self._memory.pop((artifact_id, ver_entry.file_id), None)
                if legacy_file_id is not None:
                    self._memory.pop((artifact_id, legacy_file_id), None)
            if ver_entry is not None:
                self._delete_file(artifact_id, ver_entry.file_id)
            if legacy_file_id is not None:
                self._delete_file(artifact_id, legacy_file_id)
            logger.info("Pruned version %s of artifact %s", number, artifact_id)
            excess -= 1

    def _retire_legacy_if_covered(self, artifact_id: str, version: int) -> None:
        """Delete the schema-1 envelope once a real v1 file supersedes it."""
        if version != 1:
            return
        with self._lock:
            entry = self._index.get(artifact_id)
            if entry is None or entry.legacy_file_id is None:
                return
            if 1 not in entry.versions:
                return
            legacy_file_id = entry.legacy_file_id
            entry.legacy_file_id = None
            self._memory.pop((artifact_id, legacy_file_id), None)
        self._delete_file(artifact_id, legacy_file_id)

    def _delete_stale_meta_files(self, artifact_id: str, keep_file_id: int) -> None:
        """Best-effort removal of superseded meta files. Never raises."""
        try:
            files = self._backend.search_by_tag(tag_for_id(artifact_id))
        except BackendError as exc:
            logger.warning(
                "Cannot list meta files of artifact %s: %s", artifact_id, exc
            )
            return
        for info in files:
            if info.id == keep_file_id or TAG_META not in (info.tags or []):
                continue
            self._delete_file(artifact_id, info.id)

    def _delete_file(self, artifact_id: str, file_id: int) -> None:
        """Delete one Storage file and its cache entry. Never raises."""
        try:
            self._backend.delete(file_id)
        except BackendError as exc:
            logger.warning(
                "Cannot delete file %s of artifact %s: %s", file_id, artifact_id, exc
            )
        self._delete_disk_cache(artifact_id, file_id)

    # ----------------------------------------------------------------- load

    def _is_live(self, artifact_id: str, entry: _ArtEntry, version: int) -> bool:
        env = self._load_version(artifact_id, entry, version)
        return env is not None and env.status == STATUS_LIVE

    def _load_version(
        self, artifact_id: str, entry: _ArtEntry, version: int
    ) -> Envelope | None:
        """Load one version, transparently migrating a legacy schema-1 file."""
        ver_entry = entry.versions.get(version)
        if ver_entry is not None:
            env = self._load_envelope(artifact_id, ver_entry.file_id)
            if env is not None:
                ver_entry.status = env.status
                env.version = version
            return env
        if version == 1 and entry.legacy_file_id is not None:
            migrated = self._load_legacy(artifact_id, entry.legacy_file_id)
            return migrated[0] if migrated is not None else None
        return None

    def _load_legacy(
        self, artifact_id: str, file_id: int
    ) -> tuple[Envelope, ArtifactMeta] | None:
        raw = self._read_raw(artifact_id, file_id)
        if raw is None:
            return None
        try:
            return migrate_legacy(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt legacy envelope for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None

    def _load_envelope(self, artifact_id: str, file_id: int) -> Envelope | None:
        key = (artifact_id, file_id)
        with self._lock:
            envelope = self._memory.get(key)
            if envelope is not None:
                self._memory.move_to_end(key)
                return envelope

        raw = self._read_raw(artifact_id, file_id)
        if raw is None:
            return None
        try:
            envelope = Envelope.from_json(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt envelope in Storage for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        with self._lock:
            self._remember_locked(artifact_id, file_id, envelope)
        return envelope

    def _load_meta(self, artifact_id: str, file_id: int) -> ArtifactMeta | None:
        key = (artifact_id, file_id)
        with self._lock:
            meta = self._meta_memory.get(key)
            if meta is not None:
                self._meta_memory.move_to_end(key)
                return meta

        raw = self._read_raw(artifact_id, file_id)
        if raw is None:
            return None
        try:
            meta = ArtifactMeta.from_json(raw)
        except ValueError as exc:
            logger.error(
                "Corrupt meta in Storage for artifact %s (file %s): %s",
                artifact_id,
                file_id,
                exc,
            )
            return None
        with self._lock:
            self._remember_meta_locked(artifact_id, file_id, meta)
        return meta

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

    # ---------------------------------------------------------- memory LRU

    def _remember_locked(
        self, artifact_id: str, file_id: int, envelope: Envelope
    ) -> None:
        """Insert into the envelope LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._memory[key] = envelope
        self._memory.move_to_end(key)
        while len(self._memory) > self._cache_max_entries:
            self._memory.popitem(last=False)

    def _remember_meta_locked(
        self, artifact_id: str, file_id: int, meta: ArtifactMeta
    ) -> None:
        """Insert into the meta LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._meta_memory[key] = meta
        self._meta_memory.move_to_end(key)
        while len(self._meta_memory) > self._cache_max_entries:
            self._meta_memory.popitem(last=False)

    def _forget_memory_locked(self, artifact_id: str) -> None:
        """Drop every envelope LRU entry of an artifact. Caller holds the lock."""
        for key in [k for k in self._memory if k[0] == artifact_id]:
            self._memory.pop(key, None)

    def _forget_meta_locked(self, artifact_id: str) -> None:
        """Drop every meta LRU entry of an artifact. Caller holds the lock."""
        for key in [k for k in self._meta_memory if k[0] == artifact_id]:
            self._meta_memory.pop(key, None)

    # --------------------------------------------------------- disk cache

    def _cache_path(self, artifact_id: str, file_id: int) -> Path | None:
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return None
        return self._cache_dir / f"{artifact_id}-{file_id}.json"

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
                "Corrupt disk cache for artifact %s (file %s): %s",
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
        """Remove every cached file of an artifact."""
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return
        try:
            candidates = list(self._cache_dir.glob(f"{artifact_id}-*.json"))
        except OSError as exc:
            logger.warning("Cannot scan cache dir %s: %s", self._cache_dir, exc)
            return
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Cannot prune disk cache %s: %s", path, exc)


def _absorb_file(entry: _ArtEntry, info: FileInfo) -> None:
    """Record one Storage file in an index entry (newest file ID wins)."""
    tags = info.tags or []
    if TAG_META in tags:
        if entry.meta_file_id is None or entry.meta_file_id < info.id:
            entry.meta_file_id = info.id
        return
    version = _version_from_tags(tags)
    if version is not None:
        existing = entry.versions.get(version)
        if existing is None or existing.file_id < info.id:
            entry.versions[version] = _VerEntry(file_id=info.id)
        return
    # Neither meta nor version tag: a schema-1 single-envelope artifact.
    if entry.legacy_file_id is None or entry.legacy_file_id < info.id:
        entry.legacy_file_id = info.id
