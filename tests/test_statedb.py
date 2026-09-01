"""Tests for the SQLite operational-state sidecar (:mod:`src.statedb`)."""

import time
from pathlib import Path

import pytest

from src.kbc import BackendError, InMemoryFilesBackend
from src.statedb import SNAPSHOT_FILE_NAME, TAG_STATE, VIEW_DAYS_REPORTED, StateDB


@pytest.fixture
def db(backend: InMemoryFilesBackend, tmp_path: Path):
    """A started StateDB with the snapshot thread disabled (interval 0)."""
    state = StateDB(
        backend=backend,
        db_path=tmp_path / "state.sqlite3",
        snapshot_interval_s=0,
        max_snapshot_bytes=10 * 1024 * 1024,
    )
    state.start()
    yield state
    state.stop()


def _snapshots(backend: InMemoryFilesBackend) -> list:
    return backend.search_by_tag(TAG_STATE)


class TestCounters:
    def test_bump_returns_running_value(self, db: StateDB) -> None:
        assert db.bump("submissions", "abc", "2026-09-01") == 1
        assert db.bump("submissions", "abc", "2026-09-01") == 2
        assert db.bump("submissions", "abc", "2026-09-01", by=5) == 7
        assert db.count("submissions", "abc", "2026-09-01") == 7

    def test_count_of_unknown_counter_is_zero(self, db: StateDB) -> None:
        assert db.count("submissions", "nobody", "2026-09-01") == 0

    def test_buckets_scopes_and_keys_are_independent(self, db: StateDB) -> None:
        db.bump("submissions", "abc", "2026-09-01")
        db.bump("submissions", "abc", "2026-09-02")
        db.bump("submissions", "xyz", "2026-09-01")
        db.bump("comments", "abc", "2026-09-01")

        assert db.count("submissions", "abc", "2026-09-01") == 1
        assert db.count("submissions", "abc", "2026-09-02") == 1
        assert db.count("submissions", "xyz", "2026-09-01") == 1
        assert db.count("comments", "abc", "2026-09-01") == 1

    def test_hourly_buckets_for_unlock_throttling(self, db: StateDB) -> None:
        assert db.bump("unlock_failures", "abc:1.2.3.4", "2026-09-01T14") == 1
        assert db.bump("unlock_failures", "abc:1.2.3.4", "2026-09-01T14") == 2
        assert db.count("unlock_failures", "abc:1.2.3.4", "2026-09-01T15") == 0

    def test_prune_counters_before_drops_older_buckets_only(self, db: StateDB) -> None:
        db.bump("submissions", "abc", "2026-08-30")
        db.bump("submissions", "abc", "2026-08-31")
        db.bump("submissions", "abc", "2026-09-01")

        removed = db.prune_counters_before("2026-09-01")

        assert removed == 2
        assert db.count("submissions", "abc", "2026-08-31") == 0
        assert db.count("submissions", "abc", "2026-09-01") == 1


class TestViews:
    def test_views_aggregate_total_days_and_kinds(self, db: StateDB) -> None:
        db.record_view("abc", "2026-09-01", "page")
        db.record_view("abc", "2026-09-01", "page")
        db.record_view("abc", "2026-09-01", "raw")
        db.record_view("abc", "2026-09-02", "source")

        result = db.views("abc")

        assert result["total"] == 4
        assert result["by_day"] == [
            {"day": "2026-09-01", "count": 3},
            {"day": "2026-09-02", "count": 1},
        ]
        assert result["by_kind"] == {"page": 2, "raw": 1, "source": 1}

    def test_views_of_unknown_artifact_are_empty(self, db: StateDB) -> None:
        assert db.views("nope") == {"total": 0, "by_day": [], "by_kind": {}}

    def test_by_day_keeps_only_the_most_recent_thirty_days(self, db: StateDB) -> None:
        for index in range(40):
            db.record_view("abc", f"2026-01-{index + 1:02d}", "page")

        result = db.views("abc")

        assert result["total"] == 40
        assert len(result["by_day"]) == VIEW_DAYS_REPORTED
        # Oldest first, and the ten oldest days are trimmed away.
        assert result["by_day"][0]["day"] == "2026-01-11"
        assert result["by_day"][-1]["day"] == "2026-01-40"

    def test_views_are_per_artifact(self, db: StateDB) -> None:
        db.record_view("abc", "2026-09-01", "page")
        db.record_view("xyz", "2026-09-01", "page")

        assert db.views("abc")["total"] == 1
        assert db.views("xyz")["total"] == 1


class TestForgetArtifact:
    def test_purges_views_and_prefixed_counters(self, db: StateDB) -> None:
        db.record_view("abc", "2026-09-01", "page")
        db.bump("submissions", "abc", "2026-09-01")
        db.bump("unlock_failures", "abc:1.2.3.4", "2026-09-01T14")
        db.record_view("keep", "2026-09-01", "page")
        db.bump("submissions", "keep", "2026-09-01")

        db.forget_artifact("abc")

        assert db.views("abc") == {"total": 0, "by_day": [], "by_kind": {}}
        assert db.count("submissions", "abc", "2026-09-01") == 0
        assert db.count("unlock_failures", "abc:1.2.3.4", "2026-09-01T14") == 0
        # A different artifact whose id merely shares a prefix is untouched.
        assert db.views("keep")["total"] == 1
        assert db.count("submissions", "keep", "2026-09-01") == 1

    def test_forgetting_an_unknown_artifact_is_a_no_op(self, db: StateDB) -> None:
        db.forget_artifact("never-seen")


class TestSnapshots:
    def test_snapshot_uploads_one_tagged_file(
        self, db: StateDB, backend: InMemoryFilesBackend
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01")

        assert db.snapshot_now() is True

        files = _snapshots(backend)
        assert len(files) == 1
        assert files[0].name == SNAPSHOT_FILE_NAME
        assert files[0].tags == [TAG_STATE]
        assert backend.download(files[0].id).startswith(b"SQLite format 3")

    def test_second_snapshot_retires_the_older_one(
        self, db: StateDB, backend: InMemoryFilesBackend
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01")
        assert db.snapshot_now() is True
        first_id = _snapshots(backend)[0].id

        db.bump("submissions", "abc", "2026-09-01")
        assert db.snapshot_now() is True

        files = _snapshots(backend)
        assert len(files) == 1
        assert files[0].id != first_id

    def test_snapshot_without_changes_uploads_nothing(
        self, db: StateDB, backend: InMemoryFilesBackend
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01")
        assert db.snapshot_now() is True
        first_id = _snapshots(backend)[0].id

        assert db.snapshot_now() is False

        files = _snapshots(backend)
        assert len(files) == 1
        assert files[0].id == first_id

    def test_snapshot_of_an_untouched_database_is_a_no_op(
        self, db: StateDB, backend: InMemoryFilesBackend
    ) -> None:
        assert db.snapshot_now() is False
        assert _snapshots(backend) == []

    def test_upload_failure_returns_false_and_keeps_the_data_dirty(
        self, db: StateDB, backend: InMemoryFilesBackend, monkeypatch
    ) -> None:
        def boom(*args, **kwargs):
            raise BackendError("storage is down")

        monkeypatch.setattr(backend, "upload", boom)
        db.bump("submissions", "abc", "2026-09-01")

        assert db.snapshot_now() is False
        assert _snapshots(backend) == []

        # Once Storage recovers the pending write is still snapshotted.
        monkeypatch.undo()
        assert db.snapshot_now() is True
        assert len(_snapshots(backend)) == 1

    def test_oversized_snapshot_is_not_uploaded(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=16,  # smaller than any real SQLite file
        )
        state.start()
        try:
            state.bump("submissions", "abc", "2026-09-01")
            assert state.snapshot_now() is False
            assert _snapshots(backend) == []
        finally:
            state.stop()


class TestRestore:
    def test_round_trip_through_storage(
        self, db: StateDB, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01", by=3)
        db.record_view("abc", "2026-09-01", "page")
        assert db.snapshot_now() is True
        db.stop()

        # A brand-new container: same Storage, empty disk.
        restored = StateDB(
            backend=backend,
            db_path=tmp_path / "fresh" / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        restored.start()
        try:
            assert restored.count("submissions", "abc", "2026-09-01") == 3
            assert restored.views("abc")["total"] == 1
        finally:
            restored.stop()

    def test_missing_snapshot_starts_an_empty_database(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=0,
        )
        state.start()
        try:
            assert state.count("submissions", "abc", "2026-09-01") == 0
        finally:
            state.stop()

    def test_oversized_stored_snapshot_is_skipped(
        self, db: StateDB, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01", by=3)
        assert db.snapshot_now() is True
        db.stop()

        restored = StateDB(
            backend=backend,
            db_path=tmp_path / "fresh" / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=32,  # smaller than the stored snapshot
        )
        restored.start()
        try:
            assert restored.count("submissions", "abc", "2026-09-01") == 0
        finally:
            restored.stop()

    def test_corrupt_snapshot_is_skipped(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        backend.upload(SNAPSHOT_FILE_NAME, b"not a database at all", [TAG_STATE])

        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        try:
            assert state.count("submissions", "abc", "2026-09-01") == 0
            state.bump("submissions", "abc", "2026-09-01")
            assert state.count("submissions", "abc", "2026-09-01") == 1
        finally:
            state.stop()

    def test_storage_failure_on_restore_degrades_to_empty(
        self, backend: InMemoryFilesBackend, tmp_path: Path, monkeypatch
    ) -> None:
        def boom(*args, **kwargs):
            raise BackendError("storage is down")

        monkeypatch.setattr(backend, "search_by_tag", boom)
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        try:
            assert state.bump("submissions", "abc", "2026-09-01") == 1
        finally:
            state.stop()


class TestLifecycle:
    def test_stop_is_idempotent(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        state.bump("submissions", "abc", "2026-09-01")
        state.stop()
        state.stop()  # must not raise
        assert len(_snapshots(backend)) == 1

    def test_stop_snapshots_pending_writes(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        state.record_view("abc", "2026-09-01", "page")
        state.stop()

        assert len(_snapshots(backend)) == 1

    def test_start_twice_keeps_the_data(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        try:
            state.bump("submissions", "abc", "2026-09-01")
            state.start()  # no-op
            assert state.count("submissions", "abc", "2026-09-01") == 1
        finally:
            state.stop()

    def test_use_before_start_raises(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=0,
            max_snapshot_bytes=0,
        )
        with pytest.raises(RuntimeError):
            state.bump("submissions", "abc", "2026-09-01")

    def test_background_thread_snapshots_on_its_own(
        self, backend: InMemoryFilesBackend, tmp_path: Path
    ) -> None:
        state = StateDB(
            backend=backend,
            db_path=tmp_path / "state.sqlite3",
            snapshot_interval_s=1,
            max_snapshot_bytes=10 * 1024 * 1024,
        )
        state.start()
        try:
            state.bump("submissions", "abc", "2026-09-01")
            deadline = time.monotonic() + 5
            while not _snapshots(backend) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(_snapshots(backend)) == 1
        finally:
            state.stop()


class TestSingleInstanceInvariant:
    """This deployment is exactly one container, and the sidecar assumes it.

    Every instance snapshots its *whole* local database and retires every
    other snapshot, so two instances writing at once would each destroy the
    other's counters in turn. That is not handled -- it is ruled out by the
    deployment shape (one Keboola App per organisation, one container). What
    the code can do is notice when the assumption is broken: a snapshot in
    Storage that is newer than the one this instance last wrote, and is not
    the one it just uploaded, was written by somebody else.
    """

    def test_a_foreign_newer_snapshot_is_reported(
        self, db: StateDB, backend: InMemoryFilesBackend, caplog
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01")
        assert db.snapshot_now() is True

        # Another instance writes state between this instance's snapshots.
        backend.upload(SNAPSHOT_FILE_NAME, b"SQLite format 3\0foreign", [TAG_STATE])

        db.bump("submissions", "abc", "2026-09-01")
        with caplog.at_level("ERROR", logger="src.statedb"):
            assert db.snapshot_now() is True

        messages = [rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("another instance" in msg for msg in messages), messages
        assert any("one" in msg and "replica" in msg for msg in messages), messages

    def test_an_ordinary_second_snapshot_reports_nothing(
        self, db: StateDB, backend: InMemoryFilesBackend, caplog
    ) -> None:
        db.bump("submissions", "abc", "2026-09-01")
        assert db.snapshot_now() is True
        db.bump("submissions", "abc", "2026-09-01")
        with caplog.at_level("ERROR", logger="src.statedb"):
            assert db.snapshot_now() is True
        assert not [rec for rec in caplog.records if rec.levelname == "ERROR"]

    def test_the_snapshot_left_by_a_previous_boot_is_not_foreign(
        self, backend: InMemoryFilesBackend, tmp_path: Path, caplog
    ) -> None:
        """A restart restores the last snapshot and then writes past it -- normal."""
        first = StateDB(backend, tmp_path / "a" / "s.sqlite3", 0, 10 * 1024 * 1024)
        first.start()
        first.bump("submissions", "abc", "2026-09-01")
        assert first.snapshot_now() is True
        first.stop()

        second = StateDB(backend, tmp_path / "b" / "s.sqlite3", 0, 10 * 1024 * 1024)
        second.start()
        second.bump("submissions", "abc", "2026-09-01")
        with caplog.at_level("ERROR", logger="src.statedb"):
            assert second.snapshot_now() is True
        second.stop()
        assert not [rec for rec in caplog.records if rec.levelname == "ERROR"]
