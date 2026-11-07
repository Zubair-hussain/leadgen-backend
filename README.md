# LeadGen backend

Django API for LeadGen: lead discovery (Google Maps, Yelp, Nextdoor, True Local), regional compliance
(US, UK/EU, Australia) and Gmail outreach with a 10-20 emails per day cap.

## Deploy on Vercel (free)
1. Vercel -> Add New -> Project -> import this repo. Framework: **Other**. Root Directory: leave empty.
2. Add the environment variables from `.env.example` (Neon `DATABASE_URL`, `SECRET_KEY`, `CRON_SECRET`, Gmail keys...).
3. GitHub -> Settings -> Secrets -> Actions: `DATABASE_URL`, `SECRET_KEY`, `BACKEND_URL`, `CRON_SECRET`.
4. Actions -> **Database migrate** -> Run workflow (once).
5. Open `https://<your-backend>.vercel.app/api/health/`.

`Outreach scheduler` calls `/api/outreach/cron/tick/` every 5 minutes: it sends at most one due email,
batches drafts, and checks replies. `OUTREACH_DRY_RUN=True` keeps everything from really sending.

## CI/CD (GitHub Actions -> Vercel)
`.github/workflows/deploy.yml` runs on every push to `main`: tests -> database migrations -> Vercel production
deploy -> live health check. It needs the secrets `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`
(plus optional `DATABASE_URL`, `SECRET_KEY`, `BACKEND_URL`). Without them only the tests run.
Use it instead of Vercel's own Git auto-deploy (add `"git": { "deploymentEnabled": false }` to `vercel.json`).

## Local
```
pip install -r requirements-dev.txt
python -m pytest
```
