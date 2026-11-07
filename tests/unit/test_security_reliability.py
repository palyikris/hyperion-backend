from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, WebSocketDisconnect
from jose import jwt
from starlette.datastructures import URL
from starlette.requests import Request
from fastapi.responses import Response

from app.api import auth, deps, upload
from app.api.upload_utils.conn_manager import ConnectionManager
from app.core import security
from app.core.csrf import enforce_csrf_origin
from app.database import get_db
from app.database import engine


class ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class AuthSession:
    def __init__(self, user=None):
        self.user = user
        self.execute = AsyncMock(return_value=ScalarResult(user))
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.added = []

    def add(self, value):
        self.added.append(value)


def test_database_errors_hide_bound_credentials():
    assert engine.sync_engine.hide_parameters is True


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,expected", [
    ("https://attacker.example.test", 403), ("null", 403), ("http://[", 403),
    ("https://app.example.test", 200), (None, 200),
])
async def test_anonymous_login_rejects_untrusted_browser_origins(monkeypatch, origin, expected):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    request = Request({
        "type": "http", "method": "POST", "scheme": "https",
        "path": "/api/auth/login", "root_path": "", "query_string": b"",
        "server": ("backend.example.test", 443),
        "headers": [(b"origin", origin.encode())] if origin else [],
    })
    next_handler = AsyncMock(return_value=Response(status_code=200))
    response = await enforce_csrf_origin(request, next_handler)
    assert response.status_code == expected
    assert next_handler.await_count == int(expected == 200)


def auth_app(db):
    app = FastAPI()
    app.include_router(auth.router, prefix="/auth")

    async def session():
        yield db

    app.dependency_overrides[get_db] = session
    return app


def test_tokens_issued_in_the_same_second_have_distinct_ids(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(security, "datetime", Clock)
    tokens = [security.create_access_token({"sub": "test@example.test"}) for _ in range(2)]
    claims = [jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM],
                         options={"verify_exp": False}) for token in tokens]
    assert all(claim.get("jti") for claim in claims)
    assert claims[0]["jti"] != claims[1]["jti"]


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{}, {"sub": "test@example.test"}, {"exp": 4102444800}])
async def test_missing_authentication_claims_are_rejected_before_database_access(claims):
    token = jwt.encode(claims, security.SECRET_KEY, algorithm=security.ALGORITHM)
    db = AuthSession()
    with pytest.raises(HTTPException) as error:
        await deps.get_current_user_from_token(token, db)
    assert error.value.status_code == 401
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["signup", "login"])
@pytest.mark.parametrize("password", ["", "x" * 73, "é" * 37])
async def test_unsupported_password_lengths_do_not_reach_the_database(endpoint, password):
    db = AuthSession()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(db)),
                                 base_url="https://test") as client:
        response = await client.post(f"/auth/{endpoint}", json={
            "email": "test@example.test", "password": password,
        })
    assert response.status_code == 422
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_does_not_report_success_or_log_credentials_when_revocation_fails(caplog):
    db = AuthSession()
    db.execute.side_effect = RuntimeError("sensitive-token-marker")
    token = security.create_access_token({"sub": "test@example.test"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(db)),
                                 base_url="https://test", cookies={"access_token": token}) as client:
        response = await client.post("/auth/logout")
    assert response.status_code == 503
    assert "sensitive-token-marker" not in caplog.text
    assert "set-cookie" not in response.headers
    db.rollback.assert_awaited_once()


class Socket:
    def __init__(self, origin="https://app.example.test", cookie="session-token"):
        self.cookies = {"access_token": cookie} if cookie else {}
        self.headers = {"origin": origin} if origin is not None else {}
        self.base_url = URL("wss://backend.example.test/")
        self.url = URL("wss://backend.example.test/api/upload/ws/updates")
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_json = AsyncMock()
        self.receive_text = AsyncMock(side_effect=WebSocketDisconnect())


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example.test"])
async def test_cookie_websocket_rejects_untrusted_origins_before_authentication(monkeypatch, origin):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    authenticate = AsyncMock(return_value=SimpleNamespace(id="owner"))
    monkeypatch.setattr(upload, "get_current_user_from_token", authenticate)

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(upload, "AsyncSessionLocal", session)
    monkeypatch.setattr(upload, "manager", ConnectionManager())
    socket = Socket(origin)
    await upload.websocket_updates(socket, token=None)
    socket.close.assert_awaited_once_with(code=1008)
    socket.accept.assert_not_awaited()
    authenticate.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_tabs_receive_updates_and_closing_one_does_not_disconnect_another():
    manager = ConnectionManager()
    first, second, other = Socket(), Socket(), Socket()
    await manager.connect("owner", first)
    await manager.connect("owner", second)
    await manager.connect("other", other)
    await manager.send_status("owner", "media", "READY")
    first.send_json.assert_awaited_once()
    second.send_json.assert_awaited_once()
    other.send_json.assert_not_awaited()

    manager.disconnect("owner", first)
    await manager.send_status("owner", "media", "READY")
    assert second.send_json.await_count == 2
    assert first.send_json.await_count == 1


@pytest.mark.asyncio
async def test_one_broken_socket_does_not_remove_healthy_tabs():
    manager = ConnectionManager()
    broken, healthy = Socket(), Socket()
    broken.send_json.side_effect = RuntimeError("disconnected")
    await manager.connect("owner", broken)
    await manager.connect("owner", healthy)
    await manager.send_status("owner", "media", "READY")
    await manager.send_status("owner", "media", "FAILED")
    assert broken.send_json.await_count == 1
    assert healthy.send_json.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://app.example.test", "https://backend.example.test"])
async def test_trusted_websocket_closes_authentication_session_before_accept(monkeypatch, origin):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    active = False

    @asynccontextmanager
    async def session():
        nonlocal active
        active = True
        try:
            yield object()
        finally:
            active = False

    socket = Socket(origin)

    async def accept():
        assert active is False

    socket.accept.side_effect = accept
    authenticate = AsyncMock(return_value=SimpleNamespace(id="owner"))
    monkeypatch.setattr(upload, "AsyncSessionLocal", session)
    monkeypatch.setattr(upload, "get_current_user_from_token", authenticate)
    manager = ConnectionManager()
    monkeypatch.setattr(upload, "manager", manager)
    await upload.websocket_updates(socket, token=None)
    socket.accept.assert_awaited_once()
    socket.close.assert_not_awaited()
    assert not manager.active_connections


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected_code", [(HTTPException(401), 1008), (RuntimeError("unavailable"), 1011)])
async def test_active_websocket_rechecks_revocation_without_holding_database_connections(monkeypatch, error, expected_code):
    import asyncio

    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    monkeypatch.setattr(upload, "WEBSOCKET_AUTH_CHECK_INTERVAL_SECONDS", 0)

    @asynccontextmanager
    async def session():
        yield object()

    authenticate = AsyncMock(side_effect=[SimpleNamespace(id="owner"), error])
    socket = Socket()
    closed = asyncio.Event()

    async def close(code):
        closed.set()

    async def receive():
        await closed.wait()
        raise WebSocketDisconnect()

    socket.close.side_effect = close
    socket.receive_text.side_effect = receive
    monkeypatch.setattr(upload, "AsyncSessionLocal", session)
    monkeypatch.setattr(upload, "get_current_user_from_token", authenticate)
    manager = ConnectionManager()
    monkeypatch.setattr(upload, "manager", manager)
    await asyncio.wait_for(upload.websocket_updates(socket, token=None), timeout=2)
    socket.close.assert_awaited_once_with(code=expected_code)
    assert authenticate.await_count == 2
    assert not manager.active_connections


@pytest.mark.asyncio
async def test_logout_is_idempotent_and_deletes_the_cross_site_cookie():
    db = AuthSession()
    token = security.create_access_token({"sub": "test@example.test"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(db)),
                                 base_url="https://test") as client:
        for _ in range(2):
            client.cookies.set("access_token", token)
            response = await client.post("/auth/logout")
            assert response.status_code == 200
            cookie = response.headers["set-cookie"].lower()
            assert "samesite=none" in cookie and "secure" in cookie and "httponly" in cookie
    inserts = [call.args[0] for call in db.execute.await_args_list if getattr(call.args[0], "is_insert", False)]
    assert len(inserts) == 2
    assert all(statement._post_values_clause is not None for statement in inserts)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["signup", "login"])
async def test_password_hashing_and_verification_run_off_the_event_loop(monkeypatch, endpoint):
    import threading

    loop_thread = threading.get_ident()
    seen_threads = []

    def bcrypt_operation(*args):
        seen_threads.append(threading.get_ident())
        return "hashed" if endpoint == "signup" else True

    user = SimpleNamespace(id="owner", email="test@example.test", full_name="Test",
                           language="en", hashed_password="stored")
    db = AuthSession(user if endpoint == "login" else None)
    monkeypatch.setattr(security, "hash_password" if endpoint == "signup" else "verify_password", bcrypt_operation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app(db)), base_url="https://test") as client:
        response = await client.post(f"/auth/{endpoint}", json={"email": user.email, "password": "password"})
    assert response.status_code == (201 if endpoint == "signup" else 200)
    assert len(seen_threads) == 1
    assert seen_threads[0] != loop_thread
