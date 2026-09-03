export default {
  async scheduled(event: ScheduledEvent, env: any, ctx: ExecutionContext) {
    // NOTE: the {workflow} segment must be the workflow FILE name (or numeric
    // id) — the display `name:` does NOT resolve and returns 404.
    const url = `https://api.github.com/repos/PhiBao/printrunner/actions/workflows/cycle.yml/dispatches`;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "Authorization": `token ${env.GITHUB_TOKEN}`,
          "Accept": "application/vnd.github.v3+json",
          "User-Agent": "printrunner-cron",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      });
      console.log(`[printrunner-cron] GitHub dispatch ${res.status} at ${new Date().toISOString()} cron: ${event.cron}`);
      if (!res.ok) {
        const txt = await res.text();
        console.error(`GitHub dispatch failed: ${res.status} ${txt}`);
      }
    } catch (e) {
      console.error("cron failed", e);
    }
  },
  async fetch(request: Request, env: any): Promise<Response> {
    const html = `<!doctype html><title>PrintRunner Cron</title><style>body{font-family:monospace;max-width:720px;margin:40px auto;padding:0 16px}</style><h1>PrintRunner Cron Worker</h1><p>Runs every 20m 13-20 UTC (market hours) and triggers <a href="https://github.com/PhiBao/printrunner/actions">PhiBao/printrunner</a> via GitHub API.</p><p>Status: ok — ${new Date().toISOString()}</p><p><a href="https://printrunner.vercel.app/">Dashboard</a> · <a href="https://printrunner.vercel.app/PrintRunner.pdf">Slides</a> · <a href="https://printrunner.vercel.app/video/PrintRunner.mp4">Video</a></p>`;
    return new Response(html, { headers: { "content-type": "text/html" } });
  },
} as ExportedHandler<any>;
