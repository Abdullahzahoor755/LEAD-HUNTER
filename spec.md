# LeadForge AI — Enterprise SaaS System Specification

**Platform Name:** LeadForge AI (Lead Hunter / LeadForge AI Sales Engine)
**Architecture Class:** Multi-Tenant Production SaaS
**Backend:** FastAPI + Async SQLAlchemy + PostgreSQL
**AI Runtime:** Claude (Anthropic)
**Version:** 1.0.0-production

---

## 1. Platform Overview

LeadForge AI is a multi-tenant, AI-powered B2B lead generation and outreach automation platform built for enterprise sales teams targeting high-ticket IT infrastructure, cybersecurity, cloud, and managed services buyers — primarily across GCC markets (Saudi Arabia, UAE, Qatar) with global enterprise reach.

The platform replaces legacy CSV-based automation with a fully orchestrated async pipeline spanning discovery, AI qualification, outreach generation, Gmail delivery, reply classification, and follow-up scheduling — all scoped to isolated per-tenant data environments.

---

## 2. Tenant Isolation Architecture

### 2.1 Multi-Tenancy Model

LeadForge AI implements **row-level multi-tenancy** using a `tenant_id` UUID column on every data-bearing table. There is no schema-per-tenant separation; instead, all tenants share the same PostgreSQL schema with mandatory `tenant_id` enforcement at the ORM layer.

### 2.2 Tenant Isolation Enforcement

- Every SQLAlchemy model includes a non-nullable `tenant_id: UUID` column with a database-level NOT NULL constraint and a foreign key reference to the `tenants` table.
- All ORM queries are wrapped via a `TenantSession` context manager that automatically appends `.filter(Model.tenant_id == current_tenant_id)` to every query.
- No cross-tenant query is permitted outside of the internal admin superuser role.
- FastAPI dependency injection resolves `current_tenant_id` from the decoded JWT on every authenticated request.
- Middleware enforces that the resolved `tenant_id` matches the JWT claims before any handler executes.

### 2.3 Tenant Provisioning Flow

1. Admin creates tenant record via `/admin/tenants` API.
2. System generates `tenant_id` (UUID v4), `api_key`, and default configuration row.
3. Tenant billing plan is assigned (starter / growth / enterprise).
4. Tenant Gmail OAuth credential slot is initialized as empty.
5. Tenant receives invite link to dashboard onboarding.

### 2.4 Tenant Configuration Store

Each tenant has a `tenant_config` JSONB column storing:

```json
{
  "outreach_daily_limit": 50,
  "follow_up_delay_days": 3,
  "max_follow_ups": 3,
  "scoring_threshold": 7,
  "target_regions": ["SA", "UAE", "QA"],
  "target_industries": ["IT Infrastructure", "Cybersecurity", "Cloud"],
  "gmail_from_alias": "sales@tenantdomain.com",
  "reply_classification_enabled": true,
  "auto_followup_enabled": true
}
```

---

## 3. PostgreSQL Architecture

### 3.1 Core Tables

| Table | Purpose |
|---|---|
| `tenants` | Tenant registry, billing plan, status |
| `tenant_config` | Per-tenant operational configuration |
| `users` | Tenant users with role (owner, member, viewer) |
| `jobs` | Async job records with state machine |
| `leads` | Enriched lead records with lifecycle state |
| `campaigns` | Outreach campaign groupings |
| `outreach_messages` | Generated email drafts per lead |
| `sent_emails` | Gmail send log with message_id tracking |
| `email_replies` | Inbound reply records from Gmail watch |
| `follow_ups` | Scheduled follow-up queue |
| `gmail_credentials` | Encrypted OAuth tokens per tenant |
| `billing_events` | Credit usage, plan changes, payments |
| `audit_log` | Immutable tenant action audit trail |

### 3.2 Lead Table Schema

```sql
CREATE TABLE leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    job_id UUID REFERENCES jobs(id),
    company_name TEXT NOT NULL,
    website TEXT,
    industry TEXT,
    country TEXT,
    city TEXT,
    raw_html TEXT,
    cleaned_text TEXT,
    ai_response JSONB,
    company_summary TEXT,
    needs_it_services BOOLEAN,
    lead_score INTEGER CHECK (lead_score BETWEEN 0 AND 10),
    buying_intent_score INTEGER CHECK (buying_intent_score BETWEEN 0 AND 100),
    service_demand_score INTEGER CHECK (service_demand_score BETWEEN 0 AND 100),
    urgency_score INTEGER CHECK (urgency_score BETWEEN 0 AND 100),
    intent_summary TEXT,
    signals JSONB,
    lifecycle_state TEXT NOT NULL DEFAULT 'discovered',
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT valid_lifecycle CHECK (
        lifecycle_state IN (
            'discovered', 'scraped', 'cleaned', 'scored',
            'qualified', 'rejected', 'outreach_pending',
            'outreach_sent', 'replied', 'follow_up_scheduled',
            'follow_up_sent', 'converted', 'unsubscribed', 'dead'
        )
    )
);
```

### 3.3 Job Table Schema

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    config JSONB NOT NULL DEFAULT '{}',
    result_summary JSONB,
    error_log JSONB,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT valid_status CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'retrying')
    ),
    CONSTRAINT valid_job_type CHECK (
        job_type IN (
            'discovery', 'scraping', 'cleaning', 'scoring',
            'outreach_generation', 'gmail_send', 'reply_monitor',
            'follow_up', 'full_pipeline'
        )
    )
);
```

### 3.4 Indexing Strategy

```sql
-- Tenant scoping — all queries filter by tenant_id
CREATE INDEX idx_leads_tenant_id ON leads(tenant_id);
CREATE INDEX idx_leads_lifecycle ON leads(tenant_id, lifecycle_state);
CREATE INDEX idx_leads_score ON leads(tenant_id, lead_score DESC);
CREATE INDEX idx_jobs_tenant_status ON jobs(tenant_id, status);
CREATE INDEX idx_follow_ups_scheduled ON follow_ups(tenant_id, scheduled_at) WHERE sent = false;
CREATE INDEX idx_sent_emails_message_id ON sent_emails(gmail_message_id);
CREATE INDEX idx_email_replies_thread ON email_replies(gmail_thread_id);
```

---

## 4. Async Worker Queue Design

### 4.1 Architecture

LeadForge AI uses an internal async task queue backed by PostgreSQL (no external broker required at baseline). Workers are async Python coroutines managed by a `WorkerPool` class using `asyncio.gather` with concurrency limits per job type.

For production scale, the queue is designed to be swappable with Celery + Redis or ARQ without API changes.

### 4.2 Job Lifecycle State Machine

```
pending → running → completed
                 → failed → retrying → running
                                    → failed (exhausted)
pending → cancelled
```

### 4.3 Worker Concurrency Limits

| Job Type | Max Concurrent (per tenant) | Global Limit |
|---|---|---|
| discovery | 1 | 10 |
| scraping | 3 | 20 |
| cleaning | 5 | 30 |
| scoring (AI) | 2 | 10 |
| outreach_generation | 2 | 10 |
| gmail_send | 1 | 10 |
| reply_monitor | 1 | 20 |
| follow_up | 1 | 10 |

### 4.4 Retry & Recovery

- All jobs support configurable `max_retries` (default 3).
- Retry delay uses exponential backoff: `2^retry_count * 10` seconds.
- On final failure, the job is marked `failed` and the `error_log` JSONB is populated with the exception class, message, and traceback.
- A dead-letter alerting webhook is fired to the tenant's configured webhook URL if set.
- Failed lead-level steps mark the lead `lifecycle_state` as `{step}_failed` with a `rejection_reason` populated from the error context.

### 4.5 Job Orchestration (Full Pipeline)

When a `full_pipeline` job is submitted, the orchestrator:

1. Creates child jobs: `discovery → scraping → cleaning → scoring → outreach_generation → gmail_send`
2. Each child job is linked via `parent_job_id` on the jobs table.
3. The orchestrator monitors child completion via async polling with 5s intervals.
4. If any child fails beyond retries, the pipeline halts and the tenant is notified.
5. The parent job summarizes aggregate results in `result_summary` JSONB.

---

## 5. AI Pipeline Orchestration

### 5.1 Agent Definitions

**DiscoveryAgent**
Accepts a search query + region config. Submits Google Search API calls (or SerpAPI) and returns raw URL + title + snippet lists. Deduplicates against already-scraped domains in the tenant's lead table.

**ScraperAgent**
Accepts URL list. Uses async HTTP (httpx) with rotating user-agents and optional proxy pool. Returns raw HTML per URL. Implements robots.txt checking. Timeout: 15s per URL. Max HTML size: 500KB (truncated beyond).

**CleaningAgent**
Accepts raw HTML. Strips scripts, styles, nav, footer boilerplate. Extracts: visible text, meta description, title, contact signals, LinkedIn hints, job posting signals. Returns `cleaned_text` (max 4000 tokens for AI context safety).

**ScoringAgent**
Accepts `cleaned_text` + `company_name` + `website`. Calls Claude API with `claude.md` system prompt + `skill.md` scoring rubric injected. Parses strict JSON response. Populates all lead score fields. Enforces schema validation before DB write.

**OutreachAgent**
Accepts qualified leads (score ≥ threshold). Generates personalized cold email using lead signals, company summary, and tenant's service profile. Returns subject + body. Stores in `outreach_messages` table.

**ReplyMonitorAgent**
Uses Gmail Push Notifications (Pub/Sub) or polling. Fetches new messages in tracked threads. Classifies reply intent (interested / not interested / auto-reply / bounce / referral / request_info). Updates lead lifecycle accordingly.

**FollowupAgent**
Queries `follow_ups` table for scheduled items where `scheduled_at <= NOW()` and `sent = false`. Sends follow-up emails via Gmail. Updates lead lifecycle to `follow_up_sent`. Schedules next follow-up if count < max.

---

## 6. Gmail Outreach Architecture

### 6.1 OAuth Per Tenant

- Each tenant authenticates a Gmail account via Google OAuth 2.0 (scope: `gmail.send`, `gmail.readonly`, `gmail.modify`).
- OAuth tokens (access + refresh) are stored encrypted in `gmail_credentials` table using AES-256-GCM with a per-tenant-derived key.
- Token refresh is handled automatically before each Gmail API call with a 5-minute expiry buffer.
- Each tenant can connect exactly one Gmail account per plan tier (enterprise allows multiple).

### 6.2 Send Rate Limiting

- Gmail API limits: 250 quota units/second, 1,000,000 units/day.
- Platform enforces: max 50 sends/day per tenant (configurable per plan).
- Send queue drains at max 1 email per 30 seconds per tenant to avoid spam classification.
- All sends are logged in `sent_emails` with `gmail_message_id` and `gmail_thread_id` for reply tracking.

### 6.3 Reply Detection

- Gmail Push Notifications via Google Cloud Pub/Sub (preferred) or polling fallback every 5 minutes.
- On new message in tracked thread: fetch full message, classify via ReplyMonitorAgent.
- Classification result stored in `email_replies` table.
- Lead lifecycle updated based on classification.

### 6.4 Bounce & Unsubscribe Handling

- Hard bounces: lead marked `dead`, domain added to tenant bounce list.
- Soft bounces: retry after 48h, max 2 retries, then `dead`.
- Unsubscribe keywords detected in reply body → lead marked `unsubscribed`, domain suppressed permanently.
- All suppressions stored in `email_suppressions` table scoped to `tenant_id`.

---

## 7. Billing & Payment Approval Flow

### 7.1 Plan Tiers

| Plan | Monthly Leads | Daily Sends | AI Calls | Price |
|---|---|---|---|---|
| Starter | 500 | 25 | 500 | $49/mo |
| Growth | 2,000 | 100 | 2,000 | $149/mo |
| Enterprise | Unlimited | 500 | Unlimited | Custom |

### 7.2 Credit Tracking

- Each AI scoring call = 1 AI credit consumed.
- Each Gmail send = 1 send credit consumed.
- Credits are tracked in `billing_events` table with `event_type`, `quantity`, `tenant_id`, `timestamp`.
- Monthly usage aggregated via a scheduled worker job at midnight UTC on the 1st of each month.

### 7.3 Billing Approval Flow

1. Tenant submits payment intent via Stripe Checkout session.
2. Stripe webhook fires `payment_intent.succeeded`.
3. Platform handler validates webhook signature.
4. Billing event written: `plan_upgraded`, new plan limits applied to tenant config.
5. Admin panel shows all billing events with approve/reject override for enterprise custom plans.

### 7.4 Over-Limit Enforcement

- On credit exhaustion: new jobs are queued but not started; tenant receives in-dashboard alert.
- API calls beyond limit return HTTP 402 with `X-LeadForge-Limit-Reason` header.
- Admin can grant one-time credit overrides from the admin panel.

---

## 8. Dashboard API Architecture

### 8.1 Endpoint Groups

| Group | Base Path | Purpose |
|---|---|---|
| Auth | `/api/v1/auth` | Login, refresh, logout |
| Tenants | `/api/v1/tenants` | Tenant CRUD (admin only) |
| Jobs | `/api/v1/jobs` | Submit, status, cancel jobs |
| Leads | `/api/v1/leads` | Lead list, detail, export |
| Campaigns | `/api/v1/campaigns` | Campaign management |
| Outreach | `/api/v1/outreach` | Draft review, approve, send |
| Gmail | `/api/v1/gmail` | OAuth connect, status, revoke |
| Billing | `/api/v1/billing` | Usage, plan, invoices |
| Analytics | `/api/v1/analytics` | Aggregate metrics per tenant |
| Admin | `/api/v1/admin` | Superuser-only operations |

### 8.2 Analytics Endpoints

- `GET /api/v1/analytics/leads` — leads by lifecycle state, daily discovery rate, qualification rate.
- `GET /api/v1/analytics/outreach` — sends, open rates (estimated), reply rates, bounce rates.
- `GET /api/v1/analytics/scoring` — average lead score, score distribution, rejection rate.
- `GET /api/v1/analytics/pipeline` — funnel view from discovered → converted.

---

## 9. Lead Lifecycle States

| State | Description |
|---|---|
| `discovered` | URL found by DiscoveryAgent, not yet scraped |
| `scraped` | Raw HTML captured |
| `cleaned` | Text extracted and normalized |
| `scored` | AI analysis complete, score assigned |
| `qualified` | Score meets tenant threshold, eligible for outreach |
| `rejected` | Score below threshold or hard rejection signal detected |
| `outreach_pending` | Email draft generated, awaiting send |
| `outreach_sent` | Email delivered via Gmail |
| `replied` | Inbound reply detected and classified |
| `follow_up_scheduled` | Follow-up queued |
| `follow_up_sent` | Follow-up email delivered |
| `converted` | Positive reply → marked as sales opportunity |
| `unsubscribed` | Explicit or detected opt-out |
| `dead` | Hard bounce, invalid domain, or manual discard |

---

## 10. Security Architecture

### 10.1 Authentication

- JWT RS256 tokens issued on login. Access token TTL: 15 minutes. Refresh token TTL: 7 days.
- Refresh tokens stored hashed in `user_sessions` table with `tenant_id` binding.
- All tokens validated for tenant_id claim consistency on every request.

### 10.2 Secrets Management

- Gmail OAuth tokens: AES-256-GCM encrypted at rest, key derived per tenant from master secret via HKDF.
- Database credentials: injected via environment variables, never hardcoded.
- API keys: stored as bcrypt hashes, never returned after creation.
- Stripe webhook secret: validated on every incoming Pub/Sub and Stripe event.

### 10.3 Input Validation

- All incoming JSON validated via Pydantic v2 models.
- URL inputs sanitized and validated against allowlist schemes (https only).
- AI responses validated against JSON schema before any DB write.
- SQL injection not possible via async SQLAlchemy parameterized queries.

### 10.4 Audit Logging

- Every privileged action (lead export, credential change, plan upgrade, admin override) written to `audit_log` with `tenant_id`, `user_id`, `action`, `payload_hash`, `timestamp`.
- Audit log is append-only; no UPDATE or DELETE permitted by application role.

---

## 11. Admin Panel Behavior

- Superuser-only access enforced via `is_superuser` flag on `users` table.
- Admin can: view all tenants, impersonate tenant session, override billing, grant credits, disable tenant, view global job queue, clear failed jobs, inject test leads.
- All admin actions recorded in `audit_log` with `actor_type: superuser`.
- Admin panel exposes `/api/v1/admin/metrics` for global platform health: total tenants, active jobs, failed jobs (24h), daily AI calls, daily sends.

---

## 12. Scaling Considerations

### 12.1 Horizontal Scaling

- FastAPI workers are stateless and horizontally scalable behind a load balancer (nginx / AWS ALB).
- Database connections managed via `asyncpg` connection pool with per-instance limits.
- Worker processes can run as separate Kubernetes pods or Docker containers, all reading from the same PostgreSQL job queue.

### 12.2 Read Replicas

- Analytics and dashboard read queries route to a PostgreSQL read replica.
- Write operations (lead state changes, job updates) route to primary.
- Read/write routing handled via `TenantSession` context: `session.execute(select(...), execution_options={"schema_translate_map": None, "use_replica": True})`.

### 12.3 AI Rate Limiting

- Claude API calls are rate-limited per tenant and globally.
- A token bucket implementation controls burst: max 10 scoring calls/minute per tenant.
- Overflow queued in the job system with `status: pending` until capacity available.

### 12.4 Storage

- Raw HTML and cleaned text stored in leads table JSONB/TEXT columns for datasets < 10M rows.
- Beyond 10M rows: migrate raw_html to S3-compatible object storage, store only S3 key in DB.
- Outreach message bodies stored as TEXT, indexed on `tenant_id + lead_id`.
