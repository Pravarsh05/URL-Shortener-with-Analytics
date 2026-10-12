# PRD: URL Shortener with Analytics

| Field | Value |
|---|---|
| **Status** | Draft v1.0 |
| **Type** | Portfolio / system design project |
| **Owner** | You |

---

## 1. Overview
A service that turns long URLs into short links, redirects visitors quickly and reliably, and gives link owners analytics on how their links perform. It supports custom aliases, link expiry, rate limiting, and Redis caching, and is designed so it can be discussed and demonstrated at scale (millions of requests).

---

## 2. Problem Statement
Long URLs are hard to share, look untrustworthy, and give no visibility into engagement. Existing shorteners often paywall analytics and custom aliases. Users need a fast, reliable way to create branded, trackable, optionally temporary links, and operators need protection against abuse.

---

## 3. Goals and Non-Goals

### Goals
* Create and resolve short links with very low redirect latency.
* Let users pick custom aliases and set expiry.
* Provide per-link click analytics (volume, time series, referrer, device, country).
* Protect the service from abuse via rate limiting and URL validation.
* Demonstrate scalable design: caching, indexing, async pipelines, ID generation.

### Non-Goals (v1)
* Full-featured marketing suite (A/B testing, UTM builders, team workspaces).
* Custom domains per user.
* Real-time (sub-second) analytics dashboards.
* Native mobile apps.

---

## 4. Target Users

| Persona | Needs |
|---|---|
| **Casual sharer** | Paste a URL, get a short link instantly, no signup friction |
| **Creator / marketer** | Custom aliases, click stats, expiry for campaigns |
| **Developer** | Clean REST API, API keys, predictable rate limits |
| **Operator (you)** | Metrics, abuse controls, ability to disable bad links |

---

## 5. User Stories

1. As a user, I can submit a long URL and receive a short URL so I can share it easily.
2. As a user, I can request a custom alias so my link is memorable and branded.
3. As a user, I can set an expiry date so a campaign link stops working automatically.
4. As a visitor, I am redirected to the destination almost instantly.
5. As a link owner, I can view total clicks, clicks over time, top referrers, devices, and countries.
6. As a link owner, I can disable or delete a link at any time.
7. As an operator, I can throttle abusive clients and block malicious or self-referencing URLs.
8. As a developer, I can call the API with an API key and see clear rate limit headers.

---

## 6. Functional Requirements

> Priority: **P0** = must have, **P1** = should have, **P2** = nice to have.

### 6.1 Link creation

| ID | Requirement | Priority |
|---|---|---|
| **FR-1** | Accept a long URL and return a unique short code and short URL | **P0** |
| **FR-2** | Validate URL: http/https only, max length, reject localhost/private IPs (SSRF), reject links to own domain | **P0** |
| **FR-3** | Support optional custom alias: 3-16 chars, `[a-zA-Z0-9_-]`, case-sensitive, unique | **P0** |
| **FR-4** | Reject reserved aliases (`api`, `admin`, `health`, etc.) | **P0** |
| **FR-5** | Return a clear conflict error if an alias is taken (no check-then-insert race) | **P0** |
| **FR-6** | Support optional expiry timestamp | **P0** |
| **FR-7** | Generated codes must be non-sequential and non-guessable | **P1** |

### 6.2 Redirect

| ID | Requirement | Priority |
|---|---|---|
| **FR-8** | `GET /:code` returns HTTP 302 to the destination | **P0** |
| **FR-9** | Unknown code returns 404; expired or disabled returns 410 | **P0** |
| **FR-10** | Redirect path never blocks on analytics writes | **P0** |
| **FR-11** | Redirect endpoint continues serving from cache if the database is degraded | **P1** |

### 6.3 Link management

| ID | Requirement | Priority |
|---|---|---|
| **FR-12** | Fetch link metadata by code | **P0** |
| **FR-13** | Delete or disable a link; change takes effect immediately (cache invalidated) | **P0** |
| **FR-14** | List a user's links, newest first | **P1** |
| **FR-15** | API key authentication and per-user quotas | **P2** |

### 6.4 Analytics

| ID | Requirement | Priority |
|---|---|---|
| **FR-16** | Record each click: timestamp, hashed IP, country, device, browser, referrer | **P0** |
| **FR-17** | Stats endpoint: total clicks, clicks by day/hour, top referrers, devices, countries, filterable by date range | **P0** |
| **FR-18** | Unique visitor counts (approximate is acceptable) | **P1** |
| **FR-19** | Daily rollups so dashboards stay fast on large link histories | **P1** |
| **FR-20** | Basic dashboard UI with charts | **P2** |

### 6.5 Protection

| ID | Requirement | Priority |
|---|---|---|
| **FR-21** | Rate limit link creation (per IP / API key), e.g. 20 per minute | **P0** |
| **FR-22** | Looser rate limit on redirects, e.g. 300 per minute per IP | **P1** |
| **FR-23** | Return 429 with `Retry-After` and `X-RateLimit-Remaining` headers | **P0** |
| **FR-24** | Malicious URL check (e.g. safe-browsing lookup) | **P2** |

---

## 7. Non-Functional Requirements

| Area | Target |
|---|---|
| **Redirect latency** | p99 under 50 ms on cache hit |
| **Throughput** | 5-10k redirects/sec on a single node with a warm cache (load-test goal) |
| **Availability** | Redirects highly available; degrade gracefully if DB or Redis is unavailable |
| **Read/write ratio** | Designed for roughly 100:1 |
| **Consistency** | Link CRUD strongly consistent; analytics eventually consistent (seconds) |
| **Privacy** | Store hashed IPs only; no raw IP retention |
| **Security** | Input validation, SSRF protection, no open redirect to internal hosts |
| **Observability** | Metrics for latency, cache hit rate, error rate, queue lag; structured logs |
| **Scalability** | Stateless API tier; horizontal scaling path for cache, DB, and analytics store |

---

## 8. Key Design Decisions

* **302 over 301**: keeps click counting, expiry, and deletion enforceable.
* **Counter + base62 with scrambling**: collision-free codes without guessable sequences; range allocation removes the single-sequence bottleneck.
* **Cache-aside Redis** with negative caching and stampede protection.
* **Async analytics** via Redis Streams and a batch worker, with rollup tables for dashboards.
* **Lazy expiry check** at redirect time plus a background cleanup job.
* **Atomic rate limiting** using Redis Lua scripts; fail-open for redirects.

---

## 9. API Surface

```http
POST   /api/v1/links            {url, alias?, expires_at?}  -> 201 {short_url, code}
GET    /api/v1/links/:code                                  -> link metadata
DELETE /api/v1/links/:code                                  -> 204
GET    /api/v1/links/:code/stats?from=&to=&granularity=day  -> analytics
GET    /:code                                               -> 302 redirect
```

---

## 10. Success Metrics

| Metric | Target |
|---|---|
| **Redirect p99 latency (cache hit)** | < 50 ms |
| **Cache hit rate on redirects** | > 90% under realistic traffic |
| **Load test throughput** | 5-10k RPS single node |
| **Alias collision correctness** | 0 duplicate aliases under concurrent creation tests |
| **Analytics delay** | Clicks visible in stats within ~5 seconds |
| **Click loss** | < 0.1% under normal operation |
| **Availability during simulated DB outage** | Cached links still redirect |

---

## 11. Release Scope

* **MVP**: All P0 requirements: create, alias, expiry, redirect, delete, basic stats, rate limiting, Redis caching.
* **v1.1**: P1 items: scrambled codes, unique visitors, rollups, link listing, DB-degraded redirects, redirect rate limits.
* **Later**: P2 items: API keys and quotas, dashboard UI, malicious URL checks, QR codes, link previews, password-protected links, bulk creation, bot filtering.

---

## 12. Assumptions and Dependencies

* PostgreSQL and Redis are available (Docker Compose locally, managed services in deployment).
* MaxMind GeoLite2 (offline DB) for country resolution; a user-agent parser library for device and browser.
* Anonymous link creation is allowed in v1, with stricter rate limits than authenticated use.

---

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Abuse for phishing or spam** | Rate limits, URL validation, disable capability, optional safe-browsing check |
| **Redis outage loses in-flight click events** | Streams with consumer acks; fail-open redirects; Kafka at higher scale |
| **Hot key from a viral link** | In-process LRU in front of Redis; CDN caching of short-TTL redirects |
| **Alias squatting and reserved-word collisions** | Reserved list, length rules, rate limits |
| **Raw click table growth** | Monthly partitioning, rollups, archive or move to ClickHouse/TimescaleDB |
| **Guessable codes enable enumeration** | Scrambled IDs, negative caching, redirect rate limits |

---

## 14. Open Questions

1. Is anonymous creation allowed permanently, or only until accounts are added?
2. Should expired links free their alias for reuse, and after how long?
3. How long should raw click events be retained before aggregation-only storage?
4. Is a public dashboard in scope, or is the API enough for the first release?
5. Which language and hosting platform will be used for the reference deployment?
