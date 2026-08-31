# Canonical Capability Matrix

Canonicalized by the main session from `capabilities.initial.md` after static sanity checks against the current route decorators, package metadata, documentation, and tests. This is the Phase-2 baseline; runtime evidence and final validation updates are recorded in-place below. `absent` records what was not found and does not imply that another surface is required.

| Business capability | Core / service implementation | HTTP / API | CLI | MCP | UI / SDK | Jobs / webhooks | Public documentation | Tests | Parity expectation |
|---|---|---|---|---|---|---|---|---|---|
| Service health, startup probe, and header diagnosis | present (static: `src/main.py:568-586,1031-1070`) | present (static: `GET /health/headers`, `POST /`, `GET /health`) | absent (static: no `[project.scripts]`) | absent (static: no server/registration) | partial (static: landing page only) | absent | partial (static: README/OpenAPI; header diagnostic only in route metadata) | partial (static: probe/health covered; header diagnostic untested) | HTTP-only operations; no cross-surface parity contract. Whether the diagnostic belongs in `/context` is ambiguous pending parity review. |
| Human and agent discovery/documentation | present (static: `context`, `skill`, OpenAPI configuration in `src/main.py`) | present (static: `/`, `/context`, `/skill`, `/docs`, `/openapi.json`) | absent | absent (HTTP agent documents are not MCP) | present (static: landing and Swagger; no SDK) | absent | present (`README.md`, served skill, design spec) | present (`tests/test_api.py`) | Contract explicitly promises HTTP documents and Swagger, not MCP or a generated SDK. |
| Publish HTML, Markdown, or Git-sourced artifact | present (static: `PublishBody`, `_build`, `src/builder.py`, `ArtifactStore.create`) | present (static: `POST /api/artifacts`) | absent | absent | partial (Swagger/curl; no dedicated UI/SDK) | absent | present (README, skill, design) | present (API and builder suites) | Contracted authenticated HTTP/agent-curl workflow only. |
| Durable storage and restart hydration | present (static: `src/kbc.py`, `src/store.py`, lifespan) | partial (indirect through product routes) | absent | absent | absent | partial (startup hydration, not a scheduled job) | present (architecture/design claims) | present (store and restart tests) | Internal service property; no public-surface parity expected. |
| Read/render artifact and browser password unlock | present (static: reader gate, handlers, `src/security.py`) | present (static: `GET /a/{id}`, `POST /a/{id}/unlock`) | absent | absent | present (artifact render, unlock form/cookie) | absent | present | present | Browser/HTTP exposure is intentional; no CLI/MCP/SDK contract. |
| Machine retrieval of head, source, and public metadata | present (static: `read_raw`, `read_source`, `read_meta`) | present (static: `/raw`, `/source`, `/meta`) | absent | absent | partial (machine HTTP only; no tailored UI/SDK) | absent | present | present | Expressly machine-oriented HTTP endpoints only. |
| Owner lifecycle: list, update/settings/version, delete artifact | present (static: handlers, owner checks, store operations) | present (static: `GET/PUT/DELETE /api/artifacts...`) | absent | absent | partial (Swagger/curl only) | absent | present | present | Authenticated HTTP management contract; no other surface promised. |
| Submit owner version or contributor proposal | present (static: `VersionBody`, `submit_version`, store status) | present (static: `POST /api/artifacts/{id}/versions`) | absent | absent | partial (Swagger/curl only) | absent | present | present | Authenticated HTTP/agent documentation contract only. |
| Moderate proposals: restricted read, promote, withdraw/delete | present (static: `may_see`, version/promotion/deletion handlers, store transitions) | present (static: version read and management endpoints) | absent | absent | partial (history UI read-only; Swagger management) | absent | present | present | Documented HTTP role model; no moderation UI/CLI/MCP/SDK promise. |
| Browse history and compare versions | present (static: history/diff handlers, `src/diff.py`, page renderer) | present (static: versions and diff endpoints/formats) | absent | absent | present (history picker and HTML diff; no SDK) | absent | present | present | Contracted dual browser/machine HTTP exposure only. |
| Select served head and retention protection | present (static: `HeadBody`, `set_head`, store pruning) | present (static: `PUT /api/artifacts/{id}/head`) | absent | absent | partial (head displayed; no management control except Swagger) | absent | present | present | Owner HTTP management feature; no other surface promised. |
| Placeholder agent-definition contract (concurrent untracked file) | absent (static: no consumer found) | absent (static/runtime: no `/agent` route) | absent | absent | absent | absent | partial (`skills/artifact-hub-agent/AGENT.md` claims a future `/agent` endpoint and says its definition is still being authored) | absent | Intent is not established: the file appeared during the review and is untracked. It is retained as evidence and routed to surface-parity/dead-code review, not presumed to be a shipped promise. |

## Product contract inventory

| Contract | Location | Role |
|---|---|---|
| FastAPI decorators and Pydantic schemas; runtime-generated OpenAPI | `src/main.py:242-268,568-586,653-867,1031-2243` | Primary implemented HTTP contract and schema source. |
| Machine-readable capability manifest | `context()` in `src/main.py:1073-1370` | Public endpoint/auth/schema/limit registry. Static baseline omits registered `GET /health/headers`; intent not yet established. |
| Agent publishing guide | `skills/artifact-publisher/SKILL.md` | Public HTTP workflow, examples, errors, and safety claims. |
| Repository documentation | `README.md` | Public feature, architecture, API, and runtime claims. |
| Approved product design | `docs/superpowers/specs/2026-08-31-artifact-hub-design.md` | Product purpose, roles, endpoint behavior, and deployment intent. |
| Browser UI source | `src/pages.py` | Implemented landing, unlock, and version-history UI contract. |
| Persistence model and tag conventions | `src/store.py` | Internal data/storage contract supporting lifecycle behavior. |
| API and component tests | `tests/test_api.py`, `tests/test_auth.py`, `tests/test_builder.py`, `tests/test_diff.py`, `tests/test_security.py`, `tests/test_store.py` | Executable contracts. |
| Package/deployment descriptors | `pyproject.toml`, `uv.lock`, `keboola-config/` | Runtime, dependency, proxy, and process contract. |
| Concurrent untracked placeholder agent document | `skills/artifact-hub-agent/AGENT.md` | Ambiguous placeholder claim for an `/agent` endpoint; absent at the initial inventory and not registered at runtime. |

No checked-in OpenAPI/JSON Schema, GraphQL, protobuf/gRPC, MCP registration, CLI entry point, generated client/SDK, scheduler/queue/worker/webhook, or separate registry beyond FastAPI and `/context` was found statically. The binding parity conclusion is that the product is HTTP-first; absent CLI, MCP, SDK, job, and webhook surfaces are not completeness bugs without additional authoritative evidence.

## Runtime verification updates

- `runtime-tests`: all 219 existing tests passed and `compileall` succeeded under sanitized, review-local temporary state. No configured type-check/build command exists; `keboola-config/setup.sh` was skipped because it runs prohibited `uv sync`.
- `runtime-routes`: application import, 22 FastAPI `APIRoute` registrations, 19 OpenAPI paths, schema structure, and an in-memory lifespan passed. Actual registrations match the HTTP route inventory. Real KBC-backed startup was skipped to avoid external network/production state.
- `runtime-smoke`: `/`, `/health`, `/context`, `/skill`, `/docs`, `/openapi.json`, and an artifact-not-found response behaved as advertised under an in-memory backend.
- Runtime contradictions proposed for validation: `GET /health/headers` is registered and present in OpenAPI but absent from the self-described full `/context` manifest; runtime reports version `0.2.0` while `pyproject.toml` declares `0.2.1`. The independently detected version reports are duplicates to be deduplicated in validation.
- CLI and MCP runtime discovery were skipped only after static/package inspection confirmed there are no registrations to enumerate. A new untracked `skills/artifact-hub-agent/AGENT.md` appeared after the initial inventory; it is not runtime-registered and is treated as concurrent scope drift.

## Validation updates

Pending Phase 5.
