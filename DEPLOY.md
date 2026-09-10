# Deploying the cloud edition

## The one thing to know first

**Vercel cannot host the backend.** Vercel functions are request/response and do
not support WebSockets. Every conversation in this project runs over
`/ws/session` — a persistent WebSocket carrying JSON events and streamed binary
PCM audio in both directions. There is no way to express that as a serverless
function.

So the deployment splits in two:

| Part | Where | Why |
| --- | --- | --- |
| `ngnix-astra-studio-demo` | **Vercel** | Static files, no build step. Ideal fit. |
| `ngnix-astra-brain-engine` | **Azure Container Apps**, App Service, Fly.io, Render… | Needs a live process and WebSocket proxying |

Azure Container Apps is the natural choice here because the Azure OpenAI, Azure
Speech and embedding resources are already in Azure — keeping the backend in the
same region removes a network hop from every turn.

A `Dockerfile` and `docker-entrypoint.sh` are included for that. The entrypoint
builds the RAG corpus on first boot, because the server refuses to start without
one and it cannot be baked into the image without putting live credentials in a
layer.

## Frontend to Vercel

```powershell
cd ngnix-astra-studio-demo
vercel login
vercel                 # preview deployment
vercel --prod          # production
```

`vercel.json` sets `outputDirectory: "."` with no build command, so the files are
served exactly as they are locally.

### Configuration, without committing secrets

`dev-config.mjs` hard-codes `ws://127.0.0.1:8000` and the session token for local
work. On Vercel that is replaced at runtime by `/api/config`, which reads
environment variables:

```powershell
vercel env add BRAIN_WS_URL          # wss://your-backend.azurecontainerapps.io/ws/session
vercel env add BRAIN_SESSION_TOKEN   # must match the backend's value
vercel env add DEMO_FALLBACK         # 'false' to disable scripted replies
```

`app.mjs` fetches `/api/config` before its first connection attempt. Locally that
endpoint does not exist, the fetch fails, and `dev-config.mjs` stays in force — so
the local no-build workflow is unchanged.

With no variables set, `/api/config` reports `configured: false` and the site runs
in **scripted demo mode**: the whole UI works, including the face and the language
picker, with canned replies. That is a legitimate thing to ship on its own if the
backend is not ready.

## ⚠ Before you make it public

These are not theoretical. Read them.

1. **The session token is visible to every visitor.** Moving it to
   `/api/config` keeps it out of git, but the browser still receives it — it has
   to, in order to authenticate the socket. Anyone who opens the page can read it
   from the network tab and then spend your Azure OpenAI and Deepgram quota.

   Fix it one of two ways:
   - **Quick:** turn on Vercel **Deployment Protection** (password or SSO) so
     only people you invite can load the page at all.
   - **Proper:** replace `/api/config` with an authenticated endpoint that mints a
     short-lived per-session token, and have the backend check expiry. This is
     the item already flagged in the main README's security notes.

2. **Use a different token in production than in `dev-config.mjs`.** That file is
   static, so Vercel uploads and serves it — including the
   `tars-local-dev-only-…` token it contains. That is harmless *only* because it
   points at `127.0.0.1` and because the deployed backend uses a different
   secret. Generate a fresh one:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Put it in the backend's `BRAIN_SESSION_TOKEN` and in Vercel's, and never reuse
   the development value.

3. **`BRAIN_ALLOWED_ORIGINS` must include the Vercel origin**, or the backend
   will reject the socket. Wildcards are rejected by design, so list it exactly:

   ```ini
   BRAIN_ALLOWED_ORIGINS=https://your-project.vercel.app
   ```

   Add preview domains too if you use preview deployments.

4. **`BRAIN_WS_URL` must be `wss://`, not `ws://`.** An HTTPS page cannot open an
   insecure WebSocket; the browser blocks it as mixed content. `/api/config`
   refuses a non-local `ws://` URL and returns a warning rather than letting it
   fail silently in the console.

5. **The microphone needs a secure context.** HTTPS satisfies this, so Vercel is
   fine, but a plain-HTTP backend host is not.

6. **Keep `RAG_ALLOW_ANY_SOURCE=false` and `RAG_CITATION_MODE=strict`** for
   anything user-facing, and replace the 12-chunk demo corpus with reviewed
   documents first. `seed_corpus.jsonl` is explicitly labelled demo content.

7. **Microphone audio leaves the user's machine** — it is streamed to Deepgram or
   Azure Speech. Say so in a privacy notice.

## Backend to Azure Container Apps

```powershell
cd ngnix-astra-brain-engine

az containerapp up `
  --name tars-brain `
  --resource-group tars-rg `
  --location centralindia `
  --source . `
  --ingress external `
  --target-port 8000 `
  --env-vars `
    AZURE_OPENAI_ENDPOINT=... `
    AZURE_OPENAI_KEY=secretref:openai-key `
    AZURE_SPEECH_KEY=secretref:speech-key `
    DEEPGRAM_API_KEY=secretref:deepgram-key `
    BRAIN_SESSION_TOKEN=secretref:session-token `
    BRAIN_ALLOWED_ORIGINS=https://your-project.vercel.app
```

Use `secretref:` for every key rather than putting values on the command line,
where they land in your shell history and in the deployment record.

Container Apps proxies WebSockets without extra configuration. Set
`--min-replicas 1`: scaling to zero means the first visitor waits for a cold
start, and an in-flight WebSocket cannot survive a scale-down.

Verify it before pointing the frontend at it:

```powershell
curl https://tars-brain.<region>.azurecontainerapps.io/health
```

`/health` should report `"provider":"azure"` and `"corpus_loaded":true`.

## The local edition does not deploy

`local/` exists to run on the machine in front of the user — a laptop or a
Raspberry Pi. It has no cloud story and does not belong on Vercel: the models are
gigabytes, the LLM needs a persistent process, and the entire point is that no
audio leaves the device. See [`local/README.md`](local/README.md).
