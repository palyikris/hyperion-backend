---
title: Hyperion Project API
emoji: 🚀
colorFrom: indigo
colorTo: blue
sdk: docker
pinned: false
---

# Hyperion Backend API

FastAPI backend for the Hyperion platform, providing user authentication, dashboard monitoring, and AI worker fleet management. Built with async SQLAlchemy, Postgres, and JWT-based authentication via HttpOnly cookies.

## Features

### Authentication
- Email/password signup and login
- JWT access tokens with HttpOnly cookies (30-minute expiration)
- User logout with token blacklist
- User profile read/update (`/me`) with full_name and language fields

### System Monitoring
- Health check endpoint with database latency reporting
- Real-time system health metrics (CPU, memory, uptime)
- Container-aware resource monitoring (reads cgroup files when available)

### Dashboard
- **AI Worker Fleet**: Manage and monitor 10 AI workers (Helios, Eos, Aethon, Crius, Iapetus, Perses, Phlegon, Phoebe, Theia, Cronus)
  - Real-time worker status and task tracking
  - Task queue management and dispatch
  - Daily task counter with auto-reset
- **System Health Monitoring**: Live CPU/memory load with 7-day history
- **UX Metrics**: Active user tracking, response time analysis, daily activity trends

### Core Features
- Async SQLAlchemy ORM with Postgres
- Alembic database migrations
- Token blacklist for secure logout
- CORS middleware with configurable frontend origins
- Request tracking and UX metrics middleware

## Tech Stack

- **Framework**: FastAPI + Uvicorn
- **Database**: SQLAlchemy (async) + Alembic migrations + Postgres (asyncpg)
- **Authentication**: JWT (python-jose) + bcrypt
- **Task Queue**: Celery + Redis
- **System Monitoring**: psutil
- **Utilities**: nanoid (ID generation), python-dotenv

## Requirements

- Python 3.10+
- Postgres database

## Environment Variables

Create a `.env` file in the project root or export these variables in your shell.

| Name | Required | Example | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@localhost:5432/hyperion` | Async SQLAlchemy URL for Postgres |
| `SECRET_KEY` | Yes | `your-secret-key-here` | App fails fast if missing; used for JWT signing |
| `FRONTEND_URL` | No | `http://localhost:5173` | CORS allowlist; defaults to `http://localhost:5173` |
| `REDIS_URL` | No | `redis://localhost:6379` | Optional; used for Celery task broker |

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

- `GET /` → Basic status message
- `GET /api/health` → Health check with database latency (returns 503 if database is down)
- `POST /api/auth/signup` → Create user account
- `POST /api/auth/login` → Login, sets `access_token` HttpOnly cookie
- `POST /api/auth/logout` → Logout, clears cookies and blacklists token

### Authenticated Routes (Cookie-based)

#### Auth
- `GET /api/auth/me` → Get current user profile (id, email, full_name, language)
- `PUT /api/auth/me` → Update user profile (full_name, language)

#### Dashboard - AI Workers
- `GET /api/dashboard/ai-workers` → Get fleet status (active count, cluster health, per-worker details)
- `POST /api/dashboard/ai-workers/dispatch` → Dispatch a task to the worker queue

#### Dashboard - System Health
- `GET /api/dashboard/system-health` → Get real-time hardware metrics (CPU, memory, uptime, 7-day load history)

#### Dashboard - User Experience
- `GET /api/dashboard/user-experience` → Get engagement analytics (active users, response times, daily trends)

## Notes

- JWT access tokens expire in **30 minutes**; the login cookie is set for **60 minutes**
- `SECRET_KEY` must be set for the app to start
- The AI worker fleet consists of **10 simulated workers** that process tasks from a queue (25-second simulation per task)
- System health metrics are **container-aware** and read from cgroup files when deployed in Docker/Kubernetes
- UX metrics track active users with a **5-minute session timeout** and reset daily activity counters at midnight
- Token blacklist is stored in the database for secure logout
- Request tracking middleware measures response times for all routes

## License

MIT