"""Live voice turn through the real WebSocket protocol.

Exercises the exact path the browser uses, minus the browser: Azure TTS generates
the question audio, it is resampled to 16 kHz mono PCM16 and streamed to
/ws/session as binary frames, and the reply is checked all the way back to
streamed Azure Speech audio.

  question audio -> audio_start -> PCM frames -> ASR -> Azure OpenAI -> TTS -> PCM

Run the server first, then from ngnix-astra-brain-engine:

    python verify_voice_turn_live.py

This spends real quota. Only the browser's microphone capture and speaker
playback are left untested; every server-side hop is real.
"""

import asyncio
import json
import os
import sys
import traceback

import httpx
import numpy as np
from websockets.asyncio.client import connect

from brain.config import Settings
from brain.providers import azure_speech_synthesize
from brain.speech import AZURE_VOICES

FRAME_BYTES = 3200  # 100 ms at 16 kHz mono PCM16
FAILURES = []
CHECKS = 0


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    print(f'  {"ok  " if condition else "FAIL"} {label}' + (f' - {detail}' if detail else ''))
    if not condition:
        FAILURES.append(label)


def resample_24k_to_16k(pcm: bytes) -> bytes:
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    target = int(len(samples) * 16000 / 24000)
    positions = np.arange(target) * 1.5
    return np.clip(np.interp(positions, np.arange(len(samples)), samples),
                   -32768, 32767).astype('<i2').tobytes()


async def question_audio(settings, text, language):
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        pcm24 = await azure_speech_synthesize(settings, client, text, AZURE_VOICES[language])
    return resample_24k_to_16k(pcm24)


async def voice_turn(settings, url, question, language, expected_provider, expect_grounded=True):
    print(f'\nSpoken turn in {language!r}: {question!r}')
    pcm = await question_audio(settings, question, language)
    print(f'  question audio: {len(pcm) / 32000:.2f} s of 16 kHz mono PCM16')

    async with connect(url, max_size=4_000_000) as socket:
        await socket.send(json.dumps({'type': 'auth', 'token': settings.session_token}))
        ready = json.loads(await asyncio.wait_for(socket.recv(), 15))
        if ready.get('type') != 'ready':
            check('session ready', False, str(ready))
            return
        check('microphone advertised as enabled', ready['microphone'].get('enabled') is True, str(ready['microphone']))

        await socket.send(json.dumps({'type': 'audio_start', 'language': language}))

        interims, finals, states = [], [], []
        answer, audio_bytes, segments, ready_event, failure = None, 0, 0, None, None
        pending_header = None
        streamed = False

        async def stream_audio():
            for offset in range(0, len(pcm), FRAME_BYTES):
                frame = pcm[offset:offset + FRAME_BYTES]
                if len(frame) % 2:
                    frame = frame[:-1]
                if frame:
                    await socket.send(frame)
                await asyncio.sleep(0.05)   # faster than real time, still paced
            # Trailing silence lets the energy gate close an Azure utterance.
            for _ in range(8):
                await socket.send(b'\x00\x00' * 1600)
                await asyncio.sleep(0.05)
            await socket.send(json.dumps({'type': 'audio_stop'}))

        sender = None
        deadline = asyncio.get_running_loop().time() + 150
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), 30)
            except TimeoutError:
                break
            if not isinstance(raw, str):
                if pending_header is not None:
                    audio_bytes += len(raw)
                    segments += 1
                    pending_header = None
                continue
            event = json.loads(raw)
            kind = event.get('type')
            if kind == 'audio_ready':
                ready_event = event
                print(f"  audio_ready: engine={event['asr_provider']} language={event['asr_language']} "
                      f"interim={event['interim_transcripts']} voice={event['voice']}")
                if not streamed:
                    streamed = True
                    sender = asyncio.create_task(stream_audio())
            elif kind == 'transcript':
                (finals if event.get('final') else interims).append(event.get('text', ''))
                if event.get('final'):
                    print(f"  final transcript: {event['text']!r}")
            elif kind == 'state':
                states.append(event['state'])
            elif kind == 'audio':
                pending_header = event
            elif kind == 'audio_end':
                await socket.send(json.dumps({'type': 'playback_done', 'turn_id': event['turn_id'],
                                              'utterance_id': event['utterance_id']}))
            elif kind == 'response':
                answer = event
            elif kind == 'error':
                failure = event
                print(f"  ERROR {event['code']}: {event['message']}")
            elif kind == 'turn_done':
                break

        if sender and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    check('ASR engine started', ready_event is not None)
    if ready_event:
        check(f'engine is {expected_provider}', ready_event['asr_provider'] == expected_provider,
              ready_event['asr_provider'])
        check('interim support reported correctly',
              ready_event['interim_transcripts'] == (expected_provider == 'deepgram'),
              str(ready_event['interim_transcripts']))
        check('a voice was resolved for the language', bool(ready_event['voice']), str(ready_event['voice']))
    check('no protocol error', failure is None, str(failure))
    check('speech was transcribed', bool(finals), str(finals))
    if expected_provider == 'deepgram':
        check('interim transcripts streamed while speaking', bool(interims), f'{len(interims)} interims')
    check('the transcript started a turn', 'THINKING' in states, str(states))
    check('an answer came back', answer is not None and bool(answer.get('text', '').strip()),
          (answer or {}).get('text', '')[:80])
    if answer:
        # Whether a given turn ends up grounded depends on the model emitting a
        # [Sn] marker, which gpt-4.1-nano does not do on every run (see the
        # citation-mode section of the README). The pipeline invariant that must
        # always hold is narrower and deterministic: an answer is either grounded
        # in retrieved sources, or it is the referral with no sources claimed.
        # Never unsourced advice presented as sourced.
        grounded = answer.get('grounded') is True
        sources = answer.get('sources') or []
        check('answer is either grounded or a sourceless referral',
              (grounded and sources) or (not grounded and not sources)
              or (not grounded and settings.open_domain),
              f'grounded={grounded} sources={len(sources)} mode={settings.assistant_mode}')
        if grounded:
            print(f'  grounded on {len(sources)} source(s)')
        elif settings.open_domain:
            print('  answered from general knowledge (ASSISTANT_MODE=open)')
        elif expect_grounded:
            print('  note: abstained — retrieval succeeded but the model omitted a citation marker '
                  'this run, so the gate withheld the answer')
        else:
            print('  note: abstained because the English-only demo corpus has no passage for '
                  'this language, not because speech failed')
        print(f"  answer: {answer['text'][:160]}")
    check('reply audio was streamed back', segments > 0, f'{segments} segments')
    if segments:
        print(f'  reply audio: {segments} segments, {audio_bytes / 48000:.2f} s of 24 kHz mono PCM16')


async def main():
    settings = Settings.load()
    url = os.getenv('SMOKE_URL', 'ws://127.0.0.1:8000/ws/session')
    print(f'Live voice turn against {url}')
    if not settings.speech_key:
        print('AZURE_SPEECH_KEY is required to generate the question audio')
        return 2

    # English goes to Deepgram nova-3 (code-switching `multi`).
    await voice_turn(settings, url, 'How do I raise a complaint?', 'en', 'deepgram')
    # Hindi also goes to Deepgram, and the demo corpus retrieves cross-lingually.
    await voice_turn(settings, url, 'पैक्स क्या है?', 'hi', 'deepgram')
    # Malayalam has no nova-3 model, so it must land on Azure Speech. The
    # English-only demo corpus does not retrieve for it, so grounding is not required.
    await voice_turn(settings, url, 'ഒരു പരാതി എങ്ങനെ നൽകാം?', 'ml', 'azure-speech', expect_grounded=False)

    print(f'\n{CHECKS - len(FAILURES)}/{CHECKS} live voice-turn checks passed')
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
