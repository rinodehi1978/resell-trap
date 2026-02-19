"""Simple API key authentication.

When API_KEY is set in .env, all /api/ endpoints require either:
- Header: X-API-Key: <key>
- Query param: ?api_key=<key>

Web UI pages (/, /items, /search, /keepa, /partials/) are not protected,
as the dashboard calls the API internally.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.api_key:
            return await call_next(request)

        path = request.url.path

        # Only protect /api/ endpoints; skip docs, web UI, partials
        if not path.startswith("/api/"):
            return await call_next(request)

        # Allow OpenAPI docs
        if path in ("/api/openapi.json",):
            return await call_next(request)

        # Check API key
        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if not key or not secrets.compare_digest(key, settings.api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
