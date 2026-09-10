"""Live speech round-trip against the real services.

Uses Azure Speech neural TTS as the audio source, so the ears can be tested
without a microphone: synthesise a known phrase, resample it to the 16 kHz mono
PCM16 the microphone path produces, feed it to Deepgram nova-3 and to Azure
Speech recognition, and check the phrase comes back.

This spends real quota on every run. From ngnix-astra-brain-engine:

    python verify_speech_live.py
"""

import asyncio
import dataclasses
import os
import sys
import traceback

import httpx
import numpy as np

from brain.asr import DeepgramInput
from brain.asr_azure import AzureSpeechInput
from brain.config import Settings
from brain.providers import azure_speech_synthesize
from brain.speech import AZURE_VOICES, deepgram_language, azure_locale

FAILURES = []
CHECKS = 0
FRAME_BYTES = 3200  # 100 ms at 16 kHz mono PCM16, inside the 6400 byte cap


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    print(f'  {"ok  " if condition else "FAIL"} {label}' + (f' - {detail}' if detail else ''))
    if not condition:
        FAILURES.append(label)


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """Azure TTS emits 24 kHz; the microphone path is 16 kHz."""
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    target = int(len(samples) * 16000 / 24000)
    positions = np.arange(target) * (24000 / 16000)
    resampled = np.interp(positions, np.arange(len(samples)), samples)
    return np.clip(resampled, -32768, 32767).astype('<i2').tobytes()


def normalise(text: str) -> str:
    return ''.join(character.lower() for character in text if character.isalnum() or character.isspace()).split()


async def feed(engine, pcm: bytes, pace: float = 0.0):
    for offset in range(0, len(pcm), FRAME_BYTES):
        frame = pcm[offset:offset + FRAME_BYTES]
        if len(frame) % 2:
            frame = frame[:-1]
        if frame:
            await engine.send(frame)
        if pace:
            await asyncio.sleep(pace)


async def spoken_audio(settings, client, text, voice):
    """Synthesise a phrase and hand back microphone-shaped PCM."""
    pcm24 = await azure_speech_synthesize(settings, client, text, voice)
    pcm16 = resample_24k_to_16k(pcm24)
    return pcm24, pcm16


async def verify_tts(settings, client):
    print('\nAzure Speech neural TTS (live)')
    phrase = 'A PACS is a village level cooperative credit society.'
    pcm24, pcm16 = await spoken_audio(settings, client, phrase, AZURE_VOICES['en'])
    seconds = len(pcm24) / (24000 * 2)
    check('English synthesis returned audio', len(pcm24) > 0, f'{seconds:.2f} s, {len(pcm24)} bytes')
    check('audio duration is plausible for the phrase', 1.5 < seconds < 12, f'{seconds:.2f} s')
    check('resampled to the microphone rate',
          abs(len(pcm16) / (16000 * 2) - seconds) < 0.05, f'{len(pcm16) / (16000 * 2):.2f} s')
    peak = int(np.abs(np.frombuffer(pcm24, dtype='<i2').astype(np.int32)).max())
    check('audio is not silent', peak > 2000, f'peak {peak}')

    hindi_pcm24, _ = await spoken_audio(settings, client, 'पैक्स एक गाँव स्तर की सहकारी समिति है।', AZURE_VOICES['hi'])
    check('Hindi synthesis returned audio', len(hindi_pcm24) > 0, f'{len(hindi_pcm24) / 48000:.2f} s')

    malayalam_pcm24, malayalam_pcm16 = await spoken_audio(
        settings, client, 'ഒരു പാക്സ് ഗ്രാമതല സഹകരണ സംഘമാണ്.', AZURE_VOICES['ml'])
    check('Malayalam synthesis returned audio', len(malayalam_pcm24) > 0, f'{len(malayalam_pcm24) / 48000:.2f} s')
    return pcm16, malayalam_pcm16


async def verify_tts_fallback(settings, client):
    print('\nAzure Speech fallback region (live)')
    key = os.getenv('AZURE_SPEECH_KEY_FALLBACK', '')
    region = os.getenv('AZURE_SPEECH_REGION_FALLBACK', '')
    if not key or not region:
        print('  skip  no fallback speech resource configured')
        return
    fallback = dataclasses.replace(settings, speech_key=key, speech_region=region)
    try:
        pcm = await azure_speech_synthesize(fallback, client, 'Fallback region check.', AZURE_VOICES['en'])
        check(f'fallback region {region} synthesises', len(pcm) > 0, f'{len(pcm) / 48000:.2f} s')
    except Exception as error:  # noqa: BLE001
        check(f'fallback region {region} synthesises', False, repr(error))


async def verify_deepgram(settings, client, pcm16):
    print('\nDeepgram nova-3 recognition (live)')
    transcripts, interims, errors = [], [], []

    async def on_transcript(text, final):
        (transcripts if final else interims).append(text)

    async def on_speech():
        pass

    async def on_error(message):
        errors.append(message)

    language = deepgram_language('en', settings.asr_language)
    engine = DeepgramInput(settings, on_transcript, on_speech, on_error, language=language, client=client)
    try:
        await engine.start()
        check('socket opened', engine.socket is not None)
        check('English uses the code-switching model', language == 'multi', language)
        # Paced roughly like a live microphone so endpointing behaves normally.
        await feed(engine, pcm16, pace=0.05)
        await asyncio.sleep(1.0)
        await engine.stop(flush=True)
    except Exception as error:  # noqa: BLE001
        check('socket opened', False, repr(error))
        return

    heard = ' '.join(transcripts).strip()
    print(f'  heard: {heard!r}')
    check('a final transcript arrived', bool(heard), str(transcripts))
    words = normalise(heard)
    check('transcript contains "cooperative"', 'cooperative' in words, str(words))
    check('transcript contains "society"', 'society' in words, str(words))
    check('interim results were streamed', bool(interims), f'{len(interims)} interims')
    check('no ASR errors', not errors, str(errors))


async def verify_azure_stt(settings, client, malayalam_pcm16):
    print('\nAzure Speech recognition, Malayalam (live) — the nova-3 language gap')
    transcripts, errors = [], []

    async def on_transcript(text, final):
        transcripts.append((text, final))

    async def on_speech():
        pass

    async def on_error(message):
        errors.append(message)

    locale = azure_locale('ml')
    engine = AzureSpeechInput(settings, on_transcript, on_speech, on_error, language=locale, client=client)
    await engine.start()
    check('Malayalam routes to the ml-IN locale', locale == 'ml-IN', str(locale))
    await feed(engine, malayalam_pcm16)
    # Trailing silence so the energy gate closes the utterance.
    await feed(engine, b'\x00\x00' * 8000)
    await engine.stop(flush=True)

    heard = ' '.join(text for text, _ in transcripts).strip()
    print(f'  heard: {heard!r}')
    check('a final transcript arrived', bool(heard), str(transcripts))
    check('transcript is Malayalam script',
          any('\u0d00' <= character <= '\u0d7f' for character in heard), heard)
    check('no recognition errors', not errors, str(errors))


async def main():
    settings = Settings.load()
    print('Live speech round-trip: Azure TTS -> PCM -> Deepgram nova-3 / Azure Speech STT')
    print(f'brain={settings.llm_provider} tts_region={settings.speech_region} asr_model={settings.asr_model}')
    if not settings.speech_key:
        print('AZURE_SPEECH_KEY is required for this test')
        return 2

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        english_pcm16, malayalam_pcm16 = await verify_tts(settings, client)
        await verify_tts_fallback(settings, client)
        if settings.deepgram_key:
            await verify_deepgram(settings, client, english_pcm16)
        else:
            print('\nDeepgram nova-3 recognition (live)\n  skip  no DEEPGRAM_API_KEY')
        await verify_azure_stt(settings, client, malayalam_pcm16)

    print(f'\n{CHECKS - len(FAILURES)}/{CHECKS} live checks passed')
    if FAILURES:
        print('failed: ' + ', '.join(FAILURES))
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
