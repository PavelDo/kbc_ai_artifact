# Coverage Ledger

Initialized in Phase 2. Every discovered public entry point, business capability, sensitive sink, trust boundary, and critical flow starts `NOT_REVIEWED`. The main session is the sole writer and will update assignments/status after runtime verification, detector review, and final validation.

| ID | Kind | Item | Assigned detector(s) | Files in scope | Status | Gap reason |
|---|---|---|---|---|---|---|
| E01 | entry point | `GET /` landing | runtime-smoke, runtime-tests | `src/main.py`, `src/pages.py` | PARTIAL | Runtime 200 and tests pass; detector review pending |
| E02 | entry point | `POST /` platform probe | runtime-tests, runtime-routes | `src/main.py` | PARTIAL | Runtime registration and tests pass; detector review pending |
| E03 | entry point | `GET /health` | runtime-smoke, runtime-tests | `src/main.py` | PARTIAL | Runtime works but version contradiction proposed; detector review pending |
| E04 | entry point | `GET /health/headers` | runtime-routes | `src/main.py` | PARTIAL | Runtime works and OpenAPI includes it; `/context` omission proposed; no direct test |
| E05 | entry point | `GET /context` | runtime-smoke, runtime-routes, runtime-tests | `src/main.py` | PARTIAL | Runtime works; manifest omission/version contradiction proposed |
| E06 | entry point | `GET /skill` | runtime-smoke, runtime-routes, runtime-tests | `src/main.py`, `skills/artifact-publisher/SKILL.md` | PARTIAL | Runtime and tests pass; parity/security review pending |
| E07 | entry point | `GET /a/{id}` | runtime-smoke, runtime-tests | `src/main.py`, `src/store.py`, `src/security.py` | PARTIAL | Not-found smoke and full tests pass; security/flow review pending |
| E08 | entry point | `POST /a/{id}/unlock` | runtime-tests | `src/main.py`, `src/security.py`, `src/pages.py` | PARTIAL | Full tests pass; security/flow review pending |
| E09 | entry point | `GET /a/{id}/raw` | runtime-tests, runtime-routes | `src/main.py`, `src/store.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E10 | entry point | `GET /a/{id}/source` | runtime-tests, runtime-routes | `src/main.py`, `src/store.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E11 | entry point | `GET /a/{id}/meta` | runtime-tests, runtime-routes | `src/main.py`, `src/store.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E12 | entry point | `GET /a/{id}/v/{version}` | runtime-tests, runtime-routes | `src/main.py`, `src/store.py` | PARTIAL | Registered and tests pass; authz/flow review pending |
| E13 | entry point | `GET /a/{id}/versions` | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, `src/pages.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E14 | entry point | `GET /a/{id}/diff/{older}..{newer}` | runtime-tests, runtime-routes | `src/main.py`, `src/diff.py`, `src/store.py` | PARTIAL | Registered and tests pass; limit/flow review pending |
| E15 | entry point | `POST /api/artifacts` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/builder.py`, `src/store.py`, `src/kbc.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E16 | entry point | `PUT /api/artifacts/{id}` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/builder.py`, `src/store.py`, `src/kbc.py` | PARTIAL | Registered and tests pass; security/flow review pending |
| E17 | entry point | `GET /api/artifacts` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/store.py` | PARTIAL | Registered and tests pass; authz/flow review pending |
| E18 | entry point | `DELETE /api/artifacts/{id}` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/store.py` | PARTIAL | Registered and tests pass; integrity/flow review pending |
| E19 | entry point | `POST /api/artifacts/{id}/versions` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/builder.py`, `src/store.py`, `src/kbc.py` | PARTIAL | Registered and tests pass; abuse/business-flow review pending |
| E20 | entry point | `POST /api/artifacts/{id}/versions/{version}/promote` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/store.py` | PARTIAL | Registered and tests pass; integrity/business-flow review pending |
| E21 | entry point | `DELETE /api/artifacts/{id}/versions/{version}` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/store.py` | PARTIAL | Registered and tests pass; integrity/business-flow review pending |
| E22 | entry point | `PUT /api/artifacts/{id}/head` | runtime-tests, runtime-routes | `src/main.py`, `src/auth.py`, `src/store.py` | PARTIAL | Registered and tests pass; integrity/business-flow review pending |
| E23 | entry point | FastAPI `/docs` and `/openapi.json` | runtime-smoke, runtime-routes, runtime-tests | `src/main.py`, generated schema | PARTIAL | Runtime schema and UI pass; surface-parity review pending |
| C01 | capability | Service health/probe/header diagnosis | runtime-smoke, runtime-routes, runtime-tests | `src/main.py`, docs, tests | PARTIAL | Runtime works; two contract contradictions proposed |
| C02 | capability | Human/agent discovery and documentation | runtime-smoke, runtime-routes, runtime-tests | `src/main.py`, `src/pages.py`, README, skill, tests | PARTIAL | Runtime/docs route checks pass; parity review pending |
| C03 | capability | Publish HTML/Markdown/Git artifact | runtime-tests, runtime-routes | `src/main.py`, `src/builder.py`, `src/store.py`, `src/kbc.py` | PARTIAL | Tests/registration pass; external backends and detector review pending |
| C04 | capability | Durable storage and restart hydration | runtime-tests, runtime-routes | `src/store.py`, `src/kbc.py`, lifespan, tests | PARTIAL | In-memory lifespan/tests pass; real KBC backend skipped |
| C05 | capability | Render/read/unlock artifact | runtime-tests, runtime-smoke | `src/main.py`, `src/security.py`, `src/pages.py`, `src/store.py` | PARTIAL | Tests/not-found smoke pass; security review pending |
| C06 | capability | Machine head/source/meta retrieval | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, docs, tests | PARTIAL | Registration/tests pass; security/parity review pending |
| C07 | capability | Owner lifecycle list/update/delete | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, `src/auth.py` | PARTIAL | Registration/tests pass; authz/integrity review pending |
| C08 | capability | Submit owner version or contributor proposal | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, `src/auth.py`, `src/builder.py` | PARTIAL | Registration/tests pass; abuse/business review pending |
| C09 | capability | Proposal visibility, promotion, withdrawal | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, `src/auth.py` | PARTIAL | Registration/tests pass; authz/business review pending |
| C10 | capability | Version history and diff | runtime-tests, runtime-routes | `src/main.py`, `src/store.py`, `src/diff.py`, `src/pages.py` | PARTIAL | Registration/tests pass; limit/flow review pending |
| C11 | capability | Head selection and retention protection | runtime-tests, runtime-routes | `src/main.py`, `src/store.py` | PARTIAL | Registration/tests pass; integrity/business review pending |
| C12 | capability | Placeholder agent-definition `/agent` claim in concurrent untracked file | surface-parity, dead-code (pending) | `skills/artifact-hub-agent/AGENT.md`, route registries, docs | NOT_REVIEWED | File appeared after initial inventory; no route/runtime registration found |
| S01 | sensitive sink | Caller-token verification over outbound HTTP | unassigned | `src/auth.py`, `src/main.py`, `src/config.py` | NOT_REVIEWED | Initial state |
| S02 | sensitive sink | KBC authenticated upload/list/download/delete | unassigned | `src/kbc.py`, `src/store.py`, `src/main.py` | NOT_REVIEWED | Initial state |
| S03 | sensitive sink | Git subprocess and remote clone | unassigned | `src/builder.py`, request schemas | NOT_REVIEWED | Initial state |
| S04 | sensitive sink | Temporary and cache filesystem writes/deletes | unassigned | `src/kbc.py`, `src/builder.py`, `src/store.py`, `src/config.py` | NOT_REVIEWED | Initial state |
| S05 | sensitive sink | User HTML/Markdown to same-origin HTML response | unassigned | `src/builder.py`, `src/main.py`, `src/pages.py`, `src/diff.py` | NOT_REVIEWED | Initial state |
| S06 | sensitive sink | Storage JSON deserialization into auth/content state | unassigned | `src/store.py`, `src/kbc.py` | NOT_REVIEWED | Initial state |
| S07 | sensitive sink | Owner/contributor authentication and authorization | unassigned | `src/auth.py`, `src/main.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| S08 | sensitive sink | Password hashing and signed unlock cookies | unassigned | `src/security.py`, `src/main.py` | NOT_REVIEWED | Initial state |
| S09 | sensitive sink | Log/error handling and forwarded-host URL generation | unassigned | `src/main.py`, `src/auth.py`, `src/builder.py`, `src/kbc.py` | NOT_REVIEWED | Initial state |
| T01 | trust boundary | Anonymous reader to public artifact routes | unassigned | `src/main.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| T02 | trust boundary | Capability-URL/password holder to content | unassigned | `src/main.py`, `src/security.py` | NOT_REVIEWED | Initial state |
| T03 | trust boundary | Verified caller project to owner/contributor privileges | unassigned | `src/auth.py`, `src/main.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| T04 | trust boundary | Hub service identity to host KBC project | unassigned | `src/config.py`, `src/kbc.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| T05 | trust boundary | Caller-selected stack/token to remote identity response | unassigned | `src/auth.py`, `src/config.py` | NOT_REVIEWED | Initial state |
| T06 | trust boundary | Caller-selected Git host/content to build/render | unassigned | `src/builder.py`, `src/main.py` | NOT_REVIEWED | Initial state |
| T07 | trust boundary | KBC Storage files/metadata to hydrated authority | unassigned | `src/kbc.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| T08 | trust boundary | Reverse proxy forwarded headers to returned URLs | unassigned | `src/main.py`, nginx config | NOT_REVIEWED | Initial state |
| T09 | trust boundary | Browser to executable artifact/CDN content | unassigned | `src/builder.py`, `src/main.py` | NOT_REVIEWED | Initial state |
| T10 | trust boundary | Process-local quota/cache state across replicas | unassigned | `src/main.py`, `src/store.py`, deployment config | NOT_REVIEWED | Initial state |
| F01 | critical flow | Publish HTML/Markdown through auth, build, canonical copy, and host store | unassigned | `src/main.py`, `src/auth.py`, `src/builder.py`, `src/kbc.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| F02 | critical flow | Publish Git repository through URL/ref/path, clone, render, and persist | unassigned | `src/main.py`, `src/builder.py`, `src/kbc.py`, `src/store.py` | NOT_REVIEWED | Initial state |
| F03 | critical flow | Protected public read and browser unlock | unassigned | `src/main.py`, `src/security.py`, `src/store.py`, `src/pages.py` | NOT_REVIEWED | Initial state |
| F04 | critical flow | Cross-project proposal, owner promotion, contributor withdrawal | unassigned | `src/main.py`, `src/auth.py`, `src/store.py`, `src/builder.py` | NOT_REVIEWED | Initial state |
| F05 | critical flow | Startup hydration, version history, and diff | unassigned | `src/main.py`, `src/store.py`, `src/kbc.py`, `src/diff.py`, `src/pages.py` | NOT_REVIEWED | Initial state |
