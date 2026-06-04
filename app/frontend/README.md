# Frontend Layer

`app/frontend` is the tenant-aware presentation layer.

- `dashboard.py` builds summary context per `tenant_id`.
- `app.py` is a lightweight frontend entrypoint for wiring the API/application runtime into dashboards or future web apps.

