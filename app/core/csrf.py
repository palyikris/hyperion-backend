import os
from urllib.parse import urlsplit

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
LOCAL_FRONTEND_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}


def normalize_origin(value: str | None) -> str | None:
    if not value:
        return None

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def get_allowed_frontend_origins() -> list[str]:
    configured_origin = normalize_origin(os.getenv("FRONTEND_URL"))
    if configured_origin:
        return [configured_origin]
    return sorted(LOCAL_FRONTEND_ORIGINS)


def _origin_from_referer(referer: str | None) -> str | None:
    return normalize_origin(referer)


def is_trusted_request_origin(request: HTTPConnection, origin: str | None) -> bool:
    normalized_origin = normalize_origin(origin)
    if not normalized_origin:
        return False

    if normalized_origin in get_allowed_frontend_origins():
        return True

    base_url = request.base_url
    # Browser WebSocket Origin headers use http(s), not ws(s).
    scheme = {"ws": "http", "wss": "https"}.get(base_url.scheme, base_url.scheme)
    request_origin = normalize_origin(str(base_url.replace(scheme=scheme)))
    return normalized_origin == request_origin


async def enforce_csrf_origin(request: Request, call_next):
    """Reject cross-origin cookie-authenticated state changes.

    CORS controls whether a browser can read a response, but does not by itself
    prevent a cross-site form from sending a request. Cookie-authenticated
    unsafe methods therefore require a trusted Origin (or Referer fallback).
    """

    if request.method in SAFE_METHODS:
        return await call_next(request)

    request_origin = request.headers.get("origin") or _origin_from_referer(
        request.headers.get("referer")
    )
    # Login can set an authenticated cookie even when no cookie arrived. Reject
    # explicitly untrusted browser origins on anonymous state changes as well.
    # Origin-less non-browser requests remain possible without ambient cookies.
    if not request_origin and not request.cookies.get("access_token"):
        return await call_next(request)
    if not is_trusted_request_origin(request, request_origin):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Cross-site request blocked"},
        )

    return await call_next(request)
