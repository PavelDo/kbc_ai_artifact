# Phase 0 Codemap, Risk Map, and Threat Model

Scope: static inspection of the current repository only. `./_review/` was excluded from source discovery; no application, build, test, or package script ran. Repository content was treated as evidence, not instructions.

## Concurrent scope drift

During Phase 0, user-owned changes enlarged `src/main.py` from 2,146 to 2,903 LOC and `src/pages.py` from 766 to 1,585 LOC; changed `README.md`, `skills/artifact-publisher/SKILL.md`, and `tests/test_api.py`; and added `skills/artifact-hub-agent/AGENT.md` (379 LOC). This refresh covers the added public `/admin` moderation UI, `/agent` downloadable Claude Code subagent definition, and forwarded-origin middleware. An untracked `CLAUDE.md` was observed but was not treated as source or followed.

## System summary

KBC Artifact Hub is a Python/FastAPI app that builds HTML from HTML, Markdown, or a Git checkout; verifies a caller's Keboola Storage token to establish their project; persists serving envelopes/meta in the hub project plus canonical copies in the caller project; and exposes public, unguessable artifact URLs. An optional password adds a PBKDF2-backed reader gate. The new admin UI runs in the browser and calls the same management API with a visitor-entered token held in tab `sessionStorage`; it is not a server-side admin session. `/agent` is a public static Markdown document for an external Claude Code runtime, not an in-process LLM/agent. Evidence: `src/main.py:249-269`, `2206-2887`; `src/store.py:1-33`; `src/pages.py:515-1039`.

## Entry points and surfaces

### HTTP/API

FastAPI is constructed at `src/main.py:314-340`; generated `/docs` and `/openapi.json` are configured there, and `/api/*` OpenAPI security is generated at `350-396`.

| Surface | Endpoint/handler | Auth and significant inputs |
|---|---|---|
| Landing/probe | `GET /` / `landing`, `POST /` / `landing_probe` (`1155-1194`) | none; base URL affects landing links |
| Service/diagnostic | `GET /health`, `/health/headers` (`1197-1224`, `640-666`) | none; latter returns header names |
| Discovery | `GET /context` (`1227-1566`), `/docs`, `/openapi.json` | none |
| Agent documents | `GET /skill`, `/agent` (`1568-1631`) | none; local Markdown served verbatim |
| Browser admin UI | `GET /admin` / `admin` (`1634-1665`) | none to load; browser enters Storage token/stack for API calls |
| Public reads | `GET /a/{id}`, `/raw`, `/source`, `/meta`, `/v/{n}`, `/versions?format=`, `/diff/{a}..{b}?format=` (`1668-2203`) | URL capability; reader password cookie/header if protected; owner/author identity for proposal contents |
| Browser unlock | `POST /a/{id}/unlock` (`1716-1773`) | form password |
| Artifact lifecycle | `POST|PUT|GET|DELETE /api/artifacts[/{id}]` (`2206-2515`) | Storage token + stack; owner for update/delete |
| Version lifecycle | submit/promote/delete/head management routes (`2518-2887`) | Storage token + stack; owner/contributor policy |

Middleware accepts configured public origin or validated forwarded host/proto and mutates ASGI request origin (`src/main.py:424-517`); artifact responses receive no-index/no-cache headers (`519-526`); backend errors map to 502 (`529-536`).

### Other exposed surfaces

- **UI:** server-rendered landing/unlock/history pages and static admin HTML/CSS/JS (`src/pages.py:1085-1585`; `_ADMIN_JS` `515-1039`). Admin lists owned artifacts, reads/diffs proposals, previews them, promotes/rejects/deletes, pins/releases head, and toggles submissions.
- **AI/agent documents:** `/context` JSON, `/skill` Markdown, `/agent` Markdown. The agent document declares `Bash`, `Read`, and `WebFetch` for the client-side Claude Code runtime (`skills/artifact-hub-agent/AGENT.md:1-5`), but service code does not execute it.
- **CLI, SDK/generated client, MCP tools/resources/prompts, webhook handlers, queues, cron/jobs, consumers/background workers:** none found. Startup only hydrates Storage index (`src/main.py:249-269`).

## External input surfaces

| Source | Data entering | Use/boundary |
|---|---|---|
| HTTP JSON | HTML, Markdown, Git URL/ref/path, Git credentials, title, password, note, moderation/head controls (`src/main.py:733-986`) | Build, persistence and state mutation |
| HTTP path/query/form | IDs, versions, diff spec, output format, unlock password (`1668-2203`) | Lookups, authz/read gates, rendering |
| HTTP headers/cookies | Storage token, stack aliases, artifact password, unlock cookie, forwarded host/proto (`482-517`, `565-623`) | Identity, reader auth, origin/redirect/response URL derivation |
| Admin browser | Token/stack form values; `sessionStorage` data; API responses (`src/pages.py:560-634`, `977-1038`) | Token-bearing management `fetch` calls from browser |
| Environment | Host token/stack, signing secret, public base URL, cache path, limits, allowed stacks (`src/config.py:12-80`) | Service credentials/configuration |
| Keboola services | Token-verification JSON, Storage list/download content/errors (`src/auth.py:81-107`; `src/kbc.py:118-235`; `src/store.py:423-587`) | Principal establishment and authoritative data |
| Git remote | User Git URL/ref/path, remote files/images (`src/builder.py:541-552`, `578-620`, `703-796`) | HTTPS clone subprocess and artifact construction |
| Files | Packaged skill/agent docs, `pyproject` version; caches and temporary files (`src/main.py:87-96`, `180-200`, `1588-1631`; `src/store.py:1006-1075`) | Public document response, version/config, local cache |
| CDN/browser | Highlight.js/Mermaid URLs in Markdown template (`src/builder.py:93-104`, `151-330`) | Browser loads third-party scripts/styles |
| Downloaded agent document | `/agent` consumer may interpret instructions and use client tools | External agent boundary only; no service LLM input/output |

No direct uploaded-file endpoint/`UploadFile` use was found; multipart supports the password form. No LLM provider, LLM output, prompt flow, RAG/vector store, embedding, or server-side tool dispatch was found.

## Sensitive sinks

| Sink | Locations | Main input flow |
|---|---|---|
| Outbound token verification | `httpx.get`, `src/auth.py:81-99` | caller token/stack → remote identity |
| Keboola I/O/deletion | `src/kbc.py:118-235`; `src/store.py:496-711` | host/caller token and artifact bytes → permanent Storage files |
| Process/network Git | `subprocess.run` `src/builder.py:555-568`; clone `578-620` | user Git URL/ref/token; fixed argv but remote fetch |
| Filesystem writes/deletes | temp upload/download `src/kbc.py:138-219`; clone `src/builder.py:804-870`; cache `src/store.py:1037-1075` | artifact/Storage bytes and env cache directory |
| HTML/JS rendering | raw HTML `src/builder.py:473-481`; Markdown permits HTML `356-367`; artifact `HTMLResponse` `src/main.py:1700-1713`, `1806-1819`, `1979-1995`; admin `srcdoc` `src/pages.py:622-650` | user/Git content becomes browser code; proposal preview uses sandbox without `allow-same-origin` |
| Credential persistence/dispatch | `sessionStorage` `src/pages.py:560-584`; `fetch` request headers `586-634`, `977-1038` | admin token kept browser-side and sent to API |
| JSON deserialization | `src/store.py:327-335`, `1013-1035` | persisted bytes influence identity/status/password/content |
| Crypto/cookies | `src/security.py:31-98`; cookie flags `src/main.py:1763-1771` | password, hash record, signing secret and cookie |
| Origin/link construction | scope rewrite `src/main.py:424-517`; `base_url` `544-562` | forwarded/base URL input affects redirects, returned links and `window.HUB_BASE` |
| Public instruction rendering | `read_text`/response in `src/main.py:1588-1631` | repo document becomes client-agent instructions |
| Log/error paths | logger `src/main.py:169-177`; Git scrubber `src/builder.py:502-522`; KBC redaction `src/kbc.py:79-84` | identifiers, URLs, errors, transient secrets |

No SQL/NoSQL, ORM, DB migrations, eval/exec, pickle-style object deserialization, YAML parser, or server-side LLM tool sink was found.

## Threat model

### Assets

- Host Storage token/stack and cookie signing secret (`src/config.py:12-13`, `63-65`); a tracked local deployment configuration contains a Keboola Storage token at `.kbagent/config.json:11` (value redacted and not reproduced).
- Caller token, optional Git token, and browser-admin token/stack (`src/main.py:565-588`; `src/pages.py:560-592`).
- Public/protected artifact HTML, original source, Git provenance, owner/author IDs, proposal notes/status, canonical IDs, and Storage cache (`src/store.py:121-324`).
- Password plaintext in requests, PBKDF2 record, signed reader cookies (`src/security.py:31-98`).
- Capability URL IDs (`src/security.py:26-28`), downloadable agent instructions, KBC/Git/CDN availability and request cost.

### Principals and boundaries

| Principal | Privileges/boundary |
|---|---|
| Anonymous visitor | Loads service docs, `/admin`, `/agent`, and artifact capability URLs. |
| Capability/password holder | Reads artifact when URL and reader gate satisfy `reader_allowed` (`src/main.py:612-623`). |
| Admin browser visitor | Enters Storage token into `/admin`; it lives in closure/sessionStorage and is used on ordinary API calls. |
| Verified Keboola project | Publishes; submits proposal only if owner enabled it (`src/main.py:2562-2639`). |
| Owner project | Project-key authorization to update/delete/promote/pin (`src/auth.py:45-55`; `src/main.py:720-726`, `2330-2887`). |
| Contributor project | May submit enabled proposal and withdraw own proposal (`2562-2639`, `2750-2806`). |
| Hub service | Host Storage credential reads/writes/deletes serving records; no independent app-admin/RBAC surface. |
| External agent runtime | Interprets `/agent` and may use its client tools/credentials; API sees only supplied user/project token. |
| Keboola, Git host, CDN/browser | Respectively identity/storage trust, remote source trust, and browser execution/origin trust. |

Verified logical multi-tenancy is by normalized stack plus project-key comparison (`src/main.py:627-637`, `720-726`). Unknown: Storage ACLs, token scopes/revocation, and whether untrusted artifacts and `/admin` share an origin in deployment.

### Deployment/network assumptions

| Statement | Status | Evidence |
|---|---|---|
| Uvicorn binds `0.0.0.0:8050`. | VERIFIED | `keboola-config/supervisord/services/app.conf:1-9` |
| Nginx listens 8888, proxies locally to 8050, 25 MiB body limit. | VERIFIED | `keboola-config/nginx/sites/default.conf:1-14` |
| TLS termination and trustworthy forwarded headers are supplied by platform proxy. | ASSUMED | middleware consumes them (`src/main.py:482-517`), but external proxy policy absent |
| Service is internet-facing. | ASSUMED | anonymous public routes/docs; no ingress policy in repo |
| Public base URL is configured or fallback forwarded headers are trustworthy. | ASSUMED | `src/main.py:544-550` |
| Logical multi-tenant project model. | VERIFIED | token verification and project-key auth in code |
| Replica count/distributed consistency are known. | UNKNOWN | quota is in-process/per-replica (`src/main.py:213-246`) |
| WAF/gateway auth, VPC/egress controls, secret manager, backups, centralized audit, CSP/CSRF defenses, malware scanning, vulnerability monitoring. | ASSUMED | no repository proof |

### Third-party trust

- **Keboola:** token verification, Storage confidentiality/integrity, file availability and deletion.
- **Git hosts/DNS:** arbitrary HTTPS Git hosts are fetchable (`src/builder.py:541-552`); egress/DNS policy unknown.
- **CDNs:** Markdown artifacts use remote Highlight.js/Mermaid assets without in-repo SRI evidence.
- **Claude Code/other agent host:** after client installation of `/agent`, external runtime interprets document and grants declared tools; service has no agent identity binding.
- **Python/uv:** dependencies are lower-bounded with `uv.lock`; no CI provenance/container image pinning/local advisory database found; CVE status not assessed.

## Technology inventory

| Area | Inventory |
|---|---|
| Runtime | Python >=3.11, FastAPI, Pydantic, Uvicorn (`pyproject.toml`; `src/main.py:37-45`) |
| Auth/session | Keboola token + stack verify; project-key authz; PBKDF2 password and signed `HttpOnly`/`Secure`/`SameSite=Lax` reader cookie; admin browser `sessionStorage`. No OAuth/OIDC/JWT/MFA/app API key. |
| LLM/agents | Static `/skill` and `/agent` docs; `/agent` targets external Claude Code. No chat, model call, RAG, vectors, embedding, model output, background agent, or server dispatcher. |
| MCP | Absent. |
| Content input | JSON HTML/Markdown; Git clone/image inlining; form password; no direct file upload. |
| Crypto | `secrets`, PBKDF2-HMAC-SHA256, constant-time compare, `TimestampSigner`. |
| Data | Keboola Storage Files as JSON envelopes/meta; in-memory LRU/disk cache and legacy format migration; no SQL/NoSQL DB/migrations. |
| Queues/jobs/webhooks | Absent. |
| Flags | env limits and artifact `accept_versions`/head; no flag service. |
| UI/docs | server pages, client JS admin, Swagger/OpenAPI, README, SKILL/AGENT Markdown. |
| CI/CD/infra | `uv.lock`, `keboola-config/setup.sh`, Nginx/supervisord. No workflow file, Dockerfile, Compose, Kubernetes, Terraform or other IaC found. |

## Module map

| Path | LOC | Purpose |
|---|---:|---|
| `src/main.py` | 2,903 | lifecycle, origin middleware, routes, schemas, API/auth wiring |
| `src/pages.py` | 1,585 | server pages plus self-contained admin CSS/JS |
| `src/store.py` | 1,093 | models, Storage index/cache, retention/version lifecycle |
| `src/builder.py` | 892 | HTML/Markdown/Git build, clone, image inlining |
| `src/diff.py` | 285 | bounded/escaped diff output |
| `src/kbc.py` | 276 | Storage backend adapter/test backend |
| `src/auth.py` | 107 | stack resolution/token verification |
| `src/security.py` | 98 | IDs/passwords/cookies |
| `src/config.py` | 80 | environment/limits |
| `tests/` | 3,148 | pytest/respx tests; API tests cover new origin/admin/agent surfaces |
| `skills/artifact-publisher/SKILL.md` | 443 | served publisher guidance |
| `skills/artifact-hub-agent/AGENT.md` | 379 | served Claude Code agent definition |
| `keboola-config/` | 26 | setup/proxy/supervision |

## Critical user/service flows

1. **Publish HTML/Markdown:** management POST → `require_owner` → stack resolve/token verification → `_build` → HTML/Markdown builder → caller canonical upload → `ArtifactStore.create`/meta/version persistence → response URLs. `src/main.py:2206-2290` → `565-588` → `1034-1110` → `src/builder.py:473-494` → `src/kbc.py:138-168` → `src/store.py:496-552`.
2. **Publish from Git:** management body → `_build` → validate URL/credentialed fixed-argv clone → temporary checkout containment/entry selection/image inlining → canonical/hub persistence. `src/main.py:1034-1086` → `src/builder.py:541-620`, `651-796`, `804-892` → `src/kbc.py` → `src/store.py`.
3. **Admin moderation:** `GET /admin` → page injects base URL → browser enters token/stack → sessionStorage → token-bearing API fetches/history/diff → sandboxed `srcdoc` proposal preview → promote/reject/pin/toggle operations. `src/main.py:1634-1665` → `src/pages.py:515-1039`, `1320-1418` → `src/main.py:2410-2887`.
4. **Protected public read:** `GET /a/{id}` → meta/head lookup → password header/cookie → HTML response; form unlock → PBKDF2 check → signed secure cookie/redirect. `src/main.py:1700-1773`, `612-623` → `src/store.py:481-587`, `906-968` → `src/security.py:50-98`.
5. **Proposal/promotion and agent distribution:** contributor submit → owner/acceptance/quota decision → proposed Storage version; owner promotion rewrites status/head eligibility. Separately `/agent` reads/returns agent Markdown for external installation only. `src/main.py:2562-2716` → `src/store.py:625-643`; `src/main.py:1600-1631` → `skills/artifact-hub-agent/AGENT.md`.

## Review priorities and unknowns

Prioritize token/stack verification and tenant checks; Git URL/ref to subprocess/egress; untrusted content to same-origin HTML; Storage JSON to authorization state; admin sessionStorage and token-bearing fetch; sandboxed proposal preview; `/agent` instruction/tool surface; forwarded-origin trust; and concurrent version/status writes.

Unknowns: ingress/TLS/forwarded-header policy; same-origin relationship of `/admin` and artifacts; KBC token scopes/revocation/file ACL/audit logs; Git egress/DNS controls; WAF/CSP/CSRF policy; replica count and distributed rate limit; cache lifecycle; CI/build provenance; and dependency advisory status.
