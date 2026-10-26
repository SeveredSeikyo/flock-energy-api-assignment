from __future__ import annotations
from typing import Optional, List, Dict, Any, Generic, TypeVar
from datetime import datetime
from pydantic import BaseModel, Field

T = TypeVar("T")

class GeoLocation(BaseModel):
    latitude: float = Field(..., description="Latitude coordinate", examples=[26.938961])
    longitude: float = Field(..., description="Longitude coordinate", examples=[75.830957])

class HierarchyUnit(BaseModel):
    name: str = Field(..., description="Name of the administrative network entity", examples=["Malviya Nagar DT 1"])
    code: str = Field(..., description="System code identifier", examples=["DT-001"])

class NetworkHierarchy(BaseModel):
    zone: Optional[HierarchyUnit] = Field(None, description="Distribution Zone")
    circle: Optional[HierarchyUnit] = Field(None, description="Distribution Circle")
    division: Optional[HierarchyUnit] = Field(None, description="Distribution Division")
    subdivision: Optional[HierarchyUnit] = Field(None, description="Distribution Subdivision")
    substation: Optional[HierarchyUnit] = Field(None, description="Distribution Substation")
    feeder: Optional[HierarchyUnit] = Field(None, description="Power Feeder Line")
    dt: Optional[HierarchyUnit] = Field(None, description="Distribution Transformer")

class MeterSummary(BaseModel):
    meter_id: str = Field(..., description="Unique alphanumeric identifier of the meter", examples=["J100000"])
    serial_number: str = Field(..., description="Hardware manufacturer serial number", examples=["SE33962"])
    make: str = Field(..., description="Meter manufacturer (e.g. HPL, Genus, Secure, L&T, Allied)", examples=["HPL"])
    phase_type: str = Field(..., description="Phase specification (single or three)", examples=["single"])
    installation_status: str = Field(..., description="Status (Installed, Faulty, Decommissioned)", examples=["Installed"])
    installation_type: Optional[str] = Field(None, description="Installation type (Whole Current or CT Operated)", examples=["Whole Current"])
    build: Optional[str] = Field(None, description="Firmware build generation (legacy or v2)", examples=["legacy"])
    dt_code: Optional[str] = Field(None, description="Associated Distribution Transformer code", examples=["DT-001"])
    geo: Optional[GeoLocation] = Field(None, description="Geographical coordinates of installation")

class MeterDetail(MeterSummary):
    hierarchy: Optional[NetworkHierarchy] = Field(None, description="Full reconstructed network hierarchy tree")
    raw_attributes: Optional[Dict[str, str]] = Field(None, description="Normalized nameplate parameters scraped from portal")

class EnergyReading(BaseModel):
    timestamp: datetime = Field(..., description="ISO 8601 standardized timestamp", examples=["2026-06-23T23:30:00Z"])
    raw_timestamp: str = Field(..., description="Original timestamp as reported by legacy system (DD/MM/YYYY HH:MM)", examples=["23/06/2026 23:30"])
    kwh: float = Field(..., description="Active energy reading in kilowatt-hours (kWh)", examples=[48438.74])
    kvah: float = Field(..., description="Apparent energy reading in kilovolt-ampere-hours (kVAh)", examples=[52313.84])
    voltage_r: float = Field(..., description="Phase R voltage in Volts", examples=[226.0])

class ConsumptionStats(BaseModel):
    total_readings: int = Field(..., description="Total number of discrete readings in record", examples=[337])
    start_time: Optional[datetime] = Field(None, description="Timestamp of oldest reading in record")
    end_time: Optional[datetime] = Field(None, description="Timestamp of latest reading in record")
    min_voltage: Optional[float] = Field(None, description="Minimum recorded voltage (V)", examples=[215.0])
    max_voltage: Optional[float] = Field(None, description="Maximum recorded voltage (V)", examples=[242.0])
    avg_voltage: Optional[float] = Field(None, description="Average recorded voltage across time window (V)", examples=[228.4])
    total_kwh_delta: Optional[float] = Field(None, description="Net active energy consumed over observation period (kWh)", examples=[142.05])
    total_kvah_delta: Optional[float] = Field(None, description="Net apparent energy consumed over observation period (kVAh)", examples=[153.41])
    latest_reading: Optional[EnergyReading] = Field(None, description="Most recent meter reading")

class MeterConsumptionResponse(BaseModel):
    meter_id: str = Field(..., description="Meter identifier", examples=["J100000"])
    stats: ConsumptionStats = Field(..., description="Aggregated consumption and voltage telemetry analytics")
    readings: List[EnergyReading] = Field(default_factory=list, description="Ordered chronological telemetry readings")

class NearbyMeterResult(BaseModel):
    meter: MeterSummary = Field(..., description="Meter details")
    distance_km: float = Field(..., description="Calculated geodesic distance in kilometers", examples=[1.42])

class Transformer(BaseModel):
    code: str = Field(..., description="Transformer code", examples=["DT-001"])
    name: str = Field(..., description="Transformer name", examples=["Malviya Nagar DT 1"])
    feeder_code: str = Field(..., description="Associated Feeder code", examples=["F-001"])
    capacity_kva: float = Field(..., description="Nameplate power rating in kVA", examples=[100.0])
    connected_meters_count: Optional[int] = Field(None, description="Number of registered downstream smart meters", examples=[11])

class TransformerDetail(Transformer):
    meters: List[MeterSummary] = Field(default_factory=list, description="List of smart meters connected to this transformer")

class HierarchyTreeNode(BaseModel):
    level: str = Field(..., description="Administrative tier (Zone, Circle, Division, Subdivision, Substation, Feeder, DT)", examples=["Zone"])
    code: str = Field(..., description="Identifier code", examples=["Z-01"])
    name: str = Field(..., description="Entity display name", examples=["Jaipur Zone 1"])
    meter_count: int = Field(..., description="Number of meters encompassed under this branch", examples=[135])
    children: List[HierarchyTreeNode] = Field(default_factory=list, description="Subordinate children units")

class PaginatedResponse(BaseModel, Generic[T]):
    items: List[T] = Field(..., description="Page of items")
    total: int = Field(..., description="Total items matching filter", examples=[403])
    page: int = Field(..., description="Current page number (1-indexed)", examples=[1])
    page_size: int = Field(..., description="Page size limit", examples=[20])
    total_pages: int = Field(..., description="Total available pages", examples=[21])

class ExportResponse(BaseModel):
    total: int = Field(..., description="Total count of exported meters", examples=[403])
    data: List[MeterDetail] = Field(..., description="Full dataset of meters with hierarchies and coordinates")
    exported_at: str = Field(..., description="Timestamp of export generation")
    signing_verified: bool = Field(True, description="Whether the HMAC-SHA256 signature was verified by Urja Ops")

class AuthLoginRequest(BaseModel):
    email: Optional[str] = Field(None, description="Operator email. Defaults to configured environment operator if omitted.", examples=["operator@urja.local"])
    password: Optional[str] = Field(None, description="Operator password. Defaults to configured environment operator if omitted.", examples=["urja-ops-2026"])

class AuthStatusResponse(BaseModel):
    authenticated: bool = Field(..., description="Whether client holds a valid session token")
    message: str = Field(..., description="Status description")
    session_cookie_present: bool = Field(..., description="Presence of __Secure-better-auth.session_token cookie")

class AuthLogoutResponse(BaseModel):
    success: bool = Field(..., description="Sign out success status", examples=[True])
    message: str = Field(..., description="Status message", examples=["Successfully signed out."])

class HealthResponse(BaseModel):
    status: str = Field(..., description="Service status", examples=["healthy"])
    legacy_portal_accessible: bool = Field(..., description="Connectivity check to upstream Urja portal", examples=[True])
    cached_meters_count: int = Field(..., description="Count of indexed smart meters in memory", examples=[403])
    cache_last_updated: Optional[str] = Field(None, description="ISO timestamp of last cache sync")

class CacheRefreshResponse(BaseModel):
    success: bool = Field(..., description="Operation success flag")
    meters_indexed: int = Field(..., description="Count of meters synced from portal", examples=[403])
    transformers_indexed: int = Field(..., description="Count of transformers synced", examples=[40])
    timestamp: str = Field(..., description="Refresh completion timestamp")
