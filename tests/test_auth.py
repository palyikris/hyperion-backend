import pytest
from httpx import AsyncClient

# This tells pytest that these tests are asynchronous
pytestmark = pytest.mark.asyncio


async def test_signup_success(client: AsyncClient):
    """Test that a new user can successfully register."""
    response = await client.post(
        "/api/auth/signup",
        json={
            "email": "testuser@example.com",
            "password": "SuperSecretPassword123!",
            "full_name": "Test User",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["message"] == "User created successfully"


async def test_signup_duplicate_email(client: AsyncClient):
    """Test that signing up with an existing email fails gracefully."""
    # 1. Create the first user
    await client.post(
        "/api/auth/signup",
        json={
            "email": "duplicate@example.com",
            "password": "Password123!",
        },
    )

    # 2. Try to create again
    response = await client.post(
        "/api/auth/signup",
        json={
            "email": "duplicate@example.com",
            "password": "DifferentPassword456!",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Email already registered"


async def test_login_success_and_cookie(client: AsyncClient):
    """Test login functionality and verify the HttpOnly cookie is set."""
    # 1. Create a user
    await client.post(
        "/api/auth/signup",
        json={
            "email": "login_test@example.com",
            "password": "LoginPassword123!",
        },
    )

    # 2. Login
    response = await client.post(
        "/api/auth/login",
        json={"email": "login_test@example.com", "password": "LoginPassword123!"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Login successful"
    assert data["user"]["email"] == "login_test@example.com"

    # 3. Verify the cookie was set correctly
    assert "access_token" in response.cookies


async def test_get_me_protected_route(client: AsyncClient):
    """Test that the /me route requires authentication and returns the right user."""
    # 1. Access without token should fail
    unauth_response = await client.get("/api/auth/me")
    assert unauth_response.status_code == 401

    # 2. Create and login user
    await client.post(
        "/api/auth/signup", json={"email": "me@example.com", "password": "pass"}
    )
    login_resp = await client.post(
        "/api/auth/login", json={"email": "me@example.com", "password": "pass"}
    )

    # The `client` automatically stores cookies returned by login_resp!
    # 3. Access with token should succeed
    auth_response = await client.get("/api/auth/me")
    assert auth_response.status_code == 200
    assert auth_response.json()["email"] == "me@example.com"
