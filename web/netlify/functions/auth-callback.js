// TikTok OAuth callback.
// TikTok redirects the user's browser here (https://<your-site>.netlify.app/auth/callback?code=...&state=...)
// after they approve the login. This function exchanges that one-time "code" for an
// access_token + refresh_token by calling TikTok's token endpoint, then shows them on screen
// so you can copy them into scripts/.env on your computer.
//
// Required Netlify environment variables (set these in Netlify site settings -> Environment variables,
// never commit them to git):
//   TIKTOK_CLIENT_KEY
//   TIKTOK_CLIENT_SECRET
//   TIKTOK_REDIRECT_URI   (must exactly match what you registered in TikTok for Developers,
//                          e.g. https://deft-hummingbird-07156c.netlify.app/auth/callback)

export default async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");
  const errorDescription = url.searchParams.get("error_description");

  if (error) {
    return htmlResponse(`
      <h1>TikTok login failed</h1>
      <p><b>${escapeHtml(error)}</b>: ${escapeHtml(errorDescription || "")}</p>
    `);
  }

  if (!code) {
    return htmlResponse(`<h1>Missing "code" parameter</h1><p>This page is only meant to be opened via a TikTok login redirect.</p>`);
  }

  const clientKey = process.env.TIKTOK_CLIENT_KEY;
  const clientSecret = process.env.TIKTOK_CLIENT_SECRET;
  const redirectUri = process.env.TIKTOK_REDIRECT_URI;

  if (!clientKey || !clientSecret || !redirectUri) {
    return htmlResponse(`
      <h1>Server misconfigured</h1>
      <p>Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI environment variables
      in the Netlify site settings.</p>
    `);
  }

  try {
    const tokenRes = await fetch("https://open.tiktokapis.com/v2/oauth/token/", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
      },
      body: new URLSearchParams({
        client_key: clientKey,
        client_secret: clientSecret,
        code,
        grant_type: "authorization_code",
        redirect_uri: redirectUri,
      }),
    });

    const data = await tokenRes.json();

    if (!tokenRes.ok || data.error) {
      return htmlResponse(`
        <h1>Token exchange failed</h1>
        <pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>
      `);
    }

    // NOTE: this displays the tokens once, in your own browser, so you can copy them into
    // scripts/.env on your computer. They are never stored anywhere by this function.
    return htmlResponse(`
      <h1>TikTok login successful</h1>
      <p>Copy these two values into <code>scripts/.env</code> (see <code>scripts/.env.example</code>), then close this tab.</p>
      <p><b>TIKTOK_ACCESS_TOKEN</b></p>
      <pre>${escapeHtml(data.access_token || "")}</pre>
      <p><b>TIKTOK_REFRESH_TOKEN</b></p>
      <pre>${escapeHtml(data.refresh_token || "")}</pre>
      <p>open_id: ${escapeHtml(data.open_id || "")} &nbsp; expires_in: ${escapeHtml(String(data.expires_in || ""))}s</p>
    `);
  } catch (e) {
    return htmlResponse(`<h1>Unexpected error</h1><pre>${escapeHtml(String(e))}</pre>`);
  }
};

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function htmlResponse(bodyHtml) {
  return new Response(
    `<!doctype html><html><head><meta charset="utf-8"><title>ReactionFlow - TikTok login</title>
     <style>body{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;word-break:break-all}
     pre{background:#f4f4f4;padding:12px;border-radius:8px;white-space:pre-wrap}</style></head>
     <body>${bodyHtml}</body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
