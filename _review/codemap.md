# Phase 0 Codemap, Risk Map, and Threat Model

Scope: static inspection only, excluding `./_review/` from application-source discovery. Paths and line numbers below refer to repository source. No build, test, package, or application scripts were executed.

## System summary

KBC Artifact Hub is a Python/FastAPI service that accepts HTML, Markdown, or a Git repository and publishes versioned HTML artifacts. It uses a caller's Keboola Storage token to identify the caller's project and to create a canonical copy, while service-owned Keboola Storage Files hold artifact envelopes and metadata for public serving. Public reads use an unguessable artifact identifier as a capability, optionally supplemented by a password and signed cookie. Evidence: `src/main.py:175-195`, `src/main.py:1597-1664`, `src/store.py:1-33`.

## Entry points and exposed surfaces

### HTTP/API (the only runtime entry surface found)

`src/main.py` constructs `app = FastAPI(...)` at lines 240-266, mounts the `/docs` Swagger UI and `/openapi.json` schema (lines 244-247), and declares the following routes:

| Surface | Method/path | Handler | Authentication / key inputs |
|---|---|---|---|
| Platform probe | `POST /` | `landing_probe` (`src/main.py:948-957`) | none |
| Human UI | `GET /` | `landing` (`src/main.py:934-945`) | none; forwarded-host headers affect generated base URL |
| Service | `GET /health` | `health` (`src/main.py:960-973`) | none |
| Diagnostic | `GET /health/headers` | `health_headers` (`src/main.py:471-489`) | none; returns received header *names* |
| Agent/discovery | `GET /context` | `context` (`src/main.py:976-1273`) | none |
| Agent document | `GET /skill` | `skill` (`src/main.py:1276-1292`) | none; reads packaged `skills/artifact-publisher/SKILL.md` |
| Public artifact UI | `GET /a/{artifact_id}` | `read_artifact` (`src/main.py:1295-1315`) | URL capability; password header/cookie if protected |
| Browser unlock UI | `POST /a/{artifact_id}/unlock` | `unlock_artifact` (`src/main.py:1318-1350`) | form `password` |
| Public machine read | `GET /a/{artifact_id}/raw` | `read_raw` (`src/main.py:1353-1373`) | URL capability; `X-Artifact-Password` if protected |
| Public source read | `GET /a/{artifact_id}/source` | `read_source` (`src/main.py:1376-1415`) | URL capability; password if protected |
| Public metadata | `GET /a/{artifact_id}/meta` | `read_meta` (`src/main.py:1418-1450`) | URL capability; deliberately available when password-protected |
| Version read | `GET /a/{artifact_id}/v/{version}` | `read_version` (`src/main.py:1453-1476`) | URL capability; owner/author identity additionally needed for proposals |
| Version history UI/API | `GET /a/{artifact_id}/versions?format=json|html` | `read_versions` (`src/main.py:1479-1530`) | URL capability; password if protected |
| Diff UI/API | `GET /a/{artifact_id}/diff/{older}..{newer}?format=html|unified|json` | `read_diff` (`src/main.py:1533-1589`) | URL capability; owner/author identity for proposals |
| Publish | `POST /api/artifacts` | `publish_artifact` (`src/main.py:1597-1664`) | `X-StorageApi-Token` + `X-Storage-Stack` (or local alias `X-Kbc-Stack`) |
| Update | `PUT /api/artifacts/{artifact_id}` | `update_artifact` (`src/main.py:1667-1760`) | same headers; owning project |
| List | `GET /api/artifacts` | `list_artifacts` (`src/main.py:1763-1785`) | same headers |
| Delete artifact | `DELETE /api/artifacts/{artifact_id}` | `delete_artifact` (`src/main.py:1788-1823`) | same headers; owning project |
| Submit version | `POST /api/artifacts/{artifact_id}/versions` | `submit_version` (`src/main.py:1831-1926`) | same headers; contributor permitted only if owner enabled it |
| Promote proposal | `POST /api/artifacts/{artifact_id}/versions/{version}/promote` | `promote_version` (`src/main.py:1929-1992`) | same headers; owning project |
| Delete/withdraw version | `DELETE /api/artifacts/{artifact_id}/versions/{version}` | `delete_version` (`src/main.py:1995-2066`) | owner, or contributing author for own proposal |
| Move head | `PUT /api/artifacts/{artifact_id}/head` | `set_head` (`src/main.py:2069-2146`) | same headers; owning project |

Middleware applies `X-Robots-Tag` and `Cache-Control` to every `/a/` response (`src/main.py:350-357`); the app has a `BackendError` handler (`src/main.py:360-367`).

### Non-HTTP entry points

- CLI commands: **none found**. `pyproject.toml` has no console-script entry point.
- MCP server, MCP tools/resources/prompts: **none found**. `/context` and `/skill` are HTTP documents for agents, not MCP protocol registration (`src/main.py:976-1292`).
- Background jobs, cron, queues, workers, consumers, webhooks: **none found**. Startup lifespan only constructs/hydrates the store (`src/main.py:175-195`).
- SDK/generated client: **none found**; OpenAPI is generated at runtime by FastAPI (`src/main.py:276-322`).
- Browser UI: server-rendered landing, password unlock, and version-history pages (`src/pages.py:410-602`, `src/pages.py:605-766`); Swagger UI is FastAPI-provided `/docs`.

## External-input inventory and trust boundaries

| Input source | Data entering | Primary use / boundary |
|---|---|---|
| HTTP JSON bodies | HTML, Markdown, Git URL/ref/path, transient Git credentials, title, reader password, proposal note, moderation/head controls (`PublishBody`, `UpdateBody`, `VersionBody`, `HeadBody` at `src/main.py:556-765`) | Build, persist, authorization/state changes |
| HTTP path/query/form | artifact IDs, version numbers, diff specification, `format`, browser unlock `password` (`src/main.py:1295-1589`) | Storage lookup, access-control decisions, content rendering |
| HTTP headers/cookies | `X-StorageApi-Token`, stack headers, `X-Artifact-Password`, unlock cookie; `X-Forwarded-Proto`/`X-Forwarded-Host` (`src/main.py:375-419`, `src/main.py:443-455`) | Token verification, tenant identity, reader auth, returned URLs |
| Environment | Required host Storage token, host stack URL, cookie signing secret; cache path and all limits; extra allowed stacks (`src/config.py:12-80`) | Service credentials/configuration and filesystem/network policy |
| Keboola Storage API responses/files | Token verification JSON (`src/auth.py:81-107`); file list metadata, downloaded envelope/meta JSON and canonical content (`src/kbc.py:118-235`, `src/store.py:423-587`, `src/store.py:906-968`) | Principal establishment, artifact metadata/content, persistence state |
| User-selected Git remote | `git_url`, `git_ref`, `git_path`, remote repository content and relative image bytes (`src/builder.py:541-552`, `src/builder.py:578-620`, `src/builder.py:703-796`) | Network clone and artifact construction |
| Local filesystem | Packaged skill document and `pyproject.toml` version (`src/main.py:84-87`, `src/main.py:106-126`); temporary clone/upload/download directories and disk cache | Static response, cache, temporary processing |
| Browser/CDN response context | Rendered Markdown page references Highlight.js and Mermaid assets from jsDelivr/npm (`src/builder.py:93-104`, `src/builder.py:151-330`) | Browser executes/loads third-party assets when viewing Markdown artifacts |

No multipart `UploadFile` implementation or direct uploaded-file endpoint was found; `python-multipart` is used for the password form. No LLM/agent model calls, LLM outputs, RAG retrieval, embedding store, or LLM tool-call dispatch exist in application source.

## Sensitive sinks and high-value operations

| Sink/category | Concrete locations | Inputs reaching it / review focus |
|---|---|---|
| Outbound HTTP authentication call | `httpx.get` in `src/auth.py:81-99` | Caller-provided allowed stack plus Storage token; response determines principal |
| Keboola Storage network I/O | client creation and upload/list/download/delete in `src/kbc.py:118-235` | Host secret or caller token; envelope/meta/raw artifact bytes; permanent deletion |
| Shell/process invocation | `subprocess.run` in `src/builder.py:555-568`; clone construction `src/builder.py:578-620` | User Git URL/ref and optional Git credentials; argv is used without shell; outbound network to Git host |
| Filesystem writes/deletes | temp uploads/downloads (`src/kbc.py:138-219`), Git temp tree (`src/builder.py:804-870`), disk cache (`src/store.py:1037-1075`) | Artifact bytes and Storage downloads; cache path comes from environment |
| HTML/Markdown/template rendering | raw HTML pass-through `src/builder.py:473-481`; Markdown renderer with `html=True` at `src/builder.py:356-367`; `HTMLResponse(envelope.html)` at `src/main.py:1305-1315`, `src/main.py:1363-1373`, `src/main.py:1464-1476` | User/remote-repository content becomes same-origin browser HTML; pages/diff escape their own dynamic strings (`src/pages.py:372-407`, `src/diff.py:136-151`) |
| JSON deserialization/serialization | `json.loads` of Storage files in `src/store.py:327-335`; envelope/meta serialization `src/store.py:145-158`, `src/store.py:226-242` | Storage-controlled bytes parsed into authorization and content objects |
| Authentication/authorization | stack allow-list/verification `src/auth.py:58-107`; tenant owner checks `src/main.py:396-419`, `src/main.py:543-548`; proposed-content check `src/main.py:458-468` | Header token/stack and persisted owner/author keys |
| Password/cookie cryptography | PBKDF2 record creation/check `src/security.py:31-74`; `TimestampSigner` at `src/security.py:77-98`; cookie set at `src/main.py:1341-1348` | Passwords, cookie signing key, signed reader cookie |
| Log/error pathways | configured stdout JSON logger `src/main.py:95-103`; Git secret scrubbing `src/builder.py:502-522`; Storage error token replacement `src/kbc.py:79-84` | URLs, errors, IDs, project identifiers; verify transient-token redaction on all exceptional paths |
| URL generation / proxy boundary | `base_url` trusts forwarded request headers unless configured public base URL (`src/main.py:375-393`) | Header-controlled host/scheme become management response URLs and rendered links |

No SQL/NoSQL query layer, ORM, database migrations, dynamic code evaluation (`eval`/`exec`), pickle-style object deserialization, or YAML parser was found.

## Threat model

### Assets and sensitive-data classes

- **Host-project Storage credential and stack configuration:** required at `src/config.py:12-13`, consumed to hydrate/read/write hub records (`src/main.py:175-185`). A tracked local deployment configuration contains a Keboola Storage API token at `.kbagent/config.json:11` (value intentionally not reproduced). Treat repository checkout, CI logs, and deployment tooling as sensitive contexts.
- **Transient caller and Git credentials:** management `X-StorageApi-Token` and optional `git_token` (`src/main.py:396-419`, `src/main.py:556-616`); their secrecy governs caller project access and private Git access.
- **Artifact data and source:** served HTML, retained Markdown/source, Git provenance, proposal notes, author project identity, and canonical copies (`src/store.py:121-324`). Proposed content is intended to remain private before promotion.
- **Reader-protection material:** plaintext passwords in requests, PBKDF2 salt/hash records persisted in meta, cookie signing secret, and signed unlock cookies (`src/security.py:31-98`, `src/store.py:121-158`).
- **Capability identifiers and public URLs:** `secrets.token_urlsafe(18)` IDs (`src/security.py:26-28`) are access capabilities for unprotected artifacts.
- **Availability/cost:** remote Git clone bandwidth/CPU/disk, KBC Storage calls/files, memory/disk cache, diff computation, and per-process proposal counters.

### Principals, roles, and boundaries

| Principal | Privileges / boundary evidence |
|---|---|
| Anonymous browser/API reader | Can reach all public routes; may read artifact data only through artifact URL capability, with password where configured (`src/main.py:1295-1589`). |
| Capability-URL holder | Same public reader, but knows artifact ID; can see public metadata/history subject to password gate rules. |
| Password holder / signed-cookie holder | Can pass reader gate (`src/main.py:443-455`); not a management principal. |
| Any verified Keboola project caller | Authenticated by stack token verification; can publish a new artifact and potentially submit a proposal when enabled (`src/main.py:396-419`, `src/main.py:1831-1926`). |
| Artifact owner project | Identified by `(normalized stack URL, project id)` (`src/auth.py:45-55`); can update/delete/promote/set head (`src/main.py:543-548`, `src/main.py:1667-2146`). |
| Contributor project | May create only moderated proposals when `accept_versions` is enabled; may withdraw its own proposed version (`src/main.py:1855-1909`, `src/main.py:2028-2037`). |
| Hub service identity | Holds host Storage credential and can read/write/delete serving envelope/meta files (`src/main.py:175-185`, `src/kbc.py:118-235`). No separate application administrator account/RBAC surface was found. |
| Keboola Storage / caller stack | Third party that verifies caller tokens and stores authoritative service data/canonical copies. |
| Git hosting service | Third party chosen by the caller; supplies fetched repository code/content to clone process. |
| Browser/CDN | Browser interprets served artifact HTML; for Markdown output it trusts loaded jsDelivr/npm assets. |

**Verified logical tenant boundary:** owner/author decisions are project-key comparisons and proposed content is gated by those comparisons (`src/main.py:458-468`, `src/main.py:543-548`). **Unknown:** whether KBC tokens can represent different roles inside a project, how token revocation propagates, and whether all Storage File tags/contents are protected from other identities by platform policy.

### Deployment and network assumptions

| Statement | Status | Evidence / limitation |
|---|---|---|
| Application process listens on `0.0.0.0:8050`. | VERIFIED | `keboola-config/supervisord/services/app.conf:1-9`. |
| Nginx listens on `8888` and proxies locally to `127.0.0.1:8050`; body limit is 25 MiB. | VERIFIED | `keboola-config/nginx/sites/default.conf:1-14`. |
| The service is publicly reachable through a Keboola Data App proxy. | ASSUMED | Public routes and deployment documentation support intended use, but repository config does not prove external ingress or its ACLs/TLS. |
| TLS terminates before the app and forwarded headers are set only by a trusted proxy. | ASSUMED | Nginx config forwards `X-Forwarded-*`; `base_url` consumes them (`src/main.py:375-381`), but no trusted-proxy configuration/edge policy is present. |
| The service is logically multi-tenant by Keboola project. | VERIFIED | Tokens establish project identity and project-key authorization is implemented; physical/storage isolation remains third-party dependent. |
| Deployment is single-replica or multi-replica. | UNKNOWN | Proposal quota is in-process, so it is necessarily per process (`src/main.py:139-172`); replica count is not configured. |
| Local cache is disposable/non-authoritative. | VERIFIED in code | Store calls it cache and reconstructs from Storage (`src/store.py:22-33`, `src/store.py:423-447`); actual volume lifetime is deployment-dependent. |
| WAF, API gateway authentication, rate limiting, network egress control, secret manager, backup/retention, centralized audit logging, and runtime sandboxing exist. | ASSUMED | No repository configuration proves any of these external controls. |

### Third-party trust and external controls

- **Keboola Storage and token-verification endpoints:** trusted for identity assertions, file confidentiality/integrity, file deletion, and backend availability. The service does not independently validate a token cryptographically.
- **Git hosts/DNS/network path:** caller selects any HTTPS hostname (`src/builder.py:541-552`), so clone availability and returned content are trusted only as remote input; outbound egress policy is not in repo.
- **jsDelivr/npm/Highlight.js/Mermaid:** Markdown artifacts load executable JavaScript and CSS from external CDNs (`src/builder.py:93-104`, `src/builder.py:313-320`); integrity pinning/SRI is not present in source.
- **Python packages and `uv`:** versions are lower-bounded in `pyproject.toml`, with `uv.lock` present. No CI provenance, dependency advisory scan, or container image pinning was found. Current CVE/advisory status was not assessed.

## Technology inventory

| Area | Inventory / status |
|---|---|
| Runtime/framework | Python `>=3.11`; FastAPI, Pydantic models, Uvicorn (`pyproject.toml`, `src/main.py:33-42`). |
| Authentication and authorization | Keboola API token plus stack header verified over HTTP; project-key owner/contributor authorization; optional password plus signed, `HttpOnly`, `Secure`, `SameSite=Lax` artifact cookie (`src/auth.py:58-107`, `src/main.py:396-468`, `src/main.py:1341-1348`). No OAuth/OIDC, JWT, app-managed API keys, MFA, or user sessions found. |
| MCP | Absent. `/context` and `/skill` are agent-oriented HTTP outputs, not MCP registration. |
| LLM/agents | No LLM provider SDK, prompts to a model, model output, tool-calling agent, background agent, RAG pipeline, vector database, embeddings, or retrieval code found. |
| Content / file ingestion | JSON HTML/Markdown strings; Git clone and relative-image reading. No direct file upload endpoint; browser password `Form` only. Markdown permits raw HTML (`src/builder.py:356-367`). |
| Crypto | `secrets.token_urlsafe`, PBKDF2-HMAC-SHA256 (200,000 iterations), `hmac.compare_digest`, `itsdangerous.TimestampSigner` (`src/security.py:14-98`). |
| Data store | Keboola Storage Files via `kbcstorage`; serialized JSON envelope/meta records; in-memory LRU and disk cache. No SQL/NoSQL database, ORM, schema migration tool, or transaction manager. Legacy envelope migration is an application-format migration (`src/store.py:338-361`). |
| Queues/jobs/webhooks | Absent. |
| Flags / limits | Environment-configured limits and `accept_versions` persisted per artifact; no general feature-flag platform found (`src/config.py:31-48`, `src/store.py:121-138`). |
| UI / docs | Server-rendered HTML pages, FastAPI Swagger/OpenAPI, README, and agent skill document. |
| Tests | Pytest + respx; tests cover API, auth, builder, diff, store, security (`tests/`, `pyproject.toml:18-22`). |
| CI/CD / supply chain | `uv.lock`; `keboola-config/setup.sh` runs `uv sync`; nginx + supervisord deployment config. No GitHub Actions/workflow, Dockerfile/Compose, Kubernetes, Terraform, or other infrastructure-as-code found. |
| Container/proxy | No container image definition; Nginx and supervisord config under `keboola-config/`. |

## Module and directory map (rough LOC)

| Path | Rough LOC | Purpose |
|---|---:|---|
| `src/main.py` | 2,146 | FastAPI lifecycle, schemas, routes, authentication wiring, authorization, and response construction. |
| `src/store.py` | 1,093 | Versioned artifact/meta model, KBC-backed index, cache, persistence, retention, and legacy migration. |
| `src/builder.py` | 892 | HTML/Markdown/Git artifact construction, Git invocation, image inlining, secret scrubbing. |
| `src/pages.py` | 766 | Server-rendered landing/unlock/version-history UI templates. |
| `src/diff.py` | 285 | Version comparison and HTML/text/JSON diff output. |
| `src/kbc.py` | 276 | Keboola Storage backend adapter plus in-memory test backend. |
| `src/auth.py` | 107 | Stack allow-list/resolution and remote caller-token verification. |
| `src/security.py` | 98 | Capability ID, password hashing/check, signed unlock cookies. |
| `src/config.py` | 80 | Environment configuration and service limits. |
| `tests/` | 2,813 | Unit/API tests; `test_api.py` is the largest at 1,167 LOC. |
| `skills/artifact-publisher/` | skill document | Agent-facing published documentation; delivered by `/skill`, not executable MCP. |
| `keboola-config/` | 26 | App startup, Uvicorn supervisor, and Nginx proxy configuration. |
| `docs/superpowers/specs/` | 160 approx. | Product design/specification evidence. |

## Critical user and service flows

1. **Publish HTML or Markdown**
   - `POST /api/artifacts` → `publish_artifact` (`src/main.py:1597-1664`) → `require_owner` → `resolve_stack` / `verify_token` (`src/main.py:396-419`, `src/auth.py:58-107`) → `_build` → `builder.build_from_html` or `build_from_markdown` (`src/main.py:813-867`, `src/builder.py:473-494`) → caller-project canonical `KbcFilesBackend.upload` (`src/main.py:870-889`, `src/kbc.py:138-168`) → `ArtifactStore.create`/`save_meta`/`add_version` (`src/store.py:496-552`) → response URL generation via `base_url`.

2. **Publish from a Git repository (including private repository)**
   - `POST /api/artifacts` body → `_build` (`src/main.py:813-865`) → `build_from_git` (`src/builder.py:804-892`) → `_validate_git_url` → `_clone` → `_run_git` (`src/builder.py:541-620`) → temp checkout entry selection/path containment (`src/builder.py:651-724`) → read/render HTML or Markdown and inline relative images (`src/builder.py:739-796`, `src/builder.py:850-870`) → same canonical + host Storage persistence chain as flow 1. User-controlled remote content becomes browser-served HTML.

3. **Public protected-artifact read and browser unlock**
   - `GET /a/{id}` → `_meta_of` / `ArtifactStore.get_meta` (`src/main.py:537-540`, `src/store.py:481-494`) → `reader_allowed` checks password header/cookie (`src/main.py:443-455`) → `ArtifactStore.get_head`/download/cache (`src/store.py:561-587`, `src/store.py:956-968`) → `HTMLResponse(envelope.html)` (`src/main.py:1305-1315`).
   - Browser alternative: `POST /a/{id}/unlock` form password → PBKDF2 verification → `CookieSigner.make` → signed cookie → redirect (`src/main.py:1318-1350`, `src/security.py:50-98`).

4. **Cross-project contribution and owner promotion**
   - `POST /api/artifacts/{id}/versions` → authenticate caller → load meta → compare owner key and `accept_versions` → in-process quota claim → build/canonical upload → `ArtifactStore.add_version` with `live` or `proposed` (`src/main.py:1849-1926`).
   - `POST /api/artifacts/{id}/versions/{n}/promote` → authenticate/owner check → `ArtifactStore.set_status` rewrites Storage envelope → head resolution (`src/main.py:1944-1992`, `src/store.py:625-643`). Proposal reads additionally pass `may_see` (`src/main.py:458-468`, `src/main.py:1464-1476`).

5. **Startup hydration, public version history, and diff**
   - ASGI lifespan → `ArtifactStore.hydrate` → KBC tag list → in-memory index (`src/main.py:175-195`, `src/store.py:423-447`).
   - `GET /a/{id}/versions` → stored envelope `public_meta` list → JSON or server-rendered picker (`src/main.py:1489-1530`, `src/store.py:589-615`, `src/pages.py:683-766`).
   - `GET /a/{id}/diff/{a}..{b}` → password/proposal checks → `compute_diff` (per-side byte limit) → escaped HTML or unified/JSON response (`src/main.py:1546-1589`, `src/diff.py:89-128`).

## Review priorities and explicit unknowns

Highest-risk review paths are: user HTML/Markdown/Git content to same-origin HTML response; user-controlled Git URL/ref to network/subprocess; caller headers to remote token verification and tenant authorization; Storage JSON to authorization/content state; host and transient credentials to logs/errors/config; and owner/proposal/version mutations under concurrent or multi-replica operation.

Unknown from static repository evidence: external ingress/TLS and forwarded-header trust configuration; Keboola token scopes/revocation, Storage file ACLs, and audit logs; Git egress restrictions/DNS controls; WAF/gateway/rate-limit policy; replica count and persistent-cache lifecycle; monitoring/backups/retention; CI/build provenance; and runtime dependency advisory status.
