# Engineering Reflection (`REFLECTION.md`)

Building an API wrapper over an undocumented internal portal requires a mix of reverse engineering, data modeling, and practical architectural judgment. Below are my reflections on the decisions made, the obstacles encountered, and future improvements.

---

### 1. What assumptions did you make?

My primary assumption was that the Urja Meter Ops portal functions as an informational monitoring dashboard rather than a transactional write system. Because the portal contains no UI controls for meter provisioning, firmware updates, or remote disconnects, I assumed downstream consumers needed clean, reliable read access, expressive search, and analytical aggregations rather than mutation endpoints.

Second, I assumed that the inventory of smart meters on this utility section is relatively compact (~400 to 1,000 meters in the Jaipur pilot zone). This justified building an in-memory query engine primed by the HMAC bulk export endpoint. If the utility managed 2,000,000 meters across a state, holding the full fleet in memory would be impractical, requiring an external PostgreSQL/PostGIS or Elasticsearch layer with asynchronous worker pipelines.

Finally, I assumed that 30-minute interval readings on `/portal/meters/{id}/energy` generally represent a rolling 7-day window and that timestamps follow the Indian standard (`DD/MM/YYYY HH:MM`). I verified this across sample meters, but implemented defensive sorting and timezone-aware parsing to handle edge cases.

---

### 2. Which part was the most difficult, and how did you get unstuck?

The most stubborn hurdle was authenticating against the portal programmatically. When I first attempted a standard HTTP POST with the provided credentials (`operator@urja.local` / `urja-ops-2026`), the server immediately rejected the request with HTTP 403: `"Cross-site POST form submissions are forbidden"`.

I initially suspected hidden CSRF tokens in an input tag or cookie handshakes. However, inspecting the initial GET response showed no cookies and no CSRF tokens in the HTML. I got unstuck by inspecting the raw network request headers sent by Chrome during a manual login in the browser. SvelteKit enforces cross-site protection by checking the `Origin` and `Referer` headers. Adding `Origin: https://urja-ops.flockenergy.tech` and `Referer: https://urja-ops.flockenergy.tech/login` immediately satisfied the check, returning HTTP 200 with the `__Secure-better-auth.session_token` cookie.

Another technical challenge was reverse-engineering the HMAC signature for `/portal/export`. DevTools revealed `x-signature` and `x-timestamp`, but not the exact string format hashed by HMAC-SHA256. Rather than guessing delimiters, I searched the downloaded client JavaScript chunk (`_app/immutable/nodes/4.2Bgc2kUI.js`). Finding the unminified Web Crypto call showed the exact template: `[method, path, query, timestamp].join('\n')` signed using the secret from `/portal/keys`. Replicating that in Python produced a working bulk ingestion pipeline on the first try.

---

### 3. If you had another day, what would you improve?

If I had another full day, I would focus on three enhancements:

1. **Persistent Backing Store (PostgreSQL with PostGIS)**: Right now, the query cache lives in memory inside the FastAPI process. While that keeps setup zero-dependency and fast for 403 meters, it does not persist across server restarts and cannot be shared across multiple Uvicorn worker processes. I would add a PostgreSQL/PostGIS layer with a background worker that polls the legacy portal periodically to sync new records.
2. **Time-Series Ingestion & Anomaly Detection**: The portal returns 337 readings per meter (a rolling 7-day window). Downstream analytics teams usually need historical trends retained over months or years. I would stream readings into TimescaleDB or ClickHouse and add anomaly detection for voltage sags below 200V or sudden zero-consumption drops.
3. **Circuit Breakers & Graceful Degradation**: If the upstream Urja server experiences downtime, our client should fall back to the last cached snapshot using an exponential backoff circuit breaker (`pybreaker` or `tenacity`) so that downstream microservices are shielded from upstream legacy outages.

---

### 4. What mistake did you make while solving this (there's always one)?

When writing the energy consumption normalizer, I initially calculated `net_kwh_consumed` by taking `readings[-1]['kwh'] - readings[0]['kwh']`, assuming the array from `/portal/meters/{id}/energy` was strictly pre-sorted in ascending chronological order.

When testing meter `J100039`, the calculated delta was negative. Inspecting the raw payload revealed that a few readings were slightly out of order due to transmission delays from cellular smart meters. I had to refactor the parser to parse timestamps into Python `datetime` objects and execute a strict chronological sort (`readings.sort(key=lambda r: r.timestamp)`) before computing metrics and energy deltas. It was a good reminder that legacy utility telemetry should never be assumed to be clean or pre-sorted.

A second oversight was initially treating the HMAC bulk export route only as an internal mechanism to seed the server's cache, rather than exposing it as a dedicated user-facing endpoint. Network logs showed that operations staff actively use the portal's "Export all meters" button. Downstream teams needed that exact bulk capability programmatically without having to calculate HMAC signatures themselves. I resolved this by building a dedicated `GET /api/v1/export` endpoint (supporting both JSON responses and file download attachments) and adding the full Transformers suite (`GET /api/v1/transformers/{code}` and `GET /api/v1/transformers/{code}/meters`).

---

### 5. If you were reviewing your own submission, what would you criticise?

If I were reviewing this repository, I would point out:

* **In-Memory Cache vs Distributed Cache**: Storing meters and transformers in Python memory is great for a self-contained submission with zero setup friction, but in a multi-pod container deployment, each pod maintains its own cache unless Redis or a shared database is introduced.
* **Single Shared Session**: `UrjaPortalClient` uses a single operator session protected by a thread lock. Under high concurrent load from thousands of downstream requests, session token refreshes could become a slight bottleneck. A session pool would be more appropriate for large production workloads.
* **HTML Scraper Redundancy**: Because the bulk export endpoint was discovered and provides complete data, the BeautifulSoup4 HTML scraper for `/meters/{id}` is rarely exercised in normal operation. I kept it as a fallback in case the export route is deprecated, but maintaining two ingestion paths increases code surface area.
