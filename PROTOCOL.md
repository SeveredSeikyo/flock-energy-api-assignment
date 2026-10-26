# Urja Meter Ops — Legacy Protocol Discovery (`PROTOCOL.md`)

When first inspecting the Urja Meter Ops portal (`https://urja-ops.flockenergy.tech`), it was initially presented as an internal utility portal without an API, where operations staff manually searched and copied smart meter data.

To understand how the system actually operates, I inspected the HTTP traffic across the login flow, meter navigation, and transformer views. The underlying architecture is not a legacy server-rendered monolith; it is a modern SvelteKit single-page application communicating with internal JSON endpoints and a signed bulk export route.

Below is the technical breakdown of the portal's authentication flow, internal endpoints, cryptography mechanisms, and data anomalies.

---

## 1. Authentication & Session Architecture

### SvelteKit CSRF Protection & Form Actions
When attempting a direct POST request to `https://urja-ops.flockenergy.tech/login`, the server rejects bare HTTP POST submissions:
```http
HTTP/1.1 403 Forbidden
{"message":"Cross-site POST form submissions are forbidden"}
```

Analysis of the browser's network headers revealed the requirements:
1. **CSRF Origin Check**: SvelteKit validates incoming POST requests by checking the `Origin` and `Referer` headers. If these do not match `https://urja-ops.flockenergy.tech`, the server terminates the connection.
2. **Form Payload**: The login form field uses `email`, not `username`:
   ```http
   POST /login HTTP/1.1
   Host: urja-ops.flockenergy.tech
   Origin: https://urja-ops.flockenergy.tech
   Referer: https://urja-ops.flockenergy.tech/login
   Content-Type: application/x-www-form-urlencoded
   x-sveltekit-action: true

   email=operator%40urja.local&password=urja-ops-2026
   ```

3. **Session Cookies**:
   Upon valid credentials, the server responds with a 303 redirect and sets an HTTP-only session cookie:
   ```http
   HTTP/1.1 200 OK
   Set-Cookie: __Secure-better-auth.session_token=...; Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax

   {"type":"redirect","status":303,"location":"/meters"}
   ```
   * **Session TTL**: 3600 seconds (1 hour).
   * **Invalid Credentials**: Returns HTTP 200 with an internal JSON failure structure (`{"type":"failure", "status":401}`).
   * **Expired Sessions**: Returns HTTP 401 with `{"error":"unauthorized","message":"A valid session is required."}`.

---

## 2. Discovered Endpoints

The internal endpoints powering the portal include:

| Method | Endpoint | Purpose | Upstream Format |
| :--- | :--- | :--- | :--- |
| `POST` | `/login` | Operator authentication | JSON redirect & session cookie |
| `POST` | `/api/auth/sign-out` | Session termination | JSON (`{"success":true}`) |
| `GET` | `/portal/meters/search?q={query}&page={page}` | Paginated meter search | JSON (`data: [...]`, `total: 403`) |
| `GET` | `/portal/dts?page={page}` | Distribution Transformers listing | JSON (`data: [...]`, `total: 40`) |
| `GET` | `/portal/meters/{id}/geo` | Meter coordinates | JSON (`{"data":{"latitude":"...","longitude":"..."}}`) |
| `GET` | `/portal/meters/{id}/energy` | Historical 30-min meter readings | JSON (`{"data":[{"timestamp":"...","kwh":"..."}, ...]}`) |
| `GET` | `/meters/{id}` | Meter detail page HTML | HTML (`<dl>`, `<dt>`, `<dd>`, script state) |
| `GET` | `/meters/{id}/__data.json` | SvelteKit route data | JSON (Devalue graph) |
| `GET` | `/portal/keys` | Signing secret for bulk export | JSON (`{"data":{"signingSecret":"..."}}`) |
| `GET` | `/portal/export?page=1` | HMAC-SHA256 authenticated export | JSON (`data: [...]`, 403 meters) |

---

## 3. The Bulk Export Route & HMAC-SHA256 Signing

Under the Transformers view in the portal UI, there is an "Export all meters" button. Clicking this triggers:
1. `GET /portal/keys` -> returns a dynamic signing secret:
   ```json
   {"data":{"signingSecret":"I3dZPPf5CgTp7JyGNMI8i6z8LFR7TmSR"}}
   ```
2. A request to `GET /portal/export?page=1` with two required headers:
   * `x-timestamp`: Unix epoch seconds (e.g. `1793005205`)
   * `x-signature`: HMAC-SHA256 hex digest

Decompiling the client JavaScript bundle (`_app/immutable/nodes/4.2Bgc2kUI.js`) revealed the exact signature algorithm:
```javascript
// Payload format: METHOD\nPATH\nQUERY\nTIMESTAMP
let message = [method, path, query, timestamp].join('\n');
let key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
let signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message));
```

### Architectural Impact
Calling `/portal/export` with a valid HMAC-SHA256 signature returns **all 403 meters in a single 230KB response**, complete with GPS coordinates and full 7-tier administrative hierarchies. This eliminates the need to execute 400+ sequential requests against the legacy server, enabling sub-second synchronization.

---

## 4. Data Anomalies & Inconsistencies

### A. Legacy vs v2 Build Variations
The 403 meters in the dataset belong to two distinct generations:
* `legacy` build: 238 meters
* `v2` build: 165 meters

Their internal parameter storage differs significantly:
* **Legacy meters**: Store nameplate parameters as a stringified JSON blob inside a `classData` field with camelCase keys (`MeterId`, `SerialNo`, `PhaseType`):
  ```json
  "classData": "{\"installed_meter\":{\"MeterId\":\"J100009\",\"SerialNo\":\"SE79541\",\"Make\":\"Genus\",\"PhaseType\":\"single\",\"InstallationStatus\":\"Decommissioned\",\"InstallationType\":\"Whole Current\"}}"
  ```
* **v2 meters**: Store parameters inside a `detail.data` array of key-value pairs with spaced keys (`Meter ID`, `Serial No`, `Phase Type`):
  ```json
  "detail": {
    "data": [
      {"parameterName": "Meter ID", "parameterValue": "J100002"},
      {"parameterName": "Serial No", "parameterValue": "AL28136"},
      {"parameterName": "Make", "parameterValue": "L&T"}
    ]
  }
  ```
Our adapter layer normalizes both schemas into a uniform model with standard snake_case fields.

### B. Telemetry Dates & Stringified Decimals
The `/portal/meters/{id}/energy` endpoint returns 337 readings per meter (7 days of 30-minute interval readings).
* **Timestamps**: Uses non-standard Indian/UK formatting (`DD/MM/YYYY HH:MM`, e.g. `"23/06/2026 23:30"`).
* **Data Types**: Metrics are delivered as strings (e.g. `"kwh": "38630.74"`, `"voltR": "239"`).
* **Out-of-Order Readings**: Due to cellular smart meter transmission latency, readings occasionally arrive slightly out of sequence. Computing net delta consumption requires sorting timestamps chronologically before performing math.

### C. Search Limitations
The upstream `/portal/meters/search?q={query}` endpoint only matches prefixes on `meterId` or `serialNo`.
* Searching by manufacturer (e.g., `HPL`) returns 0 results.
* Searching by status (e.g., `Installed` or `Decommissioned`) returns 0 results.
* Searching by transformer code (e.g., `DT-001`) returns 0 results.

Our adapter layer overcomes this by building an in-memory index from the bulk dataset, enabling multi-attribute filtering, free-text search, and spatial proximity queries.

---

## 5. Summary of Wrapper Architecture

1. **Authentication Layer**: Maintains a persistent HTTP cookie jar with `__Secure-better-auth.session_token`, sending `Origin` and `Referer` headers on login.
2. **Auto-Recovery**: Automatically detects 401 Unauthorized or session expiration, re-authenticates via thread-safe locking, and transparently retries failed requests.
3. **Data Ingestion**: Uses HMAC-SHA256 authenticated bulk export for initial caching, with on-demand fallback scraping for individual lookups.
4. **Clean Exposure**: Normalizes all responses into typed Pydantic models with an OpenAPI 3.1 specification.
