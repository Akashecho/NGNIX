"""Full offline speech round trip: local TTS -> PCM -> local Whisper -> text.

This is the local edition's equivalent of verify_speech_live.py, except it spends
no cloud quota and needs no network or microphone. A phrase is synthesised by
Piper or espeak-ng, resampled to the 16 kHz the microphone path uses, fed to
LocalWhisperInput exactly as browser frames would be, and the transcript is
compared against the words that went in.

Run from the repo root:  python local\\verify_local_speech.py
                         python local\\verify_local_speech.py hi

Loading Whisper 'small' takes a while on first run and the transcription itself
is CPU-bound, so expect this to take a minute or two per language.
"""

import asyncio
from pathlib import Path
import re
import sys
import os

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / 'ngnix-astra-brain-engine'
sys.path.insert(0, str(ENGINE))
os.environ['BRAIN_ENV_FILE'] = str(Path(__file__).resolve().parent / '.env.local')

import numpy as np

from brain.asr_local import LocalWhisperInput
from brain.config import Settings
from brain.speech_local import local_tts_plan, whisper_language
from brain.tts_local import LocalTts

# Phrases chosen to contain the domain vocabulary the corpus uses, because that
# is what recognition has to get right for retrieval to work.
PHRASES = {
    'en': 'What is a PACS and who can become a member',
    'hi': 'पैक्स क्या है और कौन सदस्य बन सकता है',
    'ml': 'എന്താണ് പാക്സ്',
    'ta': 'PACS என்றால் என்ன',
    'te': 'PACS అంటే ఏమిటి',
    'bn': 'PACS কি',
    'mr': 'PACS काय आहे',
    'gu': 'PACS શું છે',
    'kn': 'PACS ಎಂದರೇನು',
    'pa': 'PACS ਕੀ ਹੈ',
}

LANGUAGES = sys.argv[1:] or ['en', 'hi']
FRAME_BYTES = 3200  # 100 ms of 16 kHz mono PCM16, as the browser sends


def to_16k(pcm24: bytes) -> bytes:
    """Resample 24 kHz mono PCM16 down to the 16 kHz the ASR path expects."""
    samples = np.frombuffer(pcm24, dtype='<i2').astype(np.float32)
    count = int(len(samples) * 16000 / 24000)
    positions = np.linspace(0, len(samples), count, endpoint=False)
    resampled = np.interp(positions, np.arange(len(samples)), samples)
    return np.clip(resampled, -32768, 32767).astype('<i2').tobytes()


def words(text: str) -> set[str]:
    return {w for w in re.findall(r'\w+', text.lower()) if len(w) > 2}


async def round_trip(settings, language: str) -> tuple[bool, str, str]:
    phrase = PHRASES[language]
    plan = local_tts_plan(language, settings.tts_local_engine)
    if not plan:
        return False, phrase, 'no local voice'
    engine, voice = plan

    pcm24 = await LocalTts(settings).synthesize(phrase, voice)
    pcm16 = to_16k(pcm24)
    seconds = len(pcm16) / (16000 * 2)
    print(f'  spoke {seconds:.1f}s with {engine} ({voice})')

    transcripts: list[str] = []

    async def on_transcript(text, final):
        if final:
            transcripts.append(text)

    async def on_speech():
        pass

    async def on_error(message):
        print(f'  ASR error: {message}')

    ears = LocalWhisperInput(settings, on_transcript, on_speech, on_error,
                             language=whisper_language(language))
    await ears.start()
    # Lead-in silence lets the energy gate establish a noise floor, exactly as a
    # real room does before the speaker starts.
    silence = b'\x00' * FRAME_BYTES
    for _ in range(5):
        await ears.send(silence)
    for offset in range(0, len(pcm16), FRAME_BYTES):
        frame = pcm16[offset:offset + FRAME_BYTES]
        if len(frame) % 2:
            frame = frame[:-1]
        if frame:
            await ears.send(frame)
    for _ in range(9):
        await ears.send(silence)
    await ears.stop(flush=True)

    heard = ' '.join(transcripts).strip()
    if not heard:
        return False, phrase, ''
    # Whisper will not reproduce punctuation or casing, so compare word overlap.
    expected, got = words(phrase), words(heard)
    overlap = len(expected & got) / max(len(expected), 1)
    return overlap >= 0.5, phrase, heard


async def main() -> int:
    settings = Settings.load()
    print(f'Whisper {settings.whisper_model} on {settings.whisper_device} '
          f'({settings.whisper_compute_type}), beam {settings.whisper_beam_size}\n')
    failures = 0
    for language in LANGUAGES:
        if language not in PHRASES:
            print(f'{language}: no test phrase (Odia has no local recognition at all)')
            continue
        print(f'{language}:')
        try:
            ok, said, heard = await round_trip(settings, language)
        except Exception as error:
            print(f'  FAIL {type(error).__name__}: {error}')
            failures += 1
            continue
        print(f'  said:  {said}')
        print(f'  heard: {heard or "(nothing)"}')
        print(f'  {"ok" if ok else "FAIL"}\n')
        failures += 0 if ok else 1
    if failures:
        print(f'{failures} language(s) failed the round trip.')
        return 1
    print('Offline speech round trip works: local TTS -> local Whisper.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
