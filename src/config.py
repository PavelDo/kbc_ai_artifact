"""Application settings.

All configuration comes from environment variables. Required variables have no
defaults — the app fails fast at startup with a clear error instead of
inventing values. Optional limits have documented defaults overridable via env.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_ENV = ["HUB_STORAGE_TOKEN", "HUB_STACK_URL", "HUB_SECRET_KEY"]

#: Shortest accepted ``HUB_SECRET_KEY``. That value is the HMAC key behind
#: every unlock cookie, so a short (therefore low-entropy) one is brute-forcible
#: offline and would let anybody mint their own unlock cookies. Rejected at
#: startup rather than silently accepted.
MIN_SECRET_KEY_CHARS = 32


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Host project access (where serving envelopes live)
    hub_storage_token: str
    hub_stack_url: str
    # Signs unlock cookies
    secret_key: str
    # Optional absolute base URL used in returned artifact URLs
    public_base_url: str | None = None
    # Optional showcase artifact linked from the landing page; absent = no link.
    demo_url: str | None = None
    cache_dir: Path = field(default_factory=lambda: Path("/tmp/artifact-cache"))

    # Limits (bytes / seconds / counts)
    max_html_bytes: int = 15 * 1024 * 1024
    max_inline_image_bytes: int = 5 * 1024 * 1024
    max_inline_total_bytes: int = 15 * 1024 * 1024
    git_clone_timeout_s: int = 90
    git_max_repo_bytes: int = 200 * 1024 * 1024
    # SSRF guard: reject git_url hosts that resolve to private/loopback/
    # link-local/reserved addresses (incl. cloud metadata endpoints). Set
    # HUB_GIT_ALLOW_PRIVATE_HOSTS=1 only for trusted self-hosting.
    git_allow_private_hosts: bool = False
    cache_max_entries: int = 200
    unlock_cookie_max_age_s: int = 12 * 3600
    token_verify_timeout_s: int = 15
    # Community versioning (phase 2)
    # Live versions kept per artifact; older non-head, non-pinned ones are pruned.
    max_versions: int = 50
    # Per-contributor cap on submitted versions per rolling day.
    max_versions_per_day: int = 20
    # Largest per-side payload the diff renderer will process.
    diff_max_bytes: int = 2 * 1024 * 1024
    # Project brain (phase 3)
    # Per-contributor cap on inline comments (threads + replies) per rolling day.
    max_comments_per_day: int = 100
    # Extra stack URLs (comma-separated) beyond the *.keboola.com rule
    extra_stacks: tuple[str, ...] = ()
    # Largest persisted envelope/meta record (bytes) the store will download or
    # read from disk cache before serving. Records above this are skipped as a
    # denial-of-service guard (env HUB_MAX_ENVELOPE_BYTES). 0 disables the bound.
    max_envelope_bytes: int = 20 * 1024 * 1024
    # A meta record with no version file is a publish that died between its
    # two writes. Hydrate deletes such records once they are older than this
    # (HUB_REAP_ABORTED_PUBLISH_AFTER_S), which is what makes them safely
    # distinguishable from a publish still in flight; 0 disables reaping.
    reap_aborted_publish_after_s: int = 3600
    # Per-artifact cap on retained "proposed" versions; the oldest proposals
    # above this are pruned (env HUB_MAX_PROPOSED_VERSIONS). Proposals are never
    # served as head, so pruning the oldest is always safe.
    max_proposed_versions: int = 50
    # Trust X-Forwarded-Host / X-Forwarded-Proto to name the public origin.
    # Off by default: a direct client can forge those headers, and the
    # deployed hub always sets HUB_PUBLIC_BASE_URL (which wins outright)
    # anyway. Local development behind a proxy opts in with
    # HUB_TRUST_FORWARDED_HEADERS=1.
    trust_forwarded_headers: bool = False
    # Failed unlock attempts allowed per (artifact, client IP) per UTC hour
    # before the password gate answers 429 (env
    # HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR). Each attempt costs a full PBKDF2
    # verification, so this bounds both brute force and the CPU it can burn.
    max_unlock_attempts_per_hour: int = 30
    # Operational state sidecar (phase 4, src/statedb.py)
    # Seconds between SQLite snapshots into the host project's Storage Files
    # (env HUB_STATE_SNAPSHOT_INTERVAL_S). 0 disables the background thread and
    # leaves snapshots to explicit calls — what the tests use.
    state_snapshot_interval_s: int = 300
    # Largest state snapshot the hub will upload or restore
    # (env HUB_STATE_MAX_SNAPSHOT_BYTES). 0 disables the bound. An oversized
    # snapshot is skipped with a warning rather than restored.
    state_max_snapshot_bytes: int = 50 * 1024 * 1024
    # File name of the SQLite sidecar; main.py joins it with cache_dir, which is
    # the only writable (and ephemeral) path the container has.
    state_db_filename: str = "state.sqlite3"
    # Outbound webhooks (phase 4, src/webhooks.py)
    # Per-request HTTP timeout for a webhook delivery (HUB_WEBHOOK_TIMEOUT_S).
    webhook_timeout_s: int = 10
    # Total POST attempts per (url, event) before giving up
    # (HUB_WEBHOOK_MAX_ATTEMPTS); retries back off 2**n seconds, capped at 60.
    webhook_max_attempts: int = 3
    # How many webhook URLs one artifact may register
    # (HUB_MAX_WEBHOOKS_PER_ARTIFACT).
    max_webhooks_per_artifact: int = 5
    # Ceiling on queued-but-undelivered webhook deliveries
    # (HUB_WEBHOOK_QUEUE_MAX). One thread consumes them and sleeps through
    # retry backoff, so a stalled receiver would otherwise let the queue grow
    # without limit; past the ceiling the newest delivery is dropped with a
    # log line rather than blocking the request that produced it.
    webhook_queue_max: int = 1000
    # Guest invitations (0.7.0)
    # How many invitations one artifact may hold at once
    # (HUB_MAX_INVITATIONS_PER_ARTIFACT). Each entry is a named capability
    # stored in the artifact's meta record, so this bounds both the meta file
    # and the number of people who can comment without a Keboola account.
    max_invitations_per_artifact: int = 20
    # Vault exports (REL-100-002)
    # Ceiling on the source material one GET /a/{id}/export/vault may render,
    # and on the archive it may write (HUB_EXPORT_MAX_BYTES). 0 disables the
    # bound. Derived from the two limits that decide the worst case: an
    # artifact may keep HUB_MAX_VERSIONS (50) records of up to
    # HUB_MAX_ENVELOPE_BYTES (20 MiB) each, so an unbounded export is a ~1 GiB
    # request anybody holding the capability URL can repeat. The default is
    # three envelopes' worth -- comfortably above any real document's whole
    # history, far below what a container with one process can absorb.
    export_max_bytes: int = 64 * 1024 * 1024
    # Vault builds allowed per (artifact, client address) per UTC hour before
    # the export answers 429 (HUB_MAX_EXPORTS_PER_HOUR). Building a vault
    # diffs every version and converts every HTML document, so this bounds how
    # often one capability-URL holder can ask for that work.
    max_exports_per_hour: int = 20


def load_settings() -> Settings:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    secret_key = os.environ["HUB_SECRET_KEY"]
    if len(secret_key) < MIN_SECRET_KEY_CHARS:
        raise RuntimeError(
            f"HUB_SECRET_KEY is too short ({len(secret_key)} characters). It "
            "is the HMAC key for every unlock cookie and must be at least "
            f"{MIN_SECRET_KEY_CHARS} characters of high-entropy random data. "
            "Generate one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(48))'"
        )
    extra = tuple(
        s.strip().rstrip("/")
        for s in os.environ.get("HUB_EXTRA_STACKS", "").split(",")
        if s.strip()
    )
    return Settings(
        hub_storage_token=os.environ["HUB_STORAGE_TOKEN"],
        hub_stack_url=os.environ["HUB_STACK_URL"].rstrip("/"),
        secret_key=os.environ["HUB_SECRET_KEY"],
        public_base_url=os.environ.get("HUB_PUBLIC_BASE_URL", "").rstrip("/") or None,
        demo_url=os.environ.get("HUB_DEMO_URL", "").strip() or None,
        cache_dir=Path(os.environ.get("HUB_CACHE_DIR", "/tmp/artifact-cache")),
        max_html_bytes=_int_env("HUB_MAX_HTML_BYTES", 15 * 1024 * 1024),
        max_inline_image_bytes=_int_env("HUB_MAX_INLINE_IMAGE_BYTES", 5 * 1024 * 1024),
        max_inline_total_bytes=_int_env("HUB_MAX_INLINE_TOTAL_BYTES", 15 * 1024 * 1024),
        git_clone_timeout_s=_int_env("HUB_GIT_CLONE_TIMEOUT_S", 90),
        git_max_repo_bytes=_int_env("HUB_GIT_MAX_REPO_BYTES", 200 * 1024 * 1024),
        git_allow_private_hosts=_bool_env("HUB_GIT_ALLOW_PRIVATE_HOSTS", False),
        cache_max_entries=_int_env("HUB_CACHE_MAX_ENTRIES", 200),
        unlock_cookie_max_age_s=_int_env("HUB_UNLOCK_COOKIE_MAX_AGE_S", 12 * 3600),
        token_verify_timeout_s=_int_env("HUB_TOKEN_VERIFY_TIMEOUT_S", 15),
        max_versions=_int_env("HUB_MAX_VERSIONS", 50),
        max_versions_per_day=_int_env("HUB_MAX_VERSIONS_PER_DAY", 20),
        diff_max_bytes=_int_env("HUB_DIFF_MAX_BYTES", 2 * 1024 * 1024),
        max_comments_per_day=_int_env("HUB_MAX_COMMENTS_PER_DAY", 100),
        extra_stacks=extra,
        max_envelope_bytes=_int_env("HUB_MAX_ENVELOPE_BYTES", 20 * 1024 * 1024),
        reap_aborted_publish_after_s=_int_env("HUB_REAP_ABORTED_PUBLISH_AFTER_S", 3600),
        max_proposed_versions=_int_env("HUB_MAX_PROPOSED_VERSIONS", 50),
        trust_forwarded_headers=_bool_env("HUB_TRUST_FORWARDED_HEADERS", False),
        max_unlock_attempts_per_hour=_int_env(
            "HUB_MAX_UNLOCK_ATTEMPTS_PER_HOUR", 30
        ),
        state_snapshot_interval_s=_int_env("HUB_STATE_SNAPSHOT_INTERVAL_S", 300),
        state_max_snapshot_bytes=_int_env(
            "HUB_STATE_MAX_SNAPSHOT_BYTES", 50 * 1024 * 1024
        ),
        state_db_filename=(
            os.environ.get("HUB_STATE_DB_FILENAME", "").strip() or "state.sqlite3"
        ),
        webhook_timeout_s=_int_env("HUB_WEBHOOK_TIMEOUT_S", 10),
        webhook_max_attempts=_int_env("HUB_WEBHOOK_MAX_ATTEMPTS", 3),
        max_webhooks_per_artifact=_int_env("HUB_MAX_WEBHOOKS_PER_ARTIFACT", 5),
        webhook_queue_max=_int_env("HUB_WEBHOOK_QUEUE_MAX", 1000),
        max_invitations_per_artifact=_int_env(
            "HUB_MAX_INVITATIONS_PER_ARTIFACT", 20
        ),
        export_max_bytes=_int_env("HUB_EXPORT_MAX_BYTES", 64 * 1024 * 1024),
        max_exports_per_hour=_int_env("HUB_MAX_EXPORTS_PER_HOUR", 20),
    )
