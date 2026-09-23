"""
main.py
-------
FastAPI application — Entry point for the Email Lookup API.
Run with: uvicorn app.main:app --reload
"""

import asyncio
import os
import re
import time
import sys
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

from app.models import (
    LookupRequest, LookupResponse, PersonInfo, PlatformResult,
    CacheInvalidateRequest, CacheInvalidateResponse,
    VerifyRequest, VerifyResponse, PortCheckResponse,
)
from app.lookup_engine import run_lookup
from app.smtp_verifier import verify_email_smtp, check_port25
from app.cache import (
    init_db, get_lookup_cache, set_lookup_cache,
    delete_lookup_cache, get_verify_cache, set_verify_cache,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Email Lookup API",
    description="Reverse email lookup & SMTP verification REST API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Email Lookup API",
        "version": "1.0.0",
        "documentation": "/docs"
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time())}


@app.get("/api/port-check", response_model=PortCheckResponse)
async def port_check():
    """Check if outbound port 25 is available for direct SMTP checks."""
    available = await asyncio.to_thread(check_port25)
    return PortCheckResponse(
        port25_available=available,
        message=(
            "✅ Port 25 is open — full SMTP verification available."
            if available
            else "⚠️ Port 25 is blocked by network/ISP. Direct SMTP handshake restricted."
        ),
    )


@app.post("/api/lookup", response_model=LookupResponse)
async def email_lookup(request: LookupRequest):
    """
    Reverse email lookup — takes an email, returns all public info found.
    Results are cached for 24 hours. Pass `force_refresh: true` to bypass cache.
    """
    email = request.email.lower().strip()

    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=422,
            detail="Invalid email address syntax. Please enter a valid email."
        )

    start_time = time.time()

    # Check cache first unless force_refresh is requested
    if not request.force_refresh:
        cached = await get_lookup_cache(email)
        if cached:
            cached["cached"] = True
            cached["query_time_ms"] = max(1, int((time.time() - start_time) * 1000))
            return LookupResponse(**cached)

    # Run lookup directly
    try:
        lookup_result = await run_lookup(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lookup failed: {e}")

    platform_results = []
    profiles_found = lookup_result.get("profiles", {})
    person_data = lookup_result.get("person", {})

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
        deliverability=(lookup_result.get("email_quality") or {}).get("deliverability"),
        autocorrect=lookup_result.get("autocorrect") or (lookup_result.get("email_quality") or {}).get("autocorrect"),
        company=lookup_result.get("company"),
        email_quality=lookup_result.get("email_quality"),
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
    Email verifier — performs SMTP handshake. Results are cached for 6 hours.
    """
    email = request.email.lower().strip()

    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=422,
            detail="Invalid email address syntax. Please enter a valid email."
        )

    cached = await get_verify_cache(email)
    if cached:
        return VerifyResponse(**cached)

    result = await asyncio.to_thread(verify_email_smtp, email)
    await set_verify_cache(email, result)
    return VerifyResponse(**result)
