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
- Email/password signup and login with bcrypt password hashing
- JWT access tokens with HttpOnly cookies (30-minute expiration, 60-minute cookie)
- Token blacklist support for secure logout (revocation stored in database)
- User profile read/update (`/me`) with full_name and language fields
- Token validation via JWT decode with expiration checks

### System Monitoring
- Health check endpoint with database latency reporting and 503 error on DB failure
- Real-time system health metrics (CPU, memory) with container awareness
- Container-aware resource monitoring (reads cgroup v1/v2 files when available; falls back to psutil)
- Uptime tracking as percentage of 24-hour window
- 7-day load history tracking (combined CPU/memory pressure)
- System status classification (STABILIZED < ACTIVE < HEAVY_LOAD < STRESSED)
- Request-level tracking middleware measuring response times across all routes
- Session-based active user tracking with 5-minute timeout and daily unique user counting

### Dashboard
- **AI Worker Fleet**: Manage and monitor 10 AI workers (Helios, Eos, Aethon, Crius, Iapetus, Perses, Phlegon, Phoebe, Theia, Cronus)
  - Real-time worker status and task tracking with current task status display
  - Task queue management and dispatch
  - Daily task counter with auto-reset
- **System Health Monitoring**: Live CPU/memory load with 7-day history
- **UX Metrics**: Active user tracking, response time analysis, daily activity trends

### Media Vault
- **Personal Media Library**: View and manage user's uploaded media items
- **Advanced Search & Filtering**: 
  - Search by filename
  - Filter by media status
  - Sort by creation date, filename, or status
  - Configurable pagination (page/page_size)
- **Media Details**: Access comprehensive metadata including image URLs, technical metadata, and update timestamps
- **Media Deletion**: Delete media items from personal vault with permission validation
- **Bulk Cleanup**: Delete all vault media for the authenticated user in one request
- **Worker Assignment Tracking**: View which AI worker is assigned to process each media item

### Map & Geospatial Analytics
- **Map Data Feed**: Retrieve geotagged user media points with optional bounding-box and confidence filters
- **Grid Stats API**: Get cell-based density, average confidence, and dominant detection label for map overlays
- **Media Log Timeline**: Fetch per-media processing history for map-selected items
- **Short-Lived Map Stats Cache**: 60-second in-memory cache for repeated stats queries

### Media Upload
- **Batch File Upload**: Upload multiple image files simultaneously with automatic image dimension extraction (width, height, size)
- **HuggingFace Integration**: Automatic upload to HuggingFace Hub with organized date-based directory structure
- **Background Processing**: Asynchronous HuggingFace upload with task queue integration
- **Status Tracking**: Real-time media processing status via WebSocket with image URL updates
- **Recent Media Retrieval**: Quick access to 4 most recently uploaded items
- **Complete Media Status Pipeline**: 
  - PENDING → UPLOADED → EXTRACTING → PROCESSING → READY (or FAILED at any step)
  - Automatic status transitions with logging
  - Per-media item task logs tracking all status changes and actions

### AI Worker Fleet Processing
- **Dedicated Task Workers**: 10 persistent background workers continuously processing uploaded media
- **Automated Task Distribution**: Workers automatically claim and process queued tasks (FIFO)
- **Multi-Stage Processing**: Each task progresses through extraction (5s) → processing (20s) → completion stages
- **Real-Time Status Updates**: Users notified via WebSocket as their media progresses through each stage
- **Daily Task Counters**: Automatic per-worker task count with daily auto-reset at midnight
- **Online/Offline Detection**: Workers monitored with 2-minute ping timeout; cluster health assessed

### Task Recovery & Reliability
- **Reaper Process**: Background overseer that recovers stuck or abandoned tasks
  - Detects stale PENDING tasks (>15 min idle) and marks as FAILED
  - Detects stale UPLOADED tasks (>10 min without assignment) and notifies workers
  - Detects offline workers and reassigns their in-progress tasks back to queue
- **Media Task Logging**: Complete audit trail per media item tracking all status changes with timestamps and worker assignments
- **Cluster Status Monitoring**: Real-time fleet health (Optimal/Degraded/Stressed) based on active worker count and processing load

### Core Features
- Async SQLAlchemy ORM with Postgres and asyncpg driver
- Alembic database migrations with auto-migration support
- Token blacklist for secure logout with automatic cleanup
- CORS middleware with configurable frontend origins
- Request tracking and UX metrics middleware (built-in to main app middleware)
- Media task logging with full audit trail (status changes, worker assignments, timestamps)
- Nanoid ID generation for users and UUIDs for media items
- Cascade delete relationships (deleting user cascades to media; deleting media cascades to logs)

## Tech Stack

- **Framework**: FastAPI + Uvicorn (async Python web framework)
- **Database**: SQLAlchemy (async ORM) + Alembic (schema migrations) + Postgres + asyncpg (async driver)
- **Authentication**: JWT (python-jose) + bcrypt (password hashing)
- **Real-Time**: WebSocket support with connection manager for live status updates
- **Media Storage**: HuggingFace Hub API integration for dataset uploads
- **System Monitoring**: psutil (with container-aware cgroup support)
- **Image Processing**: Pillow (PIL) for image dimension extraction
- **Utilities**: nanoid (ID generation), python-dotenv (environment config)

## Requirements

- Python 3.10+
- Postgres database

## Environment Variables

Create a `.env` file in the project root or export these variables in your shell.

| Name | Required | Example | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@localhost:5432/hyperion` | Async SQLAlchemy URL for Postgres |
| `SECRET_KEY` | Yes | `your-secret-key-here` | App fails fast if missing; used for JWT signing and token validation |
| `HF_TOKEN` | Yes* | `hf_xxxxxxxxxxxx` | HuggingFace API token for dataset uploads (*required if using upload features) |
| `HF_REPO_ID` | Yes* | `username/hyperion-media` | HuggingFace dataset repo (*required if using upload features) |
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
alembic upgrade head
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
- `GET /api/dashboard/ai-workers` → Get fleet status (active worker count, cluster health assessment, per-worker task counts, current task tracking, queue depth)
- `POST /api/dashboard/ai-workers/dispatch` → Dispatch a task to the worker queue (triggers worker assignment)

#### Dashboard - System Health
- `GET /api/dashboard/system-health` → Get real-time hardware metrics (CPU %, memory %, uptime %, 7-day load history, system status classification, environment info)

#### Dashboard - User Experience
- `GET /api/dashboard/user-experience` → Get engagement analytics (currently active users, active user trend history, average response time, 7-day daily activity trends, last update timestamp)

#### Vault - Personal Media Library
- `GET /api/vault` → Retrieve user's media library with search, filtering, sorting, and pagination (query params: search, status, order_by, direction, page, page_size)
- `DELETE /api/vault/all` → Delete all media items owned by the authenticated user
- `DELETE /api/vault/{id}` → Delete a media item from personal vault

#### Upload - Media Management
- `POST /api/upload/files` → Batch upload multiple image files with automatic dimension extraction (creates PENDING media records, queues background HF upload)
- `GET /api/upload/recents` → Get 4 most recently uploaded media items with current status and metadata
- `WebSocket /api/upload/ws/updates` → Real-time status updates (auth via `access_token` cookie or `token` query param)

#### Map - Geospatial Data
- `GET /api/map` → Get user geotagged media points (query params: min_lat, max_lat, min_lng, max_lng, has_trash, min_confidence)
- `GET /api/map/stats` → Get grid-aggregated map stats (required query params: min_lat, max_lat, min_lng, max_lng; optional: resolution)
- `GET /api/map/{id}/logs` → Get processing/log history for one media item

## Notes

- JWT access tokens expire in **30 minutes**; the login cookie is set for **60 minutes**
- `SECRET_KEY` must be set for the app to start; used for JWT signing and token validation
- The AI worker fleet consists of **10 persistent workers** started at app startup and continuously processes queued media
- Media status pipeline: PENDING (initial upload) → UPLOADED (HF upload complete) → EXTRACTING (worker extraction phase) → PROCESSING (worker processing phase) → READY (complete) or FAILED (at any step)
- **Reaper process**: Runs every 10 minutes to detect and recover stuck tasks (>15 min PENDING timeout, >10 min UPLOADED timeout) and reassign tasks from offline workers
- **Worker health monitoring**: Workers marked offline if last_ping > 2 minutes; cluster status determined by active worker count (Optimal ≥3, Degraded <3, Stressed ≥8 working)
- System health metrics are **container-aware** and read from cgroup files when deployed in Docker/Kubernetes; falls back to psutil if unavailable
- UX metrics track active users with a **5-minute session timeout** and reset daily activity counters and session tables at midnight UTC
- Token blacklist is stored in the database for secure logout; all tokens checked against blacklist on authenticated requests
- Request tracking middleware measures response times for all routes and maintains active user session pool
- Media files uploaded to HuggingFace Hub with structure: `media/{user_id}/{YYYY-MM-DD}/{media_id}_{filename}`
- Database migrations use Alembic and must be run before first startup: `alembic upgrade head`
- All timestamps use UTC timezone internally (timezone.utc)
- Auth cookie is set with `HttpOnly`, `SameSite=None`, and `Secure=True`; browser-based local development may require HTTPS or token-query fallback for WebSocket auth

## License

MIT