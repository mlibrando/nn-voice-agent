# Mock Backend Interface Reference — Natural Nutrition
 
Source-of-truth reference for the FastAPI mock backend at [mock-backend/](mock-backend/). All field names, types, paths, and sample payloads below are taken directly from [mock-backend/app.py](mock-backend/app.py), [mock-backend/models.py](mock-backend/models.py), [mock-backend/store.py](mock-backend/store.py), and [mock-backend/seed_data.json](mock-backend/seed_data.json). Inferences are marked **(inferred)**.
 
- Base URL: `http://localhost:8001`
- Framework: FastAPI (`title="Natural Nutrition Mock Backend"`, `version="1.0.0"`)
- Interactive docs: `http://localhost:8001/docs`, OpenAPI at `/openapi.json`, Redoc at `/redoc`
- All payloads are JSON. Mutations return the updated object (no envelope).
- CORS: permissive — `allow_origins=["*"]`, all methods/headers, credentials disabled ([app.py:38-44](mock-backend/app.py#L38-L44)).
---
 
## 1. Setup & Run
 
### Dependencies
From [mock-backend/requirements.txt](mock-backend/requirements.txt):
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
```
Requires **Python 3.10+** (per mock-backend/README.md).
 
### Start
```bash
cd mock-backend
python -m venv .venv && source .venv/bin/activate    # optional
pip install -r requirements.txt
./run.sh                                              # http://localhost:8001
```
 
[mock-backend/run.sh](mock-backend/run.sh) execs:
```
uvicorn app:app --host 0.0.0.0 --port "${PORT:-8001}" --reload
```
`--reload` is enabled, so editing files restarts the server.
 
### Sanity check
```bash
curl http://localhost:8001/health
curl "http://localhost:8001/customers/lookup?phone=+15125550101"
```
 
### Environment variables (all optional)
 
| Var | Default | Source | Meaning |
|---|---|---|---|
| `PORT` | `8001` | [run.sh:6](mock-backend/run.sh#L6) | Listen port |
| `MOCK_CHAOS` | `1` (on) | [app.py:68](mock-backend/app.py#L68) | Master switch for ambient latency + random errors. `0`/`false`/`no` disables both. |
| `MOCK_LATENCY_MIN_MS` | `300` | [app.py:64](mock-backend/app.py#L64) | Min ambient latency per request (ms) |
| `MOCK_LATENCY_MAX_MS` | `1500` | [app.py:65](mock-backend/app.py#L65) | Max ambient latency per request (ms) |
| `MOCK_SLOW_ENDPOINT_MS` | `1200` | [app.py:66](mock-backend/app.py#L66) | **Additional** latency added on paths ending in `/subscriptions` |
| `MOCK_ERROR_RATE` | `0.07` | [app.py:67](mock-backend/app.py#L67) | Probability of an injected `503` per request |
| `MOCK_API_KEY` | *(unset)* | [app.py:69](mock-backend/app.py#L69) | If set, all non-exempt requests must send `Authorization: Bearer <key>` or get `401 unauthorized` |
 
### Non-obvious bits
- Seed file is loaded from `mock-backend/seed_data.json` at startup and validated against `SeedData` pydantic model — a malformed seed fails loudly at startup ([store.py:79](mock-backend/store.py#L79)).
- State is **in-memory and per-process**. Restart wipes mutations; so does `POST /admin/reset`.
- Saved transcripts are also appended to `mock-backend/transcripts.log` (one JSON object per line) ([store.py:328-332](mock-backend/store.py#L328-L332)).
- The store is a module-level singleton: `store = Store()` at the bottom of [store.py:336](mock-backend/store.py#L336).
---
 
## 2. Endpoints
 
### 2.1 Health
**`GET /health`** → `{"ok": true}`
- Never delayed, never errored, no auth required ([app.py:82-87](mock-backend/app.py#L82-L87)).
Sample:
```json
{"ok": true}
```
 
---
 
### 2.2 Customer lookup (caller-ID entry point)
**`GET /customers/lookup`**
 
Query params (exactly one required):
- `phone: str` — digits-only normalized; matches `+1 512-555-0101`, `+15125550101`, `15125550101` ([store.py:58-61](mock-backend/store.py#L58-L61))
- `email: str` — case-insensitive exact match
- `order_number: str` — accepts `"#NN1001"`, `"NN1001"`, `"1001"`, or full GID `"gid://shopify/Order/1001"` ([store.py:127-139](mock-backend/store.py#L127-L139))
Behavior:
- Providing zero or more than one returns `400 validation` ("Provide exactly one of: phone, email, order_number.")
- No match returns `404 not_found`.
- Match hydrates `{customer, orders, subscriptions}` in a single round-trip.
Response shape:
```jsonc
{
  "customer": Customer,
  "orders": Order[],          // newest-first by created_at
  "subscriptions": Subscription[]
}
```
 
Sample (`GET /customers/lookup?phone=+15125550101`):
```json
{
  "customer": {
    "customer_id": "cust_001",
    "first_name": "Margaret",
    "last_name": "Chen",
    "email": "margaret.chen@example.com",
    "phone": "+15125550101",
    "order_ids": ["gid://shopify/Order/1001", "gid://shopify/Order/1000"],
    "subscription_ids": [50001, 50010]
  },
  "orders": [
    {
      "order_id": "gid://shopify/Order/1001",
      "order_name": "#NN1001",
      "email": "margaret.chen@example.com",
      "customer_name": "Margaret Chen",
      "created_at": "2026-06-05T15:10:00Z",
      "financial_status": "PAID",
      "fulfillment_status": "FULFILLED",
      "cancelled_at": null,
      "currency": "USD",
      "total_price": "39.99",
      "total_refunded": "0.00",
      "line_items": [
        {"line_item_id": "li_1001_1", "sku": "NN-MAG", "title": "Magnesium Complex", "quantity": 1, "price": "39.99"}
      ],
      "transactions": [
        {"kind": "SALE", "status": "SUCCESS", "amount": "39.99", "currency": "USD", "card_last_four": "4242", "card_company": "Visa"}
      ],
      "tracking_info": {
        "carrier": "USPS",
        "tracking_number": "9400111899223456781001",
        "status": "delivered",
        "estimated_delivery": "2026-06-09",
        "tracking_url": "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223456781001"
      },
      "shipping_address": {
        "name": "Margaret Chen",
        "phone": "+15125550101",
        "address1": "742 Evergreen Ter",
        "address2": "",
        "city": "Austin",
        "province": "TX",
        "zip": "78701",
        "country": "United States"
      }
    },
    { /* ...older order #NN1000... */ }
  ],
  "subscriptions": [
    {
      "subscription_id": 50001,
      "status": "ACTIVE",
      "email": "margaret.chen@example.com",
      "customer_name": "Margaret Chen",
      "items": [{"sku": "NN-MAG", "title": "Magnesium Complex", "quantity": 1}],
      "total_value": 39.99,
      "currency": "USD",
      "delivery_interval": "30 day",
      "next_billing_date": "2026-07-05",
      "shipping_address": { /* Address */ },
      "discount_code": null,
      "discount_percentage": null,
      "paused_until": null,
      "cancelled_at": null,
      "cancellation_reason": null
    },
    {
      "subscription_id": 50010,
      "status": "PAUSED",
      "items": [{"sku": "NN-DK", "title": "Vitamin D3 & K2", "quantity": 1}],
      "total_value": 34.99,
      "delivery_interval": "60 day",
      "next_billing_date": "2026-08-15",
      "paused_until": "2026-08-15T00:00:00Z",
      "cancelled_at": null,
      "cancellation_reason": null
      /* ...remaining fields... */
    }
  ]
}
```
 
---
 
### 2.3 Customer orders
**`GET /customers/{customer_id}/orders`**
 
Response:
```jsonc
{"orders": Order[]}  // newest-first by created_at
```
Returns `404 not_found` if `customer_id` doesn't exist ([store.py:147-152](mock-backend/store.py#L147-L152)).
 
Sample (`GET /customers/cust_001/orders`):
```json
{
  "orders": [
    { "order_id": "gid://shopify/Order/1001", "order_name": "#NN1001", "created_at": "2026-06-05T15:10:00Z", "total_price": "39.99", "...": "..." },
    { "order_id": "gid://shopify/Order/1000", "order_name": "#NN1000", "created_at": "2026-05-06T15:10:00Z", "total_price": "39.99", "...": "..." }
  ]
}
```
 
---
 
### 2.4 Customer subscriptions (intentionally slow)
**`GET /customers/{customer_id}/subscriptions`**
 
Response:
```jsonc
{"subscriptions": Subscription[]}
```
This endpoint gets extra `MOCK_SLOW_ENDPOINT_MS` (default **+1200ms**) on top of ambient latency. Path-based match: any path ending in `/subscriptions` triggers it ([app.py:110-111](mock-backend/app.py#L110-L111)).
 
Sample (`GET /customers/cust_004/subscriptions`):
```json
{
  "subscriptions": [
    {
      "subscription_id": 50004,
      "status": "ACTIVE",
      "items": [{"sku": "NN-MAG", "title": "Magnesium Complex", "quantity": 1}],
      "total_value": 39.99,
      "delivery_interval": "30 day",
      "next_billing_date": "2026-07-08",
      "...": "..."
    },
    {
      "subscription_id": 50005,
      "status": "ACTIVE",
      "items": [{"sku": "NN-DK", "title": "Vitamin D3 & K2", "quantity": 1}],
      "total_value": 34.99,
      "delivery_interval": "60 day",
      "next_billing_date": "2026-07-20",
      "...": "..."
    }
  ]
}
```
 
---
 
### 2.5 Products
**`GET /products`** → `{"products": Product[]}`
**`GET /products/{sku}`** → `Product` (no wrapper)
 
`404` if SKU not found.
 
Sample (`GET /products/NN-MAG`):
```json
{
  "sku": "NN-MAG",
  "name": "Magnesium Complex",
  "price": "39.99",
  "currency": "USD",
  "description": "An 8-form magnesium blend for sleep, muscle relaxation, and stress.",
  "size": "30 capsules per bottle",
  "ingredients": "Magnesium (as Glycinate, Malate, Citrate, Taurate, Orotate, Oxide, Aspartate, Carbonate)",
  "benefit": "sleep & muscle relaxation",
  "url": "https://example.com/products/magnesium-complex"
}
```
 
---
 
### 2.6 Subscription mutations
 
All take an integer `subscription_id` in the path. All return the **full updated Subscription object** on success ([app.py:197-219](mock-backend/app.py#L197-L219)).
 
#### `POST /subscriptions/{subscription_id}/cancel`
Body (`CancelSubscriptionBody`):
```jsonc
{"reason": "cant_afford_the_product"}  // default "other"
```
Valid reasons (`CANCELLATION_REASONS` in [models.py:20-29](mock-backend/models.py#L20-L29)):
- `too_much_product`
- `cant_afford_the_product`
- `didnt_want_a_subscription`
- `didnt_like_the_product`
- `found_a_better_alternative`
- `going_on_a_trip`
- `dont_need_the_product_anymore`
- `other`
Errors: `404` not found; `400 validation` on bad reason; `409 conflict` if already `CANCELLED`.
 
Sample response (subscription `50003` → cancel with reason `cant_afford_the_product`):
```json
{
  "subscription_id": 50003,
  "status": "CANCELLED",
  "email": "linda.park@example.com",
  "customer_name": "Linda Park",
  "items": [{"sku": "NN-GRN", "title": "Daily Greens", "quantity": 1}],
  "total_value": 44.99,
  "currency": "USD",
  "delivery_interval": "30 day",
  "next_billing_date": "2026-07-10",
  "shipping_address": { /* Address */ },
  "discount_code": null,
  "discount_percentage": null,
  "paused_until": null,
  "cancelled_at": "2026-06-30T14:23:00Z",
  "cancellation_reason": "cant_afford_the_product"
}
```
 
#### `POST /subscriptions/{subscription_id}/pause`
Body (`PauseSubscriptionBody`):
```jsonc
{"pause_months": 1}  // default 1, must satisfy 0 < n <= 6
```
Behavior ([store.py:192-202](mock-backend/store.py#L192-L202)): sets `status = "PAUSED"`, `paused_until = now + 30*pause_months days` (ISO Z), and `next_billing_date = same date (YYYY-MM-DD)`.
Errors: `404`; `400` if out of range; `409` if already `CANCELLED`.
 
Sample response (pause `50001` for 2 months, run on 2026-06-30):
```json
{
  "subscription_id": 50001,
  "status": "PAUSED",
  "items": [{"sku": "NN-MAG", "title": "Magnesium Complex", "quantity": 1}],
  "total_value": 39.99,
  "delivery_interval": "30 day",
  "next_billing_date": "2026-08-29",
  "paused_until": "2026-08-29T14:23:00Z",
  "cancelled_at": null,
  "cancellation_reason": null
  /* ...rest of Subscription... */
}
```
 
#### `POST /subscriptions/{subscription_id}/reactivate`
No body. Errors: `404`; `409` if already `ACTIVE` ([store.py:204-214](mock-backend/store.py#L204-L214)).
Resets `cancelled_at`, `cancellation_reason`, `paused_until` to `null`; sets `next_billing_date = now + 30 days` (YYYY-MM-DD); status → `ACTIVE`.
 
Sample response (reactivate `50008`, Susan, on 2026-06-30):
```json
{
  "subscription_id": 50008,
  "status": "ACTIVE",
  "email": "susan.nguyen@example.com",
  "customer_name": "Susan Nguyen",
  "items": [{"sku": "NN-MAG", "title": "Magnesium Complex", "quantity": 1}],
  "total_value": 39.99,
  "delivery_interval": "30 day",
  "next_billing_date": "2026-07-30",
  "shipping_address": { /* Address */ },
  "discount_code": null,
  "discount_percentage": null,
  "paused_until": null,
  "cancelled_at": null,
  "cancellation_reason": null
}
```
 
#### `POST /subscriptions/{subscription_id}/discount`
Body (`DiscountBody`):
```jsonc
{"discount_pct": 20, "code": "LOYAL20"}  // defaults 20 / "LOYAL20"
```
Behavior ([store.py:216-225](mock-backend/store.py#L216-L225)): sets `discount_code`, `discount_percentage`, and **recomputes** `total_value = round(total_value * (1 - pct/100), 2)`.
Errors: `404`; `409` if `CANCELLED`; `400` if `pct` not in `(0, 100)`.
 
Sample response (apply 20% / `LOYAL20` to `50003`, original `total_value=44.99`):
```json
{
  "subscription_id": 50003,
  "status": "ACTIVE",
  "total_value": 35.99,
  "discount_code": "LOYAL20",
  "discount_percentage": 20,
  "...": "..."
}
```
 
#### `POST /subscriptions/{subscription_id}/address`
Body (`AddressBody`):
```jsonc
{
  "address1": "123 New St",
  "address2": "",                  // default ""
  "city": "Austin",
  "province": "TX",
  "country": "United States",      // default "United States"
  "zip": "78701"
}
```
Behavior ([store.py:227-236](mock-backend/store.py#L227-L236)): merges in existing `name` and `phone` if the body doesn't supply them (the `AddressBody` schema doesn't include `name`/`phone` fields at all, so they are always preserved from existing).
Errors: `404`; `409` if `CANCELLED`.
 
Sample response:
```json
{
  "subscription_id": 50001,
  "shipping_address": {
    "name": "Margaret Chen",
    "phone": "+15125550101",
    "address1": "123 New St",
    "address2": "",
    "city": "Austin",
    "province": "TX",
    "zip": "78701",
    "country": "United States"
  },
  "...": "..."
}
```
 
---
 
### 2.7 Order mutations
 
`order_id` is a Shopify GID (`gid://shopify/Order/1001`) — it contains slashes, so all order mutations take it in the **body** rather than the URL. All return the **full updated Order object**.
 
#### `POST /orders/refund`
Body (`OrderRefundBody`):
```jsonc
{
  "order_id": "gid://shopify/Order/1001",
  "refund_percentage": 50
}
```
`refund_percentage` must be one of `ALLOWED_REFUND_PERCENTAGES`: **`[10, 20, 25, 30, 35, 40, 50, 60]`** ([models.py:32](mock-backend/models.py#L32)). For 100%, use `/orders/refund/full`.
 
Behavior ([store.py:242-267](mock-backend/store.py#L242-L267)):
- Computes `add = round(total_price * pct/100, 2)`; cap so cumulative refunded ≤ total.
- Appends a `REFUND` transaction (with `card_last_four=null`, `card_company=null`).
- `financial_status` → `REFUNDED` if cumulative refunded ≥ total, else `PARTIALLY_REFUNDED`.
Errors: `404`; `400` on invalid pct; `409` if already fully `REFUNDED`.
 
Sample response (refund 50% of order `gid://shopify/Order/1001`, total `39.99`):
```json
{
  "order_id": "gid://shopify/Order/1001",
  "order_name": "#NN1001",
  "financial_status": "PARTIALLY_REFUNDED",
  "fulfillment_status": "FULFILLED",
  "total_price": "39.99",
  "total_refunded": "20.00",
  "transactions": [
    {"kind": "SALE",   "status": "SUCCESS", "amount": "39.99", "currency": "USD", "card_last_four": "4242", "card_company": "Visa"},
    {"kind": "REFUND", "status": "SUCCESS", "amount": "20.00", "currency": "USD", "card_last_four": null,   "card_company": null}
  ],
  "...": "..."
}
```
 
#### `POST /orders/refund/full`
Body (`OrderIdBody`):
```jsonc
{"order_id": "gid://shopify/Order/1007"}
```
Behavior ([store.py:269-290](mock-backend/store.py#L269-L290)): refunds remaining balance, sets `financial_status = "REFUNDED"`, appends a `REFUND` transaction, and **also sets `cancelled_at` if order was `UNFULFILLED` and not yet cancelled**.
Errors: `404`; `409` if already `REFUNDED`.
 
#### `POST /orders/cancel`
Body (`OrderIdBody`):
```jsonc
{"order_id": "gid://shopify/Order/1008"}
```
Behavior ([store.py:292-302](mock-backend/store.py#L292-L302)): sets `cancelled_at` to now.
Errors: `404`; `409` if already cancelled; `409 conflict` if `fulfillment_status != "UNFULFILLED"` (message: *"Order has already shipped and cannot be cancelled or intercepted. Offer a refund instead."*).
 
Sample response (cancel `#NN1008`, Patricia's unfulfilled order):
```json
{
  "order_id": "gid://shopify/Order/1008",
  "order_name": "#NN1008",
  "financial_status": "PAID",
  "fulfillment_status": "UNFULFILLED",
  "cancelled_at": "2026-06-30T14:23:00Z",
  "...": "..."
}
```
 
#### `POST /orders/address`
Body (`OrderAddressBody` — extends `AddressBody`):
```jsonc
{
  "order_id": "gid://shopify/Order/1008",
  "address1": "555 Main St",
  "address2": "Apt 2",
  "city": "Miami",
  "province": "FL",
  "country": "United States",
  "zip": "33102"
}
```
Behavior ([store.py:304-315](mock-backend/store.py#L304-L315)): merges existing `name`/`phone`; updates `shipping_address`.
Errors: `404`; `409 conflict` if `fulfillment_status != "UNFULFILLED"` OR `cancelled_at` is set (message: *"Shipping address can only be changed on an unfulfilled, un-cancelled order."*).
 
---
 
### 2.8 Escalations
**`POST /escalations`**
 
Body (`EscalationBody`):
```jsonc
{
  "customer_id": "cust_005",        // optional
  "customer_details": "...",        // optional, freeform
  "actions_taken": "...",           // optional, freeform
  "issue_for_human": "...",         // REQUIRED
  "mark_high_risk": true            // default false
}
```
 
Response (a thin envelope, **not** the full record):
```json
{"escalation_id": "esc_0001", "status": "queued"}
```
`escalation_id` is generated as `f"esc_{N:04d}"` ([store.py:319](mock-backend/store.py#L319)). Internally the stored record adds `created_at` + all body fields; only the trimmed payload above is returned ([app.py:256-259](mock-backend/app.py#L256-L259)).
 
---
 
### 2.9 Transcripts
**`POST /transcripts`**
 
Body (`TranscriptBody`):
```jsonc
{
  "call_id": "call_abc123",         // REQUIRED
  "customer_id": "cust_001",        // optional
  "caller_phone": "+15125550101",   // optional
  "transcript": "Agent: ...\\nCaller: ...",  // REQUIRED
  "summary": "...",                 // optional
  "outcome": "resolved",            // optional, freeform
  "recording_url": "https://..."    // optional
}
```
 
Response (thin envelope):
```json
{"transcript_id": "tr_0001", "saved": true}
```
`transcript_id` is `f"tr_{N:04d}"`. Side effect: the full record (with `transcript_id`, `created_at`, and all body fields) is appended as one JSON line to `mock-backend/transcripts.log` ([store.py:323-333](mock-backend/store.py#L323-L333)).
 
---
 
### 2.10 Admin (never delayed or errored, never auth-required)
- **`POST /admin/reset`** → `{"reset": true}` — reloads `seed_data.json` and wipes escalations/transcripts in-memory ([store.py:75-88](mock-backend/store.py#L75-L88)).
- **`GET /admin/state`** → full in-memory dump:
  ```jsonc
  {
    "products":      Product[],
    "customers":     Customer[],
    "orders":        Order[],
    "subscriptions": Subscription[],
    "escalations":   EscalationRecord[],
    "transcripts":   TranscriptRecord[]
  }
  ```
 
Note the exempt paths (chaos + auth middleware bypassed): `OPTIONS *`, `/health`, anything under `/admin/*`, `/docs`, `/openapi.json`, `/redoc` ([app.py:82-87](mock-backend/app.py#L82-L87)).
 
---
 
### 2.11 Error envelope
 
All non-2xx responses use this shape ([app.py:72-73](mock-backend/app.py#L72-L73), [store.py:31-51](mock-backend/store.py#L31-L51)):
```json
{ "error": { "code": "<code>", "message": "<human-readable>" } }
```
 
| HTTP | code | Triggered by |
|---|---|---|
| `400` | `validation` | bad params (e.g. zero or multiple lookup keys, bad reason, bad pct) |
| `401` | `unauthorized` | `MOCK_API_KEY` set and request lacks `Authorization: Bearer <key>` |
| `404` | `not_found` | unknown customer / order / subscription / product / lookup miss |
| `409` | `conflict` | business rule violations (cancel a cancelled sub, cancel a shipped order, etc.) |
| `503` | `upstream_unavailable` | ambient injected error (chaos), or `forced_error` (if `force_status` is unparseable) |
| 4xx/5xx | `forced_error` | per-request `force_error=true` (status configurable via `force_status`) |
 
---
 
## 3. Data Models (entities)
 
Defined in [mock-backend/models.py](mock-backend/models.py). All shapes below are exact field names + types.
 
### 3.1 `Customer`
```py
{
  "customer_id":      str,           # e.g. "cust_001"
  "first_name":       str,
  "last_name":        str,
  "email":            str,
  "phone":            Optional[str], # null => not reachable by caller ID (forces email/order_# fallback)
  "order_ids":        List[str],     # full Shopify GIDs
  "subscription_ids": List[int]
}
```
Sample (from seed):
```json
{
  "customer_id": "cust_001",
  "first_name": "Margaret",
  "last_name": "Chen",
  "email": "margaret.chen@example.com",
  "phone": "+15125550101",
  "order_ids": ["gid://shopify/Order/1001", "gid://shopify/Order/1000"],
  "subscription_ids": [50001, 50010]
}
```
 
### 3.2 `Order`
```py
{
  "order_id":           str,            # "gid://shopify/Order/<n>"
  "order_name":         str,            # "#NN1001"
  "email":              str,
  "customer_name":      str,
  "created_at":         str,            # ISO 8601 Z
  "financial_status":   str,            # PAID | PARTIALLY_REFUNDED | REFUNDED
  "fulfillment_status": str,            # UNFULFILLED | FULFILLED
  "cancelled_at":       Optional[str],
  "currency":           str,            # default "USD"
  "total_price":        str,            # money as string
  "total_refunded":     str,            # default "0.00"
  "line_items":         List[OrderLineItem],
  "transactions":       List[OrderTransaction],
  "tracking_info":      Optional[TrackingInfo],
  "shipping_address":   Optional[Address]
}
```
 
`OrderLineItem`:
```py
{"line_item_id": str, "sku": str, "title": str, "quantity": int, "price": str}  # price = unit price
```
 
`OrderTransaction`:
```py
{
  "kind":            str,                 # "SALE" | "REFUND" (default "SALE")
  "status":          str,                 # default "SUCCESS"
  "amount":          str,
  "currency":        str,                 # default "USD"
  "card_last_four":  Optional[str],
  "card_company":    Optional[str]
}
```
 
`TrackingInfo`:
```py
{
  "carrier":            Optional[str],   # e.g. "USPS"
  "tracking_number":    Optional[str],
  "status":             Optional[str],   # pre_transit | in_transit | out_for_delivery | delivered | exception
  "estimated_delivery": Optional[str],
  "tracking_url":       Optional[str]
}
```
 
`Address` (used by orders, subscriptions, and address-mutation bodies):
```py
{
  "name":     Optional[str],
  "phone":    Optional[str],
  "address1": str,
  "address2": Optional[str],   # default ""
  "city":     str,
  "province": str,             # state/province code, e.g. "TX"
  "zip":      str,
  "country":  str              # default "United States"
}
```
 
Sample `Order` (Patricia's partially-refunded #NN1018, useful for refund-status testing):
```json
{
  "order_id": "gid://shopify/Order/1018",
  "order_name": "#NN1018",
  "email": "patricia.gomez@example.com",
  "customer_name": "Patricia Gomez",
  "created_at": "2026-04-27T08:00:00Z",
  "financial_status": "PARTIALLY_REFUNDED",
  "fulfillment_status": "FULFILLED",
  "cancelled_at": null,
  "currency": "USD",
  "total_price": "44.99",
  "total_refunded": "22.50",
  "line_items": [
    {"line_item_id": "li_1018_1", "sku": "NN-GRN", "title": "Daily Greens", "quantity": 1, "price": "44.99"}
  ],
  "transactions": [
    {"kind": "SALE", "status": "SUCCESS", "amount": "44.99", "currency": "USD", "card_last_four": "6655", "card_company": "Visa"},
    {"kind": "REFUND", "status": "SUCCESS", "amount": "22.50", "currency": "USD", "card_last_four": null, "card_company": null}
  ],
  "tracking_info": {
    "carrier": "USPS",
    "tracking_number": "9400111899223456781018",
    "status": "delivered",
    "estimated_delivery": "2026-05-01",
    "tracking_url": "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223456781018"
  },
  "shipping_address": {
    "name": "Patricia Gomez", "phone": "+15125550108",
    "address1": "410 Sunset Dr", "address2": "",
    "city": "Miami", "province": "FL", "zip": "33101", "country": "United States"
  }
}
```
 
### 3.3 `Subscription`
```py
{
  "subscription_id":     int,
  "status":              str,             # ACTIVE | PAUSED | CANCELLED
  "email":               str,
  "customer_name":       str,
  "items":               List[SubscriptionItem],
  "total_value":         float,           # NOTE: float, not string (unlike Order.total_price)
  "currency":            str,             # default "USD"
  "delivery_interval":   str,             # e.g. "30 day", "60 day"
  "next_billing_date":   Optional[str],   # YYYY-MM-DD
  "shipping_address":    Optional[Address],
  "discount_code":       Optional[str],
  "discount_percentage": Optional[int],
  "paused_until":        Optional[str],   # ISO 8601 Z
  "cancelled_at":        Optional[str],   # ISO 8601 Z
  "cancellation_reason": Optional[str]    # one of CANCELLATION_REASONS
}
```
 
`SubscriptionItem`:
```py
{"sku": str, "title": str, "quantity": int}
```
 
Sample (Margaret's paused secondary sub):
```json
{
  "subscription_id": 50010,
  "status": "PAUSED",
  "email": "margaret.chen@example.com",
  "customer_name": "Margaret Chen",
  "items": [{"sku": "NN-DK", "title": "Vitamin D3 & K2", "quantity": 1}],
  "total_value": 34.99,
  "currency": "USD",
  "delivery_interval": "60 day",
  "next_billing_date": "2026-08-15",
  "shipping_address": {
    "name": "Margaret Chen", "phone": "+15125550101",
    "address1": "742 Evergreen Ter", "address2": "",
    "city": "Austin", "province": "TX", "zip": "78701", "country": "United States"
  },
  "discount_code": null,
  "discount_percentage": null,
  "paused_until": "2026-08-15T00:00:00Z",
  "cancelled_at": null,
  "cancellation_reason": null
}
```
 
### 3.4 `Product`
```py
{
  "sku":         str,
  "name":        str,
  "price":       str,             # money as string, mirrors Shopify
  "currency":    str,             # default "USD"
  "description": Optional[str],
  "size":        Optional[str],
  "ingredients": Optional[str],
  "benefit":     Optional[str],   # short tagline for retention personalization
  "url":         Optional[str]
}
```
 
All 5 seeded products: `NN-MAG` (Magnesium Complex, $39.99), `NN-DK` (Vitamin D3 & K2, $34.99), `NN-GRN` (Daily Greens, $44.99), `NN-LYMPH` (Lymphatic Support, $42.99), `NN-PRO` (Probiotic Blend, $49.99).
 
### 3.5 Escalation record (server-side stored shape)
The endpoint returns only `{escalation_id, status}`, but the stored record (visible via `/admin/state`) is:
```py
{
  "escalation_id":    str,          # "esc_0001"
  "created_at":       str,          # ISO 8601 Z
  "customer_id":      Optional[str],
  "customer_details": Optional[str],
  "actions_taken":    Optional[str],
  "issue_for_human":  str,
  "mark_high_risk":   bool
}
```
 
### 3.6 Transcript record (server-side stored shape)
```py
{
  "transcript_id": str,          # "tr_0001"
  "created_at":    str,          # ISO 8601 Z
  "call_id":       str,
  "customer_id":   Optional[str],
  "caller_phone":  Optional[str],
  "transcript":    str,
  "summary":       Optional[str],
  "outcome":       Optional[str],
  "recording_url": Optional[str]
}
```
 
---
 
## 4. Seeded Cast (test personas)
 
From [mock-backend/seed_data.json](mock-backend/seed_data.json):
 
| ID | Name | Phone | Email | Orders | Subscriptions | Use case |
|---|---|---|---|---|---|---|
| `cust_001` | Margaret Chen | `+15125550101` | margaret.chen@example.com | `#NN1001`, `#NN1000` | `50001` ACTIVE Magnesium, `50010` **PAUSED** Vit D3+K2 | happy long-time subscriber |
| `cust_002` | James Rodriguez | `+15125550102` | james.rodriguez@example.com | `#NN1002` (in_transit) | `50002` ACTIVE Greens | lost/stalled package |
| `cust_003` | Linda Park | `+15125550103` | linda.park@example.com | `#NN1003` | `50003` ACTIVE Greens | wants to cancel over price → retention |
| `cust_004` | David Thompson | `+15125550104` | david.thompson@example.com | `#NN1004` | `50004` ACTIVE Mag, `50005` ACTIVE Vit D3+K2 (60-day) | **two active subs** — must disambiguate |
| `cust_005` | Karen Mitchell | `+15125550105` | karen.mitchell@example.com | `#NN1005` | `50006` ACTIVE Lymph | angry / legal-fraud language |
| `cust_006` | Robert Lee | **`null`** | robert.lee@example.com | `#NN1006` (in_transit) | `50007` ACTIVE Probiotic | **no phone — forces email/order fallback** |
| `cust_007` | Susan Nguyen | `+15125550107` | susan.nguyen@example.com | `#NN1007` (shipped, in_transit) | `50008` **CANCELLED** Magnesium | "stop my already-shipped order" → refund; also reactivate path |
| `cust_008` | Patricia Gomez | `+15125550108` | patricia.gomez@example.com | `#NN1008` **UNFULFILLED**, `#NN1018` **PARTIALLY_REFUNDED**, `#NN1028` | `50009` ACTIVE Greens | cancel/address-edit on unfulfilled; refund-status questions |
 
Pre-set states to test reads without mutating:
- Margaret's `50010` is **PAUSED** out of the box.
- Susan's `50008` is already **CANCELLED**.
- Patricia's `#NN1018` is already **PARTIALLY_REFUNDED** ($22.50 of $44.99).
---
 
## 5. Injected Latency & Errors (chaos)
 
All implemented in the single middleware at [app.py:79-133](mock-backend/app.py#L79-L133).
 
### Ambient (controlled by `MOCK_CHAOS`)
When `MOCK_CHAOS=1` (default):
- **Every non-exempt request** sleeps for `random.uniform(MOCK_LATENCY_MIN_MS, MOCK_LATENCY_MAX_MS)` ms — default **300–1500 ms**.
- **Paths ending in `/subscriptions`** add `MOCK_SLOW_ENDPOINT_MS` on top — default **+1200 ms** (so 1500–2700 ms typical). This matches both `/customers/{id}/subscriptions` and any future path ending the same way.
- **`MOCK_ERROR_RATE`** chance per request of a `503 upstream_unavailable` — default **0.07 (7%)**.
Set `MOCK_CHAOS=0` to disable both ambient latency and ambient errors completely. The exempt-from-chaos list: `OPTIONS`, `/health`, anything under `/admin/*`, `/docs`, `/openapi.json`, `/redoc`.
 
### Per-request deterministic controls (work even when `MOCK_CHAOS=0`)
| Query param | Header | Behavior |
|---|---|---|
| `?delay_ms=4000` | `X-Delay-Ms: 4000` | Sleeps that many ms before response. Clamped to `[0, 60000]`. Stacks with ambient latency when chaos is on. |
| `?force_error=true` | `X-Force-Error: true` | Returns an error immediately (after the deterministic delay). Code is `forced_error`. |
| `?force_status=500` | `X-Force-Status: 500` | Status for the forced error. Must be `400-599`, else defaults to `503`. |
 
Order of operations within the middleware (relevant if combining knobs):
1. Auth check (if `MOCK_API_KEY` set)
2. Deterministic `delay_ms` sleep
3. Ambient chaos latency sleep (if `MOCK_CHAOS`)
4. Deterministic `force_error` (returns immediately, bypassing the handler)
5. Ambient chaos error roll (if `MOCK_CHAOS`)
6. Handler runs
---
 
## 6. Auth & Customer Identification
 
### API auth (server-level)
Optional. Set `MOCK_API_KEY` env var; the middleware then requires `Authorization: Bearer <MOCK_API_KEY>` on every non-exempt request, else `401 unauthorized` ([app.py:89-92](mock-backend/app.py#L89-L92)). No per-user auth, no token issuance, no scopes.
 
### Caller identity (business logic)
The mock has **no concept of an authenticated caller** — there's no session, JWT, or principal. All identification is via the `/customers/lookup` endpoint, which accepts exactly one of three signals:
 
1. **`phone`** — digits-only normalized via `re.sub(r"\D", "", phone)` ([store.py:58-61](mock-backend/store.py#L58-L61)). Matches any seeded customer whose `phone` field normalizes to the same digit string. Customers with `phone: null` (Robert Lee, `cust_006`) **cannot be matched by phone at all** — they force the email/order fallback.
2. **`email`** — case-insensitive exact match against `Customer.email`.
3. **`order_number`** — strips leading `#`, lowercases, and matches against three candidates per order: `order_id` (full GID), `order_name` minus `#`, and the GID's trailing number. So `"#NN1001"`, `"NN1001"`, `"1001"`, and `"gid://shopify/Order/1001"` all match the same order ([store.py:127-139](mock-backend/store.py#L127-L139)). Then the customer is looked up via the order's `email`.
### Caller-ID trust model — important
**The mock trusts whatever you pass.** There is no verification, challenge, or signed assertion. If your agent sends `phone=+15125550101`, the mock returns Margaret Chen's full record (including order history and subscriptions) — there is no check that the call actually originated from that number, no requirement to confirm a code, address, or last-four. Any anti-spoofing or identity-confirmation logic (asking for order #, email, ZIP, etc.) must live in **your agent**; the mock does not enforce it.
 
Available challenge data per customer (i.e. fields you could verify against in the agent):
- `email`
- `phone` (when present)
- `shipping_address` (on order + sub) — `address1`, `city`, `province`, `zip`
- `order_name` / `order_id`
- last `created_at` for an order ("when was your last order")
- last-four (`card_last_four`) and `card_company` on transactions
- product titles on the most recent order / sub items
- `customer_name` (string match)
### Robert Lee's edge case
`cust_006` (Robert Lee) has `phone: null`. The README explicitly calls him out as the "caller ID not in data, fall back to email/order" persona. His seeded order `#NN1006` and email `robert.lee@example.com` are the only ways to look him up. (Aside: his order's `shipping_address.phone` is set to `+15125550106` — a number that **doesn't match any customer** — so phone-on-order isn't a usable signal either.)
 
---
 
## 7. Surprising / Gotchas
 
1. **Caller ID is fully trusted by the mock.** No challenge, no verification. Your agent must enforce any identity-confirmation flow itself. See §6.
2. **`/customers/lookup` requires *exactly one* identifier.** Passing two or three returns `400 validation`. Even if `phone` and `email` belong to the same customer, you can't combine them on the lookup — pick one.
3. **`order_id` lives in the request body, not the URL**, for all four order mutations. The reason is the Shopify GID format (`gid://shopify/Order/1001`) contains slashes that would break path routing. Subscription mutations *do* use `subscription_id` in the URL because it's a plain int.
4. **`Order.total_price` and `OrderLineItem.price` are strings; `Subscription.total_value` is a float.** This mismatch is deliberate ([models.py:54-55](mock-backend/models.py#L54-L55) note: "mirrors Shopify"). Discount math on subs ([store.py:224](mock-backend/store.py#L224)) uses float arithmetic with `round(.., 2)`; order refund math uses `float(value)` parsed from the string.
5. **`/orders/refund` only allows specific percentages**: `10, 20, 25, 30, 35, 40, 50, 60`. For 100% use `/orders/refund/full`. Sending e.g. `15` or `100` returns `400 validation`.
6. **`/orders/refund/full` on an unfulfilled order silently sets `cancelled_at`** ([store.py:278-279](mock-backend/store.py#L278-L279)). So full-refunding an unfulfilled order has the side-effect of also marking it cancelled — your agent may want to surface that combined effect to the user.
7. **Refund transactions don't carry card details.** Newly appended `REFUND` rows have `card_last_four = null, card_company = null` — so don't tell the customer "refunded to Visa ending 4242" based on the refund txn; pull that from the original `SALE` txn.
8. **The discount mutation overwrites `total_value` destructively.** `total_value *= (1 - pct/100)`. Calling it again will compound the discount against the already-reduced value. Idempotency is not the agent's friend here ([store.py:224](mock-backend/store.py#L224)).
9. **Address mutations never accept `name`/`phone`.** `AddressBody` only has `address1`, `address2`, `city`, `province`, `country`, `zip` — `name` and `phone` are always preserved from the existing address on the order/sub ([store.py:227-236](mock-backend/store.py#L227-L236), [store.py:304-315](mock-backend/store.py#L304-L315)). You can't change the recipient name via this API.
10. **The "slow endpoint" is matched by path suffix `/subscriptions`** ([app.py:110](mock-backend/app.py#L110)) — so it catches both `/customers/{id}/subscriptions` and (inferred) `/subscriptions` if you ever added it. The `+1200ms` stacks on top of the ambient `300-1500ms`, so realistic delays on this endpoint with default config are **1.5-2.7 seconds**. This is the canonical place to test filler-phrases / no-dead-air behavior.
11. **`/escalations` and `/transcripts` return thin envelopes**, not the full record. To see the stored shape (with `created_at`, full body), call `GET /admin/state`. Transcripts are *also* persisted to `mock-backend/transcripts.log` (one JSON object per line); escalations are **not** persisted to disk — they live in memory only.
12. **Order/subscription mutations return the full updated object**, but escalation/transcript mutations don't. Don't expect a consistent response envelope across mutations.
13. **The middleware runs `delay_ms` *before* it runs `force_error`.** So a request with `?delay_ms=4000&force_error=true` waits 4 seconds and *then* errors — useful for simulating "the call timed out" rather than "the call failed instantly."
14. **`/admin/reset` and `/admin/state` are completely unauthenticated** even when `MOCK_API_KEY` is set ([app.py:82-87](mock-backend/app.py#L82-L87)). Fine for local dev; would be a footgun if this were ever deployed.
15. **State is in-memory.** Any mutation (cancel, pause, refund, address change, escalation, transcript) is wiped by process restart or `POST /admin/reset`. There's no DB, no sqlite, nothing on disk — except `transcripts.log` which keeps growing across restarts and is not cleared by `/admin/reset` (**inferred** from [store.py:84-85](mock-backend/store.py#L84-L85) — `reset()` clears `self.transcripts` in memory but doesn't truncate the log file).
16. **The seed dates are anchored in 2026.** Orders span Feb–June 2026; next-billing-dates are July–August 2026. If "today" matters for your agent's time-aware copy ("your order shipped two weeks ago"), wire the agent's clock to the seed era or adjust the seed.
17. **Robert Lee's order ships to a phone number that doesn't match any customer** (`+15125550106`, [seed_data.json:286](mock-backend/seed_data.json#L286)). Looks like a typo but is consistent with the "caller ID not in data" setup — don't rely on `shipping_address.phone` as a secondary caller-ID signal.
18. **CORS is wide open** (`allow_origins=["*"]`) — fine because this is a local dev mock, but don't deploy this anywhere without changing that.
---
 
## 8. Quick Reference — Endpoint Table
 
| Method | Path | Body | Returns | Mutates? | Slow? |
|---|---|---|---|---|---|
| GET | `/health` | — | `{ok:true}` | no | exempt |
| GET | `/customers/lookup?phone=\|email=\|order_number=` | — | `{customer, orders, subscriptions}` | no | normal |
| GET | `/customers/{id}/orders` | — | `{orders:[...]}` | no | normal |
| GET | `/customers/{id}/subscriptions` | — | `{subscriptions:[...]}` | no | **+1200ms** |
| GET | `/products` | — | `{products:[...]}` | no | normal |
| GET | `/products/{sku}` | — | `Product` | no | normal |
| POST | `/subscriptions/{id}/cancel` | `{reason}` | `Subscription` | yes | normal |
| POST | `/subscriptions/{id}/pause` | `{pause_months}` | `Subscription` | yes | normal |
| POST | `/subscriptions/{id}/reactivate` | — | `Subscription` | yes | normal |
| POST | `/subscriptions/{id}/discount` | `{discount_pct, code}` | `Subscription` | yes | normal |
| POST | `/subscriptions/{id}/address` | `AddressBody` | `Subscription` | yes | normal |
| POST | `/orders/refund` | `{order_id, refund_percentage}` | `Order` | yes | normal |
| POST | `/orders/refund/full` | `{order_id}` | `Order` | yes | normal |
| POST | `/orders/cancel` | `{order_id}` | `Order` | yes | normal |
| POST | `/orders/address` | `OrderAddressBody` | `Order` | yes | normal |
| POST | `/escalations` | `EscalationBody` | `{escalation_id, status}` | yes (in-mem only) | normal |
| POST | `/transcripts` | `TranscriptBody` | `{transcript_id, saved}` | yes (+log file) | normal |
| POST | `/admin/reset` | — | `{reset:true}` | yes | exempt |
| GET | `/admin/state` | — | full dump | no | exempt |