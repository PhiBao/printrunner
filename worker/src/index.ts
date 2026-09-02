export default {
  async scheduled(event: ScheduledEvent, env: any, ctx: ExecutionContext) {
    const url = `https://api.github.com/repos/PhiBao/printrunner/actions/workflows/printrunner-cycle/dispatches`;
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
    const html = `<!doctype html><title>PrintRunner Cron</title><style>body{font-family:monospace;max-width:720px;margin:40px auto;padding:0 16px}</style><h1>PrintRunner Cron Worker</h1><p>Runs every 20m 13-20 UTC (market hours) and triggers <a href="https://github.com/PhiBao/printrunner/actions">PhiBao/printrunner</a> via GitHub API.</p><p>Status: ok — ${new Date().toISOString()}</p><p><a href="https://phibao.github.io/printrunner/">Dashboard</a> · <a href="https://phibao.github.io/printrunner/PrintRunner.pdf">Slides</a> · <a href="https://phibao.github.io/printrunner/video/PrintRunner.mp4">Video</a></p>`;
    return new Response(html, { headers: { "content-type": "text/html" } });
  },
} as ExportedHandler<any>;
