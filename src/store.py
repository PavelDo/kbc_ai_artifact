"""Artifact envelopes and the serving store.

The single source of truth for served artifacts is Storage Files of the *host*
project: every artifact is one JSON "envelope" file named ``artifact-{id}.json``
tagged ``artifact-hub``, ``artifact-id-{id}`` and ``artifact-owner-{key}``.

Because the app container has no permanent disk, the process keeps only:

- an in-memory index ``{artifact_id: (file_id, owner_key)}`` rebuilt on startup
  by listing files tagged ``artifact-hub`` (:meth:`ArtifactStore.hydrate`),
- a small in-process LRU of parsed :class:`Envelope` objects, and
- a disk cache of raw envelope JSON under ``cache_dir`` (pure cache — safe to
  wipe at any time).

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
from dataclasses import dataclass, field
from pathlib import Path

from src.kbc import BackendError, FileInfo, FilesBackend

logger = logging.getLogger(__name__)

TAG_ALL = "artifact-hub"
_TAG_ID_PREFIX = "artifact-id-"
_TAG_OWNER_PREFIX = "artifact-owner-"

# Characters accepted in an artifact ID when it is used to build a cache file
# name. Artifact IDs come from ``secrets.token_urlsafe`` so this is a superset
# of what we generate; anything else simply skips the disk cache.
_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

_SOURCE_TYPES = ("html", "markdown", "git-html", "git-markdown")


def tag_for_id(artifact_id: str) -> str:
    """Storage tag identifying a single artifact."""
    return f"{_TAG_ID_PREFIX}{artifact_id}"


def tag_for_owner(owner_key: str) -> str:
    """Storage tag identifying all artifacts of one owner (stack + project)."""
    return f"{_TAG_OWNER_PREFIX}{owner_key}"


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


@dataclass
class Envelope:
    """The serving record for one artifact, stored as JSON in Storage Files."""

    id: str
    title: str
    html: str
    # One of _SOURCE_TYPES.
    source_type: str
    # Original input: {"markdown": "..."} / {"git": {...}} / {} for raw HTML.
    source: dict = field(default_factory=dict)
    # {"stack_url": str, "project_id": int, "project_name": str, "key": str}
    owner: dict = field(default_factory=dict)
    # security.hash_password() record, or None when the artifact is public.
    password: dict | None = None
    # File ID of the canonical copy in the *author's* project (may be absent).
    canonical_file_id: int | None = None
    # ISO 8601 UTC; produced by the caller, never by this module.
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    schema: int = 1

    def to_json(self) -> bytes:
        """Serialize the envelope to UTF-8 JSON bytes."""
        payload = {
            "id": self.id,
            "title": self.title,
            "html": self.html,
            "source_type": self.source_type,
            "source": self.source,
            "owner": self.owner,
            "password": self.password,
            "canonical_file_id": self.canonical_file_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "schema": self.schema,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "Envelope":
        """Parse envelope JSON.

        Unknown keys are ignored and missing optional fields fall back to their
        defaults, so envelopes written by older/newer versions still load.
        Raises ``ValueError`` when the payload is not a JSON object or carries
        no usable artifact ID — callers treat that as a corrupt record.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"envelope is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("envelope JSON is not an object")

        artifact_id = data.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("envelope has no usable 'id'")

        source = data.get("source")
        owner = data.get("owner")
        password = data.get("password")
        canonical_file_id = data.get("canonical_file_id")

        return cls(
            id=artifact_id,
            title=data.get("title") or "",
            html=data.get("html") or "",
            source_type=data.get("source_type") or "html",
            source=source if isinstance(source, dict) else {},
            owner=owner if isinstance(owner, dict) else {},
            password=password if isinstance(password, dict) else None,
            canonical_file_id=(
                canonical_file_id if isinstance(canonical_file_id, int) else None
            ),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            version=data.get("version") if isinstance(data.get("version"), int) else 1,
            schema=data.get("schema") if isinstance(data.get("schema"), int) else 1,
        )

    def public_meta(self) -> dict:
        """Metadata safe to expose publicly (no owner details, no password)."""
        git = self.source.get("git") if self.source_type.startswith("git") else None
        return {
            "id": self.id,
            "title": self.title,
            "source_type": self.source_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "protected": bool(self.password),
            "size_bytes": len(self.html.encode("utf-8")),
            "git": git,
        }


@dataclass(frozen=True)
class _IndexEntry:
    """Where an artifact currently lives in the host project."""

    file_id: int
    owner_key: str


class ArtifactStore:
    """Serving store backed by Storage Files, with memory + disk caches.

    Thread safety: the index and the in-process LRU are guarded by a lock;
    backend calls happen outside the lock so slow Storage requests never block
    other requests.
    """

    def __init__(
        self, backend: FilesBackend, cache_dir: Path, cache_max_entries: int
    ) -> None:
        self._backend = backend
        self._cache_dir = Path(cache_dir)
        self._cache_max_entries = max(0, int(cache_max_entries))
        self._index: dict[str, _IndexEntry] = {}
        self._memory: OrderedDict[tuple[str, int], Envelope] = OrderedDict()
        self._lock = threading.Lock()
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # Disk cache is optional — degrade gracefully.
            logger.warning("Cannot create cache dir %s: %s", self._cache_dir, exc)

    # ---------------------------------------------------------------- index

    def hydrate(self) -> int:
        """Rebuild the in-memory index from Storage. Returns artifact count.

        When several files carry the same artifact ID (an interrupted publish
        left an older version behind) the highest file ID — the newest upload —
        wins.
        """
        files = self._backend.search_by_tag(TAG_ALL)
        index: dict[str, _IndexEntry] = {}
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
            owner_key = _tag_value(info.tags, _TAG_OWNER_PREFIX)
            if owner_key is None:
                logger.warning(
                    "File %s (artifact %s) has no %s* tag; owner unknown",
                    info.id,
                    artifact_id,
                    _TAG_OWNER_PREFIX,
                )
                owner_key = ""
            existing = index.get(artifact_id)
            if existing is not None and existing.file_id >= info.id:
                continue
            index[artifact_id] = _IndexEntry(file_id=info.id, owner_key=owner_key)

        with self._lock:
            self._index = index
        logger.info("Hydrated index with %d artifact(s)", len(index))
        return len(index)

    def count(self) -> int:
        """Number of artifacts currently indexed."""
        with self._lock:
            return len(self._index)

    # -------------------------------------------------------------- publish

    def publish(self, envelope: Envelope) -> None:
        """Upload the envelope, retire older versions and refresh the caches."""
        artifact_id = envelope.id
        owner_key = str(envelope.owner.get("key") or "")
        if not owner_key:
            logger.warning("Publishing artifact %s without an owner key", artifact_id)
        raw = envelope.to_json()
        tags = [TAG_ALL, tag_for_id(artifact_id), tag_for_owner(owner_key)]

        file_id = self._backend.upload(f"artifact-{artifact_id}.json", raw, tags)
        self._cleanup_old_versions(artifact_id, keep_file_id=file_id)

        with self._lock:
            self._index[artifact_id] = _IndexEntry(
                file_id=file_id, owner_key=owner_key
            )
            self._forget_memory_locked(artifact_id)
            self._remember_locked(artifact_id, file_id, envelope)
        self._write_disk_cache(artifact_id, file_id, raw)

    def _cleanup_old_versions(self, artifact_id: str, keep_file_id: int) -> None:
        """Best-effort removal of superseded envelope files. Never raises."""
        try:
            files = self._backend.search_by_tag(tag_for_id(artifact_id))
        except BackendError as exc:
            logger.warning(
                "Cannot list old versions of artifact %s: %s", artifact_id, exc
            )
            return
        for info in files:
            if info.id == keep_file_id:
                continue
            try:
                self._backend.delete(info.id)
            except BackendError as exc:
                logger.warning(
                    "Cannot delete stale file %s of artifact %s: %s",
                    info.id,
                    artifact_id,
                    exc,
                )

    # ------------------------------------------------------------------ get

    def get(self, artifact_id: str) -> Envelope | None:
        """Load an artifact, or None when it does not exist.

        ``BackendError`` from Storage reads propagates (the API maps it to 502);
        only best-effort cleanup paths swallow errors.
        """
        entry = self._resolve(artifact_id)
        if entry is None:
            return None
        return self._load(artifact_id, entry.file_id)

    def owner_key_of(self, artifact_id: str) -> str | None:
        """Owner key of an artifact, or None when the artifact is unknown."""
        entry = self._resolve(artifact_id)
        return entry.owner_key if entry is not None else None

    def _resolve(self, artifact_id: str) -> _IndexEntry | None:
        """Index lookup with a Storage fallback for artifacts we never saw.

        Another replica may have published after our last hydrate, so an index
        miss is re-checked against Storage before we answer "not found".
        """
        with self._lock:
            entry = self._index.get(artifact_id)
        if entry is not None:
            return entry

        files = self._backend.search_by_tag(tag_for_id(artifact_id))
        if not files:
            return None
        newest = max(files, key=lambda info: info.id)
        owner_key = _tag_value(newest.tags, _TAG_OWNER_PREFIX) or ""
        entry = _IndexEntry(file_id=newest.id, owner_key=owner_key)
        with self._lock:
            current = self._index.get(artifact_id)
            if current is None or current.file_id < entry.file_id:
                self._index[artifact_id] = entry
            else:
                entry = current
        return entry

    def _load(self, artifact_id: str, file_id: int) -> Envelope | None:
        """Memory LRU -> disk cache -> Storage download."""
        key = (artifact_id, file_id)
        with self._lock:
            envelope = self._memory.get(key)
            if envelope is not None:
                self._memory.move_to_end(key)
                return envelope

        raw = self._read_disk_cache(artifact_id, file_id)
        if raw is not None:
            try:
                envelope = Envelope.from_json(raw)
            except ValueError as exc:
                logger.warning(
                    "Corrupt disk cache for artifact %s (file %s): %s",
                    artifact_id,
                    file_id,
                    exc,
                )
                self._delete_disk_cache(artifact_id, file_id)
            else:
                with self._lock:
                    self._remember_locked(artifact_id, file_id, envelope)
                return envelope

        raw = self._backend.download(file_id)
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
        self._write_disk_cache(artifact_id, file_id, raw)
        return envelope

    # ----------------------------------------------------------------- list

    def list_owner(self, owner_key: str) -> list[dict]:
        """Public metadata of one owner's artifacts, newest ``updated_at`` first."""
        with self._lock:
            artifact_ids = [
                artifact_id
                for artifact_id, entry in self._index.items()
                if entry.owner_key == owner_key
            ]

        metas: list[dict] = []
        for artifact_id in artifact_ids:
            try:
                envelope = self.get(artifact_id)
            except BackendError:
                # Storage is unhealthy — the caller maps this to 502.
                raise
            except Exception as exc:  # noqa: BLE001 - one bad record must not
                # break the whole listing.
                logger.warning(
                    "Cannot load artifact %s for listing: %s", artifact_id, exc
                )
                continue
            if envelope is None:
                logger.warning("Artifact %s vanished while listing", artifact_id)
                continue
            metas.append(envelope.public_meta())

        metas.sort(key=lambda meta: meta.get("updated_at") or "", reverse=True)
        return metas

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
        self._purge_disk_cache(artifact_id)
        return deleted

    # ---------------------------------------------------------- memory LRU

    def _remember_locked(
        self, artifact_id: str, file_id: int, envelope: Envelope
    ) -> None:
        """Insert into the LRU. Caller must hold the lock."""
        if self._cache_max_entries <= 0:
            return
        key = (artifact_id, file_id)
        self._memory[key] = envelope
        self._memory.move_to_end(key)
        while len(self._memory) > self._cache_max_entries:
            self._memory.popitem(last=False)

    def _forget_memory_locked(self, artifact_id: str) -> None:
        """Drop every LRU entry of an artifact. Caller must hold the lock."""
        for key in [k for k in self._memory if k[0] == artifact_id]:
            self._memory.pop(key, None)

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
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Cannot read disk cache %s: %s", path, exc)
            return None

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
            return
        self._purge_disk_cache(artifact_id, keep_file_id=file_id)

    def _delete_disk_cache(self, artifact_id: str, file_id: int) -> None:
        path = self._cache_path(artifact_id, file_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Cannot delete disk cache %s: %s", path, exc)

    def _purge_disk_cache(
        self, artifact_id: str, keep_file_id: int | None = None
    ) -> None:
        """Remove cached files of an artifact, optionally keeping one version."""
        if not artifact_id or not set(artifact_id) <= _SAFE_ID_CHARS:
            return
        keep = (
            self._cache_path(artifact_id, keep_file_id)
            if keep_file_id is not None
            else None
        )
        try:
            candidates = list(self._cache_dir.glob(f"{artifact_id}-*.json"))
        except OSError as exc:
            logger.warning("Cannot scan cache dir %s: %s", self._cache_dir, exc)
            return
        for path in candidates:
            if keep is not None and path == keep:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Cannot prune disk cache %s: %s", path, exc)
