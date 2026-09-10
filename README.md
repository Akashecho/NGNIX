# TARS — Astra cooperative assistant

A voice-first assistant that explains cooperative topics (PACS, crop insurance,
grievances, loan literacy) in plain language, grounded in a retrieved document
corpus.

## Two editions

The same backend runs either way; the edition is chosen by configuration, not by
a fork. Nothing needs editing to switch.

| | Cloud edition | Local edition |
| --- | --- | --- |
| Start | `.\run.ps1` | `.\local\run-local.ps1` |
| Brain | Azure OpenAI `gpt-4.1-nano` | Ollama, `qwen2.5:3b-instruct` |
| Embeddings | Azure `text-embedding-3-large` | Ollama `nomic-embed-text` |
| Ears | Deepgram nova-3 + Azure Speech | faster-whisper, on the CPU |
| Voice | Azure Speech neural TTS | Piper + espeak-ng, on the CPU |
| Network | required | **none at all** |
| Profile | `ngnix-astra-brain-engine\.env` | `local\.env.local` |
| RAG index | `corpus.index.json` | `corpus.local.json` |
| Turn latency | ~4 s | ~3–7 s warm, ~25 s cold (measured, CPU-only laptop) |

The local edition is documented in full, with hardware tiers and measured
latency, in **[`local/README.md`](local/README.md)**. It exists to run on
Raspberry-Pi-class hardware and takes its persona, humour dials, robotic voice
and Piper/espeak engine split from the TARS-AI reference project.

`BRAIN_ENV_FILE` selects the profile, so the two editions never overwrite each
other's configuration and keep separate RAG indexes — a 3072-dimension Azure
embedding and a 768-dimension local one are not interchangeable.

The rest of this document describes the cloud edition.

## Cloud edition

Azure hosts the OpenAI model that does the reasoning and response generation.
TARS supplies everything around it: session state, prompt and persona, retrieval,
tool routing, streaming logic and robot behaviour.

```
microphone audio
  → Deepgram nova-3 (speech to text)          ← Azure Speech STT for language gaps / failover
  → TARS session: persona, RAG, tools, policy
  → Azure OpenAI  (default deployment gpt-4.1-nano)
  → Azure Speech neural TTS
  → streamed audio back to the client
```

## Hands-free: "hey TARS"

With hands-free on, the microphone stays open and TARS answers only when spoken
to by name. Everything else you say is heard, ignored and never sent to the
model.

```
"What is a PACS?"              → heard, ignored, no turn
"Hey TARS, what is a PACS?"    → wake phrase stripped, answered in one turn
"Hey TARS."                    → acknowledged ("Go ahead."), waits for the question
```

Turn it on in **Voice & listening → Hands-free**, or make it the default with
`WAKE_WORD_REQUIRED=true`. `WAKE_WORD` changes the phrase; the last word is
treated as the name and anything before it as an optional attention word, so
`WAKE_WORD=ok astra` works without touching code.

It is a **transcript filter, not a second audio pipeline**. The session already
streams audio and already produces transcripts, so the wake word sits between
"we heard words" and "we answer them". That means no extra model, no extra
download, nothing new listening, and it works in all eleven languages because it
rides on whichever ASR engine is already configured.

The hard part is not matching `hey tars` — it is matching what recognition
actually returns. Deepgram and Whisper write it back as "hey tarts", "hey stars",
"Hey, tars," and worse, because it is two short words with no context and TARS is
in no general vocabulary. So `brain/wake.py` matches on phonetic shape and edit
distance over the first three words only, and treats ambiguous homophones
differently from the name itself:

- `hey stars, what is a PACS` → wakes. An attention word preceded it.
- `the stars are bright tonight` → does not. No attention word, and "stars" is an
  ordinary English word.
- `TARS, help me` → wakes. The actual name needs no attention word.

`python verify_wake.py` runs 68 checks over real recognition output in all eleven
scripts. `python verify_wake_live.py` drives a full spoken hands-free turn over
the WebSocket, synthesising its own audio locally so it needs no microphone.

One thing this required in the ASR layer: Deepgram closes a socket that receives
no audio for about ten seconds, and hands-free hits that on every reply because
the microphone is muted for the whole of playback. `DeepgramInput` now sends
`KeepAlive` while idle. Without it the ears died after the first answer and the
next wake word was never heard.

## The face

The sidebar shows an animated TARS face on a canvas: two eye panels over a voice
bar, following the markup it replaced (`▰ ▰` over `━`). TARS has no face in the
film — it is a rectangular slab with a bar display — so this keeps to that
vocabulary rather than adding cartoon eyes.

It is a readout of the pipeline, not decoration. Every state comes from an event
the backend already emits:

| State | Look |
| --- | --- |
| `ARMED` | half-lidded, panels pulsing alternately — waiting for the wake word |
| `LISTENING` | panels wide, bar tracking your voice level |
| `THINKING` | a bright band sweeping across both panels |
| `SPEAKING` | bar driven by the amplitude of the audio being played |
| `IDLE` | slow breathing, occasional blink |
| `OFFLINE` | nearly shut, unlit |

The bar follows the real PCM amplitude of TARS's own voice rather than a fake
animation, so the mouth matches what is being said. Colours come from CSS custom
properties, so it follows the light and dark themes. It runs at 30fps and stops
entirely when the tab is hidden, which matters on a Pi. It honours
`prefers-reduced-motion` by dropping the blink, sweep and breathing.

```powershell
cd ngnix-astra-studio-demo; node face_test.mjs      # 42 checks, no browser
```

## Layout

| Folder | What it is |
| --- | --- |
| `ngnix-astra-brain-engine` | FastAPI backend: WebSocket conversation service, ASR routing, RAG retrieval, persona and grounding policy, robot-body hub |
| `ngnix-astra-studio-demo` | Static browser frontend (no build step), including microphone capture and PCM playback |
| `local` | The offline edition: its `.env.local` profile, model downloader, launcher, benchmarks and tests. No backend code — see [`local/README.md`](local/README.md) |
| `ngnix` | Placeholder. Intended for the ESP32 body firmware; currently contains only an INFO.md |

## Quick start

Fill in the keys in `ngnix-astra-brain-engine/.env` first — at minimum
`AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_KEY`. Then:

```powershell
.\run.ps1
```

Then open <http://localhost:8080>.

The script is provider-aware: with `LLM_PROVIDER=azure` it does not start Ollama
or pull any models. It reports which speech engines are configured, rebuilds the
RAG corpus when the embedding model has changed, starts the backend on port 8000
and serves the frontend on port 8080.

Stop everything with `.\stop.ps1`.

### Manual start

```powershell
cd ngnix-astra-brain-engine
pip install -r requirements.txt
python -m brain.ingest seed_corpus.jsonl        # builds corpus.index.json
python -m uvicorn brain.app:app --port 8000

cd ..\ngnix-astra-studio-demo
python -m http.server 8080 --bind 127.0.0.1     # port must be 8080
```

Serve the frontend over HTTP — opening `index.html` as a `file://` URL sends an
Origin the backend rejects. The microphone additionally requires a secure
context, which `http://localhost` satisfies.

## The three services

| Layer | Service | Configuration |
| --- | --- | --- |
| Brain | Azure OpenAI, `gpt-4.1-nano` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_LLM_DEPLOYMENT` |
| Embeddings | Azure OpenAI, `text-embedding-3-large` | `AZURE_OPENAI_EMBED_DEPLOYMENT` |
| Ears | Deepgram nova-3 streaming | `DEEPGRAM_API_KEY`, `ASR_MODEL`, `ASR_LANGUAGE`, `ASR_KEYTERMS` |
| Ears (gaps + failover) | Azure Speech recognition | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` |
| Voice | Azure Speech neural TTS | `AZURE_SPEECH_KEY`, `TARS_VOICE` |
| Speech failover | Second Azure Speech region | `AZURE_SPEECH_KEY_FALLBACK`, `AZURE_SPEECH_REGION_FALLBACK` |

`AZURE_OPENAI_API_KEY` is accepted as an alias for `AZURE_OPENAI_KEY`, since that
is the name the Azure portal uses.

Each layer degrades independently. Without a Deepgram key all recognition goes to
Azure Speech; without an Azure Speech key the browser speaks replies with the Web
Speech API and Malayalam and Odia cannot be transcribed at all; without either,
the microphone is disabled and questions are typed. Only the brain is required.

When a second Azure Speech resource is configured, both recognition and synthesis
retry against it if the primary region refuses the request, which covers a
regional outage or a throttled key.

### Which engine hears which language

`brain/speech.py` holds the routing table. Deepgram nova-3 is the default for
every language it supports; Azure Speech covers the rest and stands in whenever
Deepgram fails to open a socket.

| Language | Deepgram nova-3 | Notes |
| --- | --- | --- |
| Hindi, English | `multi` | nova-3's code-switching mode, so Hinglish inside one sentence is transcribed correctly |
| Bengali, Gujarati, Kannada, Marathi, Punjabi, Tamil, Telugu | monolingual model | |
| **Malayalam, Odia** | **not supported** | always routed to Azure Speech |

Set `STT_PROVIDER` to change the preference:

- `auto` (default) — Deepgram first, Azure Speech for gaps and as failover.
- `deepgram` — pin to Deepgram; the language gaps still go to Azure Speech,
  because nova-3 cannot transcribe them at all.
- `azure` — Azure Speech first, Deepgram as failover.

Azure recognition uses the REST short-audio endpoint, which returns only final
results. That has one visible consequence: Deepgram turns show live interim
transcripts while you speak, Azure turns show text only once you pause. The
`audio_ready` event names the engine that actually started, and the UI reports it.

### Domain vocabulary

`ASR_KEYTERMS` feeds nova-3 keyterm prompts from `DOMAIN_KEYTERMS` in
`brain/speech.py`. This is not cosmetic: without it, spoken "पैक्स" comes back as
"Bags", the retrieval query misses the passage that answers it, and the grounding
policy correctly abstains on a question the corpus can answer. With the keyterms
the same audio transcribes as "PACS क्या हैं?" and the turn is grounded.

Set it to a comma-separated list to replace the defaults, or leave it empty to
disable prompting. Deepgram caps keyterms at 500 tokens and rejects the whole
request when that is exceeded, so `DeepgramInput` retries once without keyterms
rather than losing the microphone.

### Voices

One Azure male neural voice per language, listed in `AZURE_VOICES` in
`brain/speech.py` (`hi-IN-MadhurNeural`, `ta-IN-ValluvarNeural`,
`ml-IN-MidhunNeural`, and so on). A client may override it per turn with the
`voice` field. `TARS_VOICE` only sets the Hindi default.

Replies are synthesised in ordered chunks with at most two syntheses in flight,
coloured by the robot filter in `brain/voice.py`, and streamed to the client as
24 kHz mono PCM16: one JSON `audio` header followed by one binary frame per
chunk. The client acknowledges each finished utterance with `playback_done`.

### Only your voice is sent

`voice.mjs` gates the microphone before anything leaves the browser. On start it
measures the room for about a second without sending audio, takes the median RMS
as the noise floor, and then forwards only frames at or above four times that
floor. Details that matter in a real room:

- **Hysteresis.** The gate closes at two times the floor, not four, so it does
  not chatter on syllable boundaries.
- **Pre-roll.** The three frames before the gate opens are flushed with it, so
  the attack of the first word is not clipped.
- **Hangover.** It stays open for 700 ms after the level drops, so a pause
  mid-sentence does not end the turn.
- **Absolute limits.** The threshold is clamped to 0.006–0.09 RMS, so a silent
  room does not produce a hair-trigger gate and a loud one cannot raise the bar
  above ordinary speech.
- **Drift.** While the gate is shut the floor keeps tracking the room, so a fan
  switching on is absorbed. "Re-measure the room" in settings forces a fresh
  calibration.

The level meter still moves with everything the microphone hears; only the part
above the threshold is transmitted. The microphone button shows which state it is
in: `…` calibrating, `■` listening, and a faster pulse while your voice is
actually opening the gate.

Tune the behaviour by passing overrides for `GATE_DEFAULTS` when constructing
`MicrophoneStream`. `node voice_test.mjs` runs the gate against synthetic room
noise, speech, pauses and a changed room.

### Barge-in and self-interruption

Two separate mechanisms stop TARS talking over you:

- **Mute while speaking.** With "Mute the microphone while TARS speaks" on (the
  default), capture is paused for the duration of playback, so TARS cannot hear
  itself through your speakers and cut its own reply short. Turn it off to
  interrupt TARS mid-sentence; use headphones if you do.
- **Words, not noise.** Barge-in fires on the first interim transcript that
  contains actual words, not on the raw voice-activity event. A door slam raises
  a VAD event but produces no words, so it no longer cancels a reply. The server
  still emits `speech_started` for the UI.

The Azure recognition path has the same protection server-side: its energy gate
estimates the noise floor from the quiet frames it receives and sets the
threshold to 3.5 times it, clamped to 0.012–0.09, rather than using one fixed
level for every room.

## Running the brain locally

To swap only the reasoning model, keeping Azure Speech as the ears and the voice:

```ini
LLM_PROVIDER=ollama
OLLAMA_LLM_MODEL=qwen2.5:3b-instruct
OLLAMA_EMBED_MODEL=nomic-embed-text
```

The chat models (`qwen3.5`, `qwen2.5`, `gemma4`) do **not** expose an embeddings
endpoint; requesting embeddings from them returns HTTP 501, so a dedicated
embedding model is required.

For a stack with **no network at all** — local model, local speech recognition
and local synthesis — use the local edition instead of editing this file:

```powershell
.\local\run-local.ps1
```

See [`local/README.md`](local/README.md). It is not merely this switch with the
keys removed: it adds `brain/asr_local.py` and `brain/tts_local.py`, routes each
language to whichever local engine can serve it, shrinks the prompt because local
latency is prompt-bound, and discards inherited cloud credentials so "offline" is
enforced rather than assumed.

The corpus records which embedding model built it and the server refuses to start
on a mismatch, so re-ingest after any provider switch:

```powershell
python -m brain.ingest seed_corpus.jsonl
```

`run.ps1` detects the mismatch and rebuilds automatically. The two editions write
to different index files, so switching between them does not force a rebuild.

## Configuration

All settings live in `ngnix-astra-brain-engine/.env`. Beyond the service keys
above:

| Variable | Purpose |
| --- | --- |
| `BRAIN_SESSION_TOKEN` | Shared secret for the WebSocket auth frame, min 24 chars |
| `BRAIN_ALLOWED_ORIGINS` | Exact origin allowlist; wildcards are rejected |
| `RAG_CITATION_MODE` | `strict` or `lenient` — see below |
| `RAG_ALLOW_ANY_SOURCE` | Permits non-government corpus URLs |
| `WAKE_WORD` | Hands-free phrase, default `hey tars`. Last word is the name |
| `WAKE_WORD_REQUIRED` | Make hands-free the default for every session |
| `TOOLS_ENABLED` | Tool calling; safe to leave on with gpt-4.1-nano |

## Two answering modes

`ASSISTANT_MODE` decides what happens when a question falls outside the corpus.

| Mode | Behaviour |
| --- | --- |
| `open` | Answers anything, like a general assistant. Uses retrieved passages and cites them when they fit the question, otherwise answers from the model's own knowledge, and calls `web_search`, `fetch_page` or `get_weather` for current facts. |
| `grounded` | Advisory turns must be supported by retrieved passages. If retrieval finds nothing, or the answer carries no valid citation, the answer is replaced by a referral to the PACS secretary or Registrar. |

`open` is the default in `.env` because the seed corpus is 12 demo chunks, so
`grounded` refuses almost every real question. Measured on a mixed set of ten
general, current-affairs and cooperative questions: `open` produced a usable
answer for 10 of 10, `grounded` produced the referral for 5 of 10.

Both modes keep the rules that matter: the model may not invent a source, a legal
requirement, an eligibility decision, a deadline or a filing confirmation, must
not claim to have searched when it did not, and is told to send anyone asking
about their own money, land or eligibility to their PACS secretary or the
appropriate Registrar. A fabricated citation marker is rejected in every mode.

Use `grounded` with `RAG_CITATION_MODE=strict` and a real reviewed corpus for
anything user-facing.

### Citation modes, used by `grounded`

Questions are classified as *advisory* (almost everything) or *social* (greetings,
jokes, weather). Advisory answers are checked for `[S1]`-style markers that refer
to genuinely retrieved sources.

- `strict` — every sentence must carry a citation. Correct for production.
- `lenient` — the answer must contain at least one valid citation.
- `flag` — the answer is delivered either way, labelled "not citation-checked".

If the first answer omits the marker, the turn is retried once with the valid ids
spelled out before anything is withheld. The citation instruction in
`Session.system_prompt` is deliberately firm and carries worked examples: a softer
wording produced markers on 0 of 6 turns, the firm one on 6 of 6, and telling the
model that a missing marker is tolerated makes it stop citing altogether.

## Web search without an API key

`web_search` uses the DuckDuckGo Instant Answer API, which needs no key and
returns real abstracts for entities, definitions and people. It is weak for news
and prices. Scraping `html.duckduckgo.com` no longer works at all — it answers
every request with a JavaScript challenge — and Wikipedia rejects unattributed
crawlers by policy, so neither is used.

For dependable current-affairs search, set `SEARCH_API_KEY` to a Brave Search API
key and `web_search` switches to Brave, which returns ranked results with
snippets. Nothing else changes.

`fetch_page` reads any public HTTPS page so the model can follow a search result.
It resolves the host first and refuses loopback, private, link-local and cloud
metadata addresses, rejects non-HTTPS and non-443 URLs, follows at most one
redirect and revalidates the target, and caps the response at 500 KB.

## The seed corpus is demo content

`seed_corpus.jsonl` is **not** scraped government documentation. It is
general, deliberately non-committal explanatory text, labelled with
`demo.local` URLs and a `DEMO CONTENT - not an official source` jurisdiction so
it can never be mistaken for a real citation. `RAG_ALLOW_ANY_SOURCE=true` exists
only to permit those URLs.

For real use, replace it with reviewed official documents, set
`RAG_ALLOW_ANY_SOURCE=false`, and re-ingest. In that mode only `*.gov.in`,
`*.nic.in` and `rbi.org.in` HTTPS sources are accepted.

## Security notes

These matter before this leaves localhost:

- `dev-config.mjs` embeds the session token in client-side JavaScript, readable
  by anyone who loads the page. Replace it with short-lived per-session tokens
  minted by an authenticated endpoint.
- Serve over HTTPS/WSS in any networked deployment. The microphone requires a
  secure context in any case.
- Microphone audio now leaves the machine: it is streamed to Deepgram or Azure
  Speech while the user is listening. Say so in your privacy notice.
- Keep `RAG_ALLOW_ANY_SOURCE=false` and `RAG_CITATION_MODE=strict` in production.

## What is not connected

- **The robot body.** `brain/body.py` and the `/ws/body` endpoint implement
  gesture pairing and telemetry, but no ESP32 firmware exists in this repo.
- **Guided dialogs.** `brain/dialog.py` implements eligibility, grievance and
  financial-literacy flows over the WebSocket protocol, but the frontend has no
  UI for them yet.

## Testing

Pipeline wiring, without credentials or network access, from
`ngnix-astra-brain-engine`:

```powershell
python verify_pipeline.py
```

Runs the real provider, ASR and routing code against a stubbed HTTP transport and
a fake Deepgram socket. It checks the language routing table, the Deepgram socket
parameters, the keepalive that holds an idle socket open, the Azure OpenAI
deployment path, the Azure Speech SSML and output format, the WAV framing sent to
Azure recognition, and that a Deepgram outage falls over to Azure Speech. Exits
non-zero on any failure.

Wake word matching, without credentials or network access:

```powershell
python verify_wake.py
```

68 checks over the strings recognition actually returns for "hey TARS" in all
eleven scripts, plus the sentences most likely to fire it by accident.

Backend protocol against a running server:

```powershell
python smoke_test.py "What is a PACS?"
```

Opens a real session, asks a question and prints every protocol event, exiting
non-zero on failure. Set `SMOKE_LANGUAGE=hi` to test a Hindi turn, and
`SMOKE_SPEAK=1` to request Azure Speech TTS as well — that additionally checks
each `audio` header is followed by one binary frame of the advertised length, and
acknowledges playback the way the browser does.

The two live speech tests spend real Azure and Deepgram quota:

```powershell
python verify_speech_live.py       # TTS -> PCM -> both ASR engines, no server needed
python verify_voice_turn_live.py   # full spoken turn over the WebSocket protocol
```

`verify_speech_live.py` synthesises a phrase with Azure TTS, resamples it to the
microphone's 16 kHz, and feeds it to Deepgram nova-3 and to Azure recognition, so
the ears can be tested without a microphone. It also checks the fallback region.

`verify_voice_turn_live.py` drives `/ws/session` exactly as the browser does —
`audio_start`, binary PCM frames, transcript, answer, streamed reply audio — for
an English turn (expects Deepgram), a Hindi turn (expects Deepgram with
code-switching) and a Malayalam turn (expects Azure Speech). Only the browser's
own microphone capture and speaker playback are left untested.

A full spoken hands-free turn, against a running server:

```powershell
python verify_wake_live.py
```

Synthesises its own audio with the local Piper/espeak voices, so producing the
speech costs nothing, then streams it as the browser would and checks that
"what is a PACS" is ignored, "hey TARS, what is a PACS" is answered with the wake
phrase stripped, and "hey TARS" alone is acknowledged without calling the model.
It works against either edition; against the cloud edition the audio reaches
Deepgram, so it spends a little quota.

Browser client module, from `ngnix-astra-studio-demo`:

```powershell
node client_test.mjs "How do I raise a complaint?"
node voice_test.mjs                 # the calibrated noise gate, no browser needed
node face_test.mjs                  # the animated face, no browser needed
```

`client.mjs` uses no DOM APIs, so Node's global `WebSocket` runs exactly the code
the page loads. This is the fastest way to tell a UI bug apart from a backend bug.
`voice_test.mjs` runs the real capture worklet against synthetic audio. Only the
browser's own microphone and speakers are left untested.
