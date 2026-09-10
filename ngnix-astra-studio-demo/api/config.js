/**
 * Runtime configuration for the browser client.
 *
 * The backend URL and session token are read from Vercel environment variables
 * rather than committed in dev-config.mjs, so no secret lives in the repository
 * or in the static bundle.
 *
 * ⚠ This does NOT make the token secret. It is still handed to every visitor
 * who loads the page, exactly as the embedded version was — it only stops the
 * value being in git. A public deployment therefore lets anyone spend the Azure
 * OpenAI and Deepgram quota behind it. Before exposing this publicly, either:
 *
 *   1. turn on Vercel's Deployment Protection (password / SSO), or
 *   2. replace this with an authenticated endpoint that mints a short-lived,
 *      per-session token, and have the backend verify expiry.
 *
 * Environment variables (set in the Vercel dashboard, or `vercel env add`):
 *
 *   BRAIN_WS_URL         wss://your-backend.example.com/ws/session
 *   BRAIN_SESSION_TOKEN  must match the backend's BRAIN_SESSION_TOKEN
 *   DEMO_FALLBACK        'false' to disable scripted replies when offline
 *
 * With no variables set the response reports `configured: false` and the client
 * stays in scripted demo mode, which is the sensible default for a UI-only
 * deployment with no backend behind it.
 */
export default function handler(request, response) {
  const url = (process.env.BRAIN_WS_URL || '').trim();
  const token = (process.env.BRAIN_SESSION_TOKEN || '').trim();

  // A page served over HTTPS cannot open an insecure ws:// socket — browsers
  // block it as mixed content — so reject that here with an explanation rather
  // than letting it fail silently in the console.
  const insecure = url.startsWith('ws://') && !url.includes('127.0.0.1') && !url.includes('localhost');

  response.setHeader('Cache-Control', 'no-store, max-age=0');
  response.status(200).json({
    url: insecure ? '' : url,
    token: insecure ? '' : token,
    configured: Boolean(url && token) && !insecure,
    demoFallback: process.env.DEMO_FALLBACK !== 'false',
    ...(insecure
      ? {warning: 'BRAIN_WS_URL must use wss:// — an HTTPS page cannot open an insecure WebSocket.'}
      : {}),
    ...(url && !token ? {warning: 'BRAIN_WS_URL is set but BRAIN_SESSION_TOKEN is not.'} : {}),
  });
}
