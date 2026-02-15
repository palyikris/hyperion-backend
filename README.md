---
title: Hyperion Project API
emoji: 🚀
colorFrom: indigo
colorTo: blue
sdk: docker
pinned: false
---

# Hyperion Backend API

FastAPI backend for the Hyperion platform, providing authentication, basic user profile management, and system health checks for now. Designed to run with an async SQLAlchemy + Postgres database and issue JWT-based sessions via HttpOnly cookies.

## Features

- Email/password signup and login
- Session cookies (HttpOnly) with JWT access tokens
- User profile read/update (`/me`)
- Health check with database latency reporting

## Tech Stack

- FastAPI + Uvicorn
- SQLAlchemy (async) + Alembic migrations
- Postgres (via `asyncpg`)
- JWT + bcrypt for auth

## Requirements

- Python 3.10+
- Postgres database

## Environment Variables

Create a `.env` file in the project root or export these variables in your shell.

| Name | Required | Example | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@localhost:5432/hyperion` | Async SQLAlchemy URL |
| `SECRET_KEY` | Yes | `change-me` | App fails fast if missing |
| `FRONTEND_URL` | No | `http://localhost:5173` | CORS allowlist; defaults to `http://localhost:5173` |

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the API
uvicorn main:app --reload
```

The API will be available at `http://127.0.0.1:8000` by default.

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

## Database Migrations

Alembic reads `DATABASE_URL` from the environment.

```bash
alembic upgrade head
```

## Docker

The Docker image runs on port `7860` (Hugging Face Spaces default).

```bash
docker build -t hyperion-backend .
docker run --rm -p 7860:7860 --env-file .env hyperion-backend
```

## API Overview

### Public

- `GET /` -> basic status message
- `GET /api/health` -> health check (503 if database is down)
- `POST /api/auth/signup` -> create user
- `POST /api/auth/login` -> login, sets `access_token` cookie
- `POST /api/auth/logout` -> clears cookies

### Authenticated (cookie-based)

- `GET /api/auth/me` -> current user profile
- `PUT /api/auth/me` -> update `full_name` and `language`

## Notes

- JWT access tokens expire in 30 minutes; the login cookie is set for 60 minutes.
- `SECRET_KEY` must be set for the app to start.

## License

MIT