# Setting up the offline edition on a new machine

This machine will run **only** the local edition: local LLM, local speech
recognition, local voice. No API key, no network call at runtime.

There is a copy-paste prompt for an AI agent at the bottom. The steps below are
the same thing written for a person.

---

## 1. What to copy, and what to leave behind

Copy the whole project folder, then **delete one file**:

```powershell
Remove-Item ngnix-astra-brain-engine\.env
```

That file holds live Azure OpenAI, Azure Speech and Deepgram keys. The offline
edition does not use them and must not carry them onto another laptop. Everything
the offline edition needs is in `local\.env.local`, which contains no secrets.

Worth copying if it came along — it saves a download:

| Path | Size | If missing |
| --- | --- | --- |
| `ngnix-astra-brain-engine\models\piper\` | ~180 MB | re-downloaded by `get_models.ps1` |

Do **not** bother copying, they are rebuilt or re-fetched automatically:

- `corpus.local.json` and `corpus.index.json` — rebuilt on first run
- `__pycache__`, `.venv` — machine-specific
- `%USERPROFILE%\.cache\huggingface` — the Whisper model, re-downloaded (~470 MB
  for `small`). Copy it if you want a fully offline first boot.

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

> I have copied a project folder called `tars` onto this machine. I want to run
> **only its offline/local edition** — local LLM, local speech recognition, local
> voice, no cloud keys and no network calls at runtime. The cloud edition is not
> wanted here.
>
> Read `local/README.md` and `SETUP-LOCAL.md` in the project first; they describe
> the architecture and the setup steps, including the hardware tiers.
>
> Please:
> 1. Delete `ngnix-astra-brain-engine/.env` if it exists — it holds live cloud
>    keys that must not be on this machine. The offline profile is
>    `local/.env.local` and contains no secrets.
> 2. Check the prerequisites and install whatever is missing: Python 3.11+,
>    Ollama, and espeak-ng. Note that the Windows espeak-ng installer does not add
>    itself to PATH; the project already searches `C:\Program Files\eSpeak NG`, so
>    do not modify PATH for it.
> 3. Install both requirements files:
>    `ngnix-astra-brain-engine/requirements.txt` and
>    `local/requirements-local.txt`.
> 4. Tell me this machine's RAM, CPU and whether it has a discrete GPU with how
>    much VRAM, then pick the matching tier (`pi`, `beta` or `gpu`) and run
>    `local/get_models.ps1 -Tier <tier>`. If it is not `beta`, update
>    `OLLAMA_LLM_MODEL` and `WHISPER_MODEL` in `local/.env.local` to match, and on
>    a GPU also set `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`,
>    `PROMPT_STYLE=full` and `RAG_TOP_K=4`.
> 5. Run `python local/verify_local.py` and report the result. All 82 checks
>    should pass.
> 6. Start it with `local/run-local.ps1`, then confirm
>    `curl http://127.0.0.1:8000/health` reports `"offline": true`,
>    `"provider":"ollama"`, `"tts_provider":"local"` and 11 languages in
>    `languages_spoken`. Tell me if any of those are wrong.
> 7. Run `python local/verify_local_speech.py` to prove the speech round trip
>    works, and `python local/bench_speech.py` plus `python local/bench_local.py`
>    to measure this machine. Report the numbers so I can compare them with the
>    reference laptop (~60 prompt tokens/sec, 13–21 generated tokens/sec,
>    Whisper `small` at about 1x realtime).
>
> Do not add cloud credentials, do not switch the provider to Azure, and do not
> deploy anything. If a turn takes longer than about 30 seconds, tell me and
> recommend a smaller tier rather than silently changing models.
>
> Known limitation to expect, not a bug: Odia can be spoken but not transcribed,
> because Whisper has no Odia model.
