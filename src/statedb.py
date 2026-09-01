"""Operational state: a SQLite sidecar snapshotted into Storage Files.

Content (artifacts, versions, comments) stays where it always was — one Storage
File per item, authoritative, reconstructible by tag listing. This module covers
the *other* kind of state, the one that class of durability fits badly:

* **rate-limit counters** — today they live in ``main.py`` dicts and evaporate on
  every restart, which hands a fresh daily budget to anybody who waits for a
  redeploy;
* **view analytics** — per-artifact, per-day, per-kind counts, which are far too
  chatty to write one Storage File per event.

Both are aggregates rather than documents, so they get a real database. The DB
lives on the container's ephemeral disk and periodically snapshots itself into
the *host* project's Storage Files under the :data:`TAG_STATE` tag; on startup
the newest snapshot is restored. One instance at a time is assumed and
detected, not coordinated (see ``_retire_older_snapshots``). Losing the last
few minutes of counters to a
crash is acceptable for this class of data — losing *all* of it on every restart
was not.

Like :mod:`src.store` and :mod:`src.comments` this module is clock-free: buckets
and days are strings chosen by the caller (``"2026-09-01"``, ``"2026-09-01T14"``),
which keeps it trivially testable. The only exception is the snapshot thread's
own scheduling, which is elapsed time, not wall-clock.

Every Storage failure degrades instead of raising: a missing, oversized or
corrupt snapshot simply means starting from an empty database, and a failed
upload means the next snapshot tries again. Nothing here may take the app down.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from src.kbc import FilesBackend

logger = logging.getLogger(__name__)

__all__ = [
    "SNAPSHOT_FILE_NAME",
    "TAG_STATE",
    "ForeignWriterError",
    "StateDB",
    "foreign_writer_detected",
    "note_foreign_writer",
    "reset_foreign_writer",
]


class ForeignWriterError(RuntimeError):
    """Raised by every :class:`StateDB` write once a second writer was seen.

    ARCH-100-001. A foreign snapshot used to be logged and then retired,
    which meant the two instances kept taking turns destroying each other's
    counters while both went on serving. The log line was the only signal, and
    nothing consumed it. Now the first detection latches the process into a
    read-only state: writes raise this, snapshots stop, the foreign snapshot is
    left alone for an operator to look at, and ``/health`` reports 503.

    Callers in ``src/main.py`` already treat a sidecar failure as "degrade to
    the per-process fallback counter", so raising here costs a request nothing
    but its durable tally -- which is exactly the thing that is no longer
    trustworthy.
    """


# The latch is deliberately module-level rather than per-instance: it describes
# the *process*, not one database handle. A rebuilt StateDB in a process that
# has already seen a second writer is no more entitled to write than the old
# one was. ``reset_foreign_writer`` exists for tests, which need each case to
# start from a clean process-wide state.
_foreign_writer_lock = threading.Lock()
_foreign_writer_detail: str | None = None


def note_foreign_writer(detail: str) -> None:
    """Latch "another instance is writing", recording why. First call wins."""
    global _foreign_writer_detail
    with _foreign_writer_lock:
        if _foreign_writer_detail is None:
            _foreign_writer_detail = detail


def foreign_writer_detected() -> str | None:
    """The reason this process stopped writing state, or ``None`` if it did not."""
    with _foreign_writer_lock:
        return _foreign_writer_detail


def reset_foreign_writer() -> None:
    """Clear the latch. For tests only -- a live process never un-detects."""
    global _foreign_writer_detail
    with _foreign_writer_lock:
        _foreign_writer_detail = None


#: Tag carried by every state snapshot in the host project. Disjoint from the
#: ``artifact-hub`` / ``artifact-hub-cmt`` namespaces, so neither
#: ``ArtifactStore.hydrate`` nor ``CommentStore.hydrate`` ever sees a snapshot.
TAG_STATE = "artifact-hub-state"

#: Storage file name used for every snapshot. Snapshots are distinguished by
#: file id (newest wins), not by name.
SNAPSHOT_FILE_NAME = "artifact-hub-state.sqlite3"

#: Magic header of a SQLite 3 database file. A restored blob that does not start
#: with it is not worth handing to ``sqlite3`` — we start fresh instead.
_SQLITE_MAGIC = b"SQLite format 3\x00"

#: Number of most recent days :meth:`StateDB.views` reports in ``by_day``.
VIEW_DAYS_REPORTED = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    scope  TEXT NOT NULL,
    "key"  TEXT NOT NULL,
    bucket TEXT NOT NULL,
    value  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, "key", bucket)
);
CREATE TABLE IF NOT EXISTS views (
    artifact_id TEXT NOT NULL,
    day         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (artifact_id, day, kind)
);
CREATE INDEX IF NOT EXISTS views_by_artifact ON views (artifact_id);
CREATE INDEX IF NOT EXISTS counters_by_bucket ON counters (bucket);
"""


class StateDB:
    """SQLite-backed operational state with Storage Files durability.

    :param backend: host-project files backend used for snapshots.
    :param db_path: local path of the SQLite file (ephemeral disk).
    :param snapshot_interval_s: seconds between background snapshots; ``0``
        disables the thread entirely and leaves snapshots to explicit
        :meth:`snapshot_now` calls (what the tests do).
    :param max_snapshot_bytes: refuse to restore *or* upload a snapshot larger
        than this; ``0`` disables the bound.
    """

    def __init__(
        self,
        backend: FilesBackend,
        db_path: Path,
        snapshot_interval_s: int,
        max_snapshot_bytes: int,
    ) -> None:
        self._backend = backend
        self._db_path = Path(db_path)
        self._interval = max(0, int(snapshot_interval_s))
        self._max_snapshot_bytes = max(0, int(max_snapshot_bytes))
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        #: Id of the snapshot currently in Storage, so older ones can be retired.
        self._snapshot_file_id: int | None = None
        #: Writes applied so far, and the value that was current when the last
        #: successful snapshot was serialized. Equal means "nothing changed
        #: since", which is what makes :meth:`snapshot_now` a no-op. A counter
        #: rather than a bool so a write landing *while* a snapshot uploads is
        #: not mistaken for one the snapshot already contains.
        self._writes = 0
        self._snapshotted_writes = 0
        self._started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Restore the newest snapshot, open the DB and start the timer thread.

        Safe to call once; a second call is a no-op. Never raises for a Storage
        problem — a hub that cannot reach its snapshot still has to serve.
        """
        if self._started:
            return
        self._restore()
        self._open()
        self._started = True
        if self._interval > 0:
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._snapshot_loop,
                name="statedb-snapshot",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        """Take a final snapshot and close. Idempotent, and never raises."""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        if self._conn is not None:
            try:
                self.snapshot_now()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Final state snapshot failed: %s", exc)
            try:
                with self._lock:
                    self._conn.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Closing the state database failed: %s", exc)
            self._conn = None
        self._started = False

    # ------------------------------------------------------------------ #
    # counters
    # ------------------------------------------------------------------ #

    def bump(self, scope: str, key: str, bucket: str, by: int = 1) -> int:
        """Add ``by`` to one counter and return its new value.

        ``scope`` names the counter family (``"submissions"``,
        ``"unlock_failures"``, ...), ``key`` identifies who/what is counted and
        ``bucket`` is the caller-chosen time window (``"2026-09-01"`` for a day,
        ``"2026-09-01T14"`` for an hour). The upsert is atomic; concurrent
        requests cannot lose an increment.

        By convention ``key`` starts with the artifact id (alone, or followed by
        ``":"`` and more) so :meth:`forget_artifact` can purge it.
        """
        self._refuse_if_foreign()
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                'INSERT INTO counters (scope, "key", bucket, value) '
                "VALUES (?, ?, ?, ?) "
                'ON CONFLICT(scope, "key", bucket) '
                "DO UPDATE SET value = value + excluded.value",
                (scope, key, bucket, int(by)),
            )
            row = conn.execute(
                'SELECT value FROM counters WHERE scope = ? AND "key" = ? '
                "AND bucket = ?",
                (scope, key, bucket),
            ).fetchone()
            conn.commit()
            self._writes += 1
        return int(row[0]) if row else 0

    def count(self, scope: str, key: str, bucket: str) -> int:
        """Current value of one counter; ``0`` when it was never bumped."""
        self._refuse_if_foreign()
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                'SELECT value FROM counters WHERE scope = ? AND "key" = ? '
                "AND bucket = ?",
                (scope, key, bucket),
            ).fetchone()
        return int(row[0]) if row else 0

    def prune_counters_before(self, bucket: str) -> int:
        """Delete every counter whose bucket sorts before ``bucket``.

        Buckets are ISO-ish strings, so lexicographic order *is* chronological
        order. Called with e.g. yesterday's day string this keeps the counters
        table (and therefore the snapshot) from growing without bound. Returns
        the number of rows removed.
        """
        self._refuse_if_foreign()
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM counters WHERE bucket < ?", (bucket,)
            )
            conn.commit()
            removed = int(cursor.rowcount or 0)
            if removed:
                self._writes += 1
        return removed

    # ------------------------------------------------------------------ #
    # view analytics
    # ------------------------------------------------------------------ #

    def record_view(self, artifact_id: str, day: str, kind: str) -> None:
        """Count one view of ``artifact_id`` on ``day`` of the given ``kind``.

        ``kind`` is the surface that was hit — ``"page"``, ``"raw"``,
        ``"source"``, ... — kept as a free string so new surfaces need no schema
        change.
        """
        self._refuse_if_foreign()
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO views (artifact_id, day, kind, count) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(artifact_id, day, kind) "
                "DO UPDATE SET count = count + 1",
                (artifact_id, day, kind),
            )
            conn.commit()
            self._writes += 1

    def views(self, artifact_id: str) -> dict:
        """Aggregate view counts for one artifact.

        Returns ``{"total": int, "by_day": [{"day", "count"}, ...],
        "by_kind": {kind: count}}``. ``total`` and ``by_kind`` cover all of
        recorded history; ``by_day`` is trimmed to the
        :data:`VIEW_DAYS_REPORTED` most recent days and ordered oldest first, so
        it can be charted as-is.
        """
        conn = self._require_conn()
        with self._lock:
            total_row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM views WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            day_rows = conn.execute(
                "SELECT day, SUM(count) FROM views WHERE artifact_id = ? "
                "GROUP BY day ORDER BY day DESC LIMIT ?",
                (artifact_id, VIEW_DAYS_REPORTED),
            ).fetchall()
            kind_rows = conn.execute(
                "SELECT kind, SUM(count) FROM views WHERE artifact_id = ? "
                "GROUP BY kind ORDER BY kind",
                (artifact_id,),
            ).fetchall()
        by_day = [
            {"day": str(day), "count": int(count)}
            for day, count in reversed(day_rows)
        ]
        by_kind = {str(kind): int(count) for kind, count in kind_rows}
        return {
            "total": int(total_row[0]) if total_row else 0,
            "by_day": by_day,
            "by_kind": by_kind,
        }

    def forget_artifact(self, artifact_id: str) -> None:
        """Purge every row belonging to one artifact (called on delete).

        Removes its view rows and any counter whose ``key`` is the artifact id
        or begins with ``"{artifact_id}:"``. Matching is done with ``substr``
        rather than ``LIKE`` so ids containing ``%`` or ``_`` behave.
        """
        self._refuse_if_foreign()
        conn = self._require_conn()
        prefix = f"{artifact_id}:"
        with self._lock:
            conn.execute("DELETE FROM views WHERE artifact_id = ?", (artifact_id,))
            conn.execute(
                'DELETE FROM counters WHERE "key" = ? '
                'OR substr("key", 1, ?) = ?',
                (artifact_id, len(prefix), prefix),
            )
            conn.commit()
            self._writes += 1

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def snapshot_now(self) -> bool:
        """Serialize the DB and upload it as the newest snapshot.

        Returns ``True`` when a snapshot was uploaded, ``False`` when there was
        nothing to do (no writes since the last snapshot) or the attempt failed.
        Failure is logged and swallowed: the next tick tries again, and the
        write counter is left untouched so no write is silently dropped.

        A snapshot replaces the whole database, so once a second writer has
        been detected (ARCH-100-001) uploading one would destroy that writer's
        state -- which is precisely the damage the detector exists to stop.
        This becomes a quiet no-op instead, and stays one for the life of the
        process. It returns ``False`` rather than raising because ``stop()``
        and the background thread both call it and neither may fail.
        """
        conn = self._conn
        if conn is None:
            return False
        if foreign_writer_detected() is not None:
            return False
        with self._lock:
            if self._writes == self._snapshotted_writes:
                return False
            captured_writes = self._writes
            try:
                blob = self._serialize(conn)
            except Exception as exc:  # noqa: BLE001 - snapshotting is optional
                logger.warning("Could not serialize the state database: %s", exc)
                return False
        if self._max_snapshot_bytes and len(blob) > self._max_snapshot_bytes:
            logger.warning(
                "State snapshot is %d bytes, over the %d byte limit; skipping "
                "the upload (raise HUB_STATE_MAX_SNAPSHOT_BYTES or prune)",
                len(blob),
                self._max_snapshot_bytes,
            )
            return False
        previous = self._snapshot_file_id
        try:
            file_id = self._backend.upload(
                SNAPSHOT_FILE_NAME, blob, [TAG_STATE]
            )
        except Exception as exc:  # noqa: BLE001 - BackendError and anything else
            logger.warning("State snapshot upload failed: %s", exc)
            return False
        with self._lock:
            self._snapshot_file_id = file_id
            self._snapshotted_writes = captured_writes
        self._retire_older_snapshots(keep_id=file_id, previous_id=previous)
        return True

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise RuntimeError("StateDB.start() has not been called")
        return conn

    @staticmethod
    def _refuse_if_foreign() -> None:
        """Raise once this process has seen a second writer (ARCH-100-001).

        Reads go through here too, not only writes. A counter is a bump/read
        pair: if the bump degraded to the per-process fallback dict while the
        read still came from a database another instance is also editing, the
        limit would be compared against a tally that never received the
        increments -- worse than having no sidecar at all. Refusing both keeps
        the pair on one consistent source. ``views()`` is exempt: it is pure
        analytics, nothing enforces anything with it, and stale numbers are
        better than an owner-facing error.
        """
        detail = foreign_writer_detected()
        if detail is not None:
            raise ForeignWriterError(detail)

    def _open(self) -> None:
        """Open (or re-create) the SQLite file and make sure the schema is there."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            logger.warning(
                "State database at %s is unusable (%s); starting fresh",
                self._db_path,
                exc,
            )
            self._db_path.unlink(missing_ok=True)
            conn = self._connect()
        self._conn = conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _restore(self) -> None:
        """Download the newest snapshot over ``db_path``, if there is a usable one."""
        try:
            found = self._backend.search_by_tag(TAG_STATE)
        except Exception as exc:  # noqa: BLE001 - degrade to an empty DB
            logger.warning("Could not list state snapshots: %s", exc)
            return
        if not found:
            logger.info("No state snapshot found; starting with an empty database")
            return
        newest = found[0]
        limit = self._max_snapshot_bytes
        if limit and newest.size_bytes > limit:
            logger.warning(
                "Newest state snapshot (file %s) is %d bytes, over the %d byte "
                "limit; starting with an empty database",
                newest.id,
                newest.size_bytes,
                limit,
            )
            return
        try:
            blob = self._backend.download(newest.id)
        except Exception as exc:  # noqa: BLE001 - degrade to an empty DB
            logger.warning("Could not download state snapshot %s: %s", newest.id, exc)
            return
        if limit and len(blob) > limit:
            logger.warning(
                "State snapshot %s is %d bytes once downloaded, over the %d "
                "byte limit; starting with an empty database",
                newest.id,
                len(blob),
                limit,
            )
            return
        if not blob.startswith(_SQLITE_MAGIC):
            logger.warning(
                "State snapshot %s is not a SQLite database; starting with an "
                "empty one",
                newest.id,
            )
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path.write_bytes(blob)
        except OSError as exc:
            logger.warning("Could not write the restored state database: %s", exc)
            return
        self._snapshot_file_id = newest.id
        logger.info(
            "Restored state database from snapshot %s (%d bytes)",
            newest.id,
            len(blob),
        )

    @staticmethod
    def _serialize(conn: sqlite3.Connection) -> bytes:
        """Return the whole database as bytes via the SQLite backup API.

        The backup API produces a consistent image of a live connection, which
        reading the file behind its back would not.
        """
        temp_dir = tempfile.mkdtemp(prefix="statedb-snap-")
        try:
            target = Path(temp_dir) / SNAPSHOT_FILE_NAME
            dest = sqlite3.connect(str(target))
            try:
                conn.backup(dest)
            finally:
                dest.close()
            return target.read_bytes()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _retire_older_snapshots(self, *, keep_id: int, previous_id: int | None) -> None:
        """Best-effort delete of every snapshot except the one just uploaded.

        Also the single-instance detector, because the listing it needs is
        already in hand. This sidecar snapshots the *whole* local database and
        retires everything else, which is only sound when exactly one instance
        writes -- the deployment invariant (one Keboola App per organisation,
        one container; see CLAUDE.md). Storage file ids are monotonic, so a
        snapshot newer than the one this instance last wrote, that is not the
        one it just uploaded, was written by another instance.

        ARCH-100-001: that discovery used to produce only a log line, and
        the retirement went ahead anyway, so the two instances kept deleting
        each other's state while both carried on serving. Now the discovery
        latches the process read-only (:func:`note_foreign_writer`), which
        stops every later write and snapshot and turns ``/health`` into a 503,
        and **nothing is deleted** on that pass -- both snapshots are left in
        Storage for an operator to inspect and choose between. There is still
        no merge, because there is still no way to invent one correctly; what
        changed is that the process stops making the situation worse.
        """
        try:
            found = self._backend.search_by_tag(TAG_STATE)
            stale = [info.id for info in found if info.id != keep_id]
        except Exception as exc:  # noqa: BLE001 - retiring is best effort
            logger.warning("Could not list old state snapshots: %s", exc)
            stale = [previous_id] if previous_id is not None else []
        else:
            if previous_id is not None:
                foreign = sorted(
                    info.id for info in found
                    if info.id != keep_id and info.id > previous_id
                )
                if foreign:
                    detail = (
                        f"State snapshot(s) {foreign} were written by another "
                        f"instance since this instance's last snapshot "
                        f"{previous_id}. This deployment assumes exactly one "
                        "replica per hub (CLAUDE.md, \"Exactly one instance, "
                        "ever\"); with two, each overwrites the other's "
                        "counters and the artifact index of a warm process can "
                        "serve a link that was rotated away elsewhere. This "
                        "process has stopped writing operational state and "
                        "reports itself unready; run exactly one container and "
                        "restart."
                    )
                    logger.error("%s", detail)
                    note_foreign_writer(detail)
                    # Leave every snapshot in place, the foreign one included:
                    # deleting it is the data loss this detector exists to
                    # prevent, and an operator needs both to decide which
                    # instance's counters to keep.
                    return
        for file_id in stale:
            try:
                self._backend.delete(file_id)
            except Exception as exc:  # noqa: BLE001 - retiring is best effort
                logger.warning("Could not delete old state snapshot %s: %s", file_id, exc)

    def _snapshot_loop(self) -> None:
        """Daemon loop: snapshot every ``snapshot_interval_s`` until stopped."""
        while not self._stop_event.wait(self._interval):
            try:
                self.snapshot_now()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.warning("Scheduled state snapshot failed: %s", exc)
