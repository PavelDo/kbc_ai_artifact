# Runtime route and import audit

All checks were read-only. The app was imported with `env -i`, inert dummy required settings, no production credentials, no service start, no external network, and `PYTHONPYCACHEPREFIX`/cache paths under `_review/runtime/routes-tmp/`. The real lifespan was exercised only after replacing its Storage backend constructor with the repository's in-memory backend. Generated OpenAPI was written only to `_review/runtime/routes-tmp/openapi.json`.

## Command log

### 1. Repository/runtime inventory and hook inspection

- Command: `rg --files -g '!_review/**' | sort; sed -n '1,260p' pyproject.toml; sed -n '1,220p' src/config.py; rg -n "lifespan|@app\\.|include_router|add_api_route|startup|shutdown|middleware|mount\\(" src -g '*.py'`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: normal read-only shell; no application command executed.
- Exit code: `0`
- Relevant excerpt: `pyproject.toml` has FastAPI/Uvicorn/httpx/kbcstorage dependencies, no project scripts or lifecycle hooks; `REQUIRED_ENV = ["HUB_STORAGE_TOKEN", "HUB_STACK_URL", "HUB_SECRET_KEY"]`; `src.main` defines one `lifespan`, two HTTP middleware functions, one `BackendError` handler, and route decorators.
- Expected: establish import requirements and inspect hooks before execution.
- Proof: no package installation, build hook, test plugin, or external-service script was invoked.

### 2. Import and APIRoute enumeration with inert settings

- Command: `env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache" HOME="$PWD/_review/runtime/routes-tmp/home" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/cache" .venv/bin/python -I - <<'PY' ... import src.main; enumerate APIRoute entries; write app.openapi() to _review/runtime/routes-tmp/openapi.json ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert values supplied only for required settings; bytecode/cache redirected under `_review/runtime/routes-tmp`; no server started.
- Exit code: `0`
- Relevant excerpt: `IMPORT_OK kbc-artifact-hub 0.2.0`; `APP_LIFESPAN True`; 22 APIRoute registrations, including `GET /health/headers`, the public artifact routes, and authenticated artifact/version routes; `OPENAPI_OK 19` paths.
- Expected: import succeeds without network and all statically registered routes are enumerable.
- Proof: import and schema generation completed without entering lifespan or contacting Storage.

### 3. Contained ASGI smoke checks

- Command: `env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache" HOME="$PWD/_review/runtime/routes-tmp/home" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/cache" .venv/bin/python -I - <<'PY' ... set app.state.store to InMemoryFilesBackend-backed ArtifactStore, use httpx.ASGITransport without lifespan, request GET /context, /health/headers, /openapi.json, /skill, /health ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert dummy settings; ASGI transport only; no external network; cache under `_review/runtime/routes-tmp`.
- Exit code: `0`
- Relevant excerpt: all five requests returned HTTP 200; `/health/headers` returned `{"received_header_names":[...]}`; `/context` returned 23 endpoint entries; `/openapi.json` returned OpenAPI 3.1.0 with `/health/headers`.
- Expected: local public service/documentation routes respond and manifest/schema are internally discoverable.
- Proof: route operation is callable and in OpenAPI, while the manifest omission is evidenced by the runtime response.

### 4. Full route table versus `/context` manifest

- Command: `env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache" HOME="$PWD/_review/runtime/routes-tmp/home" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/cache" .venv/bin/python -I - <<'PY' ... enumerate all app.routes including Starlette docs routes; call context(); compare normalized method/path sets ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert settings; no network or lifespan.
- Exit code: `0`
- Relevant excerpt: `all_routes` includes `GET /health/headers`; `manifest_count=23`; after parameter-name normalization, the material application operation absent from the manifest is `GET /health/headers` (framework-added `/docs/oauth2-redirect` and HEAD routes are not counted as documented API operations).
- Expected: manifest operation set should include the manually registered diagnostic route if it claims to be full.
- Proof: direct runtime comparison, independent of static decorator reading.

### 5. OpenAPI basic schema validation

- Command: `env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache" HOME="$PWD/_review/runtime/routes-tmp/home" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/cache" .venv/bin/python -I - <<'PY' ... generate app.openapi(), write routes-tmp/openapi.json, validate openapi/info/paths/components, required paths, and /api security ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert settings; generated file only at `_review/runtime/routes-tmp/openapi.json`.
- Exit code: `0`
- Relevant excerpt: `openapi=3.1.0`, `path_count=19`, `required_path_missing=[]`, `component_schema_count=7`, `api_security_count=8`, `bad_api_security=[]`, `errors=[]`.
- Expected: valid non-empty OpenAPI structure, all discovered paths present, and both auth schemes on `/api/*` operations.
- Proof: schema generation and structural checks passed; no schema finding filed.

### 6. Contained FastAPI lifespan

- Command: `env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache" HOME="$PWD/_review/runtime/routes-tmp/home" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/lifespan-cache" .venv/bin/python -I - <<'PY' ... replace m.KbcFilesBackend with InMemoryFilesBackend; async with m.lifespan(m.app) ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert settings; Storage constructor replaced only for containment; no network or production service.
- Exit code: `0`
- Relevant excerpt: `lifespan_entered=true`, `hydrated=true`, `artifact_count=0`, `signer_type=CookieSigner`, `lifespan_exited=true`.
- Expected: lifespan initializes store/signer and hydrates safely when backend is available.
- Proof: actual lifespan context completed with a local in-memory backend.

### 7. Missing-environment import behavior

- Command: `env -i PATH="$PATH" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache-missing" .venv/bin/python -I - <<'PY' ... import src.main ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: only PATH and redirected bytecode; all HUB settings intentionally absent; no network.
- Exit code: `0` (script caught the expected exception)
- Relevant excerpt: `RuntimeError Missing required environment variables: HUB_STORAGE_TOKEN, HUB_STACK_URL, HUB_SECRET_KEY`.
- Expected: fail fast with a clear configuration error before server startup.
- Proof: import-time configuration guard behaves as documented.

### 8. Import-time release metadata comparison

- Command: `env -i PATH="$PATH" PYTHONPYCACHEPREFIX="$PWD/_review/runtime/routes-tmp/pycache-version" HUB_STORAGE_TOKEN='[REDACTED]' HUB_STACK_URL='[REDACTED]' HUB_SECRET_KEY='[REDACTED]' HUB_CACHE_DIR="$PWD/_review/runtime/routes-tmp/version-cache" .venv/bin/python -I - <<'PY' ... import src.main, importlib.metadata, tomllib; print service, metadata, and pyproject versions ... PY`
- CWD: `/Users/padak/github/kbc_ai_artifact`
- Sanitized environment: `env -i`; inert settings; no network; bytecode/cache under `_review/runtime/routes-tmp`.
- Exit code: `0`
- Relevant excerpt: `{'service_version': '0.2.0', 'metadata_version': '0.2.0', 'pyproject_version': '0.2.1'}`.
- Expected: running service version matches checked-in `pyproject.toml` version or stale metadata is rejected.
- Proof: import-time metadata precedence produces a concrete advertised version mismatch; recorded as `runtime-routes-2`.

## Findings

- `runtime-routes-1`: `/health/headers` is callable and in OpenAPI but omitted from the `/context` full manifest.
- `runtime-routes-2`: running service advertises 0.2.0 while checked-in package descriptor declares 0.2.1 due to stale tracked egg-info precedence.

## Skipped checks / limitations

- No production Uvicorn process was started.
- No real FastAPI lifespan using `KbcFilesBackend` was run because it would contact the configured Keboola Storage host; the actual lifespan was exercised with the in-memory backend.
- No package installation, build, dependency lifecycle script, external network call, or production credential was used.
