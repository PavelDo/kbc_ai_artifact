"""Outbound webhooks: push notifications for artifact events.

An artifact can register a handful of https URLs; whenever something happens to
it (a version is published or proposed, a comment lands, it gets finalized) the
hub POSTs a small JSON envelope to each of them::

    {"event": "version.published", "event_id": "...", "delivery_id": "...",
     "artifact_id": "...", "payload": {...},
     "created_at": "2026-09-01T09:30:00+00:00"}

signed with ``X-Hub-Signature-256: sha256=<hmac hex>`` over the exact bytes sent,
so a receiver can tell a genuine delivery from a forged one. Every delivery also
carries ``X-Hub-Event-Id`` and ``X-Hub-Delivery-Id``: the event id is the same
across every receiver and every retry, the delivery id is the same across
retries to one receiver, so a receiver can recognise a retry as the same work
rather than doing it twice. URLs whose host ends
in ``hooks.slack.com`` get Slack's ``{"text": ...}`` shape instead, because
posting the envelope there renders as an empty message.

Two deliberate limits, both documented rather than engineered around:

* **The queue is in memory and bounded.** Push is best effort — a restart drops
  whatever was pending, and so does a queue that has reached ``queue_max``. The
  Storage Files record of what happened is the durable part; the notification is
  a convenience on top of it.
* **Delivery is one background thread.** Retries sleep in that thread, so a slow
  or failing receiver delays the others. Fine at this scale, and it keeps a
  failing webhook from spawning unbounded work.

The SSRF guard from :mod:`src.builder` is reused verbatim (same private/
loopback/link-local/reserved/metadata ranges) so a webhook URL cannot be pointed
at the cluster's internals or a cloud metadata endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import httpx

from src.builder import _BLOCKED_HOSTNAMES, _ip_is_blocked, _resolve_host_ips

logger = logging.getLogger(__name__)

__all__ = [
    "DELIVERY_ID_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_KINDS",
    "SIGNATURE_HEADER",
    "USER_AGENT",
    "WebhookDispatcher",
    "WebhookEvent",
    "validate_webhook_url",
]

#: Ceiling on queued deliveries when the caller does not configure one
#: (``HUB_WEBHOOK_QUEUE_MAX``). Generous next to the per-artifact webhook cap,
#: so only a genuinely stalled receiver ever reaches it.
DEFAULT_QUEUE_MAX = 1000

#: Headers letting a receiver deduplicate: the event is the same across every
#: receiver and retry, the delivery is the same across retries to one receiver.
EVENT_ID_HEADER = "X-Hub-Event-Id"
DELIVERY_ID_HEADER = "X-Hub-Delivery-Id"

#: Header carrying the HMAC-SHA256 of the delivered body, GitHub-style
#: (``sha256=<hex digest>``).
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: Sent on every delivery so receivers can recognise us in their logs.
USER_AGENT = "kbc-artifact-hub"

#: Longest backoff between retries, in seconds.
MAX_BACKOFF_S = 60

#: Host suffix that switches the body to Slack's incoming-webhook shape.
_SLACK_HOST_SUFFIX = "hooks.slack.com"

#: Event kinds the hub emits today, mapped to the human phrasing used in the
#: Slack one-liner. Unknown kinds are still delivered — the kind string itself is
#: then used as the label — so adding an event needs no change here.
EVENT_KINDS: dict[str, str] = {
    "version.published": "New version published",
    "version.proposed": "New version proposed",
    "version.promoted": "Proposed version promoted",
    "comment.created": "New comment",
    "comment.replied": "New reply on a comment",
    "artifact.finalized": "Artifact finalized",
    "artifact.deleted": "Artifact deleted",
    "artifact.trashed": "Artifact moved to the trash",
    "artifact.restored": "Artifact restored from the trash",
    "link.rotated": "Public link rotated",
}


@dataclass(frozen=True)
class WebhookEvent:
    """One thing that happened to one artifact.

    ``payload`` must be JSON-safe and must never carry a token, a password hash
    or anything else secret: it is sent to a third-party URL in the clear
    (TLS-protected, but the receiver sees all of it).
    """

    artifact_id: str
    kind: str
    payload: dict = field(default_factory=dict)
    created_at: str = ""
    #: Identifies *what happened*, once, across every receiver and retry.
    #: Generated here when the caller does not supply one, so no producer has
    #: to remember to.
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            # frozen dataclass: this is the sanctioned way to fill a default
            # that has to be computed.
            object.__setattr__(self, "event_id", secrets.token_hex(16))

    def delivery_id_for(self, url: str) -> str:
        """Identifies *this event going to this receiver*, stably across retries.

        Derived rather than random so a retry cannot invent a new one: the
        same (event, url) always yields the same value, while two receivers
        of the same event get different ones, so neither can guess or replay
        the other's. The URL is hashed rather than sent, so the id never
        discloses one receiver's endpoint to another.
        """
        digest = hashlib.sha256(f"{self.event_id}\x00{url}".encode("utf-8"))
        return digest.hexdigest()[:32]

    def envelope(self, url: str) -> dict:
        """The generic JSON body posted to non-Slack receivers."""
        return {
            "event": self.kind,
            "event_id": self.event_id,
            "delivery_id": self.delivery_id_for(url),
            "artifact_id": self.artifact_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


def validate_webhook_url(
    url: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> str:
    """Return ``url`` normalized, or raise ``ValueError`` explaining the refusal.

    Accepts https URLs only, and refuses hosts that are internal names or
    resolve to a private, loopback, link-local, reserved, multicast or
    metadata address — the same ranges :func:`src.builder._check_git_host`
    blocks for git clones, via the same helpers. ``resolver`` exists so tests can
    inject an answer instead of doing real DNS.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("A webhook URL is required.")
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise ValueError(
            "Webhook URLs must use https, so the signed payload is not sent in "
            "the clear."
        )
    if parsed.username or parsed.password:
        raise ValueError(
            "Webhook URLs must not embed credentials; put the secret in the "
            "path or let the signature header authenticate the delivery."
        )
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise ValueError("Webhook URLs must have a hostname.")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".internal"):
        raise ValueError(
            "That webhook host is not permitted: internal and metadata "
            "hostnames are blocked."
        )
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_literal_ip = False
    else:
        is_literal_ip = True
    if is_literal_ip:
        # A numeric host needs no DNS; check the literal itself, exactly like
        # builder._check_git_host does for git URLs.
        candidates = [hostname]
    else:
        resolve = resolver or _resolve_host_ips
        try:
            candidates = resolve(hostname)
        except OSError as exc:
            raise ValueError(
                f"Could not resolve the webhook host {hostname!r}."
            ) from exc
        if not candidates:
            raise ValueError(f"Could not resolve the webhook host {hostname!r}.")
    for ip in candidates:
        if _ip_is_blocked(ip):
            raise ValueError(
                "That webhook URL resolves to a private, loopback, link-local "
                "or reserved address, which is not allowed."
            )
    return candidate


def _is_slack(url: str) -> bool:
    """True when ``url`` is a Slack incoming webhook."""
    host = (urlparse(url).hostname or "").strip().rstrip(".").lower()
    return host == _SLACK_HOST_SUFFIX or host.endswith(f".{_SLACK_HOST_SUFFIX}")


def _slack_text(event: WebhookEvent) -> str:
    """One human-readable line describing ``event``, for Slack."""
    label = EVENT_KINDS.get(event.kind, event.kind)
    payload = event.payload or {}
    subject = str(payload.get("title") or "").strip() or event.artifact_id
    line = f"{label}: {subject}"
    actor = str(payload.get("actor") or "").strip()
    if actor:
        line = f"{line} by {actor}"
    link = str(payload.get("url") or "").strip()
    if link:
        line = f"{line} — {link}"
    return line


def _body_for(url: str, event: WebhookEvent) -> bytes:
    """Serialize the body this receiver expects."""
    if _is_slack(url):
        document: dict = {"text": _slack_text(event)}
    else:
        document = event.envelope(url)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def sign_body(body: bytes, secret: str) -> str:
    """Value for :data:`SIGNATURE_HEADER` over ``body``.

    Exposed so a receiver implementation (and the tests) can recompute it
    exactly the way deliveries produce it.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookDispatcher:
    """Queues events and delivers them on a background daemon thread.

    :param timeout_s: per-request HTTP timeout.
    :param max_attempts: total POST attempts per (url, event) before giving up.
    :param sign_secret: HMAC key for :data:`SIGNATURE_HEADER`.
    :param queue_max: ceiling on queued deliveries; see :meth:`emit`.
    """

    def __init__(
        self,
        timeout_s: int,
        max_attempts: int,
        sign_secret: str,
        queue_max: int = DEFAULT_QUEUE_MAX,
    ) -> None:
        self._timeout_s = max(1, int(timeout_s))
        self._max_attempts = max(1, int(max_attempts))
        self._secret = sign_secret
        # Bounded on purpose. Deliveries are produced in the request path and
        # consumed by a single thread that sleeps through retry backoff, so a
        # stalled receiver lets production outrun consumption indefinitely --
        # an unbounded queue turns that into unbounded memory in a container
        # with a fixed limit. The bound makes the failure a bounded, logged
        # loss of best-effort notifications instead.
        self._queue: queue.Queue[tuple[str, WebhookEvent] | None] = queue.Queue(
            maxsize=max(1, int(queue_max))
        )
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        #: Injection points for tests: no real sleeping, no real HTTP, no real DNS.
        self._sleep: Callable[[float], None] = time.sleep
        self._post: Callable[[str, bytes, dict], int] = self._http_post
        self._resolver: Callable[[str], list[str]] = _resolve_host_ips

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the delivery thread. A second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        thread = threading.Thread(
            target=self._run, name="webhook-dispatcher", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop the delivery thread. Idempotent, and never raises."""
        self._stopping.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    # producing
    # ------------------------------------------------------------------ #

    def emit(self, urls: list[str], event: WebhookEvent) -> None:
        """Enqueue one delivery per URL. Returns immediately.

        Never raises into the request path, and never blocks it: an unusable
        URL is dropped with a log line rather than failing the publish that
        triggered it, and so is a delivery that arrives when the queue is
        already at ``queue_max``. Dropping the newest is the deliberate
        choice -- the alternative, blocking on ``put``, would make a stalled
        receiver slow down publishing itself, which is the one thing push
        notifications must never do.
        """
        for url in urls or []:
            if not isinstance(url, str) or not url.strip():
                continue
            try:
                self._queue.put_nowait((url.strip(), event))
            except queue.Full:
                logger.warning(
                    "Dropping webhook delivery of %s for artifact %s to %s: "
                    "queue is full (%d pending)",
                    event.kind,
                    event.artifact_id,
                    _safe_url(url.strip()),
                    self._queue.maxsize,
                )

    def pending(self) -> int:
        """Number of deliveries still queued (diagnostics and tests)."""
        return self._queue.qsize()

    # ------------------------------------------------------------------ #
    # consuming
    # ------------------------------------------------------------------ #

    def drain(self) -> int:
        """Deliver everything queued, synchronously, and return how many were tried.

        The delivery thread uses exactly this code path, one item at a time.
        Tests call it directly so the whole module can be exercised without
        starting a thread.
        """
        delivered = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return delivered
            if item is None:
                continue
            url, event = item
            self._deliver(url, event)
            delivered += 1

    def _run(self) -> None:
        """Delivery loop; ``None`` is the shutdown sentinel."""
        while not self._stopping.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            url, event = item
            try:
                self._deliver(url, event)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.warning("Webhook delivery raised unexpectedly: %s", exc)

    def _deliver(self, url: str, event: WebhookEvent) -> bool:
        """POST one event to one URL, retrying with capped backoff.

        Returns True on a 2xx. Every failure is logged and swallowed — nothing
        here may propagate into the caller or the loop.
        """
        body = _body_for(url, event)
        # Built once, outside the retry loop, which is what makes the delivery
        # id stable across attempts. Sent as headers as well as in the body so
        # a receiver can dedupe without parsing -- and so Slack deliveries,
        # whose body is a different shape entirely, carry them too.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            SIGNATURE_HEADER: sign_body(body, self._secret),
            EVENT_ID_HEADER: event.event_id,
            DELIVERY_ID_HEADER: event.delivery_id_for(url),
        }
        for attempt in range(self._max_attempts):
            if attempt:
                delay = min(2**attempt, MAX_BACKOFF_S)
                self._sleep(delay)
                if self._stopping.is_set():
                    logger.info(
                        "Giving up on webhook %s for %s: shutting down",
                        _safe_url(url),
                        event.kind,
                    )
                    return False
            # Re-resolve and re-check the destination immediately before *every*
            # attempt: the host was validated at registration, but DNS can
            # rebind to an internal/metadata address between then and delivery
            # (and between retries). Residual TOCTOU: httpx re-resolves when it
            # opens the connection a moment later, and the installed sync client
            # offers no SNI-preserving IP pin, so this narrows the window rather
            # than closing it. A blocked destination drops the delivery — never
            # raised into the caller.
            if not self._destination_allowed(url):
                logger.warning(
                    "Dropping webhook delivery of %s for artifact %s to %s: host "
                    "resolves to a private, loopback, link-local, reserved or "
                    "metadata address",
                    event.kind,
                    event.artifact_id,
                    _safe_url(url),
                )
                return False
            try:
                status = self._post(url, body, headers)
            except Exception as exc:  # noqa: BLE001 - network errors are normal
                logger.warning(
                    "Webhook POST to %s failed (attempt %d/%d): %s",
                    _safe_url(url),
                    attempt + 1,
                    self._max_attempts,
                    exc,
                )
                continue
            if 200 <= status < 300:
                return True
            logger.warning(
                "Webhook POST to %s returned %d (attempt %d/%d)",
                _safe_url(url),
                status,
                attempt + 1,
                self._max_attempts,
            )
        logger.warning(
            "Gave up delivering %s for artifact %s to %s after %d attempts",
            event.kind,
            event.artifact_id,
            _safe_url(url),
            self._max_attempts,
        )
        return False

    def _destination_allowed(self, url: str) -> bool:
        """False when ``url``'s host positively resolves to a blocked address.

        Mirrors :func:`validate_webhook_url`'s checks, but runs at delivery time
        with the dispatcher's (injectable) resolver so a rebind after
        registration is caught. Never raises.

        Fails closed. A blocked hostname (metadata/internal names), a
        name/literal resolving to a private, loopback, link-local, reserved or
        metadata address, *and* a host that cannot be resolved right now
        (``OSError`` or an empty answer) all return ``False`` and drop the
        delivery.

        Allowing an unresolvable host through used to look safe -- a name that
        does not resolve cannot be connected to, so the POST would just fail.
        That reasoning does not hold, because this lookup and the one httpx
        performs when it opens the connection are two separate queries. A DNS
        server the attacker controls can answer this probe with SERVFAIL and
        then hand httpx a loopback address a moment later; fail-open would let
        that through without the attacker even having to win a race against a
        public answer. Refusing to deliver what we cannot positively clear
        costs only an attempt, and the caller retries with backoff, so a
        genuine resolver blip behaves like any other transient failure.
        """
        hostname = (urlparse(url).hostname or "").strip().rstrip(".").lower()
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".internal"):
            return False
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            try:
                candidates = self._resolver(hostname)
            except OSError:
                return False
        else:
            candidates = [hostname]
        if not candidates:
            return False
        return not any(_ip_is_blocked(ip) for ip in candidates)

    def _http_post(self, url: str, body: bytes, headers: dict) -> int:
        """The real POST. Replaced wholesale in tests.

        ``follow_redirects=False`` so a 3xx cannot bounce the signed payload to
        an unvalidated (possibly internal) host — the redirect target would skip
        the delivery-time SSRF re-check entirely.
        """
        with httpx.Client(timeout=self._timeout_s, follow_redirects=False) as client:
            response = client.post(url, content=body, headers=headers)
        return int(response.status_code)


def _safe_url(url: str) -> str:
    """Scheme + host + a truncated path, for log lines.

    Webhook URLs are themselves capability tokens (a Slack hook's path is its
    only credential), so the full path never reaches a log.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    return f"{parsed.scheme}://{host}/…"
