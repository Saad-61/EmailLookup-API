"""
main.py
-------
FastAPI application — entry point for the Email Lookup backend.
Run with: uvicorn main:app --reload
"""

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import sys
from pathlib import Path

# Ensure backend directory is in sys.path when running from repository root
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)

from models import (
    LookupRequest, LookupResponse, PersonInfo, PlatformResult,
    CacheInvalidateRequest, CacheInvalidateResponse,
    VerifyRequest, VerifyResponse, PortCheckResponse,
)
from lookup_engine import run_lookup
from smtp_verifier import verify_email_smtp, check_port25
from platform_checker import check_platforms
from cache import (
    init_db, get_lookup_cache, set_lookup_cache,
    delete_lookup_cache, get_verify_cache, set_verify_cache,
)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Email Lookup API",
    description="Reverse email lookup & SMTP verification tool",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Frontend static files removed for standalone API repo


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Email Lookup API",
        "version": "1.0.0",
        "documentation": "/docs"
    }


FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon_ico():
    svg_path = os.path.join(FRONTEND_DIR, "favicon.svg")
    if os.path.exists(svg_path):
        return FileResponse(
            svg_path,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache, must-revalidate"}
        )
    return {"message": "Favicon not found."}


@app.get("/favicon.svg", include_in_schema=False)
async def serve_favicon_svg():
    svg_path = os.path.join(FRONTEND_DIR, "favicon.svg")
    if os.path.exists(svg_path):
        return FileResponse(
            svg_path,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache, must-revalidate"}
        )
    return {"message": "Favicon not found."}


@app.post("/api/cache/invalidate")
async def invalidate_cache_entry(request: LookupRequest):
    """Purge cached lookup results for a given email."""
    email = request.email.lower().strip()
    from cache import delete_lookup_cache
    deleted = await delete_lookup_cache(email)
    return {"email": email, "invalidated": deleted}


@app.get("/api/port-check", response_model=PortCheckResponse)
async def port_check():
    """Check if outbound port 25 is available (not ISP-blocked)."""
    available = await asyncio.to_thread(check_port25)
    return PortCheckResponse(
        port25_available=available,
        message=(
            "Port 25 is open - full direct SMTP verification active."
            if available
            else "Port 25 is restricted by ISP/network. Using API fallback if configured."
        ),
    )


@app.post("/api/lookup", response_model=LookupResponse)
async def email_lookup(request: LookupRequest):
    """
    Reverse email lookup — takes an email, returns all public info we can find.
    Results are cached for 24 hours.
    """
    email = request.email.lower().strip()

    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=422,
            detail="Invalid email address syntax. Please enter a valid email (e.g. name@company.com)."
        )

    start_time = time.time()

    # Check cache first unless force_refresh is requested
    if not request.force_refresh:
        cached = await get_lookup_cache(email)
        if cached:
            cached["cached"] = True
            cached["query_time_ms"] = max(1, int((time.time() - start_time) * 1000))
            if "social_candidates_by_platform" in cached and isinstance(cached["social_candidates_by_platform"], dict):
                cached["social_candidates_by_platform"].setdefault("spotify", [])
            return LookupResponse(**cached)

    # Run lookup directly (platform check skipped to maximize speed since card is hidden)
    try:
        lookup_result = await run_lookup(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lookup failed: {e}")

    platform_results = []

    profiles_found = lookup_result.get("profiles", {})
    person_data = lookup_result.get("person", {})

    # Sync platforms with lookup discoveries (e.g. if GitHub or Gravatar profile was found, force found=True)
    for p in platform_results:
        p_name = p.get("name", "").lower()
        if p_name == "github" and profiles_found.get("github"):
            p["found"] = True
            p["url"] = profiles_found["github"].get("url") if isinstance(profiles_found["github"], dict) else None
        elif p_name == "gravatar" and (person_data.get("avatar") or "").startswith("https://www.gravatar.com"):
            p["found"] = True
        elif p_name == "linkedin" and profiles_found.get("linkedin"):
            p["found"] = True
            p["url"] = profiles_found.get("linkedin")

    person = PersonInfo(
        name=person_data.get("name"),
        avatar=person_data.get("avatar"),
        bio=person_data.get("bio"),
        location=person_data.get("location"),
        website=person_data.get("website"),
    )

    platforms = [
        PlatformResult(
            name=p["name"],
            found=p["found"],
            icon=p["icon"],
            url=p.get("url"),
        )
        for p in platform_results
    ]

    response = LookupResponse(
        email=email,
        query_time_ms=lookup_result.get("query_time_ms", 0),
        email_type=lookup_result.get("email_type", "personal"),
        domain=lookup_result.get("domain"),
        person=person,
        profiles=lookup_result.get("profiles", {}),
        platforms=platforms,
        phone=lookup_result.get("phone"),
        address=None,
        deliverability=lookup_result.get("deliverability"),
        autocorrect=lookup_result.get("autocorrect"),
        company=lookup_result.get("company"),
        social_candidates=lookup_result.get("social_candidates", []),
        social_candidates_by_platform=lookup_result.get("social_candidates_by_platform", {}),
    )

    # Cache the result
    await set_lookup_cache(email, response.model_dump())
    return response


@app.post("/api/cache/invalidate", response_model=CacheInvalidateResponse)
async def invalidate_cache(request: CacheInvalidateRequest):
    """
    Purge cached lookup result for the specified email to force a fresh live lookup.
    """
    email = request.email.lower().strip()
    if not email:
        raise HTTPException(status_code=422, detail="Email is required.")
    success = await delete_lookup_cache(email)
    return CacheInvalidateResponse(
        success=success,
        email=email,
        message="Cache entry successfully purged." if success else "No cache entry found or error purging."
    )


@app.post("/api/verify", response_model=VerifyResponse)
async def email_verify(request: VerifyRequest):
    """
    Email verifier — performs SMTP handshake or uses AbstractAPI fallback.
    Results are cached for 6 hours.
    """
    email = request.email.lower().strip()
 
    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=422,
            detail="Invalid email address syntax. Please enter a valid email (e.g. name@company.com)."
        )

    cached = await get_verify_cache(email)
    if cached:
        return VerifyResponse(**cached)

    result = await asyncio.to_thread(verify_email_smtp, email)
    await set_verify_cache(email, result)
    return VerifyResponse(**result)


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time())}
