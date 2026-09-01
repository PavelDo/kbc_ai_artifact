"""SEC-075-011: an opt-in narrowing of project-wide destructive authority.

Until v0.10.0 the hub kept only ``(stack, project)`` from a token verify, so
*every* Storage token of the owning project could purge an artifact, rotate its
public link or delete a version. That was documented as an accepted boundary.
``HUB_DESTRUCTIVE_TOKEN_POLICY`` now makes the boundary configurable without
changing the default, and these tests pin all three modes down:

* ``project`` — the default, and byte-for-byte the old behaviour. The
  regression in ``tests/test_review100_outbound_docs.py``
  (``TestOwnerAuthorityIsProjectScopedNotTokenScoped``) still passes unchanged.
* ``admin`` — a master token, or a project user whose ``admin.role`` is
  ``admin``.
* ``allowlist`` — a token whose id the operator listed.

The tests also cover what must *not* move: non-destructive owner routes stay
project-authorized under every policy, a foreign project is still refused
first, and a claim the stack answered with in some unexpected shape degrades
the caller to least privilege rather than promoting them or crashing.
"""

import dataclasses
from typing import Any

import pytest

from src import main
from src.auth import Owner
from src.config import Settings, load_settings
from tests.test_api import (  # noqa: F401
    AUTH_HEADERS,
    OTHER_AUTH_HEADERS,
    _OWNER_PROJECTS,
    _TOKEN_CLAIMS,
    _publish_markdown,
    _submit_version,
    api,
)

STACK = AUTH_HEADERS["X-Kbc-Stack"]

#: The owning project's id/name, as the standard fixture token resolves.
OWNER_PROJECT = _OWNER_PROJECTS[AUTH_HEADERS["X-StorageApi-Token"]]

#: The substring of each policy's 403 detail that the README and /context
#: promise a client can key off. Kept here so a reworded detail fails loudly
#: rather than silently breaking somebody's error handling.
ADMIN_DETAIL = "destructive_token_policy=admin"
ALLOWLIST_DETAIL = "destructive_token_policy=allowlist"


def _register(token: str, **claims: Any) -> None:
    """Teach the fixture's fake verify about one more same-project token."""
    _OWNER_PROJECTS[token] = OWNER_PROJECT
    if claims:
        _TOKEN_CLAIMS[token] = claims


def _forget(*tokens: str) -> None:
    for token in tokens:
        _OWNER_PROJECTS.pop(token, None)
        _TOKEN_CLAIMS.pop(token, None)


def _headers(token: str) -> dict[str, str]:
    return {"X-StorageApi-Token": token, "X-Kbc-Stack": STACK}


@pytest.fixture
def tokens():
    """Same-project tokens covering every claim shape the policies read.

    Registered for the duration of one test and removed afterwards, so the
    fixture's module-level fakes never leak a token into another test.
    """
    _register("policy-master", token_id="tok-master", is_master_token=True)
    _register("policy-admin-user", token_id="tok-admin-user", admin_role="admin")
    _register("policy-guest-user", token_id="tok-guest-user", admin_role="guest")
    _register("policy-readonly-user", token_id="tok-readonly", admin_role="readOnly")
    _register("policy-plain", token_id="tok-plain")
    # A token whose verify body carried no id at all: it can never match an
    # allowlist entry, and must not match an empty one either.
    _register("policy-anonymous")
    # Non-admin but holding the two capability flags, to prove neither of them
    # is quietly treated as administration.
    _register(
        "policy-capable",
        token_id="tok-capable",
        can_purge_trash=True,
        can_manage_tokens=True,
    )
    try:
        yield
    finally:
        _forget(
            "policy-master",
            "policy-admin-user",
            "policy-guest-user",
            "policy-readonly-user",
            "policy-plain",
            "policy-anonymous",
            "policy-capable",
        )


def _set_policy(
    api, monkeypatch, policy: str, token_ids: tuple[str, ...] = ()
) -> None:
    """Switch the running app to one destructive-token policy."""
    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            api.settings,
            destructive_token_policy=policy,
            destructive_token_ids=token_ids,
        ),
    )


def _destructive_calls(api, artifact_id: str, headers: dict[str, str]) -> dict:
    """Every gated route, called against one artifact with one credential.

    Ordered so that the reversible ones run first: a purge would remove the
    artifact the later calls need. Soft delete is last for the same reason —
    a trashed artifact still answers, but there is no need to rely on that.
    """
    return {
        "rotate_link": api.client.post(
            f"/api/artifacts/{artifact_id}/rotate-link", headers=headers
        ),
        "rotate_webhook_key": api.client.post(
            f"/api/artifacts/{artifact_id}/webhooks/nonexistent/rotate-key",
            headers=headers,
        ),
        "delete_version": api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1", headers=headers
        ),
        "soft_delete": api.client.delete(
            f"/api/artifacts/{artifact_id}", headers=headers
        ),
        "purge": api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=headers
        ),
    }


class TestOwnerClaimsParsing:
    """``Owner`` carries the new claims and defaults them to least privilege."""

    def test_defaults_are_least_privileged(self):
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=1,
            project_name="P",
        )
        assert owner.token_id is None
        assert owner.is_master_token is False
        assert owner.admin_role is None
        assert owner.can_purge_trash is False
        assert owner.can_manage_tokens is False
        assert owner.is_project_admin is False

    def test_master_token_is_a_project_admin(self):
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=1,
            project_name="P",
            is_master_token=True,
        )
        assert owner.is_project_admin is True

    def test_admin_role_is_a_project_admin(self):
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=1,
            project_name="P",
            admin_role="admin",
        )
        assert owner.is_project_admin is True

    @pytest.mark.parametrize("role", ["guest", "readOnly", "share", "Admin", "wizard"])
    def test_every_other_role_is_not_an_admin(self, role):
        """Including an unknown one, and including a differently cased 'Admin'."""
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=1,
            project_name="P",
            admin_role=role,
        )
        assert owner.is_project_admin is False

    def test_capability_flags_do_not_imply_administration(self):
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=1,
            project_name="P",
            can_purge_trash=True,
            can_manage_tokens=True,
        )
        assert owner.is_project_admin is False

    def test_claims_are_not_part_of_the_owner_key(self):
        """Ownership identity must stay (project, stack) — claims never key it."""
        base = Owner(
            stack_url="https://connection.keboola.com", project_id=42, project_name="P"
        )
        admin = dataclasses.replace(base, is_master_token=True, token_id="t")
        assert base.key == admin.key == "42@connection.keboola.com"


class TestSettingsValidation:
    """The policy is validated at load time, never silently defaulted."""

    def _env(self, monkeypatch, **extra: str) -> None:
        monkeypatch.setenv("HUB_STORAGE_TOKEN", "token")
        monkeypatch.setenv("HUB_STACK_URL", "https://connection.keboola.com")
        monkeypatch.setenv("HUB_SECRET_KEY", "k" * 40)
        monkeypatch.delenv("HUB_DESTRUCTIVE_TOKEN_POLICY", raising=False)
        monkeypatch.delenv("HUB_DESTRUCTIVE_TOKEN_IDS", raising=False)
        for name, value in extra.items():
            monkeypatch.setenv(name, value)

    def test_default_is_project(self, monkeypatch):
        self._env(monkeypatch)
        settings = load_settings()
        assert settings.destructive_token_policy == "project"
        assert settings.destructive_token_ids == ()

    def test_admin_policy_loads(self, monkeypatch):
        self._env(monkeypatch, HUB_DESTRUCTIVE_TOKEN_POLICY="admin")
        assert load_settings().destructive_token_policy == "admin"

    def test_value_is_case_insensitive_and_trimmed(self, monkeypatch):
        self._env(monkeypatch, HUB_DESTRUCTIVE_TOKEN_POLICY="  Admin ")
        assert load_settings().destructive_token_policy == "admin"

    def test_unknown_policy_fails_fast(self, monkeypatch):
        self._env(monkeypatch, HUB_DESTRUCTIVE_TOKEN_POLICY="admn")
        with pytest.raises(RuntimeError) as exc:
            load_settings()
        assert "HUB_DESTRUCTIVE_TOKEN_POLICY" in str(exc.value)

    def test_allowlist_without_ids_fails_fast(self, monkeypatch):
        self._env(monkeypatch, HUB_DESTRUCTIVE_TOKEN_POLICY="allowlist")
        with pytest.raises(RuntimeError) as exc:
            load_settings()
        assert "HUB_DESTRUCTIVE_TOKEN_IDS" in str(exc.value)

    def test_allowlist_of_only_separators_fails_fast(self, monkeypatch):
        """' , , ' parses to nothing, which is the same lockout."""
        self._env(
            monkeypatch,
            HUB_DESTRUCTIVE_TOKEN_POLICY="allowlist",
            HUB_DESTRUCTIVE_TOKEN_IDS=" , , ",
        )
        with pytest.raises(RuntimeError):
            load_settings()

    def test_allowlist_ids_are_trimmed_and_deduplicated(self, monkeypatch):
        self._env(
            monkeypatch,
            HUB_DESTRUCTIVE_TOKEN_POLICY="allowlist",
            HUB_DESTRUCTIVE_TOKEN_IDS=" 111 ,222, 111 ,",
        )
        assert load_settings().destructive_token_ids == ("111", "222")

    def test_ids_are_ignored_by_the_other_policies(self, monkeypatch):
        """They load, but nothing reads them — no accidental half-allowlist."""
        self._env(
            monkeypatch,
            HUB_DESTRUCTIVE_TOKEN_POLICY="project",
            HUB_DESTRUCTIVE_TOKEN_IDS="111",
        )
        settings = load_settings()
        assert settings.destructive_token_policy == "project"
        assert settings.destructive_token_ids == ("111",)


class TestProjectPolicyIsUnchanged:
    """The default keeps every documented v0.10.0 behaviour."""

    def test_any_same_project_token_may_run_every_destructive_route(
        self, api, tokens
    ):
        # No policy override: the fixture's settings carry the default.
        assert api.settings.destructive_token_policy == "project"
        for token in ("policy-plain", "policy-readonly-user", "policy-anonymous"):
            artifact_id = _publish_markdown(api, "# Default policy")
            results = _destructive_calls(api, artifact_id, _headers(token))
            assert results["rotate_link"].status_code == 200, token
            # The receiver id is deliberately unknown: a 404 means the request
            # got past authorization, which is what this asserts.
            assert results["rotate_webhook_key"].status_code == 404, token
            assert results["soft_delete"].status_code == 200, token
            assert results["purge"].status_code == 200, token

    def test_version_delete_is_allowed_for_any_same_project_token(self, api, tokens):
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1",
            headers=_headers("policy-plain"),
        )
        assert resp.status_code == 200, resp.text

    def test_foreign_project_is_still_refused(self, api):
        artifact_id = _publish_markdown(api, "# Mine")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        )
        assert resp.status_code == 403
        assert "another project" in resp.json()["detail"]


class TestAdminPolicy:
    """``admin``: a master token or a project user with the admin role."""

    @pytest.fixture(autouse=True)
    def _policy(self, api, monkeypatch, tokens):
        _set_policy(api, monkeypatch, "admin")

    @pytest.mark.parametrize("token", ["policy-master", "policy-admin-user"])
    def test_an_admin_token_runs_every_destructive_route(self, api, token):
        artifact_id = _publish_markdown(api, "# Admin allowed")
        results = _destructive_calls(api, artifact_id, _headers(token))
        assert results["rotate_link"].status_code == 200
        assert results["rotate_webhook_key"].status_code == 404
        assert results["soft_delete"].status_code == 200
        assert results["purge"].status_code == 200

    @pytest.mark.parametrize(
        "token",
        [
            "policy-plain",
            "policy-guest-user",
            "policy-readonly-user",
            "policy-anonymous",
            "policy-capable",
        ],
    )
    def test_a_non_admin_same_project_token_is_refused_everywhere(self, api, token):
        artifact_id = _publish_markdown(api, "# Admin refused")
        results = _destructive_calls(api, artifact_id, _headers(token))
        for route, resp in results.items():
            assert resp.status_code == 403, f"{route} for {token}: {resp.text}"
            assert ADMIN_DETAIL in resp.json()["detail"], route

    def test_the_refusal_never_echoes_the_token(self, api):
        artifact_id = _publish_markdown(api, "# No echo")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=_headers("policy-plain")
        )
        assert resp.status_code == 403
        body = resp.text
        assert "policy-plain" not in body
        assert "tok-plain" not in body

    def test_the_artifact_survives_a_refused_purge(self, api):
        artifact_id = _publish_markdown(api, "# Still here")
        refused = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=_headers("policy-plain")
        )
        assert refused.status_code == 403
        # The owner's listing is the index of record: a refused purge must
        # leave the artifact in it, not half-erased.
        listing = api.client.get("/api/artifacts", headers=AUTH_HEADERS)
        assert artifact_id in [row["id"] for row in listing.json()["artifacts"]]

    def test_foreign_project_is_refused_before_the_policy(self, api):
        """Ownership is still the first gate, with its own detail."""
        artifact_id = _publish_markdown(api, "# Mine")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        )
        assert resp.status_code == 403
        assert "another project" in resp.json()["detail"]
        assert ADMIN_DETAIL not in resp.json()["detail"]

    def test_owner_version_delete_needs_admin(self, api):
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        refused = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1",
            headers=_headers("policy-plain"),
        )
        assert refused.status_code == 403
        assert ADMIN_DETAIL in refused.json()["detail"]
        allowed = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1",
            headers=_headers("policy-master"),
        )
        assert allowed.status_code == 200, allowed.text

    def test_non_destructive_owner_routes_are_unaffected(self, api):
        """Update, head pin, promote, settings, invitations, stats, restore."""
        headers = _headers("policy-plain")
        artifact_id = _publish_markdown(api, "# Untouched", accept_versions=True)
        assert _submit_version(api, artifact_id, "# Two").status_code == 201

        update = api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"accept_versions": True},
            headers=headers,
        )
        assert update.status_code == 200, update.text

        promote = api.client.post(
            f"/api/artifacts/{artifact_id}/versions/2/promote", headers=headers
        )
        assert promote.status_code in (200, 409), promote.text

        head = api.client.put(
            f"/api/artifacts/{artifact_id}/head",
            json={"mode": "pinned", "version": 1},
            headers=headers,
        )
        assert head.status_code == 200, head.text

        invitation = api.client.post(
            f"/api/artifacts/{artifact_id}/invitations",
            json={"name": "Reviewer"},
            headers=headers,
        )
        assert invitation.status_code == 201, invitation.text

        invitations = api.client.get(
            f"/api/artifacts/{artifact_id}/invitations", headers=headers
        )
        assert invitations.status_code == 200
        invitation_id = invitations.json()["invitations"][0]["id"]

        revoke = api.client.delete(
            f"/api/artifacts/{artifact_id}/invitations/{invitation_id}",
            headers=headers,
        )
        assert revoke.status_code == 200, revoke.text

        stats = api.client.get(f"/api/artifacts/{artifact_id}/stats", headers=headers)
        assert stats.status_code == 200

        webhook_keys = api.client.get(
            f"/api/artifacts/{artifact_id}/webhooks", headers=headers
        )
        assert webhook_keys.status_code == 200

    def test_trash_restore_is_not_destructive(self, api):
        """An admin trashes it; a plain same-project token may still restore."""
        artifact_id = _publish_markdown(api, "# Recoverable")
        trashed = api.client.delete(
            f"/api/artifacts/{artifact_id}", headers=_headers("policy-master")
        )
        assert trashed.status_code == 200, trashed.text
        restored = api.client.post(
            f"/api/artifacts/{artifact_id}/restore", headers=_headers("policy-plain")
        )
        assert restored.status_code == 200, restored.text

    def test_a_contributor_may_still_withdraw_their_own_proposal(self, api):
        """The policy gates owner authority, not somebody's own draft."""
        artifact_id = _publish_markdown(api, "# Open", accept_versions=True)
        proposed = _submit_version(
            api, artifact_id, "# Proposal", headers=OTHER_AUTH_HEADERS
        )
        assert proposed.status_code == 201, proposed.text
        version = proposed.json()["version"]

        # The proposer's project is not the owner, so no policy applies to it;
        # what matters is that the withdrawal is not refused by this gate.
        withdrawn = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/{version}",
            headers=OTHER_AUTH_HEADERS,
        )
        assert withdrawn.status_code == 200, withdrawn.text


class TestAllowlistPolicy:
    """``allowlist``: only the token ids the operator wrote down."""

    @pytest.fixture(autouse=True)
    def _policy(self, api, monkeypatch, tokens):
        _set_policy(api, monkeypatch, "allowlist", ("tok-plain",))

    def test_a_listed_token_runs_every_destructive_route(self, api):
        artifact_id = _publish_markdown(api, "# Listed")
        results = _destructive_calls(api, artifact_id, _headers("policy-plain"))
        assert results["rotate_link"].status_code == 200
        assert results["rotate_webhook_key"].status_code == 404
        assert results["soft_delete"].status_code == 200
        assert results["purge"].status_code == 200

    @pytest.mark.parametrize(
        "token", ["policy-master", "policy-admin-user", "policy-anonymous"]
    )
    def test_an_unlisted_same_project_token_is_refused_even_when_admin(
        self, api, token
    ):
        """The allowlist is exact: being a master token is not a bypass."""
        artifact_id = _publish_markdown(api, "# Unlisted")
        results = _destructive_calls(api, artifact_id, _headers(token))
        for route, resp in results.items():
            assert resp.status_code == 403, f"{route} for {token}: {resp.text}"
            assert ALLOWLIST_DETAIL in resp.json()["detail"], route

    def test_the_refusal_never_leaks_the_allowlist(self, api):
        artifact_id = _publish_markdown(api, "# No leak")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=_headers("policy-master")
        )
        assert resp.status_code == 403
        assert "tok-plain" not in resp.text
        assert "tok-master" not in resp.text

    def test_foreign_project_is_refused_before_the_policy(self, api):
        artifact_id = _publish_markdown(api, "# Mine")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=OTHER_AUTH_HEADERS
        )
        assert resp.status_code == 403
        assert "another project" in resp.json()["detail"]

    def test_owner_version_delete_follows_the_allowlist(self, api):
        artifact_id = _publish_markdown(api, "# One")
        assert _submit_version(api, artifact_id, "# Two").status_code == 201
        refused = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1",
            headers=_headers("policy-master"),
        )
        assert refused.status_code == 403
        assert ALLOWLIST_DETAIL in refused.json()["detail"]
        allowed = api.client.delete(
            f"/api/artifacts/{artifact_id}/versions/1",
            headers=_headers("policy-plain"),
        )
        assert allowed.status_code == 200, allowed.text

    def test_non_destructive_owner_routes_are_unaffected(self, api):
        headers = _headers("policy-master")  # unlisted, therefore non-destructive only
        artifact_id = _publish_markdown(api, "# Untouched")
        update = api.client.put(
            f"/api/artifacts/{artifact_id}",
            json={"accept_versions": True},
            headers=headers,
        )
        assert update.status_code == 200, update.text
        stats = api.client.get(f"/api/artifacts/{artifact_id}/stats", headers=headers)
        assert stats.status_code == 200


class TestMalformedClaimsDegradeToLeastPrivilege:
    """A verify body in an unexpected shape must never promote a caller.

    ``verify_token``'s own parsing is unit-tested in ``tests/test_auth.py``;
    what is checked here is the consequence — a token the stack described in a
    shape the hub could not read gets refused by the ``admin`` policy rather
    than waved through.
    """

    @pytest.fixture(autouse=True)
    def _policy(self, api, monkeypatch, tokens):
        _set_policy(api, monkeypatch, "admin")

    @pytest.mark.parametrize(
        "claims",
        [
            {},
            {"token_id": None},
            {"admin_role": None},
            {"admin_role": ""},
            {"admin_role": "unheard-of-role"},
            {"is_master_token": False, "can_purge_trash": True},
        ],
        ids=[
            "no-claims",
            "no-token-id",
            "no-admin-object",
            "empty-role",
            "unknown-role",
            "capability-flag-only",
        ],
    )
    def test_unusable_claims_are_refused(self, api, claims):
        token = "policy-degraded"
        _register(token, **claims)
        try:
            artifact_id = _publish_markdown(api, "# Degraded")
            resp = api.client.delete(
                f"/api/artifacts/{artifact_id}/purge", headers=_headers(token)
            )
        finally:
            _forget(token)
        assert resp.status_code == 403
        assert ADMIN_DETAIL in resp.json()["detail"]


class TestContextManifest:
    """``/context`` reports the policy name, and never the allowlist."""

    def test_default_policy_is_reported(self, api):
        body = api.client.get("/context").json()
        assert body["limits"]["destructive_token_policy"] == "project"

    def test_active_policy_is_reported(self, api, monkeypatch):
        _set_policy(api, monkeypatch, "allowlist", ("tok-secretish",))
        body = api.client.get("/context").json()
        assert body["limits"]["destructive_token_policy"] == "allowlist"

    def test_allowlist_contents_are_never_published(self, api, monkeypatch):
        _set_policy(api, monkeypatch, "allowlist", ("tok-secretish",))
        assert "tok-secretish" not in api.client.get("/context").text

    def test_notes_explain_which_routes_are_gated(self, api, monkeypatch):
        _set_policy(api, monkeypatch, "admin")
        notes = " ".join(api.client.get("/context").json()["notes"])
        assert "destructive_token_policy" in notes
        assert "rotate-link" in notes
        assert "restore" in notes


class TestUnknownPolicyFailsClosed:
    """Defence in depth for a policy value that never reaches load_settings.

    ``load_settings`` rejects an unknown name at startup, so this state cannot
    be produced by configuration. It can be produced by a future edit that
    adds a policy to :data:`src.config.DESTRUCTIVE_TOKEN_POLICIES` and forgets
    the branch in ``_destructive_authority`` — and the answer to that must be a
    refusal, not an accidental "allow everything".
    """

    def test_an_unrecognized_policy_refuses(self, api, monkeypatch):
        _set_policy(api, monkeypatch, "policy-nobody-implemented")
        artifact_id = _publish_markdown(api, "# Fail closed")
        resp = api.client.delete(
            f"/api/artifacts/{artifact_id}/purge", headers=AUTH_HEADERS
        )
        assert resp.status_code == 403
        assert "token policy" in resp.json()["detail"]


class TestClaimsAreNeverPersisted:
    """Token claims are request-scope, like the raw token itself.

    ``_identity`` enumerates the three project fields by hand rather than
    serializing the whole ``Owner``; if that ever became a
    ``dataclasses.asdict``, a token's id and role would start being written
    into every meta record and version envelope in Storage — durable state
    that outlives the request and is readable by anyone who can read the hub
    project's files.
    """

    def test_identity_carries_only_the_project_fields(self):
        owner = Owner(
            stack_url="https://connection.keboola.com",
            project_id=123,
            project_name="Test",
            token_id="tok-secretish",
            is_master_token=True,
            admin_role="admin",
            can_purge_trash=True,
            can_manage_tokens=True,
        )
        assert set(main._identity(owner)) == {
            "stack_url",
            "project_id",
            "project_name",
            "key",
        }

    def test_no_claim_reaches_a_stored_record(self, api, tokens):
        """Publish with a fully-claimed token, then read back what Storage holds."""
        artifact_id = _publish_markdown(
            api, "# Nothing leaks", headers=_headers("policy-master")
        )
        stored = b"".join(
            content
            for info, content in api.backend.files.values()
            if artifact_id in info.name
        )
        assert stored, "expected the artifact's records in the backend"
        for needle in (b"tok-master", b"isMasterToken", b"admin_role", b"token_id"):
            assert needle not in stored, needle


class TestSettingsDataclassDefault:
    """A Settings built without the field keeps the historical behaviour."""

    def test_bare_settings_default_to_project(self):
        settings = Settings(
            hub_storage_token="t",
            hub_stack_url="https://connection.keboola.com",
            secret_key="k" * 40,
        )
        assert settings.destructive_token_policy == "project"
        assert settings.destructive_token_ids == ()
