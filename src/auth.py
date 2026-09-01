"""Client authentication: stack resolution and Storage token verification.

The management API authenticates with two headers:
  X-StorageApi-Token: any Keboola Storage API token
  X-Storage-Stack:    stack alias or full https URL

A stack URL is accepted when it is https and its hostname ends with
``.keboola.com``, or when it is explicitly listed in HUB_EXTRA_STACKS.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

STACK_ALIASES: dict[str, str] = {
    "us": "https://connection.keboola.com",
    "aws-us": "https://connection.keboola.com",
    "us-east4": "https://connection.us-east4.gcp.keboola.com",
    "gcp-us": "https://connection.us-east4.gcp.keboola.com",
    "eu": "https://connection.eu-central-1.keboola.com",
    "aws-eu": "https://connection.eu-central-1.keboola.com",
    "azure-eu": "https://connection.north-europe.azure.keboola.com",
    "north-europe": "https://connection.north-europe.azure.keboola.com",
    "gcp-eu": "https://connection.europe-west3.gcp.keboola.com",
    "europe-west3": "https://connection.europe-west3.gcp.keboola.com",
}


class StackError(ValueError):
    """Raised for an unknown or disallowed stack."""


class AuthError(Exception):
    """Raised when token verification fails (401 semantics)."""


class StackUnreachableError(Exception):
    """Raised when the stack cannot be reached (502 semantics)."""


@dataclass(frozen=True)
class Owner:
    stack_url: str
    project_id: int
    project_name: str

    @property
    def key(self) -> str:
        """Stable owner identifier usable as a Storage File tag."""
        host = urlparse(self.stack_url).hostname or "unknown"
        return f"{self.project_id}@{host}"


def resolve_stack(raw: str, extra_stacks: tuple[str, ...] = ()) -> str:
    """Turn an alias or URL into a normalized allowed stack URL."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        raise StackError("Missing X-Storage-Stack header (alias or https URL)")
    lowered = value.lower()
    if lowered in STACK_ALIASES:
        return STACK_ALIASES[lowered]
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise StackError(
            f"Stack {value!r} not allowed: use an alias "
            f"({', '.join(sorted(set(STACK_ALIASES)))}) or an https URL"
        )
    normalized = f"https://{parsed.hostname}"
    if parsed.hostname.endswith(".keboola.com") or normalized in extra_stacks:
        return normalized
    raise StackError(
        f"Stack {value!r} not allowed: hostname must end with .keboola.com "
        "or be listed in HUB_EXTRA_STACKS"
    )


def verify_token(stack_url: str, token: str, timeout_s: int = 15) -> Owner:
    """Verify a Storage token against its stack and return the owning project."""
    if not token:
        raise AuthError("Missing X-StorageApi-Token header")
    url = f"{stack_url}/v2/storage/tokens/verify"
    try:
        response = httpx.get(
            url, headers={"X-StorageApi-Token": token}, timeout=timeout_s
        )
    except httpx.HTTPError as exc:
        logger.warning("Stack %s unreachable: %s", stack_url, exc)
        raise StackUnreachableError(f"Could not reach {stack_url}: {exc}") from exc
    if response.status_code in (401, 403):
        raise AuthError("Storage token rejected by the stack")
    if response.status_code != 200:
        raise StackUnreachableError(
            f"Unexpected {response.status_code} from {stack_url}"
        )
    # A 200 is not a promise of a well-formed body: a captive portal, a
    # misrouted proxy or a schema drift can all answer 200 with something else
    # entirely. Validate the shape before indexing into it, so a surprise
    # becomes a stable AuthError/StackUnreachableError instead of a 500.
    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Non-JSON token-verify response from %s: %s", stack_url, exc)
        raise StackUnreachableError(
            f"{stack_url} answered 200 with a non-JSON token-verify body"
        ) from exc
    if not isinstance(data, dict):
        raise AuthError("Token verify response is not a JSON object")
    owner = data.get("owner")
    if not isinstance(owner, dict):
        raise AuthError("Token verify response has no owner project")
    return Owner(
        stack_url=stack_url,
        project_id=_owner_project_id(owner.get("id")),
        project_name=_owner_project_name(owner.get("name")),
    )


def _owner_project_id(raw: object) -> int:
    """The owner project id as an int, or ``AuthError`` when unusable.

    Keboola stacks return a JSON number, but some return the id as a decimal
    string; both are accepted. Anything else (missing, bool, list, dict,
    non-numeric string) is an authentication failure, never a crash.
    """
    if isinstance(raw, bool) or raw is None:
        raise AuthError("Token verify response has no owner project")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise AuthError(
                "Token verify response has a non-numeric owner project id"
            ) from exc
    raise AuthError("Token verify response has a non-numeric owner project id")


def _owner_project_name(raw: object) -> str:
    """The owner project name, normalized to a string ("" when absent)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    # A structured name is schema drift, not a name — do not stringify a dict
    # or list into the identity we hand every caller.
    raise AuthError("Token verify response has a non-scalar owner project name")
