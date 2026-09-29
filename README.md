# Email Lookup API

A lightweight, high-performance REST API for reverse email lookups, social profile discovery, deliverability checks, and SMTP verifications with SQLite caching and force-refresh capabilities.

---

## Features

- **Reverse Email Lookup (`POST /api/lookup`)**: Discovers person information (Name, Avatar, Bio, Location), social profiles (GitHub, LinkedIn, Twitter, Spotify, Gravatar), candidate matches, and company intelligence.
- **Cache & Force Refresh**: Lookup results are cached in SQLite for 24 hours. Pass `"force_refresh": true` to force a live refresh.
- **Cache Invalidation (`POST /api/cache/invalidate`)**: Explicitly purge cache for a target email.
- **SMTP Verifier (`POST /api/verify`)**: Direct socket MX queries, SMTP handshake validation, catch-all detection, and deliverability checks (cached for 6 hours).
- **Port 25 Probe (`GET /api/port-check`)**: Verifies outbound SMTP connectivity.

---

## Quick Start

### 1. Installation

```bash
# Clone repository
git clone https://github.com/Saad-61/EmailLookup-API.git
cd EmailLookup-API

# Create virtual environment
python -m venv .venv

# Activate virtualenv:
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

Configure your environment variables in `.env`:
- **`GITHUB_TOKEN`**: (Recommended) GitHub Personal Access Token to increase API rate limits from 60 to 5,000 req/hour.
- **`PROXY_IPS` / `PROXY_USERNAME` / `PROXY_PASSWORD`**: (Optional) Dynamic residential proxy pool for DuckDuckGo rotation & direct probing.
- **`SPOTIFY_CLIENT_TOKEN` / `SPOTIFY_AUTH_TOKEN`**: (Optional) Web Bearer tokens for Spotify Pathfinder GraphQL user searches.
- **`SMTP_SENDER_EMAIL` / `SMTP_HELO_HOST`**: Configures the HELO host and sender email address for Port 25 SMTP handshake verifications.

### 3. Run the API Server

```bash
uvicorn app.main:app --reload --port 8000
```

Interactive OpenAPI Documentation: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Endpoints & Request Examples

### 1. Reverse Email Lookup
`POST /api/lookup`

**Request**:
```json
{
  "email": "torvalds@linux-foundation.org",
  "force_refresh": false
}
```

**Response**:
```json
{
  "email": "torvalds@linux-foundation.org",
  "query_time_ms": 1450,
  "email_type": "corporate",
  "person": {
    "name": "Linus Torvalds",
    "avatar": "https://avatars.githubusercontent.com/u/1024025?v=4",
    "bio": "",
    "location": "Portland, OR"
  },
  "profiles": {
    "github": {
      "url": "https://github.com/torvalds",
      "username": "torvalds"
    }
  },
  "cached": false
}
```

### 2. Force Cache Refresh / Invalidation
`POST /api/cache/invalidate`

**Request**:
```json
{
  "email": "torvalds@linux-foundation.org"
}
```

### 3. SMTP Verification
`POST /api/verify`

**Request**:
```json
{
  "email": "torvalds@linux-foundation.org"
}
```

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
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```
