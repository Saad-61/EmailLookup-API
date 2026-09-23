# Email Lookup API

A lightweight, high-performance REST API for reverse email lookups, social profile discovery, deliverability checks, and SMTP verifications with SQLite caching and force-refresh capabilities.

---

## Features

- **Reverse Email Lookup (`POST /api/lookup`)**: Returns person info (name, avatar, bio, location), social profiles (GitHub, LinkedIn, Twitter, Gravatar), candidate matches, and domain intelligence.
- **Cache & Force Refresh**: Lookup results are cached in SQLite for 24 hours. Pass `"force_refresh": true` to force a live refresh.
- **Cache Invalidation (`POST /api/cache/invalidate`)**: Explicitly purge cache for a target email.
- **SMTP Verifier (`POST /api/verify`)**: Direct socket MX queries, SMTP handshake validation, catch-all detection, and deliverability checks (cached for 6 hours).
- **Optional SearXNG Integration**: Dockerized local search proxy for deep LinkedIn and social profile discovery.

---

## Quick Start

### 1. Installation

```bash
# Clone repository
git clone https://github.com/Saad-61/EmailLookup-API.git
cd EmailLookup-API

# Create virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Setup

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Add your `GITHUB_TOKEN` to `.env` for higher rate limits on GitHub profile lookups.

### 3. Optional: Start SearXNG Engine (Recommended for Deep Social Discovery)

To enable live web candidate discovery (LinkedIn, Instagram, TikTok via Yandex/Startpage):

```bash
docker compose up -d
```

Ensure `SEARXNG_URL=http://localhost:8888/search` is set in your `.env`.

### 4. Run the API Server

```bash
uvicorn app.main:app --reload --port 8000
```

Interactive OpenAPI Documentation: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Endpoints Overview

### `POST /api/lookup`
Reverse email lookup.

**Request Body**:
```json
{
  "email": "satyanadella@microsoft.com",
  "force_refresh": false
}
```

### `POST /api/cache/invalidate`
Purge cached entry.

**Request Body**:
```json
{
  "email": "satyanadella@microsoft.com"
}
```

### `POST /api/verify`
Direct SMTP verification check.

**Request Body**:
```json
{
  "email": "satyanadella@microsoft.com"
}
```

### `GET /api/health`
Health check status endpoint.

---

## Project Structure

```text
EmailLookup-API/
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI entry point
│   ├── lookup_engine.py    # Master reverse lookup engine
│   ├── social_finder.py    # Candidate search & scoring
│   ├── smtp_verifier.py    # Handshake & deliverability check
│   ├── platform_checker.py # 30+ platform checker
│   ├── cache.py            # SQLite cache management
│   └── models.py           # Pydantic request/response models
├── data/                   # Persistent cache directory
│   └── cache.db
├── searxng/                # SearXNG configuration
│   └── settings.yml
├── docker-compose.yml      # SearXNG container setup
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```
