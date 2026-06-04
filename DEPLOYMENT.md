# Railway Beta Deployment

This project deploys as two Railway services plus one Railway Postgres database:

- Backend service: FastAPI API.
- Frontend service: Streamlit dashboard.
- Database: Railway Postgres.

Do not commit local secrets. Configure all secret values in Railway service variables.

## 1. Railway Project Setup

1. Create a Railway project.
2. Add a Postgres database.
3. Add a backend service from this repository.
4. Add a second frontend service from the same repository.
5. Set the backend service public domain first.
6. Set `APP_API_BASE_URL` on the frontend service to the backend public URL.

## 2. Backend Service

Use the repository root as the service root.

Install command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn app.api.app:app --host 0.0.0.0 --port $PORT
```

Required variables:

```env
APP_ENV=production
DATABASE_BACKEND=postgres
DATABASE_URL=${{Postgres.DATABASE_URL}}
JWT_SECRET=<strong-random-secret>
SECRET_ENCRYPTION_KEY=<strong-random-encryption-key>
SERPER_API_KEY=<serper-api-key>
```

Optional variables:

```env
ANTHROPIC_API_KEY=<anthropic-api-key>
ANTHROPIC_MODEL=claude-sonnet-4-6
JWT_EXPIRATION_SECONDS=86400
DATABASE_ECHO=false
JOB_QUEUE=default
```

Lead generation can run in fallback mode when `ANTHROPIC_API_KEY` is missing or unavailable.

## 3. Frontend Service

Use the repository root as the service root.

Install command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
streamlit run dashboard.py --server.port $PORT --server.address 0.0.0.0
```

Required variables:

```env
APP_ENV=production
APP_API_BASE_URL=https://<backend-service-domain>
```

## 4. Postgres

Railway provides the Postgres `DATABASE_URL`. Attach the Postgres database to the backend service and map it into `DATABASE_URL`.

The backend initializes the SQL schema on startup. Verify readiness after deployment with:

```bash
curl https://<backend-service-domain>/readyz
```

## 5. Smoke Checklist

Backend:

- `GET /healthz` returns `{"status":"ok"}`.
- `GET /readyz` confirms database connectivity.

Frontend:

- Login works.
- Signup works and creates a Free account.
- Generate Leads starts and completes or shows a clear queued/running status.
- CSV export downloads leads.
- Admin menu is hidden for normal users.
- Admin endpoints return `403` for normal users.

## 6. Production Checklist

- `APP_ENV=production` is set on backend and frontend.
- `JWT_SECRET` is strong and not the default value.
- `SECRET_ENCRYPTION_KEY` is set for Gmail/provider credential encryption.
- `DATABASE_URL` does not use a default local password.
- `.env`, `token.json`, `credentials.json`, and exported CSV files are not committed.
- Backend public URL is copied into frontend `APP_API_BASE_URL`.
- Main admin role has been verified with:

```bash
python scripts/fix_admin_roles.py --dry-run --admin-identifier mian755
```

- Free user cannot access outreach, Gmail settings, reply checking, followups, or admin.
- Pro/Agency users can access paid automation features after plan activation.

## 7. Notes For Beta

For the beta deployment, lead generation is triggered by the dashboard request path. A separate long-running worker service can be added later if request duration becomes a Railway timeout issue.
