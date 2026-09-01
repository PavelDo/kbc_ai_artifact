"""Forwarded client addresses: the second half of SEC-100-003.

The first pass at SEC-100-003 stopped believing ``X-Real-IP`` from just
anybody, but it still read ``X-Forwarded-For`` from the *left*, and it took
whatever string it found there as the rate-limit bucket key. Both are wrong in
exactly the deployment ``HUB_TRUSTED_PROXY_CIDRS`` exists for:

* Every proxy in the path **appends** the peer it accepted the connection
  from (our own nginx uses ``$proxy_add_x_forwarded_for``). So a caller who
  sends ``X-Forwarded-For: <anything>`` gets ``<anything>, <their real
  address>, <proxy>`` delivered to us: the *leftmost* entry is the one the
  attacker chose, and reading it back handed them a fresh brute-force budget
  per made-up value -- the very thing the finding was about.
* Nothing checked that a forwarded entry was an address at all, so the key of
  the persisted ``counters`` table was an arbitrary caller-supplied string of
  arbitrary length.

The fix walks the chain from the right, skipping entries that are themselves
trusted proxies, and returns a canonical IP or nothing at all.

The ``api`` fixture and the peer/settings helpers come from the existing test
modules, so this module stays a pure addition -- no live Storage, no live
stack, no DNS.
"""

from __future__ import annotations

import ipaddress

import pytest
from starlette.requests import Request

import src.main as main
from tests.test_api import (
    Api,
    _publish_markdown,
    api,  # noqa: F401 - the fixture this module runs on
)
from tests.test_review100_request_hygiene import _tune, peer

#: Our own nginx, the process that actually opens the connection to uvicorn.
NGINX_PEER = "10.0.0.5"
#: The network an operator would name for it.
NGINX_CIDR = "10.0.0.0/8"
#: The Keboola platform proxy, one hop further out; it too appends to the
#: chain, so its network belongs in HUB_TRUSTED_PROXY_CIDRS as well.
PLATFORM_PROXY = "198.51.100.10"
PLATFORM_CIDR = "198.51.100.0/24"
#: The address the reader's browser really came from.
REAL_CLIENT = "203.0.113.9"

TRUSTED = (
    ipaddress.ip_network(NGINX_CIDR),
    ipaddress.ip_network(PLATFORM_CIDR),
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _request(peer_host: str | None, headers: dict[str, str] | None = None) -> Request:
    """A bare ASGI request with a chosen peer address and headers.

    ``_client_ip`` reads nothing else, so building the scope directly keeps
    these cases readable and lets them assert the exact key rather than
    inferring it from a status code.
    """
    raw = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": raw,
            "client": (peer_host, 40000) if peer_host else None,
            "server": ("hub", 80),
        }
    )


def _trusting(api: Api, monkeypatch, **extra) -> None:
    """Configure the fixture's settings as a real proxied deployment would."""
    _tune(
        api,
        monkeypatch,
        trust_forwarded_headers=True,
        trusted_proxy_cidrs=TRUSTED,
        **extra,
    )


def _chain(*entries: str) -> dict[str, str]:
    return {"X-Forwarded-For": ", ".join(entries)}


def _wrong_password_via(api: Api, artifact_id: str, headers: dict[str, str]):
    """One failed password attempt carrying caller-chosen forwarding headers."""
    return api.client.get(
        f"/a/{artifact_id}/raw", headers={"X-Artifact-Password": "wrong", **headers}
    )


# --------------------------------------------------------------------------
# Which end of the chain is the client
# --------------------------------------------------------------------------


def test_the_rightmost_untrusted_entry_is_the_client(api: Api, monkeypatch) -> None:
    """The attacker owns the left of the chain; our proxies own the right."""
    _trusting(api, monkeypatch)
    request = _request(NGINX_PEER, _chain("192.0.2.55", REAL_CLIENT, PLATFORM_PROXY))

    assert main._client_ip(request) == REAL_CLIENT


def test_rotating_the_leftmost_entry_buys_no_new_budget(api: Api, monkeypatch) -> None:
    """The finding, end to end: a budget of one must survive a rewritten head.

    Both attempts come from the same real client; only the entry the caller
    prepended differs. Before the fix the second one was a fresh bucket and
    answered 401 forever.
    """
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _trusting(api, monkeypatch, max_unlock_attempts_per_hour=1)

    with peer(api, NGINX_PEER):
        first = _wrong_password_via(
            api, artifact_id, _chain("192.0.2.1", REAL_CLIENT, PLATFORM_PROXY)
        )
        again = _wrong_password_via(
            api, artifact_id, _chain("192.0.2.2", REAL_CLIENT, PLATFORM_PROXY)
        )

    assert (first.status_code, again.status_code) == (401, 429)


def test_two_real_clients_behind_the_proxy_keep_separate_budgets(
    api: Api, monkeypatch
) -> None:
    """The reason the setting exists at all still holds after the fix."""
    artifact_id = _publish_markdown(api, "# Password protected", password="correct")
    _trusting(api, monkeypatch, max_unlock_attempts_per_hour=1)

    with peer(api, NGINX_PEER):
        first = _wrong_password_via(
            api, artifact_id, _chain(REAL_CLIENT, PLATFORM_PROXY)
        )
        blocked = _wrong_password_via(
            api, artifact_id, _chain(REAL_CLIENT, PLATFORM_PROXY)
        )
        neighbour = _wrong_password_via(
            api, artifact_id, _chain("203.0.113.10", PLATFORM_PROXY)
        )

    assert (first.status_code, blocked.status_code, neighbour.status_code) == (
        401,
        429,
        401,
    )


def test_a_chain_of_nothing_but_proxies_falls_back_to_the_peer(
    api: Api, monkeypatch
) -> None:
    """A request our own infrastructure originated has no client to find."""
    _trusting(api, monkeypatch)
    request = _request(NGINX_PEER, _chain(PLATFORM_PROXY, "10.0.0.9"))

    assert main._client_ip(request) == NGINX_PEER


def test_x_real_ip_still_outranks_the_chain(api: Api, monkeypatch) -> None:
    """Our own nginx sets it, so when it is present it is the better answer."""
    _trusting(api, monkeypatch)
    request = _request(
        NGINX_PEER,
        {"X-Real-IP": REAL_CLIENT, **_chain("192.0.2.55", "198.51.100.10")},
    )

    assert main._client_ip(request) == REAL_CLIENT


# --------------------------------------------------------------------------
# Every candidate has to parse as an address
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "not-an-ip",
        "; DROP TABLE counters",
        "999.999.999.999",
        "x" * 4096,
        "",
        "   ",
    ],
)
def test_an_unparsable_x_real_ip_falls_back_to_the_peer(
    api: Api, monkeypatch, value: str
) -> None:
    """Junk in the header must never become a counters-table key."""
    _trusting(api, monkeypatch)

    assert main._client_ip(_request(NGINX_PEER, {"X-Real-IP": value})) == NGINX_PEER


def test_an_unparsable_chain_entry_is_skipped_not_returned(
    api: Api, monkeypatch
) -> None:
    """A bad rightmost entry does not hide the good one behind it."""
    _trusting(api, monkeypatch)
    request = _request(
        NGINX_PEER, _chain("192.0.2.55", REAL_CLIENT, "unknown", PLATFORM_PROXY)
    )

    assert main._client_ip(request) == REAL_CLIENT


def test_a_wholly_unparsable_chain_falls_back_to_the_peer(
    api: Api, monkeypatch
) -> None:
    _trusting(api, monkeypatch)
    request = _request(NGINX_PEER, _chain("unknown", "nonsense", "<script>"))

    assert main._client_ip(request) == NGINX_PEER


def test_the_bucket_key_is_always_a_canonical_address(api: Api, monkeypatch) -> None:
    """Whatever comes in, what goes to the counters table is an IP string."""
    _trusting(api, monkeypatch)
    cases = [
        ({"X-Real-IP": "  203.0.113.9  "}, REAL_CLIENT),
        ({"X-Real-IP": "203.0.113.9:51314"}, REAL_CLIENT),
        ({"X-Real-IP": "[2001:db8::1]:51314"}, "2001:db8::1"),
        ({"X-Real-IP": "2001:0db8:0000:0000:0000:0000:0000:0001"}, "2001:db8::1"),
        ({"X-Real-IP": "::ffff:203.0.113.9"}, REAL_CLIENT),
        (_chain("192.0.2.55", "203.0.113.9:51314", PLATFORM_PROXY), REAL_CLIENT),
    ]

    for headers, expected in cases:
        key = main._client_ip(_request(NGINX_PEER, headers))
        assert key == expected, headers
        ipaddress.ip_address(key)


# --------------------------------------------------------------------------
# Cost, and dual-stack peers
# --------------------------------------------------------------------------


def test_an_over_long_chain_is_examined_only_to_the_cap(
    api: Api, monkeypatch
) -> None:
    """A 5000-hop header must cost a bounded number of parses, not 5000.

    Every entry here is inside a trusted network, so the honest answer is the
    peer; the point of the case is the parse budget it takes to get there.
    """
    _trusting(api, monkeypatch)
    calls: list[str] = []
    original = main._parse_client_address

    def counted(raw):
        calls.append(raw if isinstance(raw, str) else "")
        return original(raw)

    monkeypatch.setattr(main, "_parse_client_address", counted)
    request = _request(NGINX_PEER, _chain(*(["10.0.0.9"] * 5000)))

    assert main._client_ip(request) == NGINX_PEER
    # The chain walk, plus the peer lookups either side of it.
    assert len(calls) <= main.settings.max_forwarded_chain_entries + 4, len(calls)


def test_an_over_long_chain_of_junk_still_buckets_on_the_peer(
    api: Api, monkeypatch
) -> None:
    """The cheap path must also be the safe one: no arbitrary key escapes."""
    _trusting(api, monkeypatch)
    request = _request(NGINX_PEER, _chain(*(["not-an-ip"] * 5000)))

    assert main._client_ip(request) == NGINX_PEER


def test_a_client_pushed_past_the_cap_is_out_of_reach(api: Api, monkeypatch) -> None:
    """Padding the chain past the cap costs the attacker, it does not free them.

    Beyond the cap the walk stops looking and falls back to the peer, so a
    caller who buries their own address under a thousand hops lands in the
    proxy's shared bucket -- a strictly smaller budget than their own.
    """
    _trusting(api, monkeypatch, max_forwarded_chain_entries=4)
    padding = ["10.0.0.9"] * 10
    request = _request(NGINX_PEER, _chain(REAL_CLIENT, *padding))

    assert main._client_ip(request) == NGINX_PEER


def test_a_client_just_inside_the_cap_is_still_found(api: Api, monkeypatch) -> None:
    """The boundary itself: the cap-th entry from the right still counts."""
    _trusting(api, monkeypatch, max_forwarded_chain_entries=4)
    request = _request(
        NGINX_PEER, _chain("192.0.2.55", REAL_CLIENT, *(["10.0.0.9"] * 3))
    )

    assert main._client_ip(request) == REAL_CLIENT


def test_a_nonsense_cap_does_not_silently_disable_itself(
    api: Api, monkeypatch
) -> None:
    """A negative cap means "split everything" to str.rsplit; clamp it.

    Misconfiguring the tunable has to fail closed -- towards the peer bucket
    -- never towards walking an unbounded caller-supplied chain.
    """
    _trusting(api, monkeypatch, max_forwarded_chain_entries=-1)
    request = _request(NGINX_PEER, _chain("192.0.2.55", REAL_CLIENT, PLATFORM_PROXY))

    assert main._client_ip(request) == NGINX_PEER


def test_an_ipv4_mapped_peer_inside_the_cidr_is_trusted(
    api: Api, monkeypatch
) -> None:
    """A dual-stack listener reports 10.0.0.5 as ``::ffff:10.0.0.5``.

    Without normalizing that back to its IPv4 form the membership test in
    ``10.0.0.0/8`` fails and the operator's configuration silently does
    nothing.
    """
    _trusting(api, monkeypatch)
    request = _request(f"::ffff:{NGINX_PEER}", {"X-Real-IP": REAL_CLIENT})

    assert main._forwarded_client_trusted(request) is True
    assert main._client_ip(request) == REAL_CLIENT


def test_an_ipv4_mapped_proxy_in_the_chain_is_skipped(api: Api, monkeypatch) -> None:
    """The same normalization applies to the entries, not just the peer."""
    _trusting(api, monkeypatch)
    request = _request(NGINX_PEER, _chain(REAL_CLIENT, f"::ffff:{PLATFORM_PROXY}"))

    assert main._client_ip(request) == REAL_CLIENT


def test_an_ipv4_mapped_peer_is_canonicalized_in_the_bucket_key(
    api: Api, monkeypatch
) -> None:
    """Two spellings of one peer must not be two budgets."""
    _tune(api, monkeypatch, trust_forwarded_headers=False, trusted_proxy_cidrs=())

    assert main._client_ip(_request(f"::ffff:{REAL_CLIENT}")) == REAL_CLIENT
    assert main._client_ip(_request(REAL_CLIENT)) == REAL_CLIENT


def test_an_untrusted_peer_ignores_the_chain_entirely(api: Api, monkeypatch) -> None:
    """No trusted network configured: the peer is the only believable address."""
    _tune(api, monkeypatch, trust_forwarded_headers=True, trusted_proxy_cidrs=())
    request = _request(REAL_CLIENT, _chain("192.0.2.55", "192.0.2.56"))

    assert main._client_ip(request) == REAL_CLIENT
