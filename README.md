# ICD-10 Diagnosis Search

This project now supports two hosting shapes:

- a local Python app for quick testing
- an Azure Static Web Apps layout for low-cost hosting with a static frontend and serverless API

The search behavior is designed for diagnosis lookup work:

- wildcard-style phrase search
- ranked best-match results
- alias-aware matching for shorthand like `HTN`, `DM2`, `T2DM`, `CAD`, `COPD`, and similar inputs
- a Postgres-ready data path for future scale

## Project Layout

- `web/` static frontend for Azure Static Web Apps
- `api/` Azure Functions API used by Static Web Apps
- `api/data/` generated deployment data files for the Azure API
- `build_db.py` imports the Excel workbook and generates all runtime artifacts
- `app.py` local threaded Python web app for desktop/local usage
- `diagnosis_aliases.json` editable shorthand and preferred-code hints
- `postgres_schema.sql` scalable Postgres schema for future multi-user deployment
- `.github/workflows/azure-static-web-apps.yml` GitHub Actions deployment workflow template

## Build the Data

Use the bundled Python in this workspace or your local Python installation:

```powershell
python build_db.py "C:\Users\sonus\Downloads\section111_valid_icd10_october2025 (1).xlsx"
```

This command generates:

- `data/icd10_runtime.sqlite3` for the local Python app
- `data/postgres_seed/*.csv` for Azure Database for PostgreSQL seeding
- `api/data/icd10_entries.ndjson` and `api/data/diagnosis_aliases.json` for Azure Functions

## Local Run

```powershell
python app.py
```

Then open `http://127.0.0.1:8000`.

## Azure Static Web Apps Deployment

### Cheapest recommended Azure setup

- Host the frontend in Azure Static Web Apps Free
- Use the integrated Azure Functions API in `api/`
- Point `www.freemedidata.com` to the Static Web App
- Redirect the apex domain `freemedidata.com` to `www.freemedidata.com`

### Deploy steps

1. Push this project to GitHub.
2. In Azure Portal, create a new **Static Web App**.
3. Choose your GitHub repo and branch.
4. Use these build settings:
   - App location: `web`
   - API location: `api`
   - Output location: leave blank
5. After Azure creates the app, copy the deployment token if needed and store it in GitHub as `AZURE_STATIC_WEB_APPS_API_TOKEN`.
6. Re-run the workflow or push a new commit.

### Custom domain for freemedidata.com

Recommended:

- Primary site: `www.freemedidata.com`
- Root redirect: `freemedidata.com` -> `https://www.freemedidata.com`

In Azure Static Web Apps:

1. Open your Static Web App.
2. Go to `Custom domains`.
3. Add `www.freemedidata.com`.
4. Azure will give you a validation TXT record and a target hostname.
5. In your DNS provider:
   - add the TXT validation record Azure gives you
   - add a `CNAME` for `www` pointing to your Azure Static Web App hostname
6. After validation, Azure will issue free HTTPS automatically.

For the root domain:

- if your registrar supports forwarding, forward `freemedidata.com` to `https://www.freemedidata.com`
- if your DNS provider supports ALIAS/ANAME flattening, you can also map the apex directly, but `www` is simpler and cheaper to manage

## Postgres Scale-Up Path

When traffic grows or startup latency from loading the dataset into serverless memory becomes limiting:

1. Create Azure Database for PostgreSQL Flexible Server.
2. Run `postgres_schema.sql`.
3. Load:
   - `data/postgres_seed/icd10_entries.csv`
   - `data/postgres_seed/diagnosis_aliases.csv`
4. Replace the file-backed search in the Azure API with Postgres queries.

The schema is set up for:

- `tsvector` full-text ranking
- `pg_trgm` partial matching
- separate alias management from core ICD data

## Alias Customization

Edit `diagnosis_aliases.json` to add more shorthand used by your providers, coders, or billers.

Re-run `build_db.py` after any alias change so the local app, Azure API artifacts, and Postgres seed files all stay in sync.
