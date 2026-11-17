// Cloudflare Worker: the reliable clock for LeadGen.
// GitHub Actions cron ran only ~4 times a day on this repo, so emails were never sent inside the send
// window. Cloudflare Cron Triggers fire on time, for free.
//
//   every 5 minutes  -> POST /api/outreach/cron/tick/       (send one due email, batch drafts, replies, report)
//   every 30 minutes -> POST /api/outreach/cron/autopilot/  (find leads once a day, write drafts)
//
// Variables (wrangler.toml / `wrangler secret put CRON_SECRET`): BACKEND_URL, CRON_SECRET.

export default {
  async scheduled(event, env, ctx) {
    const path = event.cron === '*/30 * * * *' ? 'autopilot' : 'tick';
    ctx.waitUntil(call(env, path));
  },

  // Opening the worker URL shows nothing useful and nothing secret.
  async fetch() {
    return new Response('LeadGen scheduler: nothing to see here.', { status: 404 });
  },
};

async function call(env, path) {
  const started = Date.now();
  try {
    const response = await fetch(`${env.BACKEND_URL}/api/outreach/cron/${path}/`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${env.CRON_SECRET}` },
      signal: AbortSignal.timeout(100000),
    });
    const text = (await response.text()).slice(0, 300);
    console.log(JSON.stringify({ path, status: response.status, ms: Date.now() - started, body: text }));
  } catch (error) {
    console.log(JSON.stringify({ path, error: String(error), ms: Date.now() - started }));
  }
}
