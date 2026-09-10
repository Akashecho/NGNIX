/**
 * Local development configuration.
 *
 * SECURITY NOTE: `token` is a shared secret embedded in client-side JavaScript.
 * Anyone who can load this page can read it. That is acceptable only for a
 * localhost demo. For any networked deployment, replace this with a short-lived
 * per-session token minted by an authenticated server endpoint, and serve the
 * page over HTTPS/WSS.
 *
 * DEPLOYMENT: this file is static, so it IS uploaded and served by Vercel. That
 * is only safe because of one rule:
 *
 *   the deployed backend's BRAIN_SESSION_TOKEN must NOT be this value.
 *
 * Set a different, random BRAIN_SESSION_TOKEN on the backend and give the same
 * value to Vercel as an environment variable. `/api/config` then overrides both
 * fields below at runtime, and the pair here stays what it says it is: a
 * localhost-only default that authenticates nothing reachable from the internet.
 * See DEPLOY.md.
 *
 * The value must match BRAIN_SESSION_TOKEN in ngnix-astra-brain-engine/.env
 * and be at least 24 characters.
 */
export const backend = {
  url: 'ws://127.0.0.1:8000/ws/session',
  token: 'tars-local-dev-only-2f8b91c47ae35d06',
  // When true the app falls back to the old scripted replies if the backend is
  // unreachable, so the UI can still be demonstrated offline.
  demoFallback: true,
};
