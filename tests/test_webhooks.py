"""Tests for outbound webhooks (:mod:`src.webhooks`).

No network: :meth:`WebhookDispatcher._post` and ``_sleep`` are replaced with
recorders, and DNS is injected through ``validate_webhook_url(resolver=...)``.
Deliveries are driven synchronously with ``drain()`` so the whole delivery path
(body shaping, signing, retry, give-up) runs in the test's own thread.
"""

import hashlib
import hmac
import json
import time

import pytest

from src.webhooks import (
    SIGNATURE_HEADER,
    USER_AGENT,
    WebhookDispatcher,
    WebhookEvent,
    sign_body,
    validate_webhook_url,
)

SECRET = "webhook-signing-secret-for-tests"
HOOK_URL = "https://example.com/hooks/artifact"
SLACK_URL = "https://hooks.slack.com/services/T000/B000/xxxx"


class Recorder:
    """Stands in for the HTTP layer: records posts, replays canned statuses."""

    def __init__(self, statuses: list) -> None:
        self.statuses = list(statuses)
        self.posts: list[tuple[str, bytes, dict]] = []

    def __call__(self, url: str, body: bytes, headers: dict) -> int:
        self.posts.append((url, body, dict(headers)))
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_dispatcher(statuses: list, *, max_attempts: int = 3):
    """A dispatcher wired to a Recorder, with sleeping neutered."""
    dispatcher = WebhookDispatcher(
        timeout_s=5, max_attempts=max_attempts, sign_secret=SECRET
    )
    recorder = Recorder(statuses)
    slept: list[float] = []
    dispatcher._post = recorder
    dispatcher._sleep = slept.append
    # Never touch real DNS in tests: default the delivery-time SSRF guard to a
    # public answer. Cases that care about rebinding override this per test.
    dispatcher._resolver = lambda _hostname: ["93.184.216.34"]
    return dispatcher, recorder, slept


def an_event(**overrides) -> WebhookEvent:
    fields = {
        "artifact_id": "abc123",
        "kind": "version.published",
        "payload": {"title": "Quarterly review", "version": 4},
        "created_at": "2026-09-01T09:30:00+00:00",
    }
    fields.update(overrides)
    return WebhookEvent(**fields)


class TestDelivery:
    def test_posts_the_signed_envelope(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([HOOK_URL], an_event())
        assert dispatcher.drain() == 1

        url, body, headers = recorder.posts[0]
        assert url == HOOK_URL
        assert json.loads(body) == {
            "event": "version.published",
            "artifact_id": "abc123",
            "payload": {"title": "Quarterly review", "version": 4},
            "created_at": "2026-09-01T09:30:00+00:00",
        }
        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"] == USER_AGENT

    def test_signature_verifies_against_the_exact_body(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        _, body, headers = recorder.posts[0]
        expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert headers[SIGNATURE_HEADER] == f"sha256={expected}"
        assert sign_body(body, SECRET) == headers[SIGNATURE_HEADER]

    def test_a_different_secret_does_not_verify(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        _, body, headers = recorder.posts[0]
        assert headers[SIGNATURE_HEADER] != sign_body(body, "some other secret")

    def test_one_event_fans_out_to_every_url(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200, 200])

        dispatcher.emit([HOOK_URL, "https://example.org/hook"], an_event())
        assert dispatcher.drain() == 2

        assert [url for url, _, _ in recorder.posts] == [
            HOOK_URL,
            "https://example.org/hook",
        ]

    def test_emit_is_non_blocking_and_queues(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([HOOK_URL], an_event())

        assert recorder.posts == []  # nothing delivered yet
        assert dispatcher.pending() == 1
        dispatcher.drain()
        assert len(recorder.posts) == 1

    def test_blank_urls_are_ignored(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit(["", "   ", None], an_event())  # type: ignore[list-item]

        assert dispatcher.pending() == 0
        assert dispatcher.drain() == 0


class TestSlackCompatibility:
    def test_slack_hosts_get_a_text_body(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([SLACK_URL], an_event(payload={
            "title": "Quarterly review",
            "actor": "petr",
            "url": "https://hub.example.com/a/abc123",
        }))
        dispatcher.drain()

        _, body, headers = recorder.posts[0]
        document = json.loads(body)
        assert list(document) == ["text"]
        assert "New version published" in document["text"]
        assert "Quarterly review" in document["text"]
        assert "petr" in document["text"]
        assert "https://hub.example.com/a/abc123" in document["text"]
        # Still signed, over the Slack-shaped bytes.
        assert headers[SIGNATURE_HEADER] == sign_body(body, SECRET)

    def test_slack_falls_back_to_the_artifact_id(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([SLACK_URL], an_event(payload={}))
        dispatcher.drain()

        assert "abc123" in json.loads(recorder.posts[0][1])["text"]

    def test_unknown_kinds_still_deliver(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit([SLACK_URL], an_event(kind="something.new", payload={}))
        dispatcher.drain()

        assert "something.new" in json.loads(recorder.posts[0][1])["text"]

    def test_non_slack_host_keeps_the_envelope(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])

        dispatcher.emit(["https://hooks.slack.com.evil.example/x"], an_event())
        dispatcher.drain()

        assert "event" in json.loads(recorder.posts[0][1])


class TestRetries:
    def test_retries_a_500_then_succeeds(self) -> None:
        dispatcher, recorder, slept = make_dispatcher([500, 200])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        assert len(recorder.posts) == 2
        assert slept == [2]  # 2**1 seconds before the second attempt

    def test_retries_a_transport_error(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([RuntimeError("connreset"), 200])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        assert len(recorder.posts) == 2

    def test_gives_up_after_max_attempts(self) -> None:
        dispatcher, recorder, slept = make_dispatcher([500, 500, 500, 200])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        assert len(recorder.posts) == 3  # max_attempts, the 200 is never reached
        assert slept == [2, 4]

    def test_backoff_is_capped(self) -> None:
        dispatcher, recorder, slept = make_dispatcher([500] * 10, max_attempts=10)

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        assert len(recorder.posts) == 10
        assert max(slept) == 60

    def test_a_failing_url_never_raises_into_the_caller(self) -> None:
        dispatcher, _, _ = make_dispatcher([RuntimeError("boom")] * 3)

        dispatcher.emit([HOOK_URL], an_event())
        assert dispatcher.drain() == 1  # no exception escaped

    def test_non_2xx_that_is_not_retryable_still_stops_at_max_attempts(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([404, 404, 404])

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        assert len(recorder.posts) == 3


class TestValidateWebhookUrl:
    @staticmethod
    def public(_hostname: str) -> list[str]:
        return ["93.184.216.34"]

    @staticmethod
    def private(_hostname: str) -> list[str]:
        return ["10.0.0.5"]

    def test_accepts_a_public_https_url(self) -> None:
        assert (
            validate_webhook_url(f"  {HOOK_URL}  ", resolver=self.public) == HOOK_URL
        )

    def test_rejects_http(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("http://example.com/hook", resolver=self.public)

    def test_rejects_an_empty_url(self) -> None:
        with pytest.raises(ValueError):
            validate_webhook_url("   ", resolver=self.public)

    def test_rejects_embedded_credentials(self) -> None:
        with pytest.raises(ValueError, match="credentials"):
            validate_webhook_url(
                "https://user:pass@example.com/hook", resolver=self.public
            )

    def test_rejects_a_host_resolving_to_a_private_address(self) -> None:
        with pytest.raises(ValueError, match="private"):
            validate_webhook_url("https://internal.example.com/h", resolver=self.private)

    def test_rejects_a_private_literal_ip_without_touching_dns(self) -> None:
        def never(_hostname: str) -> list[str]:
            raise AssertionError("a literal IP must not be resolved")

        with pytest.raises(ValueError, match="private"):
            validate_webhook_url("https://192.168.1.10/hook", resolver=never)

    def test_rejects_the_cloud_metadata_address(self) -> None:
        with pytest.raises(ValueError, match="private"):
            validate_webhook_url(
                "https://169.254.169.254/latest/meta-data", resolver=self.public
            )

    def test_rejects_the_metadata_hostname(self) -> None:
        with pytest.raises(ValueError, match="internal"):
            validate_webhook_url(
                "https://metadata.google.internal/x", resolver=self.public
            )

    def test_rejects_localhost(self) -> None:
        with pytest.raises(ValueError, match="internal"):
            validate_webhook_url("https://localhost/hook", resolver=self.public)

    def test_rejects_an_unresolvable_host(self) -> None:
        def unresolvable(_hostname: str) -> list[str]:
            raise OSError("NXDOMAIN")

        with pytest.raises(ValueError, match="resolve"):
            validate_webhook_url("https://nope.example.com/h", resolver=unresolvable)

    def test_rejects_a_host_with_no_addresses(self) -> None:
        with pytest.raises(ValueError, match="resolve"):
            validate_webhook_url(
                "https://nope.example.com/h", resolver=lambda _host: []
            )

    def test_rejects_a_url_without_a_hostname(self) -> None:
        with pytest.raises(ValueError):
            validate_webhook_url("https:///hook", resolver=self.public)


class TestDeliveryTimeSsrfGuard:
    """A host that rebinds to a private address between registration and
    delivery must be caught at delivery, not only at registration."""

    @staticmethod
    def public(_hostname: str) -> list[str]:
        return ["93.184.216.34"]

    @staticmethod
    def private(_hostname: str) -> list[str]:
        return ["10.0.0.5"]

    def test_rebinding_to_a_private_ip_drops_the_delivery(self) -> None:
        # Public at registration...
        assert validate_webhook_url(HOOK_URL, resolver=self.public) == HOOK_URL

        dispatcher, recorder, _ = make_dispatcher([200])
        # ...private at delivery time.
        dispatcher._resolver = self.private

        dispatcher.emit([HOOK_URL], an_event())
        # The item is processed (counted) but never actually POSTed.
        assert dispatcher.drain() == 1
        assert recorder.posts == []

    def test_public_at_delivery_still_posts(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])
        dispatcher._resolver = self.public

        dispatcher.emit([HOOK_URL], an_event())
        assert dispatcher.drain() == 1
        assert len(recorder.posts) == 1

    def test_rebinding_is_re_checked_on_every_retry(self) -> None:
        # Public on the first attempt, private on the retry: the retry must be
        # dropped even though the first POST already went out.
        answers = iter([self.public("h"), self.private("h")])
        dispatcher, recorder, _ = make_dispatcher([500, 200])
        dispatcher._resolver = lambda _h: next(answers)

        dispatcher.emit([HOOK_URL], an_event())
        dispatcher.drain()

        # First attempt posted (got 500), second attempt blocked before posting.
        assert len(recorder.posts) == 1

    def test_metadata_hostname_is_dropped_at_delivery(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])
        dispatcher._resolver = self.public  # would be public, but host is blocked

        dispatcher.emit(["https://metadata.google.internal/x"], an_event())
        dispatcher.drain()

        assert recorder.posts == []

    def test_http_post_disables_redirects(self, monkeypatch) -> None:
        captured: dict = {}

        class _FakeResponse:
            status_code = 204

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

            def post(self, url, content, headers):
                return _FakeResponse()

        import src.webhooks as webhooks_module

        monkeypatch.setattr(webhooks_module.httpx, "Client", _FakeClient)

        dispatcher = WebhookDispatcher(
            timeout_s=5, max_attempts=1, sign_secret=SECRET
        )
        status = dispatcher._http_post(HOOK_URL, b"{}", {})

        assert status == 204
        assert captured.get("follow_redirects") is False


class TestLifecycle:
    def test_start_and_stop_deliver_in_the_background(self) -> None:
        dispatcher, recorder, _ = make_dispatcher([200])
        dispatcher.start()
        dispatcher.start()  # second start is a no-op
        try:
            dispatcher.emit([HOOK_URL], an_event())
            deadline = 0
            while not recorder.posts and deadline < 100:
                deadline += 1
                time.sleep(0.02)
            assert len(recorder.posts) == 1
        finally:
            dispatcher.stop()
            dispatcher.stop()  # idempotent

    def test_stop_without_start_is_a_no_op(self) -> None:
        dispatcher, _, _ = make_dispatcher([200])
        dispatcher.stop()
