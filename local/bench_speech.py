"""Measure local STT and TTS latency, so the tier table in README is real.

Usage:  python local\\bench_speech.py [whisper_model ...]
"""

import asyncio
from pathlib import Path
import os
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ngnix-astra-brain-engine'))
os.environ['BRAIN_ENV_FILE'] = str(Path(__file__).resolve().parent / '.env.local')

import numpy as np

from brain.asr_local import domain_prompt, load_model
from brain.config import Settings
from brain.tts_local import LocalTts

MODELS = sys.argv[1:] or ['tiny', 'base', 'small']
PHRASE = 'What is a PACS and who can become a member of one'


def to_16k(pcm24: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm24, dtype='<i2').astype(np.float32) / 32768
    count = int(len(samples) * 16000 / 24000)
    return np.interp(np.linspace(0, len(samples), count, endpoint=False),
                     np.arange(len(samples)), samples).astype(np.float32)


async def main():
    settings = Settings.load()
    mouth = LocalTts(settings)

    print('TTS (first call includes the one-time model load; warm is steady state)\n')
    print(f'{"engine":>10} {"voice":>22} {"first":>8} {"warm":>8} {"audio":>8} {"xrealtime":>10}')
    for label, voice in (('piper', 'en_US-ryan-medium'), ('piper', 'hi_IN-pratham-medium'),
                         ('espeak', 'ta+m3'), ('espeak', 'or+m3')):
        text = PHRASE if voice.startswith('en') else 'पैक्स क्या है'
        started = time.time()
        pcm = await mouth.synthesize(text, voice)
        first = time.time() - started
        started = time.time()
        pcm = await mouth.synthesize(text, voice)
        warm = time.time() - started
        audio_seconds = len(pcm) / (24000 * 2)
        print(f'{label:>10} {voice:>22} {first:>7.2f}s {warm:>7.2f}s {audio_seconds:>7.2f}s '
              f'{audio_seconds / max(warm, 0.001):>9.1f}x')

    reference = await mouth.synthesize(PHRASE, 'en_US-ryan-medium')
    audio = to_16k(reference)
    duration = len(audio) / 16000
    print(f'\nSTT (transcribing {duration:.1f}s of speech, int8 on CPU)\n')
    print(f'{"model":>12} {"load":>8} {"first":>8} {"warm":>8} {"xrealtime":>10}  text')
    for name in MODELS:
        started = time.time()
        model = load_model(name, 'cpu', 'int8', settings.whisper_cpu_threads)
        load_seconds = time.time() - started

        def run():
            segments, _ = model.transcribe(audio, language='en', beam_size=1,
                                           initial_prompt=domain_prompt(settings.keyterms),
                                           vad_filter=False, temperature=0.0,
                                           condition_on_previous_text=False)
            return ' '.join(s.text.strip() for s in segments).strip()

        started = time.time()
        text = run()
        first = time.time() - started
        started = time.time()
        run()
        warm = time.time() - started
        print(f'{name:>12} {load_seconds:>7.1f}s {first:>7.2f}s {warm:>7.2f}s '
              f'{duration / max(warm, 0.001):>9.1f}x  {text[:60]}')


asyncio.run(main())
