# Urja Meter Ops — Modern REST API Service

[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat&logo=python)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![OpenAPI](https://img.shields.io/badge/OpenAPI-3.1.0-orange.svg)](openapi.json)

A documented REST API wrapper built over the internal **Urja Meter Ops** utility portal (`https://urja-ops.flockenergy.tech`).

---

## Table of Contents

- [Overview & What Was Built](#overview--what-was-built)
- [Key Features & Capabilities](#key-features--capabilities)
- [Project Structure](#project-structure)
- [Quick Start: Installation & Execution](#quick-start-installation--execution)
- [Interactive API Documentation](#interactive-api-documentation)
- [Sample Requests & Curl Commands](#sample-requests--curl-commands)
- [Notes: Architecture, Trade-Offs & Decisions](#notes-architecture-trade-offs--decisions)
  - [Assumptions Made](#assumptions-made)
  - [Design Decisions & Trade-Offs](#design-decisions--trade-offs)
  - [What Was Intentionally Left Out & Why](#what-was-intentionally-left-out--why)
  - [What Would Be Improved With More Time](#what-would-be-improved-with-more-time)
- [Reflection](#reflection)
- [Companion Documents](#companion-documents)

---

## Overview & What Was Built

Field operations and data teams rely on the Urja Meter Ops web portal to check smart meter inventory, network hierarchy, and consumption readings. However, because the portal provides no public API, teams have historically had to manually navigate and copy data from browser tables.

This service acts as an automated API proxy layer in front of the legacy Urja portal. It manages session authentication and recovery, handles data normalization (converting non-standard dates and stringified numbers into typed schemas), provides multi-attribute filtering that the legacy portal lacked, and exposes documented REST endpoints for downstream microservices and data pipelines.

---

## Key Features & Capabilities

* **Automated Session Lifecycle**: Manages session cookies (`__Secure-better-auth.session_token`), satisfies SvelteKit CSRF headers, and automatically re-authenticates when tokens expire.
* **HMAC Bulk Ingestion & Export**: Implements the portal's internal HMAC-SHA256 signature protocol (`/portal/export`), allowing fast ingestion and exposing a dedicated endpoint (`/api/v1/export`) supporting JSON and direct file downloads (`meter-export.json`).
* **Data Normalization**: Converts non-standard `DD/MM/YYYY` timestamps into ISO 8601 UTC datetimes, casts numeric strings to floats, and normalizes schema discrepancies between legacy and v2 meter builds.
* **Multi-Attribute Query Engine**: Supports filtering by manufacturer (`HPL`, `Genus`, `Secure`, `L&T`, `Allied`), status (`Installed`, `Faulty`, `Decommissioned`), phase (`single`, `three`), and transformer code.
* **Geospatial Proximity Search**: Spatial querying using the Haversine great-circle formula to locate meters near specified GPS coordinates.
* **Electrical Topology**: Reconstructs the 7-tier distribution hierarchy (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT › Meters`) and lists distribution transformers with connected meter counts.

---

## Project Structure

```
project/
│
├── app/
│   ├── __init__.py          # Application package initialization
│   ├── config.py            # Configuration settings and environment variable defaults
│   ├── models.py            # Pydantic v2 data models, validation schemas, and responses
│   ├── client.py            # Urja portal HTTP adapter, session manager, and query engine
│   └── main.py              # FastAPI application, route definitions, and CORS middleware
│
├── openapi.json             # Exported OpenAPI 3.1.0 specification
├── PROTOCOL.md              # Documentation of legacy portal behavior, authentication, and protocols
├── REFLECTION.md            # Answers to the project reflection questions
├── README.md                # Project documentation, setup guide, and architectural notes
├── requirements.txt         # Project dependencies
└── .gitignore               # Git ignore rules
```

---

## Quick Start: Installation & Execution

### 1. Requirements
* Python 3.10+ (tested on Python 3.12)
* Internet access (to communicate with `https://urja-ops.flockenergy.tech`)

### 2. Setup Virtual Environment & Install Dependencies
```bash
# Navigate to project directory
cd project

# Create & activate a virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Run the Server
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
The server will start up, automatically authenticate against the Urja portal, synchronize the meter inventory, and serve endpoints on `http://localhost:8000`.

---

## Interactive API Documentation

Once the server is running:
* **Interactive Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **ReDoc Documentation**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
* **Raw OpenAPI Specification**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## Sample Requests & Curl Commands

### 1. Health & Portal Status
Checks service health and confirms active connectivity to the upstream portal:
```bash
curl -X GET "http://localhost:8000/api/v1/health"
```
```json
{
  "status": "healthy",
  "legacy_portal_accessible": true,
  "cached_meters_count": 403,
  "cache_last_updated": "2026-10-26T08:35:10.123456+00:00"
}
```

---

### 2. List & Filter Smart Meters
Retrieve paginated meters with multi-attribute filtering (e.g. manufacturer `Genus`, status `Faulty`):
```bash
curl -X GET "http://localhost:8000/api/v1/meters?make=Genus&status=Faulty&page=1&page_size=2"
```
```json
{
  "items": [
    {
      "meter_id": "J100004",
      "serial_number": "SE65293",
      "make": "Genus",
      "phase_type": "single",
      "installation_status": "Faulty",
      "installation_type": "CT Operated",
      "build": "v2",
      "dt_code": "DT-005",
      "geo": {
        "latitude": 26.907425,
        "longitude": 75.725037
      }
    },
    {
      "meter_id": "J100010",
      "serial_number": "SE88410",
      "make": "Genus",
      "phase_type": "single",
      "installation_status": "Faulty",
      "installation_type": "Whole Current",
      "build": "legacy",
      "dt_code": "DT-011",
      "geo": {
        "latitude": 26.891102,
        "longitude": 75.782015
      }
    }
  ],
  "total": 19,
  "page": 1,
  "page_size": 2,
  "total_pages": 10
}
```

---

### 3. Unified Meter Detail
Fetch complete profile with nameplate details, coordinates, and full electrical network path:
```bash
curl -X GET "http://localhost:8000/api/v1/meters/J100000"
```
```json
{
  "meter_id": "J100000",
  "serial_number": "SE33962",
  "make": "HPL",
  "phase_type": "single",
  "installation_status": "Decommissioned",
  "installation_type": "Whole Current",
  "build": "legacy",
  "dt_code": "DT-001",
  "geo": {
    "latitude": 26.938961,
    "longitude": 75.830957
  },
  "hierarchy": {
    "zone": { "name": "Jaipur Zone 1", "code": "Z-01" },
    "circle": { "name": "Circle 1", "code": "C-01" },
    "division": { "name": "Division 1", "code": "D-01" },
    "subdivision": { "name": "Subdivision 1", "code": "SD-01" },
    "substation": { "name": "Substation 1", "code": "SS-01" },
    "feeder": { "name": "Feeder 1", "code": "F-001" },
    "dt": { "name": "Malviya Nagar DT 1", "code": "DT-001" }
  },
  "raw_attributes": {
    "Make": "HPL",
    "Phase Type": "single",
    "Installation Status": "Decommissioned",
    "Installation Type": "Whole Current",
    "Build": "legacy"
  }
}
```

---

### 4. Normalized Consumption History & Telemetry Analytics
Fetches 7 days of 30-minute interval readings, converts non-standard dates to ISO 8601, and calculates aggregate metrics:
```bash
curl -X GET "http://localhost:8000/api/v1/meters/J100000/consumption"
```
```json
{
  "meter_id": "J100000",
  "stats": {
    "total_readings": 337,
    "start_time": "2026-06-23T23:30:00Z",
    "end_time": "2026-06-30T23:30:00Z",
    "min_voltage": 215.0,
    "max_voltage": 242.0,
    "avg_voltage": 228.45,
    "total_kwh_delta": 142.05,
    "total_kvah_delta": 153.41,
    "latest_reading": {
      "timestamp": "2026-06-30T23:30:00Z",
      "raw_timestamp": "30/06/2026 23:30",
      "kwh": 48580.79,
      "kvah": 52467.25,
      "voltage_r": 220.0
    }
  },
  "readings": [
    {
      "timestamp": "2026-06-23T23:30:00Z",
      "raw_timestamp": "23/06/2026 23:30",
      "kwh": 48438.74,
      "kvah": 52313.84,
      "voltage_r": 226.0
    }
    /* ... additional readings ... */
  ]
}
```

---

### 5. Bulk Export Full Dataset (HMAC-SHA256 Authenticated)
Direct programmatic access to export the full 403-meter dataset with complete hierarchies and GIS coordinates. Supports JSON format or direct file download (`meter-export.json`):
```bash
# JSON payload response
curl -X GET "http://localhost:8000/api/v1/export?format=json"

# Direct file download attachment
curl -X GET "http://localhost:8000/api/v1/export?format=download" -o meter-export.json
```

---

### 6. Distribution Transformers & Connected Meters
List transformers with pagination and search, inspect single transformer details, or retrieve all smart meters connected downstream of a transformer:
```bash
# List paginated transformers
curl -X GET "http://localhost:8000/api/v1/transformers?page=1&page_size=2"

# Inspect specific transformer details
curl -X GET "http://localhost:8000/api/v1/transformers/DT-001"

# Get all smart meters connected to DT-001
curl -X GET "http://localhost:8000/api/v1/transformers/DT-001/meters"
```

---

### 7. Geospatial Proximity Search
Find meters within a radius of given GPS coordinates:
```bash
curl -X GET "http://localhost:8000/api/v1/meters/nearby?latitude=26.9389&longitude=75.8309&radius_km=1.5&limit=2"
```

---

### 8. Distribution Network Hierarchy Tree
Returns the nested distribution tree for the electrical grid:
```bash
curl -X GET "http://localhost:8000/api/v1/hierarchy"
```

---

### 9. Operator Sign-Out & Session Termination
Explicitly signs out of the Urja Ops portal and clears all active session cookies:
```bash
curl -X POST "http://localhost:8000/api/v1/auth/logout"
```

---

## Notes: Architecture, Trade-Offs & Decisions

### Assumptions Made
1. **Read-Only Telemetry Focus**: The Urja portal operates as an informational monitoring dashboard rather than a transactional write system. Downstream systems require high reliability, normalized data structures, and expressive search, rather than mutation endpoints.
2. **Fleet Dimension**: The Jaipur smart meter inventory for this utility section encompasses ~400–1,000 smart meters and ~40 DTs. Holding this dataset in memory is computationally cheap (~1.2MB of RAM) and unlocks sub-millisecond query responses.
3. **Telemetry Window**: The legacy portal provides a rolling 7-day window (337 readings per meter at 30-minute intervals). We assume consumers need both the full chronological array and precalculated delta metrics.

### Design Decisions & Trade-Offs

#### 1. Ingesting via HMAC Bulk Export vs Point-to-Point Scraping
* **Trade-Off**: Scraping individual meter HTML pages (`/meters/{id}`) is brittle and requires 400+ sequential HTTP requests, taking over 30 seconds and straining the upstream server.
* **Decision**: While an HTML fallback scraper is maintained in `client.py` for resilience, reverse-engineering the HMAC-SHA256 bulk export protocol (`/portal/export`) enables ingesting the entire dataset in a single 230KB request in ~350ms.

#### 2. In-Memory Indexing Layer vs External Database
* **Trade-Off**: Adding PostgreSQL/PostGIS or Redis would provide distributed caching and out-of-process persistence, but would introduce multi-container deployment overhead.
* **Decision**: Maintained the indexing layer directly in Python memory backed by a thread-safe cache with a configurable TTL (default 300s). This enables instant setup with zero external database dependencies.

#### 3. Automatic Re-Authentication
* **Trade-Off**: Re-authenticating on every request adds latency; relying on cookies until they expire risks occasional 401 failures.
* **Decision**: Implemented proactive cookie validation with automatic reactive recovery. The client validates whether `__Secure-better-auth.session_token` is present; if any request encounters a 401, it acquires a re-authentication lock, refreshes the session cookie, and transparently retries the request.

### What Was Intentionally Left Out & Why
1. **Database Persistence Layer**: Intentionally omitted external databases (Postgres, Mongo, Redis) to keep the repository self-contained and runnable with a single command.
2. **Upstream Mutation Operations**: Did not expose write endpoints (e.g. `POST /api/v1/meters`) because the upstream Urja portal does not support meter provisioning or configuration mutations.
3. **Complex User Roles**: Urja portal utilizes a single operator desk credential (`operator@urja.local`). Adding multi-tenant RBAC inside this proxy would be premature without downstream identity requirements.

### What Would Be Improved With More Time
1. **Time-Series Telemetry Store**: Stream 30-minute interval readings into TimescaleDB or ClickHouse to accumulate long-term historical trends beyond the portal's 7-day rolling window.
2. **Upstream Outage Circuit Breaker**: Implement a circuit breaker (`pybreaker` or `tenacity`) with stale-cache fallback so that downstream applications remain operational even if Urja Ops goes offline.
3. **Connection Pooling**: Implement a session pool to prevent re-authentication contention under heavy concurrent load.

---

## Reflection

Answers to the project reflection questions (also preserved in [REFLECTION.md](REFLECTION.md)):

### 1. What assumptions did you make?
My primary assumption was that the Urja Meter Ops portal is treated by the utility as a read-only telemetry and inventory dashboard rather than a transactional write system. Because the portal had no meter provisioning, status editing, or remote disconnect controls in its UI, I assumed downstream consumers needed clean, reliable read access, rich querying, and aggregation rather than write backends.

Second, I assumed that the total inventory of smart meters on this utility sub-network is relatively compact (~400 to 1,000 meters in Jaipur's pilot area). This assumption justified building an in-memory indexing layer primed by the discovered HMAC bulk export endpoint. If the utility had 2,000,000 meters, holding them in memory would be impractical, requiring an external PostgreSQL/PostGIS or Elasticsearch layer with incremental sync workers.

Finally, I assumed that 30-minute interval readings on `/portal/meters/{id}/energy` are always in chronological order (or near-chronological order) and that timestamps follow the Indian standard (`DD/MM/YYYY HH:MM`). I verified this across dozens of sample meters, but built defensive sorting and timezone-aware parsing into the pipeline anyway.

### 2. Which part was the most difficult, and how did you get unstuck?
The most stubborn hurdle was authenticating against the portal programmatically. When I first attempted a standard HTTP POST with the provided credentials (`operator@urja.local` / `urja-ops-2026`), the server immediately rejected the connection with HTTP 403: `"Cross-site POST form submissions are forbidden"`. 

I initially suspected missing CSRF tokens in a hidden `<input>` or a cookie handshake. But inspect-logging the initial GET response showed no cookies and no CSRF tokens in the HTML body. I got unstuck by tearing into the raw network request headers sent by Chrome during the operator's manual login. SvelteKit's modern form action handling checks the `Origin` and `Referer` headers. Adding `Origin: https://urja-ops.flockenergy.tech` and `Referer: https://urja-ops.flockenergy.tech/login` immediately unlocked the door, returning HTTP 200 with the `__Secure-better-auth.session_token` cookie.

Another tricky moment was reverse-engineering the HMAC signature for `/portal/export`. DevTools showed `x-signature` and `x-timestamp`, but not the exact string format hashed by HMAC-SHA256. Rather than guessing delimiters, I searched through the minified client JavaScript bundle (`_app/immutable/nodes/4.2Bgc2kUI.js`). Finding the unminified subtle crypto calls showed the exact template: `[method, path, query, timestamp].join('\n')`. Replicating that in Python worked on the very first try.

### 3. If you had another day, what would you improve?
If I had another full day, I would focus on three major enhancements:
1. **Persistent Backing Store (PostgreSQL with PostGIS)**: Right now, the query cache lives in memory inside the FastAPI process. While that makes setup zero-dependency and lightning fast for 403 meters, it doesn't persist across server restarts and doesn't scale horizontally across multiple Uvicorn worker processes. I would add a PostgreSQL/PostGIS storage layer with an automated background worker that polls the legacy portal every 15 minutes to update newly installed meters or fresh energy records.
2. **Historical Energy Ingestion & Anomaly Detection**: The portal currently returns 337 readings per meter (a rolling 7-day window). Downstream data teams usually want historical data retained over months or years. I would build a time-series store (like TimescaleDB or ClickHouse) to accumulate interval readings and add real-time anomaly alerts (e.g., sudden voltage drops below 200V, zero-consumption meter tampering, or phase imbalance).
3. **Robust Circuit Breaker & Rate Limiter**: If the upstream Urja server experiences downtime (e.g. 502 Bad Gateway), our client should gracefully fail over to the last cached snapshot using an exponential backoff circuit breaker (`pybreaker` or `tenacity`) so that downstream services aren't stalled by upstream legacy outages.

### 4. What mistake did you make while solving this (there's always one)?
When writing the energy consumption normalizer, I initially made the rookie mistake of calculating `net_kwh_consumed` by taking `readings[-1]['kwh'] - readings[0]['kwh']` assuming the raw array from `/portal/meters/{id}/energy` was strictly sorted in ascending chronological order. 

When I tested this on meter `J100039`, the calculated delta was negative! Looking into the raw response, the legacy endpoint returned a few readings where timestamps were slightly out of order due to delayed cellular meter push intervals. 

I had to pause and refactor the parsing pipeline to explicitly parse all timestamps into Python `datetime` objects and execute a strict chronological sort (`readings.sort(key=lambda r: r.timestamp)`) before computing any metrics, voltage minimums/maximums, or energy deltas. It was a great reminder that legacy utility data should never be trusted to be clean or pre-sorted.

A second oversight was initially treating the HMAC bulk export purely as an internal performance mechanism to prime the server's cache, rather than exposing it as a first-class REST endpoint. Inspecting network logs showed operators frequently utilizing the portal's "Export all meters" button. Downstream data teams needed that exact bulk capability programmatically without having to reconstruct HMAC signatures themselves. I promptly corrected this by building a dedicated `GET /api/v1/export` endpoint (supporting both JSON responses and file download attachments) and fleshing out the Transformers suite (`GET /api/v1/transformers/{code}` and `GET /api/v1/transformers/{code}/meters`).

### 5. If you were reviewing your own submission, what would you criticise?
If I were sitting on the other side of the table reviewing this repo, here is what I would call out:
* **In-Memory Cache vs Distributed Cache**: Storing the 403 meters and 40 transformers in a Python dictionary is great for a standalone demonstration and keeps installation trivial (`pip install -r requirements.txt`), but in a real-world multi-pod Kubernetes deployment, each container would maintain its own cache unless Redis or a shared database is introduced.
* **Single Shared Session vs Connection Pooling**: The `UrjaPortalClient` currently uses a single `operator@urja.local` session with an internal lock. If thousands of downstream consumers hammered individual uncached endpoints simultaneously, re-authentications or cookie refreshes could become a slight bottleneck. A session pool or token distributor would be cleaner for heavy production loads.
* **HTML Parsing Redundancy**: Because the bulk export endpoint was discovered and provides full hierarchies and nameplate details, the BeautifulSoup4 HTML fallback scraper for `/meters/{id}` is rarely exercised in normal operation. I kept it as a resilient fallback in case the `/portal/export` endpoint is deprecated or locked down by utility administrators, but maintaining two ingestion paths adds code surface area.

---

## Companion Documents

* **[PROTOCOL.md](PROTOCOL.md)** — Detailed write-up on the legacy SvelteKit architecture, the CSRF Origin/Referer requirement, internal API mapping, the HMAC-SHA256 signature algorithm, and data anomalies across legacy vs v2 builds.
* **[REFLECTION.md](REFLECTION.md)** — Engineering reflections answering the project reflection questions.
* **[openapi.json](openapi.json)** — Exported OpenAPI 3.1.0 schema specification.
