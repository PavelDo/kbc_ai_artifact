# KBC Artifact Hub — Design

Date: 2026-08-31
Status: approved (user: "vyrob to celé, nasaď do Kebooly a vyzkoušej")

## Purpose

A Keboola App (Python/JS app, FastAPI) that hosts HTML+JS artifacts under public
URLs. Anyone holding **any** Keboola Storage API token (any stack) can publish a
document; the service returns an unguessable ID and a public URL. Humans read
the rendered page; machines fetch raw/source/meta over a simple API. Content is
canonically stored in the author's own KBC project; the service keeps a serving
copy in its host project.

## Key decisions

1. **Read path = cached copy.** At publish time the service stores:
   - a canonical copy of the built HTML in the *author's* project
     (Storage File, tags `kbc-artifact`, `artifact-id-<id>`), uploaded with the
     author's token, and
   - a serving envelope (JSON: html + metadata + password hash) in the *host*
     project (tags `artifact-hub`, `artifact-id-<id>`, `artifact-owner-<key>`).
   The service never persists client tokens; they are used only during the
   publish request.
2. **Restart safety.** The app container has no permanent disk. The single
   source of truth is Storage Files of the host project. On startup the service
   rebuilds its in-memory index by listing files tagged `artifact-hub`. Local
   disk is only an LRU cache.
3. **URLs are capabilities.** `/a/<unguessable-id>` (token_urlsafe, 24 chars).
   No public listing exists. `X-Robots-Tag: noindex` on all artifact responses.
4. **Optional password.** PBKDF2-SHA256 hash stored in the envelope. Web
   readers unlock via a form; a signed cookie scoped to `path=/a/<id>` keeps
   the session. Machines send `X-Artifact-Password` header.
5. **Publish inputs**: `html` (served as-is), `markdown` (rendered by the
   built-in template: GFM tables, task lists, mermaid fences, highlight.js),
   or `git_url` (+ optional `git_ref`, `git_path`): shallow clone, pick
   `git_path` → `index.html` → `README.md` → single `*.html`; markdown gets
   rendered; relative local images are inlined as data URIs (self-contained
   HTML, following the agnes-implementation-package pattern).
6. **Auth** (management API only): headers `X-StorageApi-Token` +
   `X-Kbc-Stack` (alias like `us`, `gcp-eu` or a full `https://…keboola.com`
   URL). Verified against `GET {stack}/v2/storage/tokens/verify`. Ownership =
   (normalized stack, project id); update/delete require a token from the
   owning project.
7. **Multi-cloud upload** handled by `kbcstorage` (official sapi-python-client)
   — covers S3/GCS/Azure prepare+upload on any stack.
8. **Agent-first**: `GET /context` returns a machine-readable manifest;
   `GET /skill` serves a SKILL.md teaching agents how to author rich content
   and publish it (cf-ng pattern).

## Endpoints

Public:
- `GET /` landing page (docs); `POST /` returns 200 (platform health check)
- `GET /context` JSON manifest
- `GET /skill` SKILL.md (text/markdown)
- `GET /a/{id}` rendered artifact or unlock form
- `POST /a/{id}/unlock` password form target, sets signed cookie
- `GET /a/{id}/raw` raw HTML (machine; password via header if protected)
- `GET /a/{id}/source` original source (markdown or html)
- `GET /a/{id}/meta` public metadata JSON (no owner details)
- `GET /health` liveness + index stats

Authenticated (`X-StorageApi-Token` + `X-Kbc-Stack`):
- `POST /api/artifacts` publish `{html | markdown | git_url[, git_ref, git_path], title?, password?}` → `{id, url, …}`
- `PUT /api/artifacts/{id}` update content and/or password (owner only)
- `GET /api/artifacts` list caller's project artifacts
- `DELETE /api/artifacts/{id}` delete serving copy (owner only)

## Components

- `src/config.py` — env-driven settings; required vars fail fast at startup
  (`HUB_STORAGE_TOKEN`, `HUB_STACK_URL`, `HUB_SECRET_KEY`); limits overridable.
- `src/auth.py` — stack alias resolution + allowlist (`*.keboola.com` https
  only), token verification (httpx), returns owner identity.
- `src/kbc.py` — `FilesBackend` protocol + `KbcFilesBackend` (kbcstorage) +
  `InMemoryFilesBackend` (tests).
- `src/store.py` — `ArtifactStore`: envelope build/parse, publish/get/list/
  delete, startup hydration, disk LRU cache.
- `src/builder.py` — markdown → HTML template (mermaid, tables, highlight),
  git clone + entry selection + image inlining.
- `src/security.py` — PBKDF2 password hashing, signed cookies (itsdangerous).
- `src/main.py` — FastAPI wiring, all routes.
- `skills/artifact-publisher/SKILL.md` — served at `/skill`.

## Error handling

- Invalid/unknown stack → 400 with the allowlist rules.
- Token verify failure → 401; network failure to stack → 502.
- Unknown artifact id → 404 (identical response whether it never existed or
  was deleted; no oracle).
- Wrong password → 401 (machine) / re-rendered form (web).
- Git failures (timeout, too big, no entry file) → 422 with reason.
- Payload caps: request body 20 MB (nginx 25 MB), built HTML 15 MB.

## Security notes (v1 accepted risks)

- All artifacts share the app's origin; unlock cookies are path-scoped
  (`/a/<id>`) which is a soft boundary — documented, acceptable for v1.
- No rate limiting in v1 (platform proxy in front; revisit if abused).
- Password protects reads only; publishing is always token-authenticated.

## Community versioning (phase 2)

User decisions: moderated-only in v1; version history public to capability-URL
holders; diff = unified + side-by-side HTML (difflib, no new deps).

Data model:
- Versions are never deleted on update. Each version is its own Storage file
  `artifact-{id}-v{n}.json`, tags `artifact-hub`, `artifact-id-{id}`,
  `artifact-ver-{n}`. A version envelope carries content (html/source/title),
  `version`, `author` (verified project identity), `status` ("live" |
  "proposed"), optional `note`, `created_at`.
- Artifact-level state lives in a small meta file `artifact-{id}-meta.json`
  (tags `artifact-hub`, `artifact-id-{id}`, `artifact-meta`): owner, password
  record, `accept_versions` (bool, default false = moderated opt-in),
  `head_mode` ("latest" | "pinned"), `head_version`, timestamps. The head
  pointer is what `/a/{id}` serves: latest live version, or a pinned one.
- Legacy schema-1 envelopes are migrated lazily: treated as version 1 live;
  the meta file is materialized on the next write.

Rules:
- Owner submissions are always accepted and go live. Non-owner submissions
  require `accept_versions=true` (else 403) and always land as "proposed"
  (moderated). Proposed content is readable only by the owner or its author
  (token headers); everyone else sees it listed as metadata only.
- Contributors authenticate exactly like publishers; every version records a
  verified author. The contributor's canonical copy goes to the contributor's
  own project.
- Retention: `HUB_MAX_VERSIONS` (default 50) live versions per artifact;
  oldest non-pinned, non-head versions are pruned. Per-contributor rate cap
  `HUB_MAX_VERSIONS_PER_DAY` (default 20, in-memory counter).

Endpoints (password gate applies to all reads):
- `GET /a/{id}` — head version. `GET /a/{id}/v/{n}` — one version.
- `GET /a/{id}/versions` — history (JSON; `?format=html` simple picker page).
- `GET /a/{id}/diff/{a}..{b}` — side-by-side HTML; `?format=unified|json`
  for machines. Diffs markdown source when both versions have it, else HTML.
- `POST /api/artifacts/{id}/versions` — submit a version (+ `note`).
- `POST /api/artifacts/{id}/versions/{n}/promote` — owner approves a proposal.
- `DELETE /api/artifacts/{id}/versions/{n}` — owner removes any non-head
  version; a contributor may withdraw their own proposal.
- `PUT /api/artifacts/{id}/head` — `{"mode": "latest"}` or
  `{"mode": "pinned", "version": n}` (owner only).
- `PUT /api/artifacts/{id}` — existing update becomes "owner adds a live
  version"; body additionally accepts `accept_versions`.

## Testing

pytest + FastAPI TestClient. `InMemoryFilesBackend` for store tests; auth
mocked via respx/monkeypatch; builder tests use local fixture repos rendered
offline. No live Keboola calls in tests.

## Deployment

GitHub `padak/kbc_ai_artifact` (public) → `kbagent data-app create
--project artifacts --git-repo … --git-public`. Secrets via `kbagent data-app
secrets-set`: scoped Storage token for the host project (minted with
`kbagent token create … --can-read-all-file-uploads`), secret key. Nginx
proxies 8888 → uvicorn :8050.
