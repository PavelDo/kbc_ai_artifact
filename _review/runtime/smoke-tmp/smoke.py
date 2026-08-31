"""Contained unauthenticated ASGI smoke checks for the review only."""

import dataclasses
import json
from pathlib import Path

from fastapi.testclient import TestClient

import src.main as main
from src.kbc import InMemoryFilesBackend


tmp = Path(__file__).resolve().parent
base_settings = main.settings
test_settings = dataclasses.replace(
    base_settings,
    hub_storage_token="smoke-storage-token",
    hub_stack_url="https://smoke.invalid",
    secret_key="smoke-secret-key",
    cache_dir=tmp / "cache",
)
backend = InMemoryFilesBackend()

# Replace only the runtime object used by the app lifespan. No application
# source is changed and no real Storage client is instantiated.
main.settings = test_settings
main.KbcFilesBackend = lambda stack_url, token: backend

checks = [
    ("GET", "/", 200),
    ("GET", "/health", 200),
    ("GET", "/context", 200),
    ("GET", "/skill", 200),
    ("GET", "/docs", 200),
    ("GET", "/openapi.json", 200),
    ("GET", "/a/runtime-smoke-not-found", 404),
]

with TestClient(main.app, base_url="http://testserver") as client:
    observed = []
    for method, path, expected_status in checks:
        response = client.request(method, path)
        row = {
            "method": method,
            "path": path,
            "status": response.status_code,
            "expected_status": expected_status,
            "content_type": response.headers.get("content-type", ""),
            "body_bytes": len(response.content),
            "ok": response.status_code == expected_status,
        }
        if path == "/health":
            row["json"] = response.json()
        elif path == "/context":
            payload = response.json()
            row["service"] = payload.get("service")
            row["endpoint_count"] = len(payload.get("endpoints", []))
            row["advertised_paths"] = [e.get("path") for e in payload.get("endpoints", [])]
        elif path == "/openapi.json":
            payload = response.json()
            row["openapi"] = payload.get("openapi")
            row["schema_path_count"] = len(payload.get("paths", {}))
        elif path == "/a/runtime-smoke-not-found":
            row["json"] = response.json()
        observed.append(row)

    registered = []
    for route in main.app.routes:
        methods = sorted((route.methods or set()) - {"HEAD"})
        if methods:
            registered.append({"path": route.path, "methods": methods})
    print(json.dumps({"checks": observed, "registered_routes": registered}, sort_keys=True))
