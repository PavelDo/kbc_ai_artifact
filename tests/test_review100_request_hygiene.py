"""Inbound request hygiene: SEC-100-002, SEC-100-003 and SEC-100-004.

Regression tests for the three findings of the v0.10.0 security review that
concern what an *unauthenticated* caller can make this service do before it
has decided whether to talk to them at all:

* ``SEC-100-003`` — the password/guest brute-force budget was keyed on
  ``X-Real-IP``, a header the caller writes, so changing it handed the caller
  a fresh budget. (The forwarded-address half of the same finding — walking
  ``X-Forwarded-For`` from the right, and validating every candidate as an
  address — has its own module, ``test_review100_forwarded_ip.py``.)
* ``SEC-100-004`` — nothing bounded an inbound body before it was read,
  JSON-decoded and validated, so a three-megabyte comment on a nonexistent
  artifact was fully parsed on the way to its 404.
* ``SEC-100-002`` — the per-artifact lock registry took its key straight from
  the URL and kept every entry forever, so made-up ids grew it without bound.

The probes that demonstrated all three live in the review directory
(``_review/v0.10.0/runtime/test_lock_registry_probe.py``); each is ported here
with its assertion turned around to the fixed behaviour.

The ``api`` fixture and its helpers come from ``tests.test_api`` so this
module stays a pure addition — no live Storage, no live stack, no DNS.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import threading
from contextlib import contextmanager

import pytest

import src.main as main
from src.comments import MAX_BODY_CHARS
from src.config import REQUEST_ENVELOPE_SLACK_BYTES, load_settings
from tests.test_api import (
    AUTH_HEADERS,
    Api,
    _comment,
    _in_thread,
    _pause_inside,
    _publish_markdown,
    api,  # noqa: F401 - the fixture this module runs on
)

#: A documentation-range address (RFC 5737) standing in for a real proxy.
PROXY_PEER = "198.51.100.10"
#: The network the proxy above sits in, as an operator would configure it.
PROXY_CIDR = "198.51.100.0/24"
#: A peer that is *not* a trusted proxy, however convincing its headers are.
STRANGER_PEER = "203.0.113.7"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@contextmanager
def peer(api: Api, host: str):
    """Run the block with the TestClient's transport reporting ``host``.

    ``request.client.host`` is the one address in a request nobody can forge,
    and it is what SEC-100-003 falls back to. The TestClient normally reports
    the literal string ``"testclient"``, which is not an address at all, so a
    test that wants to be inside (or outside) a trusted network has to say so.
    """
    transport = api.client._transport
    original = transport.client
    transport.client = (host, 40000)
    try:
        yield
    finally:
        transport.client = original


def _tune(api: Api, monkeypatch, **fields) -> None:
    """Swap ``main.settings`` for the test's own, keeping the fixture's wiring."""
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(api.settings, **fields)
    )


def _wrong_password(api: Api, artifact_id: str, real_ip: str | None = None):
    """One failed password attempt on ``/a/{id}/raw``."""
    headers = {"X-Artifact-Password": "wrong"}
    if real_ip is not None:
        headers["X-Real-IP"] = real_ip
    return api.client.get(f"/a/{artifact_id}/raw", headers=headers)


class LoudStore:
    """Stand-in artifact store that fails the test if it is touched at all.

    SEC-100-004's whole point is that an oversized body is refused *before*
    anything looks the target up, so "was the store consulted" is the
    assertion, not a proxy for it.
    """

    def __getattr__(self, name: str):
        raise AssertionError(
            f"the artifact store was consulted ({name}) for a request that "
            "should have been refused on size alone"
        )


# --------------------------------------------------------------------------
# SEC-100-003 — spoofable rate-limit identity
# --------------------------------------------------------------------------


def test_x_real_ip_spoof_no_longer_resets_the_password_budget(
    api: Api, monkeypatch
) -> None:
    """Ported probe ``test_x_real_ip_spoof_resets_password_attempt_budget``.

    With a budget of one the probe observed 401, 429, then 401 again from a
    changed ``X-Real-IP``. No trusted proxy network is configured (the
    default), so the header now counts for nothing and the third attempt is
    refused like the second.
    """
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _tune(api, monkeypatch, max_unlock_attempts_per_hour=1)

    first = _wrong_password(api, artifact_id, "198.51.100.1")
    blocked = _wrong_password(api, artifact_id, "198.51.100.1")
    spoofed = _wrong_password(api, artifact_id, "198.51.100.2")

    assert (first.status_code, blocked.status_code, spoofed.status_code) == (
        401,
        429,
        429,
    )


def test_a_trusted_proxy_peer_gets_per_forwarded_address_buckets(
    api: Api, monkeypatch
) -> None:
    """Behind a configured proxy the forwarded address is the bucket again.

    This is the deployment the setting exists for: without it every reader
    behind the Keboola proxy would share one budget, and one guesser would
    lock everybody else out of a password-protected document.
    """
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1,
        trust_forwarded_headers=True,
        trusted_proxy_cidrs=(ipaddress.ip_network(PROXY_CIDR),),
    )

    with peer(api, PROXY_PEER):
        first = _wrong_password(api, artifact_id, "10.1.1.1")
        blocked = _wrong_password(api, artifact_id, "10.1.1.1")
        other_reader = _wrong_password(api, artifact_id, "10.1.1.2")

    assert (first.status_code, blocked.status_code, other_reader.status_code) == (
        401,
        429,
        401,
    )


def test_a_peer_outside_the_trusted_cidrs_is_not_believed(
    api: Api, monkeypatch
) -> None:
    """Same configuration, a peer that is simply not one of our proxies."""
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1,
        trust_forwarded_headers=True,
        trusted_proxy_cidrs=(ipaddress.ip_network(PROXY_CIDR),),
    )

    with peer(api, STRANGER_PEER):
        first = _wrong_password(api, artifact_id, "10.1.1.1")
        blocked = _wrong_password(api, artifact_id, "10.1.1.1")
        spoofed = _wrong_password(api, artifact_id, "10.1.1.2")

    assert (first.status_code, blocked.status_code, spoofed.status_code) == (
        401,
        429,
        429,
    )


def test_forwarded_client_addresses_need_the_forwarded_switch_too(
    api: Api, monkeypatch
) -> None:
    """A trusted CIDR alone is not enough: the header opt-in must be on."""
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1,
        trust_forwarded_headers=False,
        trusted_proxy_cidrs=(ipaddress.ip_network(PROXY_CIDR),),
    )

    with peer(api, PROXY_PEER):
        assert _wrong_password(api, artifact_id, "10.1.1.1").status_code == 401
        assert _wrong_password(api, artifact_id, "10.1.1.2").status_code == 429


def test_the_per_artifact_budget_trips_regardless_of_address(
    api: Api, monkeypatch
) -> None:
    """Defence in depth: changing address cannot be unlimited.

    The per-address budget is generous here and the per-artifact one is two,
    and every attempt arrives from a different (believed) address — so only
    the address-independent budget can be what stops the fourth.
    """
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1000,
        max_unlock_attempts_per_artifact_per_hour=2,
        trust_forwarded_headers=True,
        trusted_proxy_cidrs=(ipaddress.ip_network(PROXY_CIDR),),
    )

    with peer(api, PROXY_PEER):
        statuses = [
            _wrong_password(api, artifact_id, f"10.1.1.{n}").status_code
            for n in range(1, 5)
        ]

    assert statuses == [401, 401, 429, 429], statuses


def test_the_per_artifact_budget_is_scoped_to_one_artifact(
    api: Api, monkeypatch
) -> None:
    """Grinding one document must not lock readers out of another."""
    first_id = _publish_markdown(api, "# One", password="correct")
    second_id = _publish_markdown(api, "# Two", password="correct")
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1000,
        max_unlock_attempts_per_artifact_per_hour=1,
    )

    assert _wrong_password(api, first_id).status_code == 401
    assert _wrong_password(api, first_id).status_code == 429
    assert _wrong_password(api, second_id).status_code == 401


def test_guest_credential_probing_shares_the_per_artifact_budget(
    api: Api, monkeypatch
) -> None:
    """The same defence covers ``X-Artifact-Guest``, in its own scope."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text here.")
    invitation = api.client.post(
        f"/api/artifacts/{artifact_id}/invitations",
        json={"name": "Jana"},
        headers=AUTH_HEADERS,
    )
    assert invitation.status_code == 201, invitation.text
    review_url = invitation.json()["review_url"]
    invitation_id = review_url.split("#invite=", 1)[1].split(".", 1)[0]
    wrong = {"X-Artifact-Guest": f"{invitation_id}.not-the-secret"}
    _tune(
        api,
        monkeypatch,
        max_unlock_attempts_per_hour=1000,
        max_unlock_attempts_per_artifact_per_hour=1,
    )

    assert api.client.get(f"/a/{artifact_id}/guest", headers=wrong).status_code == 401
    blocked = api.client.get(f"/a/{artifact_id}/guest", headers=wrong)
    assert blocked.status_code == 429
    # A wrong *password* budget is a different scope and is untouched.
    assert "invitation" in blocked.json()["detail"]


def test_load_settings_rejects_a_malformed_trusted_proxy_cidr(monkeypatch) -> None:
    """A typo in the trust list is a startup failure, not a silent narrowing."""
    monkeypatch.setenv("HUB_TRUSTED_PROXY_CIDRS", "10.0.0.0/8, not-a-network")
    with pytest.raises(RuntimeError, match="HUB_TRUSTED_PROXY_CIDRS"):
        load_settings()


def test_load_settings_parses_trusted_proxy_cidrs(monkeypatch) -> None:
    """Blank entries are skipped and a bare address becomes a host route."""
    monkeypatch.setenv(
        "HUB_TRUSTED_PROXY_CIDRS", " 10.0.0.0/8 , ,192.0.2.7, fd00::/8 "
    )
    assert load_settings().trusted_proxy_cidrs == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("192.0.2.7/32"),
        ipaddress.ip_network("fd00::/8"),
    )


def test_trusted_proxy_cidrs_default_to_trusting_nothing(monkeypatch) -> None:
    monkeypatch.delenv("HUB_TRUSTED_PROXY_CIDRS", raising=False)
    assert load_settings().trusted_proxy_cidrs == ()


# --------------------------------------------------------------------------
# SEC-100-004 — no inbound body budget before parsing
# --------------------------------------------------------------------------


def test_unauthenticated_multi_megabyte_body_is_refused_early(
    api: Api, monkeypatch
) -> None:
    """Ported probe ``test_unauthenticated_multi_megabyte_body_has_no_early_size_gate``.

    The probe watched 3 MiB be consumed and parsed before the nonexistent
    target produced a 400. It is now a 413 decided from the declared length
    alone: no lock is allocated and the store is never asked anything.
    """
    monkeypatch.setattr(main.app.state, "store", LoudStore())
    main._artifact_locks.clear()
    oversized = "x" * (3 * 1024 * 1024)

    response = api.client.post(
        "/api/artifacts/arbitrary-unknown-id/comments",
        json={
            "version": 1,
            "exact": "quote",
            "prefix": "",
            "suffix": "",
            "body": oversized,
        },
    )

    assert response.status_code == 413, response.text
    assert response.json()["limit"] == api.settings.max_small_request_bytes
    assert len(main._artifact_locks) == 0, "a lock was allocated for a refused body"


def test_a_chunked_oversized_body_without_content_length_is_cut_off(
    api: Api, monkeypatch
) -> None:
    """A request that declares no length is counted as it streams in.

    httpx sends an iterator body with ``Transfer-Encoding: chunked``, so there
    is no ``Content-Length`` to check — the ceiling has to come from the bytes
    that actually arrive.
    """
    _tune(api, monkeypatch, max_small_request_bytes=4096)
    chunk = b"x" * 1024
    declared: list[int | None] = []
    real_declared = main._declared_content_length
    monkeypatch.setattr(
        main,
        "_declared_content_length",
        lambda scope: declared.append(real_declared(scope)) or declared[-1],
    )

    def body_chunks():
        # Well past the ceiling, and never fully sent: the stream is closed
        # the moment the running total crosses it.
        yield b'{"version": 1, "exact": "q", "prefix": "", "suffix": "", "body": "'
        for _ in range(64):
            yield chunk
        yield b'"}'

    response = api.client.post(
        "/api/artifacts/arbitrary-unknown-id/comments",
        content=body_chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413, response.text
    # The declared-length gate could not have been what refused it.
    assert declared == [None], declared


def test_a_body_just_under_the_ceiling_still_works(api: Api, monkeypatch) -> None:
    """The gate refuses what is over the line and nothing that is under it."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    _tune(api, monkeypatch, max_small_request_bytes=4096)

    accepted = _comment(api, artifact_id, body="y" * 3000)
    assert accepted.status_code == 201, accepted.text

    refused = _comment(api, artifact_id, body="y" * 5000)
    assert refused.status_code == 413, refused.text


def test_a_large_legitimate_publish_still_works(api: Api, monkeypatch) -> None:
    """Documents go to the content routes, which have their own big ceiling.

    The small ceiling is squeezed to nothing here, so a publish getting
    through proves it is not the budget being applied to it.
    """
    _tune(api, monkeypatch, max_small_request_bytes=1024)
    document = "# Big report\n\n" + ("filler paragraph. " * 90_000)
    assert len(document) > 1024 * 1024

    published = api.client.post(
        "/api/artifacts", json={"markdown": document}, headers=AUTH_HEADERS
    )

    assert published.status_code == 201, published.text[:400]


def test_the_content_ceiling_follows_the_document_limits(api: Api) -> None:
    """Raising what the hub accepts raises what it will let in the door."""
    bigger = dataclasses.replace(api.settings, max_html_bytes=40 * 1024 * 1024)
    assert bigger.max_content_request_bytes == (
        40 * 1024 * 1024 + REQUEST_ENVELOPE_SLACK_BYTES
    )
    assert (
        api.settings.max_content_request_bytes
        > api.settings.max_small_request_bytes
    )


def test_a_publish_above_the_content_ceiling_is_still_refused(
    api: Api, monkeypatch
) -> None:
    """The content routes are budgeted too, just far more generously."""
    _tune(api, monkeypatch, max_html_bytes=1024, max_envelope_bytes=1024)

    published = api.client.post(
        "/api/artifacts",
        json={"markdown": "# Big\n\n" + "x" * (2 * 1024 * 1024)},
        headers=AUTH_HEADERS,
    )

    assert published.status_code == 413, published.text[:200]


def test_the_platform_startup_probe_still_answers(api: Api) -> None:
    """CLAUDE.md: ``POST /`` is the platform health check and must stay 200."""
    probe = api.client.post("/")
    assert probe.status_code == 200
    assert probe.text == "OK"


def test_comment_fields_carry_their_own_length_ceilings(api: Api) -> None:
    """A body under the request ceiling but over the field ceiling is 422."""
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")

    too_long = _comment(api, artifact_id, body="y" * (MAX_BODY_CHARS + 1))
    assert too_long.status_code == 422
    assert f"at most {MAX_BODY_CHARS} characters" in too_long.text

    long_context = _comment(api, artifact_id, prefix="y" * 2001)
    assert long_context.status_code == 422
    assert "at most 2000 characters" in long_context.text


def test_a_reply_body_is_bounded_by_the_same_field_ceiling(api: Api) -> None:
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    thread = _comment(api, artifact_id)
    assert thread.status_code == 201, thread.text
    thread_id = thread.json()["thread_id"]

    too_long = api.client.post(
        f"/api/artifacts/{artifact_id}/comments/{thread_id}/replies",
        json={"body": "y" * (MAX_BODY_CHARS + 1)},
        headers=AUTH_HEADERS,
    )
    assert too_long.status_code == 422
    assert f"at most {MAX_BODY_CHARS} characters" in too_long.text


# --------------------------------------------------------------------------
# SEC-100-002 — attacker-growable lock registry
# --------------------------------------------------------------------------


def test_unknown_comment_targets_do_not_grow_the_lock_registry(api: Api) -> None:
    """Ported probe ``test_unknown_comment_targets_grow_lock_registry``.

    The probe watched 250 anonymous requests naming made-up artifacts grow
    the registry from 0 to 250 entries that nothing would ever remove. The
    target is now resolved before a lock is taken, so a name that matches no
    artifact allocates nothing at all.
    """
    main._artifact_locks.clear()
    before = len(main._artifact_locks)
    statuses: set[int] = set()

    for index in range(250):
        response = api.client.post(
            f"/api/artifacts/not-an-artifact-{index}/comments",
            json={
                "version": 1,
                "exact": "quote",
                "prefix": "",
                "suffix": "",
                "body": "probe",
            },
        )
        statuses.add(response.status_code)

    assert len(main._artifact_locks) == before
    # The answer itself is unchanged: which of 400/401/404 an anonymous
    # caller sees is decided by the route's own authentication order, not by
    # the lock decorator.
    assert statuses and statuses <= {400, 401, 404}, statuses


def test_a_malformed_path_id_is_refused_without_touching_the_store(
    api: Api, monkeypatch
) -> None:
    """Nothing shaped like this was ever minted, so nothing is looked up."""
    monkeypatch.setattr(main.app.state, "store", LoudStore())
    main._artifact_locks.clear()

    for bad_id in ("x" * 200, "has%20space", "with.dot", "a/b"):
        response = api.client.post(
            f"/api/artifacts/{bad_id}/comments",
            json={
                "version": 1,
                "exact": "quote",
                "prefix": "",
                "suffix": "",
                "body": "probe",
            },
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 404, (bad_id, response.status_code)

    assert len(main._artifact_locks) == 0


def test_the_registry_stays_within_its_bound_for_many_real_artifacts(
    api: Api, monkeypatch
) -> None:
    """Real artifacts are reclaimed too, once nothing holds their lock."""
    _tune(api, monkeypatch, lock_registry_max_entries=5)
    main._artifact_locks.clear()

    for index in range(20):
        artifact_id = _publish_markdown(api, f"# Document {index}")
        policy = api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"comments_mode": "off"},
            headers=AUTH_HEADERS,
        )
        assert policy.status_code == 200, policy.text

    assert len(main._artifact_locks) <= 5, len(main._artifact_locks)


def test_a_reclaimed_key_still_serializes_when_it_comes_back(api: Api) -> None:
    """Eviction is not a correctness hole: a fresh lock serializes just as well."""
    registry = main._ArtifactLockRegistry()
    with registry.hold("first"):
        pass
    assert len(registry) == 1
    registry.clear()

    order: list[str] = []
    inside, release = threading.Event(), threading.Event()

    def slow():
        with registry.hold("first"):
            order.append("slow-in")
            inside.set()
            release.wait(timeout=5)
            order.append("slow-out")

    def quick():
        with registry.hold("first"):
            order.append("quick-in")

    first = threading.Thread(target=slow)
    first.start()
    assert inside.wait(timeout=5)
    second = threading.Thread(target=quick)
    second.start()
    second.join(timeout=0.5)
    assert order == ["slow-in"], f"the second holder was not blocked: {order}"
    # One key, one entry: both holders are on the same lock object.
    assert len(registry) == 1
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert order == ["slow-in", "slow-out", "quick-in"], order


def test_an_idle_bound_of_zero_never_evicts_a_held_lock() -> None:
    """Even with no idle room a lock somebody holds stays in the registry."""
    registry = main._ArtifactLockRegistry()
    inside, release = threading.Event(), threading.Event()
    blocked = threading.Event()

    def holder():
        with registry.hold("busy"):
            inside.set()
            release.wait(timeout=5)

    def churn():
        # Cycle other keys through the (zero-sized) idle queue while "busy"
        # is held, then try to take "busy" itself.
        for index in range(50):
            with registry.hold(f"other-{index}"):
                pass
        with registry.hold("busy"):
            blocked.set()

    first = threading.Thread(target=holder)
    first.start()
    assert inside.wait(timeout=5)
    second = threading.Thread(target=churn)
    second.start()
    second.join(timeout=0.5)
    assert not blocked.is_set(), "the held lock was evicted and stopped serializing"
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert blocked.is_set()


def test_two_comments_on_one_artifact_are_still_serialized(api: Api) -> None:
    """Route-level: the second request waits for the first, as it always did.

    Adapted from ``TestConcurrentMutationsAreSerializedPerArtifact`` in
    tests/test_api.py — the same freeze-one-request-at-the-seam pattern, aimed
    at the comment routes the lock manager now guards.
    """
    artifact_id = _publish_markdown(api, "# Title\n\nBody text")
    comments = main.app.state.comments
    entered, release = _pause_inside(comments, "create")

    first, box_first = _in_thread(
        lambda: _comment(api, artifact_id, exact="Body text", body="one").status_code
    )
    assert entered.wait(timeout=5)
    second, box_second = _in_thread(
        lambda: _comment(api, artifact_id, exact="Body text", body="two").status_code
    )
    second.join(timeout=0.5)
    assert box_second == [], "a second comment ran while the first held the lock"
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert box_first == [201] and box_second == [201], (box_first, box_second)


def test_a_comment_still_serializes_against_the_owner_routes(api: Api) -> None:
    """The lock key is the internal id, so both families share one lock.

    A comment addressed by share id must queue behind an owner route
    addressed by internal id; that ordering is the reason the decorator
    resolves the key before locking, and SEC-100-002's early resolution must
    not have quietly changed it.
    """
    artifact_id = _publish_markdown(api, "# Title\n\nBody text", accept_versions=True)
    store = main.app.state.store
    entered, release = _pause_inside(store, "save_meta")

    policy, box_policy = _in_thread(
        lambda: api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"comments_mode": "anyone"},
            headers=AUTH_HEADERS,
        ).status_code
    )
    assert entered.wait(timeout=5)
    commenting, box_comment = _in_thread(
        lambda: _comment(api, artifact_id, exact="Body text").status_code
    )
    commenting.join(timeout=0.5)
    assert box_comment == [], "a comment landed while an owner route held the lock"
    release.set()
    policy.join(timeout=5)
    commenting.join(timeout=5)

    assert box_policy == [200] and box_comment == [201], (box_policy, box_comment)
