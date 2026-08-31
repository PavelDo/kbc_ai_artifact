"""Tests for src.auth: stack resolution and Storage token verification."""

import httpx
import pytest
import respx

from src.auth import (
    AuthError,
    StackError,
    StackUnreachableError,
    resolve_stack,
    verify_token,
)

VERIFY_URL = "https://connection.keboola.com/v2/storage/tokens/verify"


class TestResolveStack:
    def test_alias_us(self):
        assert resolve_stack("us") == "https://connection.keboola.com"

    def test_alias_eu(self):
        assert resolve_stack("eu") == "https://connection.eu-central-1.keboola.com"

    def test_alias_gcp_eu(self):
        assert (
            resolve_stack("gcp-eu") == "https://connection.europe-west3.gcp.keboola.com"
        )

    def test_alias_is_case_insensitive(self):
        assert resolve_stack("US") == "https://connection.keboola.com"

    def test_full_https_url_normalizes_and_strips_path(self):
        assert (
            resolve_stack("https://connection.keboola.com/v2/storage/")
            == "https://connection.keboola.com"
        )

    def test_trailing_slash_stripped(self):
        assert (
            resolve_stack("https://connection.keboola.com/")
            == "https://connection.keboola.com"
        )

    def test_http_rejected(self):
        with pytest.raises(StackError):
            resolve_stack("http://connection.keboola.com")

    def test_non_keboola_host_rejected(self):
        with pytest.raises(StackError):
            resolve_stack("https://evil.example.com")

    def test_extra_stacks_allows_explicit_url(self):
        extra = ("https://custom.example.com",)
        assert (
            resolve_stack("https://custom.example.com", extra_stacks=extra)
            == "https://custom.example.com"
        )

    def test_extra_stacks_does_not_allow_unlisted_hosts(self):
        extra = ("https://custom.example.com",)
        with pytest.raises(StackError):
            resolve_stack("https://other.example.com", extra_stacks=extra)

    def test_empty_raises(self):
        with pytest.raises(StackError):
            resolve_stack("")

    def test_whitespace_only_raises(self):
        with pytest.raises(StackError):
            resolve_stack("   ")


class TestVerifyToken:
    def test_success_returns_owner(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(
                return_value=httpx.Response(
                    200, json={"owner": {"id": 123, "name": "Proj"}}
                )
            )
            owner = verify_token("https://connection.keboola.com", "some-token")

        assert owner.project_id == 123
        assert owner.project_name == "Proj"
        assert owner.key == "123@connection.keboola.com"

    def test_401_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "bad-token")

    def test_403_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(403))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "bad-token")

    def test_500_raises_stack_unreachable(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(StackUnreachableError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_connect_error_raises_stack_unreachable(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(side_effect=httpx.ConnectError("boom"))
            with pytest.raises(StackUnreachableError):
                verify_token("https://connection.keboola.com", "some-token")

    def test_missing_token_raises_autherror_without_network(self):
        with respx.mock:
            # No route is registered for VERIFY_URL. If verify_token attempted
            # a network call before checking the token, respx would raise its
            # own "not mocked" error instead of AuthError, failing this test.
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "")

    def test_owner_missing_id_raises_autherror(self):
        with respx.mock as mock:
            mock.get(VERIFY_URL).mock(return_value=httpx.Response(200, json={"owner": {}}))
            with pytest.raises(AuthError):
                verify_token("https://connection.keboola.com", "some-token")
