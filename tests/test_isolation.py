import pytest


@pytest.mark.parametrize("_iteration", [1, 2])
async def test_endpoint_commits_do_not_leak_between_tests(client, _iteration):
    # Both cases must start clean even though signup commits its transaction.
    # Without the outer rollback, the second case fails as a duplicate email.
    response = await client.post(
        "/api/auth/signup",
        json={
            "email": "transaction-isolation@example.com",
            "password": "IsolationPassword123!",
            "full_name": "Isolation Test",
        },
    )
    assert response.status_code == 201
