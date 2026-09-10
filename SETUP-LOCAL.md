# Setting up the offline edition on a new machine

This machine will run **only** the local edition: local LLM, local speech
recognition, local voice. No API key, no network call at runtime.

There is a copy-paste prompt for an AI agent at the bottom. The steps below are
the same thing written for a person.

---

## 1. Get the code

```powershell
git clone https://github.com/Akashecho/NGNIX.git tars
cd tars
```

A clone deliberately does **not** include several things. All of them are
regenerated or downloaded by the steps below, so this is expected, not a problem:

| Not in the clone | Why | Restored by |
| --- | --- | --- |
| `ngnix-astra-brain-engine\.env` | holds live cloud keys | not needed; the offline edition uses `local\.env.local`, which **is** in the clone |
| `models\piper\*.onnx` | 181 MB of weights, CC BY-NC-SA | `get_models.ps1` |
| `corpus.local.json` | embeds the corpus, rebuilt per model | `run-local.ps1`, automatically |
| Whisper model | lives in the user cache, not the repo | first use, or `get_models.ps1` |
| Ollama models | Ollama's own store | `get_models.ps1` |

If instead you **copied the folder** from another machine rather than cloning,
delete the cloud keys before doing anything else — they should not travel:

```powershell
Remove-Item ngnix-astra-brain-engine\.env -ErrorAction SilentlyContinue
```

Copying does bring the Piper voices and the corpus with it, so step 4 has less to
download.

## 2. Prerequisites

Three things, none of which are Python packages:

```powershell
# Python 3.11 or newer. 3.14 is fine — every wheel this project needs exists for it.
python --version

# Ollama: runs the local language model.
winget install Ollama.Ollama

# espeak-ng: the only local voice for Tamil, Telugu, Bengali, Marathi,
# Gujarati, Kannada, Punjabi and Odia.
winget install eSpeak-NG.eSpeak-NG
```

The Windows espeak-ng installer does **not** add itself to `PATH`. That is
expected and handled: the engine also looks in `C:\Program Files\eSpeak NG`.

On Raspberry Pi OS or Debian, the last one is `sudo apt install -y espeak-ng`.

## 3. Install the Python dependencies

```powershell
cd "<project folder>"
pip install -r ngnix-astra-brain-engine\requirements.txt
pip install -r local\requirements-local.txt
```

The second file adds `faster-whisper`, `piper-tts` and `onnxruntime` — the offline
speech engines. Roughly 150 MB of wheels, nothing compiles from source.

## 4. Pick a tier and download the models

Choose by the new machine's specs:

| Tier | For | LLM | Whisper | Disk |
| --- | --- | --- | --- | --- |
| `pi` | Raspberry Pi 5, or 8 GB RAM | `qwen2.5:1.5b-instruct` | `base` | ~1.4 GB |
| `beta` | 16 GB RAM laptop, no GPU | `qwen2.5:3b-instruct` | `small` | ~3.0 GB |
| `gpu` | 24 GB RAM + 8 GB VRAM | `qwen2.5:7b-instruct` | `large-v3-turbo` | ~6.5 GB |

```powershell
.\local\get_models.ps1 -Tier beta
```

This pulls the Ollama models, caches the Whisper model, and downloads the three
Piper voices. It needs the internet **once**; after this the stack runs with the
network unplugged.

If you chose a tier other than `beta`, set the two matching values in
`local\.env.local`:

```ini
OLLAMA_LLM_MODEL=qwen2.5:1.5b-instruct
WHISPER_MODEL=base
```

On the `gpu` tier also set, and raise the prompt budget since a GPU makes the
compact prompt unnecessary:

```ini
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
PROMPT_STYLE=full
RAG_TOP_K=4
```

## 5. Check, then run

```powershell
python local\verify_local.py      # 82 checks, no network needed
.\local\run-local.ps1
```

Open <http://localhost:8080>. Serve it over HTTP as the script does — opening
`index.html` as a `file://` URL sends an Origin the backend rejects, and the
microphone needs a secure context, which `http://localhost` satisfies.

The launcher prints what it found. A healthy start looks like this:

```
[ok]   profile: local\.env.local
[ok]   model present: qwen2.5:3b-instruct
[ok]   ears: faster-whisper 'small' (local)
[ok]   voice: Piper (en, hi, ml)
[ok]   voice: espeak-ng (ta, te, bn, mr, gu, kn, pa, or)
[ok]   corpus ready (ollama:nomic-embed-text -> corpus.local.json)
```

Confirm it is genuinely offline:

```powershell
curl http://127.0.0.1:8000/health
```

`"offline": true` is the claim to look for. It is only reported when the brain,
the ears and the voice are all local.

Stop everything with `.\stop.ps1`.

## 6. Prove the speech chain works

```powershell
python local\verify_local_speech.py       # Piper speaks, Whisper listens back
python local\bench_speech.py              # STT and TTS latency on this machine
python local\bench_local.py               # LLM prompt vs generation cost
```

`verify_local_speech.py` is the useful one: it synthesises a phrase locally,
resamples it to the microphone's 16 kHz, feeds it to the recogniser exactly as
the browser would, and compares the transcript. No microphone needed.

## What to expect, and what not to

**The first turn is slow.** Whisper loads, Piper loads its ONNX session, and the
model's prompt cache is cold. On a 16 GB CPU-only laptop that is ~25 s. Later
turns in the same conversation are ~3 s, or ~7 s with speech, because the prompt
prefix is then cached.

**Latency is set by prompt length, not model size.** Measured at ~60 prompt
tokens per second on a CPU-only laptop against 13–21 generated tokens per second.
That is why the offline profile ships a compact persona and `RAG_TOP_K=2`; it took
a cold turn from 62.8 s to 25.0 s and made the answer *better*, because the
shorter prompt came back grounded with a citation where the long one did not.

**Odia can be spoken but not heard.** Whisper has no Odia model at all. Those
turns stay in text mode. Everything else works in all eleven languages.

**Hands-free works and costs nothing here.** The wake word is matched against the
transcript Whisper already produces, so there is no extra model and no audio
leaves the machine. Turn it on in Voice & listening, or set
`WAKE_WORD_REQUIRED=true` in `local\.env.local`.

## If something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| `faster-whisper is missing` | `pip install -r local\requirements-local.txt` |
| Only English, Hindi and Malayalam have a voice | espeak-ng not installed |
| `Ollama is not installed` | `winget install Ollama.Ollama`, then reopen the shell |
| `corpus was built with '...'` then a rebuild | Normal after changing `OLLAMA_EMBED_MODEL` |
| `BRAIN_ENV_FILE points at ... which does not exist` | Run the launcher from the project root |
| Turns take 60 s or more | Drop a tier: smaller `OLLAMA_LLM_MODEL`, `WHISPER_MODEL=base` |
| Microphone button disabled | Page not served over `http://localhost` or HTTPS |
| Backend will not start, no reason given | Run it in the foreground to see the error: `cd ngnix-astra-brain-engine; $env:BRAIN_ENV_FILE="..\local\.env.local"; python -m uvicorn brain.app:app` |

A stray `AZURE_OPENAI_API_KEY` or `AZURE_SPEECH_KEY` in the machine's environment
cannot leak into this edition: real environment variables outrank the profile
file, so `Settings.load()` deliberately discards cloud credentials whenever the
brain, ears and voice are all local. `/health` reporting `"offline": true` is the
proof.

---

## Copy-paste prompt for an AI agent on the new machine

> Clone `https://github.com/Akashecho/NGNIX.git` and set it up to run **only its
> offline/local edition** — local LLM, local speech recognition, local voice, no
> cloud API keys and no network calls at runtime. The cloud/Azure edition is not
> wanted on this machine; do not configure it.
>
> Read `SETUP-LOCAL.md` and `local/README.md` first. They describe the
> architecture, the hardware tiers and the known limitations. Follow
> `SETUP-LOCAL.md`; the notes below are the parts people get wrong.
>
> 1. The clone will not contain `ngnix-astra-brain-engine/.env`, the Piper `.onnx`
>    voices, `corpus.local.json`, or any model weights. That is intentional. Do
>    not try to recreate `.env` and do not add cloud credentials anywhere. The
>    offline profile is `local/.env.local` and it is in the clone.
> 2. Install prerequisites that are missing: Python 3.11+, Ollama
>    (`winget install Ollama.Ollama`), and espeak-ng
>    (`winget install eSpeak-NG.eSpeak-NG`, or `sudo apt install -y espeak-ng`).
>    The Windows espeak-ng installer does not add itself to PATH — that is
>    expected and already handled in `brain/tts_local.py`, so do not edit PATH.
> 3. Install both requirement files:
>    `pip install -r ngnix-astra-brain-engine/requirements.txt` then
>    `pip install -r local/requirements-local.txt`.
> 4. Report this machine's CPU, RAM and whether it has a discrete GPU with how
>    much VRAM. Then pick the tier from the table in `SETUP-LOCAL.md` (`pi`,
>    `beta` or `gpu`) and run `./local/get_models.ps1 -Tier <tier>`. This needs
>    the internet once. If the tier is not `beta`, update `OLLAMA_LLM_MODEL` and
>    `WHISPER_MODEL` in `local/.env.local` to match. On a GPU also set
>    `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`, `PROMPT_STYLE=full`
>    and `RAG_TOP_K=4`.
> 5. Run `python local/verify_local.py`. All 103 checks must pass. If any fail,
>    show me the failures rather than working around them.
> 6. Start it with `./local/run-local.ps1`. It will build the RAG corpus on first
>    run, which takes a minute. Then confirm `curl http://127.0.0.1:8000/health`
>    reports `"offline": true`, `"provider": "ollama"`, `"tts_provider": "local"`,
>    11 entries in `languages_spoken`, and `indic_nlp.available: true`. Tell me if
>    any of those are wrong.
> 7. Run `python local/verify_local_speech.py` to prove the speech round trip
>    (local Piper out, local Whisper back in). Then run
>    `python local/bench_speech.py` and `python local/bench_local.py` and report
>    the numbers, so I can compare against the reference laptop: about 60 prompt
>    tokens/sec, 13–21 generated tokens/sec, Whisper `small` at roughly 1x
>    realtime, Piper 16–24x realtime.
>
> Constraints: do not add cloud credentials, do not switch `LLM_PROVIDER` to
> azure, do not deploy anything, and do not commit model weights (they are
> CC BY-NC-SA and deliberately gitignored). If a turn takes longer than about 30
> seconds, tell me and recommend a smaller tier rather than silently swapping
> models.
>
> Expected behaviour, not bugs: Odia can be spoken but not transcribed, because
> Whisper has no Odia model. Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada,
> Punjabi and Odia are spoken by espeak-ng and sound robotic; only English, Hindi
> and Malayalam have neural Piper voices. The first turn after startup is slow
> because Whisper loads, Piper loads and the prompt cache is cold.
