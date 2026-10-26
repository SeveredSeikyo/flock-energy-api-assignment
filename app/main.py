from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Path as FPath, status
from fastapi.responses import JSONResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.client import UrjaPortalClient
from app.models import (
    GeoLocation,
    MeterSummary,
    MeterDetail,
    MeterConsumptionResponse,
    NearbyMeterResult,
    Transformer,
    TransformerDetail,
    HierarchyTreeNode,
    PaginatedResponse,
    AuthLoginRequest,
    AuthStatusResponse,
    AuthLogoutResponse,
    HealthResponse,
    CacheRefreshResponse,
    ExportResponse,
)

# Shared UrjaPortalClient instance
portal_client = UrjaPortalClient()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup & shutdown events."""
    # Pre-populate initial cache and verify upstream connectivity on startup
    try:
        portal_client.refresh_cache(force=True)
    except Exception:
        # Will lazily sync on first query if initial attempt fails
        pass
    yield

app = FastAPI(
    title="Urja Meter Ops REST API",
    description="""
A clean, documented REST API service built over the internal Urja Meter Ops utility portal.

### Features:
* **Session Persistence & Auto-Recovery**: Automates authentication with transparent token refresh on expiration.
* **Telemetry Normalization**: Standardizes non-uniform timestamps into ISO 8601 UTC and converts string values to floats.
* **Multi-Attribute Search**: Filter smart meters across manufacturer, phase, status, and transformer hierarchy.
* **Geospatial Proximity**: Find meters within a geographic radius using the Haversine formula.
* **Bulk Export**: Direct programmatic access to export the full meter dataset with verified HMAC-SHA256 signatures.
* **Electrical Topology**: Hierarchical distribution tree and transformer inventory with connected meter counts.
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# CORS middleware for downstream consumer applications
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
def root():
    """Redirect root to interactive Swagger UI documentation."""
    return RedirectResponse(url="/docs")

# -----------------------------------------------------------------------------
# System & Authentication Routes
# -----------------------------------------------------------------------------

@app.get("/api/v1/health", response_model=HealthResponse, tags=["System"])
def check_health():
    """Check API operational health and upstream legacy portal connectivity."""
    is_live = portal_client.is_authenticated()
    if not is_live:
        is_live = portal_client.login()

    return HealthResponse(
        status="healthy" if is_live else "degraded",
        legacy_portal_accessible=is_live,
        cached_meters_count=len(portal_client._meters_cache),
        cache_last_updated=portal_client._cache_last_updated.isoformat() if portal_client._cache_last_updated else None
    )

@app.post("/api/v1/auth/login", response_model=AuthStatusResponse, tags=["Authentication"])
def login_operator(creds: Optional[AuthLoginRequest] = None):
    """
    Manually trigger authentication with Urja Meter Ops.
    If no credentials are submitted, defaults to configured system operator credentials.
    """
    email = creds.email if creds and creds.email else None
    password = creds.password if creds and creds.password else None

    success = portal_client.login(email=email, password=password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to authenticate against legacy Urja portal with provided credentials."
        )

    return AuthStatusResponse(
        authenticated=True,
        message="Session successfully established with legacy portal.",
        session_cookie_present=portal_client.is_authenticated()
    )

@app.get("/api/v1/auth/status", response_model=AuthStatusResponse, tags=["Authentication"])
def auth_status():
    """Verify current authentication and session validity."""
    active = portal_client.is_authenticated()
    return AuthStatusResponse(
        authenticated=active,
        message="Active session cookie present." if active else "No active session. Re-authentication will occur on next request.",
        session_cookie_present=active
    )

@app.post("/api/v1/auth/logout", response_model=AuthLogoutResponse, tags=["Authentication"])
@app.post("/api/v1/auth/sign-out", response_model=AuthLogoutResponse, tags=["Authentication"], include_in_schema=False)
def logout_operator():
    """
    Sign out of Urja Meter Ops and clear active session cookies.
    Mirrors POST /api/auth/sign-out from the portal.
    """
    portal_client.logout()
    return AuthLogoutResponse(
        success=True,
        message="Session terminated and cookies cleared successfully."
    )

@app.post("/api/v1/cache/refresh", response_model=CacheRefreshResponse, tags=["System"])
def force_cache_refresh():
    """Force an immediate re-sync of in-memory index from upstream legacy portal."""
    try:
        portal_client.refresh_cache(force=True)
        return CacheRefreshResponse(
            success=True,
            meters_indexed=len(portal_client._meters_cache),
            transformers_indexed=len(portal_client._transformers_cache),
            timestamp=datetime.now(timezone.utc).isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to refresh cache: {str(e)}")

# -----------------------------------------------------------------------------
# Meter Endpoints
# -----------------------------------------------------------------------------

@app.get("/api/v1/meters", response_model=PaginatedResponse[MeterSummary], tags=["Meters"])
def list_meters(
    q: Optional[str] = Query(None, description="Free-text search matching meter ID, serial number, make, or DT code", examples=["SE33"]),
    make: Optional[str] = Query(None, description="Filter by hardware manufacturer (HPL, Genus, Secure, L&T, Allied)", examples=["Genus"]),
    status: Optional[str] = Query(None, description="Filter by status (Installed, Faulty, Decommissioned)", examples=["Installed"]),
    phase: Optional[str] = Query(None, description="Filter by electrical phase (single, three)", examples=["single"]),
    install_type: Optional[str] = Query(None, description="Filter by installation type (Whole Current, CT Operated)", examples=["Whole Current"]),
    dt_code: Optional[str] = Query(None, description="Filter by distribution transformer code", examples=["DT-001"]),
    page: int = Query(1, ge=1, description="Page number (1-indexed)", examples=[1]),
    page_size: int = Query(20, ge=1, le=100, description="Items per page", examples=[20])
):
    """
    Retrieve paginated smart meters with comprehensive multi-attribute filtering.
    Overcomes the legacy portal's limitation of only searching by meter ID prefix.
    """
    try:
        return portal_client.list_meters(
            query=q,
            make=make,
            status=status,
            phase=phase,
            install_type=install_type,
            dt_code=dt_code,
            page=page,
            page_size=page_size
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/meters/nearby", response_model=List[NearbyMeterResult], tags=["Meters"])
def find_nearby_meters(
    latitude: float = Query(..., description="Latitude coordinate", examples=[26.9124]),
    longitude: float = Query(..., description="Longitude coordinate", examples=[75.7873]),
    radius_km: float = Query(5.0, ge=0.1, le=50.0, description="Search radius in kilometers", examples=[3.0]),
    limit: int = Query(20, ge=1, le=100, description="Maximum meters to return", examples=[10])
):
    """
    Find smart meters in geographic proximity to given coordinates using the Haversine formula.
    """
    try:
        return portal_client.find_nearby_meters(
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            limit=limit
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/meters/{meter_id}", response_model=MeterDetail, tags=["Meters"])
def get_meter_details(
    meter_id: str = FPath(..., description="Alphanumeric meter ID", examples=["J100000"])
):
    """
    Retrieve complete unified meter profile including normalized nameplate details,
    firmware build type, GIS coordinates, and multi-tier network hierarchy.
    """
    try:
        meter = portal_client.get_meter(meter_id)
        if not meter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Smart meter with ID '{meter_id}' was not found in Urja Ops."
            )
        return meter
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/meters/{meter_id}/consumption", response_model=MeterConsumptionResponse, tags=["Meters"])
def get_meter_consumption(
    meter_id: str = FPath(..., description="Alphanumeric meter ID", examples=["J100000"])
):
    """
    Retrieve historical consumption time-series telemetry.
    Converts legacy DD/MM/YYYY timestamps into standard ISO 8601 datetimes,
    normalizes string values into IEEE floats, and calculates consumption metrics & voltage stats.
    """
    try:
        consumption = portal_client.fetch_meter_energy(meter_id)
        if not consumption:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No consumption telemetry found for meter '{meter_id}'."
            )
        return consumption
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/meters/{meter_id}/geo", response_model=GeoLocation, tags=["Meters"])
def get_meter_geo(
    meter_id: str = FPath(..., description="Alphanumeric meter ID", examples=["J100000"])
):
    """
    Retrieve geographical coordinates for a smart meter.
    Mirrors the legacy /portal/meters/{id}/geo endpoint with normalized float coordinates.
    """
    try:
        geo = portal_client.fetch_meter_geo(meter_id)
        if not geo:
            meter = portal_client.get_meter(meter_id)
            if meter and meter.geo:
                return meter.geo
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Coordinates not found for meter '{meter_id}'."
            )
        return geo
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -----------------------------------------------------------------------------
# Bulk Export Route (HMAC-SHA256 Authenticated)
# -----------------------------------------------------------------------------

@app.get("/api/v1/export", tags=["Bulk Export"])
@app.get("/export", tags=["Bulk Export"], include_in_schema=False)
@app.get("/api/v1/meters/export", tags=["Bulk Export"], include_in_schema=False)
def export_all_meters(
    format: str = Query("json", description="Output format: 'json' or 'download'", examples=["json"]),
    live: bool = Query(False, description="Whether to execute a live HMAC-signed request to Urja Ops or serve from synchronized memory cache")
):
    """
    Bulk export all 403 smart meters with full network hierarchies and GPS coordinates.
    Utilizes the reverse-engineered HMAC-SHA256 signature protocol discovered in the Urja portal.
    When format='download', returns a downloadable 'meter-export.json' attachment.
    """
    try:
        import json
        meters = portal_client.export_meters(live=live)

        if format.lower() == "download":
            payload = [m.model_dump() for m in meters]
            content = json.dumps(payload, indent=2)
            return Response(
                content=content,
                media_type="application/json",
                headers={
                    "Content-Disposition": "attachment; filename=\"meter-export.json\""
                }
            )

        return ExportResponse(
            total=len(meters),
            data=meters,
            exported_at=datetime.now(timezone.utc).isoformat(),
            signing_verified=True
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

# -----------------------------------------------------------------------------
# Electrical Hierarchy & Transformers Routes
# -----------------------------------------------------------------------------

@app.get("/api/v1/hierarchy", response_model=List[HierarchyTreeNode], tags=["Network Hierarchy"])
def get_network_hierarchy():
    """
    Retrieve the reconstructed multi-tier utility distribution tree:
    Zone -> Circle -> Division -> Subdivision -> Substation -> Feeder -> DT -> Meters.
    """
    try:
        return portal_client.build_network_hierarchy_tree()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/transformers", response_model=PaginatedResponse[Transformer], tags=["Distribution Transformers"])
@app.get("/api/v1/dts", response_model=PaginatedResponse[Transformer], tags=["Distribution Transformers"], include_in_schema=False)
def list_transformers(
    q: Optional[str] = Query(None, description="Search by transformer code or name", examples=["DT-001"]),
    feeder_code: Optional[str] = Query(None, description="Filter by feeder line code", examples=["F-001"]),
    page: int = Query(1, ge=1, description="Page number", examples=[1]),
    page_size: int = Query(20, ge=1, le=100, description="Items per page", examples=[20])
):
    """
    List distribution transformers (DTs) with connected meter counts and power capacity (kVA).
    Supports pagination and filtering by code, name, or feeder.
    """
    try:
        return portal_client.list_transformers_paginated(
            query=q,
            feeder_code=feeder_code,
            page=page,
            page_size=page_size
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/transformers/{code}", response_model=TransformerDetail, tags=["Distribution Transformers"])
@app.get("/api/v1/dts/{code}", response_model=TransformerDetail, tags=["Distribution Transformers"], include_in_schema=False)
def get_transformer_details(
    code: str = FPath(..., description="Distribution transformer code (e.g. DT-001)", examples=["DT-001"])
):
    """
    Retrieve specific distribution transformer details including its list of connected smart meters.
    """
    try:
        dt = portal_client.get_transformer(code)
        if not dt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transformer with code '{code}' was not found in Urja Ops."
            )
        return dt
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/transformers/{code}/meters", response_model=List[MeterSummary], tags=["Distribution Transformers"])
@app.get("/api/v1/dts/{code}/meters", response_model=List[MeterSummary], tags=["Distribution Transformers"], include_in_schema=False)
def get_transformer_meters(
    code: str = FPath(..., description="Distribution transformer code (e.g. DT-001)", examples=["DT-001"])
):
    """
    Retrieve all smart meters connected downstream of a specific distribution transformer.
    """
    try:
        dt = portal_client.get_transformer(code)
        if not dt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transformer with code '{code}' was not found in Urja Ops."
            )
        return dt.meters
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=settings.debug)
