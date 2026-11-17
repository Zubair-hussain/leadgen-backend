# LeadGen scheduler (Cloudflare Worker)

Calls the backend on time: `tick` every 5 minutes, `autopilot` every 30 minutes. Free plan is enough.

```bash
cd cloudflare/scheduler
npx wrangler login
npx wrangler secret put CRON_SECRET     # same value as CRON_SECRET in Vercel
npx wrangler deploy
npx wrangler tail                       # live log of every call
```

The GitHub `cron.yml` stays as a backup. Calling twice is safe: sending is protected by a database lock
and a random 3-8 minute gap, so two callers never double-send.
