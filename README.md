---
title: Hyperion Project API
emoji: 🚀
colorFrom: indigo
colorTo: blue
sdk: docker
pinned: false
---

# Hyperion Backend API

FastAPI backend for the Hyperion platform, providing user authentication, dashboard monitoring, AI worker fleet management, and advanced geospatial analytics. Built with async SQLAlchemy, PostGIS, and AI-driven object detection capabilities.

## Key Features

### Authentication & User Management
- **Secure Auth**: Email/password signup and login with bcrypt hashing.
- **Stateful Security**: JWT access tokens via HttpOnly cookies with a token blacklist for secure logout.
- **User Profiles**: Manage personal information including full name and preferred language.

### Lab & AI Validation (Human-in-the-loop)
- **Manual Override**: A dedicated interface to manually validate, correct, or delete AI-detected objects.
- **Intelligent Detection Merging**: The system intelligently compares new manual validations with existing AI results, updating confidence scores to `1.0` (validated) only where changes occur.
- **Dynamic Location Updates**: Supports updating media coordinates and altitude. If a location is moved by more than 100 meters, the system automatically triggers a reverse geocoding update to refresh the postal address.
- **Audit Logging**: Every manual intervention is automatically logged in the system as a "You" (user-validated) action in the media's history.

### Media & Video Support
- **Hybrid Media Processing**: Dedicated support for both `IMAGE` and `VIDEO` media types.
- **Video Analytics**: Specialized `VideoDetection` models to store object timestamps and specific frame paths within videos.
- **Automated Cleanup**: A background task automatically prunes temporary video processing directories every 24 hours.
- **HuggingFace Integration**: Seamless, asynchronous upload of media to HuggingFace Hub datasets.

### Geospatial Analytics (PostGIS)
- **Spatial Power**: Leverages **PostGIS** for high-performance geospatial operations.
- **Optimized Search**: Uses **GIST indexing** on location columns for fast bounding-box and proximity queries.
- **Grid Statistics**: Aggregates detection data into geographic grids for heatmaps and density analysis.
- **Rich Metadata**: Stores precision coordinates, altitude, and human-readable addresses for every media item.

### Statistics & Reporting
- **KPI Engine**: 6 specialized endpoints providing data on trash composition, environmental footprint, fleet efficiency, and temporal trends.
- **Bilingual Reporting**: Generate branded PDF reports and Excel cleanup manifests in both English and Hungarian.
- **Performance Caching**: 5-minute in-memory cache for complex statistical aggregations.

### AI Worker Fleet
- **Persistent Workers**: A fleet of 10 background workers (Helios, Eos, etc.) handles heavy processing tasks.
- **Status Pipeline**: Real-time tracking through `PENDING` → `UPLOADED` → `EXTRACTING` → `PROCESSING` → `READY` states.
- **Reaper Process**: An automated overseer that identifies and recovers stuck tasks or reassigns work from offline nodes.

## Tech Stack

- **Framework**: FastAPI (Async Python)
- **Database**: PostgreSQL + **PostGIS** (Spatial extension)
- **ORM**: SQLAlchemy (Async) + GeoAlchemy2
- **Authentication**: JWT + Bcrypt
- **Storage**: HuggingFace Hub API
- **Monitoring**: psutil (Container-aware)
- **Reporting**: Pandas, openpyxl, xhtml2pdf

## API Overview (Key Routes)

### Lab - AI Fine-tuning & Validation
- `GET /api/lab/image/{media_id}` – Retrieve image data and current detections for validation.
- `GET /api/lab/video/{media_id}` – Retrieve detailed video detection data.
- `PATCH /api/lab/{media_id}` – Update/validate location, address, and detections.

### Dashboard & System
- `GET /api/health` – System health check and database latency.
- `GET /api/dashboard/ai-workers` – Fleet status, queue depth, and worker health.
- `GET /api/dashboard/system-health` – Real-time CPU, memory, and load history.

### Vault & Map
- `GET /api/vault` – Personal media library with advanced filtering and search.
- `GET /api/map` – Geospatial feed of tagged media points.

## Automated Maintenance (Lifespan Tasks)
The application handles several background processes automatically on startup:
- **Blacklist Pruner**: Hourly cleanup of expired tokens from the database.
- **Video Cleanup**: Daily removal of temporary video processing files.
- **Worker Init**: Automatic synchronization of the AI worker fleet state.

## Getting Started

1. Configure your `.env` file with `DATABASE_URL`, `SECRET_KEY`, and `HF_TOKEN`.
2. Run database migrations:
   ```bash
   alembic upgrade head

## License

MIT