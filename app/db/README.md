# PostgreSQL Storage

`leads.csv` is no longer the target persistence model for the SaaS architecture.

Apply [`app/db/sql/schema.sql`](/home/mabdullah/Desktop/lead generator ai/app/db/sql/schema.sql) to PostgreSQL to create:

- `tenants`
- `users`
- `campaigns`
- `leads`
- `emails`
- `replies`
- `followups`

Every table includes `tenant_id`, and the schema adds tenant-scoped indexes for common filters and joins.

The web/API runtime is now configured Postgres-first. The remaining legacy compatibility runtime still falls back to the in-memory repository facade until the last sync services are migrated away from CSV-era flow.
