# TARS — local edition

Everything on one machine. No API key, no network call, no cloud bill.

```
microphone audio
  → faster-whisper          (speech to text, on this CPU)
  → TARS session: persona, RAG, policy   ← same code as the Azure edition
  → Ollama                  (local LLM + embeddings)
  → Piper / espeak-ng       (speech synthesis, on this CPU)
  → streamed 24 kHz PCM back to the client
```

## Why this is a folder, not a second copy of the project

There is **one** backend codebase. This folder holds the local edition's
*profile* — its environment, its models, its scripts and its tests — and the
backend selects the edition from `BRAIN_ENV_FILE`.

The alternative, forking `ngnix-astra-brain-engine` into a second directory,
would duplicate the session state machine, the RAG index, the grounding policy,
the persona, the WebSocket protocol and the frontend. Those are the parts most
likely to change, and a divergence between the two editions would be a bug the
tests could not see. Instead the engine-specific code is small and additive:

| File | Purpose |
| --- | --- |
| `brain/speech_local.py` | Which local engine and voice serves each language |
| `brain/asr_local.py` | `LocalWhisperInput`, same interface as the Deepgram and Azure clients |
| `brain/tts_local.py` | Piper and espeak-ng, both returning 24 kHz mono PCM16 |

Switching editions never edits a file:

```powershell
.\local\run-local.ps1     # offline edition, reads local\.env.local
.\run.ps1                 # Azure edition, reads ngnix-astra-brain-engine\.env
```

Your Azure keys are never overwritten, and the two editions keep separate RAG
indexes (`corpus.index.json` and `corpus.local.json`) because a 3072-dimension
Azure embedding and a 768-dimension `nomic-embed-text` embedding are not
interchangeable.

## Install

```powershell
pip install -r ngnix-astra-brain-engine\requirements.txt
pip install -r local\requirements-local.txt
winget install eSpeak-NG.eSpeak-NG        # Pi/Debian: sudo apt install -y espeak-ng
.\local\get_models.ps1 -Tier beta         # voices, Whisper and the LLM
.\local\run-local.ps1
```

Then open <http://localhost:8080>.

`espeak-ng` is a system package, not a pip package. The Windows installer does
not add itself to `PATH`, so the engine also looks in `C:\Program Files\eSpeak NG`.

## Which engine speaks and hears which language

Neither local engine covers all eleven languages, so they are combined. This
table is enforced by `brain/speech_local.py` and checked by `verify_local.py`.

| Language | Voice | Ears |
| --- | --- | --- |
| English | Piper `en_US-ryan-medium` | Whisper |
| Hindi | Piper `hi_IN-pratham-medium` | Whisper |
| Malayalam | Piper `ml_IN-arjun-medium` | Whisper |
| Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Punjabi | espeak-ng | Whisper |
| **Odia** | espeak-ng | **nothing — text only** |

Two facts drive it:

- **Piper publishes Indic voices for Hindi and Malayalam only.** Telugu appears
  in Piper's own `VOICES.md` but is absent from the `rhasspy/piper-voices` model
  repository, so it is routed to espeak-ng with the rest.
- **Whisper has no Odia model.** Odia is not in its language table at all, so
  asking for it does not fail loudly, it transcribes as some other language.
  Odia turns therefore stay in text mode. Odia can still be *spoken*, because
  espeak-ng has an Oriya voice.

espeak-ng is a formant synthesiser, so it sounds robotic next to Piper. For this
character that is closer to an asset than a defect, and it is the only thing that
speaks all eleven languages from a 20 MB install.

### Domain vocabulary

Whisper gets the same cooperative vocabulary Deepgram gets, through its
`initial_prompt`. This is not cosmetic. Measured on the round-trip test:

| | Without | With |
| --- | --- | --- |
| English | "What is it **packs** and who can become a member" | "What is a **PACS** and who can become a member" |
| Hindi | "**पेखs** क्या हे" | "**पैक्स** क्या हे" |

A misheard "PACS" makes the retrieval query miss the passage that answers it, and
the grounding policy then correctly abstains on a question the corpus *can*
answer. The prompt is capped at 220 characters, because a long `initial_prompt`
makes Whisper continue the prompt's style instead of transcribing.

## Hardware tiers

`get_models.ps1 -Tier <pi|beta|gpu>` picks the models for each.

| | `pi` — Raspberry Pi 5 8 GB | `beta` — this laptop | `gpu` — 24 GB RAM, 8 GB VRAM |
| --- | --- | --- | --- |
| LLM | `qwen2.5:1.5b-instruct` | `qwen2.5:3b-instruct` | `qwen2.5:7b-instruct` |
| Whisper | `base` int8 | `small` int8 | `large-v3-turbo` float16 |
| Voice | Piper + espeak-ng | Piper + espeak-ng | Piper + espeak-ng |
| Disk | ~1.4 GB | ~3.0 GB | ~6.5 GB |

On the GPU tier set `WHISPER_DEVICE=cuda` and `WHISPER_COMPUTE_TYPE=float16`.
qwen2.5:7b at q4 needs ~4.7 GB of VRAM and large-v3-turbo ~1.6 GB, so both fit
in 8 GB together with room for context. Raise `RAG_TOP_K` back to 4 and set
`PROMPT_STYLE=full` there — the reasons for shrinking them do not apply once
prompt evaluation is on a GPU.

## Measured performance

All figures from this laptop: **Intel Core 5 210H, 12 logical cores, no discrete
GPU, 15.7 GB RAM**, `qwen2.5:3b-instruct` on Ollama, Python 3.14.

**The LLM is prompt-bound, not generation-bound.** This is the single most
important number for local use:

| Prompt tokens | Wall | Prompt eval | Generation |
| --- | --- | --- | --- |
| 30 | 1.6 s | 0.5 s | 0.9 s |
| 1,020 | 17.6 s | 16.7 s | 0.6 s |
| 2,050 | 39.0 s | 37.8 s | 0.9 s |
| 4,020 | 65.3 s | 64.0 s | 1.0 s |

Prompt evaluation runs at ~60 tokens/second, generation at 13–21. A turn's cost
is therefore set almost entirely by prompt length, which is why this edition uses
`PROMPT_STYLE=compact` (persona 1,732 → 432 tokens) and `RAG_TOP_K=2` with
`RAG_BLOCK_CHARS=500`. That change alone took a cold English turn from **62.8 s
to 25.0 s**, and it also made the answer *better*: the shorter prompt came back
grounded with a `[S1]` citation where the long one did not.

Speech, by contrast, is cheap. Warm figures, after the one-time model load:

| Engine | Warm synthesis | vs realtime |
| --- | --- | --- |
| Piper `en_US-ryan-medium` | 0.11 s for 2.65 s audio | 24x |
| Piper `hi_IN-pratham-medium` | 0.06 s for 1.04 s audio | 17x |
| espeak-ng Tamil / Odia | 0.05 s | ~30x |

First Piper call per voice adds 1.8–2.7 s for the ONNX session load.

| Whisper (2.5 s of speech, int8 CPU) | Warm | vs realtime | Transcript |
| --- | --- | --- | --- |
| `tiny` | 0.43 s | 6.0x | correct |
| `base` | 0.80 s | 3.3x | correct |
| `small` | 2.40 s | 1.0x | correct |

`tiny` and `base` transcribed the English phrase correctly, so English alone does
not need `small`. Indic accuracy does; that is why `small` is the beta default.

**End-to-end spoken turns** (`smoke_test.py` with `SMOKE_SPEAK=1`):

| Turn | Answer | Total with speech |
| --- | --- | --- |
| English, prompt prefix cached | 3.0 s | 6.6 s |
| English, new question | 11.0 s | 12.0 s |
| English, cold start | 25.0 s | — |
| Hindi | 28.9 s | 32.6 s |
| Tamil | 24.3 s | 24.6 s |

Ollama caches the prompt prefix, so repeated turns in a conversation are far
faster than the first. Indic turns are slower because Devanagari and Tamil script
cost more tokens per word than English.

### What this means for a Raspberry Pi

Be realistic about the LLM. A Pi 5 is roughly three to four times slower than
this laptop at prompt evaluation, so even with the compact prompt and a 1.5B
model, expect **30–60 seconds per turn**. Whisper `base` and Piper will keep up
fine; the language model is the problem.

Three honest options:

1. **Accept it** for a kiosk or demo where a slow reply is tolerable.
2. **Keep the LLM off the Pi.** Point `OLLAMA_ENDPOINT` at a machine on the LAN
   and leave STT, TTS and the session on the Pi. This is what the reference
   TARS-AI project does with its `TARS-AI_Server`, for exactly this reason, and
   it is the configuration to prefer for a responsive robot.
3. **Drop to `qwen2.5:0.5b`** and accept noticeably worse answers.

Only option 2 gives conversational latency on Pi-class hardware. Nothing in this
edition assumes the LLM is local beyond the default endpoint, so option 2 is a
one-line change and still keeps microphone audio on the device.

## Offline is enforced, not assumed

Real environment variables outrank the profile file, by design. This machine had
`AZURE_OPENAI_API_KEY` exported at the user level, which meant an "offline"
edition silently inherited a live cloud credential.

So when the profile asks for a local brain, local ears *and* a local voice,
`Settings.load()` discards `openai_key`, `speech_key`, `speech_key_fallback` and
`deepgram_key` outright. `/health` reports `"offline": true` only in that state.

`TOOLS_ENABLED=false` for the same reason: every tool this project has —
`web_search`, `fetch_page`, `get_weather` — needs the network, and the system
prompt is switched to tell the model it has no internet rather than offering it
tools that cannot work. Promising a model tools it does not have is how you get a
confidently invented search result.

## Model licences

The weights are **not** committed to this repository. `get_models.ps1` fetches
them, and the `MODEL_CARD` file for each Piper voice is tracked alongside where
they land so the licence travels with the project.

| Model | Licence | Matters because |
| --- | --- | --- |
| `en_US-ryan-medium` | CC BY-NC-SA 4.0 | **non-commercial** |
| `hi_IN-pratham-medium` | CC BY-NC-SA 4.0 | **non-commercial** |
| `ml_IN-arjun-medium` | see its MODEL_CARD | check before shipping |
| espeak-ng | GPLv3 | separate binary, invoked not linked |
| Whisper (faster-whisper) | MIT | permissive |
| `qwen2.5` | Apache 2.0 | permissive |
| `nomic-embed-text` | Apache 2.0 | permissive |

The three Piper voices are the only non-commercial component. If this is ever
deployed as a commercial or paid service, they have to go — set
`TTS_LOCAL_ENGINE=espeak` and every language falls back to espeak-ng, which is
GPLv3 and has no such restriction. The quality drops for English, Hindi and
Malayalam; nothing else changes.

## Testing

```powershell
python local\verify_local.py           # 82 checks, no models or network needed
python local\verify_local_speech.py    # real round trip: local TTS -> local Whisper
python local\bench_local.py            # LLM prompt-eval vs generation cost
python local\bench_speech.py           # STT and TTS latency per model
```

`verify_local.py` checks the routing tables, the engine dispatch, the resampler's
maths, the config validation, that a stray cloud key cannot add a cloud failover,
and that the compact persona still carries the identity, the dial scales and the
rule about dropping the humour when someone is worried. Engine and synthesis
checks are skipped rather than failed when the models are not installed, so it is
useful before and after `get_models.ps1`.

`verify_local_speech.py` synthesises a phrase locally, resamples it to the
microphone's 16 kHz, feeds it to `LocalWhisperInput` frame by frame exactly as
the browser would, and compares the transcript by word overlap. It needs no
microphone and spends no quota.

The Azure edition's own suite must keep passing after any change here:

```powershell
cd ngnix-astra-brain-engine; python verify_pipeline.py     # 189 checks
```

## Known limitations

- **Odia cannot be heard**, only spoken. No local engine transcribes it. Using
  Odia needs either the Azure edition or an AI4Bharat IndicConformer model, which
  is not wired up.
- **espeak-ng sounds robotic** in the eight languages Piper does not cover.
- **A Pi cannot run the LLM at conversational speed.** See above.
- **First turn is slow** — Whisper load, Piper ONNX load and a cold prompt cache
  all land on it.

Hands-free "hey TARS" works in this edition too, and costs nothing here: the wake
word is matched against the transcript Whisper already produces, so no audio
leaves the device and no extra model is loaded. See the main
[README](../README.md#hands-free-hey-tars).
