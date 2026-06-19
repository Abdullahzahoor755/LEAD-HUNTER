# Lead_hunter

AI-powered lead generation and outreach platform that finds business leads, filters fake companies, analyzes fit with Claude, generates cold emails, automates outreach and follow-ups, and tracks replies in a dashboard.

## Multi-tenant SaaS structure

The project now includes a tenant-aware SaaS application package under [`app/`](/home/mabdullah/Desktop/lead generator ai/app):

- `app/api`: API-facing application layer and route functions.
- `app/agents`: modular agent system with a registry and pluggable agent classes.
- `app/core`: tenant-scoped domain models, interfaces, and isolation guards.
- `app/db`: database abstraction layer plus in-memory repository backend.
- `app/services`: business services for leads, outreach, jobs, and runtime composition.
- `app/workers`: async background job queue and worker bootstrap.
- `app/frontend`: tenant-aware frontend/dashboard context helpers.
- `app/configs`: central application settings.

## Postgres-first backend

The web runtime is now configured Postgres-first. Default environment shape:

```env
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/lead_generator
DATABASE_ECHO=false
```

- FastAPI startup initializes the SQL schema automatically.
- `GET /readyz` verifies database connectivity.
- The legacy compatibility runtime still falls back to the in-memory repository facade until the last CSV-bound sync services are migrated.

## Architecture notes

- `tenant_id` is part of every core model and every repository operation.
- Agents are no longer hard-coded scripts only; they are registered modules that can be scheduled per tenant.
- Background work is handled through `Job` records plus `AsyncJobQueue`.
- The current monolith remains in place for compatibility, while the new SaaS layer wraps and prepares it for gradual migration.
- Every API request must resolve a `tenant_id` before accessing application services.
- Request-scoped tenant middleware and DB session guards block cross-tenant access.

## Manual Voice Agent test

Developer-only checklist for verifying a real single Vapi call:

- Set `VAPI_API_KEY`.
- Set `VAPI_ASSISTANT_ID`.
- Confirm the target lead belongs to your tenant and has a verified test phone number.
- Call `POST /voice/call/{lead_id}` with an authenticated tenant user token.
- Expect the verified phone to ring.
- Confirm the response includes `call_id`, `vapi_call_id`, and `status`.

## Legacy lead pipeline agents

`leads.py` now routes the lead workflow through JSON-in/JSON-out agents:

- `DiscoveryAgent`: search query in, candidate website list out.
- `ScraperAgent`: website in, scraped text and contact data out.
- `CleaningAgent`: scraped payload in, normalized lead-ready data out.
- `ScoringAgent`: cleaned data in, scored lead JSON out, including `LeadQualityFilter` and LLM intent analysis results.
- `EmailAgent`: business info, website summary, and lead score in, 3 email variants plus hook and CTA out.
- `OutreachAgent`: lead JSON in, sent email metadata out.

Each agent returns a structured dictionary and logs a JSON summary of its output.
