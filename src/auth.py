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
    data = response.json()
    owner = data.get("owner") or {}
    if "id" not in owner:
        raise AuthError("Token verify response has no owner project")
    return Owner(
        stack_url=stack_url,
        project_id=int(owner["id"]),
        project_name=str(owner.get("name", "")),
    )
