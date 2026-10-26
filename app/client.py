import hmac
import hashlib
import time
import math
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import threading

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.config import settings
from app.models import (
    GeoLocation,
    HierarchyUnit,
    NetworkHierarchy,
    MeterSummary,
    MeterDetail,
    EnergyReading,
    ConsumptionStats,
    MeterConsumptionResponse,
    NearbyMeterResult,
    Transformer,
    TransformerDetail,
    HierarchyTreeNode,
    PaginatedResponse,
)

logger = logging.getLogger("urja_client")
logging.basicConfig(level=logging.INFO)

class UrjaPortalClient:
    """
    HTTP adapter and proxy for the legacy Urja Meter Ops portal.
    Handles session authentication, cookie persistence, HMAC signature generation
    for bulk exports, telemetry normalization, in-memory indexing, and HTML fallback scraping.
    """

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.legacy_base_url).rstrip("/")
        self.email = settings.operator_email
        self.password = settings.operator_password
        self.timeout = settings.request_timeout

        # Persistent HTTP client with cookie jar
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/login",
            }
        )

        self._lock = threading.RLock()
        self._authenticated = False

        # In-memory local cache & index
        self._meters_cache: Dict[str, MeterDetail] = {}
        self._transformers_cache: Dict[str, Transformer] = {}
        self._cache_last_updated: Optional[datetime] = None
        self._cache_ttl = settings.cache_ttl_seconds

    # -------------------------------------------------------------------------
    # Authentication & Session Management
    # -------------------------------------------------------------------------

    def is_authenticated(self) -> bool:
        """Check if session cookie exists in cookie jar."""
        for cookie in self.client.cookies.jar:
            if cookie.name == "__Secure-better-auth.session_token" and not cookie.is_expired():
                return True
        return False

    def login(self, email: Optional[str] = None, password: Optional[str] = None) -> bool:
        """
        Authenticate against Urja Meter Ops SvelteKit backend.
        Requires Origin/Referer headers to satisfy SvelteKit CSRF policy.
        """
        login_email = email or self.email
        login_password = password or self.password

        with self._lock:
            login_url = f"{self.base_url}/login"
            headers = {
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/login",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-sveltekit-action": "true",
                "Accept": "application/json",
            }
            payload = {
                "email": login_email,
                "password": login_password,
            }

            try:
                logger.info(f"Submitting login credentials for {login_email}...")
                resp = self.client.post(login_url, data=payload, headers=headers)
                
                # Check for successful redirect action or 200 with location
                if resp.status_code in (200, 302, 303):
                    try:
                        data = resp.json()
                        if data.get("type") == "failure":
                            logger.error(f"Login rejected by legacy portal: {data}")
                            self._authenticated = False
                            return False
                    except Exception:
                        pass

                    if self.is_authenticated():
                        self._authenticated = True
                        logger.info("Successfully established authenticated session with Urja portal.")
                        return True

                logger.warning(f"Login failed. HTTP {resp.status_code}: {resp.text}")
                self._authenticated = False
                return False

            except Exception as exc:
                logger.error(f"Authentication network error: {exc}")
                self._authenticated = False
                return False

    def logout(self) -> bool:
        """
        Sign out of the legacy Urja portal and destroy session tokens.
        Mirrors POST /api/auth/sign-out from the portal.
        """
        with self._lock:
            try:
                signout_url = f"{self.base_url}/api/auth/sign-out"
                self.client.post(
                    signout_url,
                    headers={
                        "Origin": self.base_url,
                        "Referer": f"{self.base_url}/meters",
                    }
                )
            except Exception as e:
                logger.warning(f"Error during upstream sign-out: {e}")
            finally:
                self.client.cookies.clear()
                self._authenticated = False
            return True

    def _ensure_authenticated(self):
        """Ensure an active session exists before executing requests."""
        if not self.is_authenticated():
            success = self.login()
            if not success:
                raise RuntimeError("Unable to authenticate with legacy Urja portal.")

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """
        Execute request with automatic session recovery on 401 Unauthorized.
        """
        self._ensure_authenticated()
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"

        resp = self.client.request(method, url, **kwargs)

        # Detect expired session
        if resp.status_code == 401 or (resp.status_code == 200 and "A valid session is required" in resp.text):
            logger.warning("Session token expired or invalidated. Refreshing credentials...")
            with self._lock:
                self.login()
            # Retry original request once
            resp = self.client.request(method, url, **kwargs)

        return resp

    # -------------------------------------------------------------------------
    # Upstream Legacy Endpoint Wrappers
    # -------------------------------------------------------------------------

    def fetch_meters_search_portal(self, query: str = "", page: int = 1) -> dict:
        """Raw call to legacy portal /portal/meters/search?q={query}&page={page}."""
        resp = self._request("GET", f"/portal/meters/search?q={query}&page={page}")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch meters search: HTTP {resp.status_code}")
        return resp.json()

    def fetch_transformers_portal(self, page: int = 1) -> dict:
        """Raw call to legacy portal /portal/dts?page={page}."""
        resp = self._request("GET", f"/portal/dts?page={page}")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch transformers: HTTP {resp.status_code}")
        return resp.json()

    def fetch_meter_geo(self, meter_id: str) -> Optional[GeoLocation]:
        """Fetch normalized coordinates from /portal/meters/{id}/geo."""
        resp = self._request("GET", f"/portal/meters/{meter_id}/geo")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning(f"Error fetching geo for meter {meter_id}: HTTP {resp.status_code}")
            return None
        
        data = resp.json().get("data", {})
        try:
            return GeoLocation(
                latitude=float(data.get("latitude")),
                longitude=float(data.get("longitude"))
            )
        except (ValueError, TypeError):
            return None

    def fetch_meter_energy(self, meter_id: str) -> Optional[MeterConsumptionResponse]:
        """
        Fetch and normalize consumption telemetry from /portal/meters/{id}/energy.
        Converts non-standard DD/MM/YYYY timestamps to ISO 8601.
        Converts stringified decimal values to IEEE-754 floats.
        Calculates aggregate statistical metrics.
        """
        resp = self._request("GET", f"/portal/meters/{meter_id}/energy")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch energy history for {meter_id}: HTTP {resp.status_code}")

        raw_records = resp.json().get("data", [])
        if not raw_records:
            return MeterConsumptionResponse(
                meter_id=meter_id,
                stats=ConsumptionStats(total_readings=0),
                readings=[]
            )

        parsed_readings: List[EnergyReading] = []
        voltages: List[float] = []

        for item in raw_records:
            raw_ts = item.get("timestamp", "")
            try:
                # Format is DD/MM/YYYY HH:MM
                dt = datetime.strptime(raw_ts, "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                try:
                    dt = date_parser.parse(raw_ts)
                    if not dt.tzinfo:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = datetime.now(timezone.utc)

            try:
                kwh_val = float(item.get("kwh", 0.0))
            except (ValueError, TypeError):
                kwh_val = 0.0

            try:
                kvah_val = float(item.get("kvah", 0.0))
            except (ValueError, TypeError):
                kvah_val = 0.0

            try:
                volt_val = float(item.get("voltR", 0.0))
                voltages.append(volt_val)
            except (ValueError, TypeError):
                volt_val = 0.0

            parsed_readings.append(EnergyReading(
                timestamp=dt,
                raw_timestamp=raw_ts,
                kwh=kwh_val,
                kvah=kvah_val,
                voltage_r=volt_val
            ))

        # Sort chronologically to resolve out-of-order pushes
        parsed_readings.sort(key=lambda x: x.timestamp)

        # Calculate analytics
        first_r = parsed_readings[0]
        last_r = parsed_readings[-1]
        kwh_delta = round(last_r.kwh - first_r.kwh, 4) if len(parsed_readings) > 1 else 0.0
        kvah_delta = round(last_r.kvah - first_r.kvah, 4) if len(parsed_readings) > 1 else 0.0

        stats = ConsumptionStats(
            total_readings=len(parsed_readings),
            start_time=first_r.timestamp,
            end_time=last_r.timestamp,
            min_voltage=min(voltages) if voltages else None,
            max_voltage=max(voltages) if voltages else None,
            avg_voltage=round(sum(voltages) / len(voltages), 2) if voltages else None,
            total_kwh_delta=kwh_delta,
            total_kvah_delta=kvah_delta,
            latest_reading=last_r
        )

        return MeterConsumptionResponse(
            meter_id=meter_id,
            stats=stats,
            readings=parsed_readings
        )

    # -------------------------------------------------------------------------
    # Bulk Export with HMAC-SHA256 Signing
    # -------------------------------------------------------------------------

    def fetch_bulk_export(self) -> List[dict]:
        """
        Acquire signing secret and execute HMAC-SHA256 authenticated GET to /portal/export?page=1.
        Returns the entire collection of 403 meters with hierarchy and geo information.
        """
        # Step 1: Obtain signing secret
        keys_resp = self._request("GET", "/portal/keys")
        if keys_resp.status_code != 200:
            raise RuntimeError(f"Unable to retrieve signing key: HTTP {keys_resp.status_code}")
        
        secret = keys_resp.json().get("data", {}).get("signingSecret")
        if not secret:
            raise RuntimeError("Signing secret missing in /portal/keys response")

        # Step 2: Compute HMAC SHA-256 signature
        # Structure: METHOD\nPATH\nQUERY\nTIMESTAMP
        method = "GET"
        path = "/portal/export"
        query = "page=1"
        ts = str(int(time.time()))

        message = f"{method}\n{path}\n{query}\n{ts}"
        signature = hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        # Step 3: Fetch export with signature headers
        export_headers = {
            "x-timestamp": ts,
            "x-signature": signature,
            "Referer": f"{self.base_url}/transformers",
        }
        
        export_resp = self._request("GET", f"/portal/export?{query}", headers=export_headers)
        if export_resp.status_code != 200:
            raise RuntimeError(f"Bulk export query failed: HTTP {export_resp.status_code} - {export_resp.text}")

        return export_resp.json().get("data", [])

    # -------------------------------------------------------------------------
    # HTML Fallback Scraper for Single Meter Detail
    # -------------------------------------------------------------------------

    def scrape_meter_html_detail(self, meter_id: str) -> Optional[Dict[str, Any]]:
        """
        Scrape legacy SSR HTML page /meters/{meter_id} using BeautifulSoup4.
        Extracts dl/dt/dd nameplate elements and embedded state.
        Normalizes keys across legacy and v2 schema variations.
        """
        resp = self._request("GET", f"/meters/{meter_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning(f"HTML fetch error for meter {meter_id}: HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        nameplate_attributes: Dict[str, str] = {}

        # Parse <dt> and <dd> pairs
        dts = [dt.get_text(strip=True) for dt in soup.find_all("dt")]
        dds = [dd.get_text(strip=True) for dd in soup.find_all("dd")]
        for key, val in zip(dts, dds):
            nameplate_attributes[key] = val

        return nameplate_attributes

    # -------------------------------------------------------------------------
    # In-Memory Indexing, Caching, and Query Engine
    # -------------------------------------------------------------------------

    def is_cache_valid(self) -> bool:
        if not self._cache_last_updated:
            return False
        delta = (datetime.now(timezone.utc) - self._cache_last_updated).total_seconds()
        return delta < self._cache_ttl and len(self._meters_cache) > 0

    def refresh_cache(self, force: bool = False):
        """
        Synchronize in-memory cache with upstream portal.
        Uses bulk signed export for fast O(1) ingestion,
        and indexes transformers and network tree.
        """
        with self._lock:
            if not force and self.is_cache_valid():
                return

            logger.info("Synchronizing in-memory index from Urja portal...")
            raw_meters = self.fetch_bulk_export()

            # Index meters
            new_meters: Dict[str, MeterDetail] = {}
            for item in raw_meters:
                m_id = item.get("meterId")
                if not m_id:
                    continue

                # Parse Geo
                geo_data = item.get("geo")
                geo_obj = None
                if geo_data and "lat" in geo_data and "lng" in geo_data:
                    try:
                        geo_obj = GeoLocation(latitude=float(geo_data["lat"]), longitude=float(geo_data["lng"]))
                    except (ValueError, TypeError):
                        pass

                # Parse Hierarchy
                h_data = item.get("hierarchy", {})
                def parse_unit(key: str) -> Optional[HierarchyUnit]:
                    unit = h_data.get(key)
                    if unit and isinstance(unit, dict) and "name" in unit and "code" in unit:
                        return HierarchyUnit(name=unit["name"], code=unit["code"])
                    return None

                hierarchy_obj = NetworkHierarchy(
                    zone=parse_unit("zone"),
                    circle=parse_unit("circle"),
                    division=parse_unit("division"),
                    subdivision=parse_unit("subdivision"),
                    substation=parse_unit("substation"),
                    feeder=parse_unit("feeder"),
                    dt=parse_unit("dt")
                )

                meter_detail = MeterDetail(
                    meter_id=m_id,
                    serial_number=item.get("serialNo", "UNKNOWN"),
                    make=item.get("make", "UNKNOWN"),
                    phase_type=item.get("phaseType", "single"),
                    installation_status=item.get("installStatus", "UNKNOWN"),
                    installation_type=item.get("installType"),
                    build=item.get("build"),
                    dt_code=item.get("dtCode"),
                    geo=geo_obj,
                    hierarchy=hierarchy_obj,
                    raw_attributes={
                        "Make": item.get("make", ""),
                        "Phase Type": item.get("phaseType", ""),
                        "Installation Status": item.get("installStatus", ""),
                        "Installation Type": item.get("installType", ""),
                        "Build": item.get("build", "")
                    }
                )
                new_meters[m_id] = meter_detail

            # Index Transformers
            new_transformers: Dict[str, Transformer] = {}
            for page in (1, 2):
                try:
                    dt_data = self.fetch_transformers_portal(page=page)
                    for dt_entry in dt_data.get("data", []):
                        code = dt_entry.get("code")
                        if code:
                            new_transformers[code] = Transformer(
                                code=code,
                                name=dt_entry.get("name", ""),
                                feeder_code=dt_entry.get("feederCode", ""),
                                capacity_kva=float(dt_entry.get("capacityKva", 0.0)),
                                connected_meters_count=0
                            )
                except Exception as ex:
                    logger.warning(f"Error fetching DT page {page}: {ex}")

            # Compute connected meters per transformer
            for meter in new_meters.values():
                if meter.dt_code and meter.dt_code in new_transformers:
                    cur = new_transformers[meter.dt_code].connected_meters_count or 0
                    new_transformers[meter.dt_code].connected_meters_count = cur + 1

            self._meters_cache = new_meters
            self._transformers_cache = new_transformers
            self._cache_last_updated = datetime.now(timezone.utc)
            logger.info(f"Cache synchronized successfully: {len(new_meters)} meters, {len(new_transformers)} transformers.")

    # -------------------------------------------------------------------------
    # High-Level Query Methods
    # -------------------------------------------------------------------------

    def get_meter(self, meter_id: str) -> Optional[MeterDetail]:
        """Lookup meter profile from index; fall back to individual upstream endpoints if absent."""
        self.refresh_cache()
        if meter_id in self._meters_cache:
            return self._meters_cache[meter_id]

        # Upstream fallback lookup
        logger.info(f"Meter {meter_id} not in cache, attempting direct query...")
        geo = self.fetch_meter_geo(meter_id)
        html_attrs = self.scrape_meter_html_detail(meter_id)
        if not html_attrs and not geo:
            return None

        # Normalize parsed html attributes
        def find_attr(keys: List[str], default: str = "") -> str:
            for k in keys:
                if k in html_attrs:
                    return html_attrs[k]
            return default

        serial = find_attr(["Serial No", "SerialNo", "serialNo", "serial_number"], default="UNKNOWN")
        make = find_attr(["Make", "make"], default="UNKNOWN")
        phase = find_attr(["Phase Type", "PhaseType", "phaseType"], default="single")
        status = find_attr(["Installation Status", "InstallationStatus", "installStatus"], default="UNKNOWN")
        inst_type = find_attr(["Installation Type", "InstallationType", "installType"], default="Whole Current")

        meter_detail = MeterDetail(
            meter_id=meter_id,
            serial_number=serial,
            make=make,
            phase_type=phase,
            installation_status=status,
            installation_type=inst_type,
            geo=geo,
            raw_attributes=html_attrs
        )
        self._meters_cache[meter_id] = meter_detail
        return meter_detail

    def list_meters(
        self,
        query: Optional[str] = None,
        make: Optional[str] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        install_type: Optional[str] = None,
        dt_code: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> PaginatedResponse[MeterSummary]:
        """
        Filtering & pagination engine supporting multi-attribute searches
        that the legacy portal could not execute.
        """
        self.refresh_cache()
        results: List[MeterSummary] = []

        q_lower = query.lower().strip() if query else None
        make_lower = make.lower().strip() if make else None
        status_lower = status.lower().strip() if status else None
        phase_lower = phase.lower().strip() if phase else None
        type_lower = install_type.lower().strip() if install_type else None
        dt_lower = dt_code.lower().strip() if dt_code else None

        for m in self._meters_cache.values():
            # Query match across ID, serial, make, DT
            if q_lower:
                match = (
                    q_lower in m.meter_id.lower() or
                    q_lower in m.serial_number.lower() or
                    q_lower in m.make.lower() or
                    (m.dt_code and q_lower in m.dt_code.lower())
                )
                if not match:
                    continue

            # Exact or substring filters
            if make_lower and make_lower != m.make.lower():
                continue
            if status_lower and status_lower != m.installation_status.lower():
                continue
            if phase_lower and phase_lower != m.phase_type.lower():
                continue
            if type_lower and (not m.installation_type or type_lower != m.installation_type.lower()):
                continue
            if dt_lower and (not m.dt_code or dt_lower != m.dt_code.lower()):
                continue

            summary = MeterSummary(
                meter_id=m.meter_id,
                serial_number=m.serial_number,
                make=m.make,
                phase_type=m.phase_type,
                installation_status=m.installation_status,
                installation_type=m.installation_type,
                build=m.build,
                dt_code=m.dt_code,
                geo=m.geo
            )
            results.append(summary)

        # Sort by meter ID
        results.sort(key=lambda x: x.meter_id)

        total = len(results)
        total_pages = max(1, math.ceil(total / page_size))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = results[start_idx:end_idx]

        return PaginatedResponse(
            items=page_items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )

    def find_nearby_meters(
        self,
        latitude: float,
        longitude: float,
        radius_km: float = 5.0,
        limit: int = 50
    ) -> List[NearbyMeterResult]:
        """
        Geospatial proximity query utilizing the Haversine great-circle distance formula.
        """
        self.refresh_cache()
        nearby: List[NearbyMeterResult] = []

        R = 6371.0  # Earth's radius in kilometers
        lat1 = math.radians(latitude)
        lon1 = math.radians(longitude)

        for m in self._meters_cache.values():
            if not m.geo:
                continue

            lat2 = math.radians(m.geo.latitude)
            lon2 = math.radians(m.geo.longitude)

            dlat = lat2 - lat1
            dlon = lon2 - lon1

            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = R * c

            if dist <= radius_km:
                summary = MeterSummary(
                    meter_id=m.meter_id,
                    serial_number=m.serial_number,
                    make=m.make,
                    phase_type=m.phase_type,
                    installation_status=m.installation_status,
                    installation_type=m.installation_type,
                    build=m.build,
                    dt_code=m.dt_code,
                    geo=m.geo
                )
                nearby.append(NearbyMeterResult(
                    meter=summary,
                    distance_km=round(dist, 3)
                ))

        nearby.sort(key=lambda x: x.distance_km)
        return nearby[:limit]

    def list_transformers(self) -> List[Transformer]:
        """List all distribution transformers enriched with registered meter count."""
        self.refresh_cache()
        return sorted(list(self._transformers_cache.values()), key=lambda x: x.code)

    def list_transformers_paginated(
        self,
        query: Optional[str] = None,
        feeder_code: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> PaginatedResponse[Transformer]:
        """Paginated and searchable list of distribution transformers."""
        self.refresh_cache()
        results: List[Transformer] = list(self._transformers_cache.values())

        if query:
            q_lower = query.lower().strip()
            results = [t for t in results if q_lower in t.code.lower() or q_lower in t.name.lower()]

        if feeder_code:
            f_lower = feeder_code.lower().strip()
            results = [t for t in results if f_lower == t.feeder_code.lower()]

        results.sort(key=lambda x: x.code)
        total = len(results)
        total_pages = max(1, math.ceil(total / page_size))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        return PaginatedResponse(
            items=results[start_idx:end_idx],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )

    def get_transformer(self, code: str) -> Optional[TransformerDetail]:
        """Get individual transformer details with its connected meters."""
        self.refresh_cache()
        code_upper = code.strip().upper()

        target: Optional[Transformer] = None
        for dt_code, dt_obj in self._transformers_cache.items():
            if dt_code.upper() == code_upper:
                target = dt_obj
                break

        if not target:
            return None

        # Gather connected meters
        connected: List[MeterSummary] = []
        for m in self._meters_cache.values():
            if m.dt_code and m.dt_code.upper() == code_upper:
                connected.append(MeterSummary(
                    meter_id=m.meter_id,
                    serial_number=m.serial_number,
                    make=m.make,
                    phase_type=m.phase_type,
                    installation_status=m.installation_status,
                    installation_type=m.installation_type,
                    build=m.build,
                    dt_code=m.dt_code,
                    geo=m.geo
                ))

        connected.sort(key=lambda x: x.meter_id)

        return TransformerDetail(
            code=target.code,
            name=target.name,
            feeder_code=target.feeder_code,
            capacity_kva=target.capacity_kva,
            connected_meters_count=len(connected),
            meters=connected
        )

    def get_meters_for_transformer(self, code: str) -> List[MeterSummary]:
        """Get all smart meters connected to a specific distribution transformer."""
        dt_detail = self.get_transformer(code)
        if not dt_detail:
            return []
        return dt_detail.meters

    def export_meters(self, live: bool = False) -> List[MeterDetail]:
        """
        Export all meters with complete hierarchy and coordinates.
        If live=True, executes a fresh signed HMAC export request to /portal/export.
        Otherwise serves from synchronized memory cache.
        """
        if live:
            self.refresh_cache(force=True)
        else:
            self.refresh_cache(force=False)
        return sorted(list(self._meters_cache.values()), key=lambda x: x.meter_id)

    def build_network_hierarchy_tree(self) -> List[HierarchyTreeNode]:
        """
        Reconstruct clean multi-tier administrative network tree:
        Zone -> Circle -> Division -> Subdivision -> Substation -> Feeder -> DT -> Meters
        """
        self.refresh_cache()
        tree_root: Dict[str, Any] = {}

        for m in self._meters_cache.values():
            if not m.hierarchy:
                continue

            z = m.hierarchy.zone
            c = m.hierarchy.circle
            d = m.hierarchy.division
            sd = m.hierarchy.subdivision
            ss = m.hierarchy.substation
            f = m.hierarchy.feeder
            dt = m.hierarchy.dt

            z_code = z.code if z else "Z-UNKNOWN"
            z_name = z.name if z else "Unknown Zone"

            c_code = c.code if c else "C-UNKNOWN"
            c_name = c.name if c else "Unknown Circle"

            d_code = d.code if d else "D-UNKNOWN"
            d_name = d.name if d else "Unknown Division"

            sd_code = sd.code if sd else "SD-UNKNOWN"
            sd_name = sd.name if sd else "Unknown Subdivision"

            ss_code = ss.code if ss else "SS-UNKNOWN"
            ss_name = ss.name if ss else "Unknown Substation"

            f_code = f.code if f else "F-UNKNOWN"
            f_name = f.name if f else "Unknown Feeder"

            dt_code = dt.code if dt else (m.dt_code or "DT-UNKNOWN")
            dt_name = dt.name if dt else f"Transformer {dt_code}"

            # Zone level
            if z_code not in tree_root:
                tree_root[z_code] = {"name": z_name, "count": 0, "circles": {}}
            tree_root[z_code]["count"] += 1

            # Circle level
            z_circles = tree_root[z_code]["circles"]
            if c_code not in z_circles:
                z_circles[c_code] = {"name": c_name, "count": 0, "divisions": {}}
            z_circles[c_code]["count"] += 1

            # Division level
            c_divs = z_circles[c_code]["divisions"]
            if d_code not in c_divs:
                c_divs[d_code] = {"name": d_name, "count": 0, "subdivisions": {}}
            c_divs[d_code]["count"] += 1

            # Subdivision level
            d_subs = c_divs[d_code]["subdivisions"]
            if sd_code not in d_subs:
                d_subs[sd_code] = {"name": sd_name, "count": 0, "substations": {}}
            d_subs[sd_code]["count"] += 1

            # Substation level
            sd_ss = d_subs[sd_code]["substations"]
            if ss_code not in sd_ss:
                sd_ss[ss_code] = {"name": ss_name, "count": 0, "feeders": {}}
            sd_ss[ss_code]["count"] += 1

            # Feeder level
            ss_feeders = sd_ss[ss_code]["feeders"]
            if f_code not in ss_feeders:
                ss_feeders[f_code] = {"name": f_name, "count": 0, "dts": {}}
            ss_feeders[f_code]["count"] += 1

            # DT level
            f_dts = ss_feeders[f_code]["dts"]
            if dt_code not in f_dts:
                f_dts[dt_code] = {"name": dt_name, "count": 0}
            f_dts[dt_code]["count"] += 1

        # Transform dictionary tree into HierarchyTreeNode instances
        final_tree: List[HierarchyTreeNode] = []

        for z_code, z_val in sorted(tree_root.items()):
            c_nodes: List[HierarchyTreeNode] = []
            for c_code, c_val in sorted(z_val["circles"].items()):
                d_nodes: List[HierarchyTreeNode] = []
                for d_code, d_val in sorted(c_val["divisions"].items()):
                    sd_nodes: List[HierarchyTreeNode] = []
                    for sd_code, sd_val in sorted(d_val["subdivisions"].items()):
                        ss_nodes: List[HierarchyTreeNode] = []
                        for ss_code, ss_val in sorted(sd_val["substations"].items()):
                            f_nodes: List[HierarchyTreeNode] = []
                            for f_code, f_val in sorted(ss_val["feeders"].items()):
                                dt_nodes: List[HierarchyTreeNode] = []
                                for dt_code, dt_val in sorted(f_val["dts"].items()):
                                    dt_nodes.append(HierarchyTreeNode(
                                        level="DT",
                                        code=dt_code,
                                        name=dt_val["name"],
                                        meter_count=dt_val["count"],
                                        children=[]
                                    ))
                                f_nodes.append(HierarchyTreeNode(
                                    level="Feeder",
                                    code=f_code,
                                    name=f_val["name"],
                                    meter_count=f_val["count"],
                                    children=dt_nodes
                                ))
                            ss_nodes.append(HierarchyTreeNode(
                                level="Substation",
                                code=ss_code,
                                name=ss_val["name"],
                                meter_count=ss_val["count"],
                                children=f_nodes
                            ))
                        sd_nodes.append(HierarchyTreeNode(
                            level="Subdivision",
                            code=sd_code,
                            name=sd_val["name"],
                            meter_count=sd_val["count"],
                            children=ss_nodes
                        ))
                    d_nodes.append(HierarchyTreeNode(
                        level="Division",
                        code=d_code,
                        name=d_val["name"],
                        meter_count=d_val["count"],
                        children=sd_nodes
                    ))
                c_nodes.append(HierarchyTreeNode(
                    level="Circle",
                    code=c_code,
                    name=c_val["name"],
                    meter_count=c_val["count"],
                    children=d_nodes
                ))
            final_tree.append(HierarchyTreeNode(
                level="Zone",
                code=z_code,
                name=z_val["name"],
                meter_count=z_val["count"],
                children=c_nodes
            ))

        return final_tree
