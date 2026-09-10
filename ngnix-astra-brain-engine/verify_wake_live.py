"""Drive a real hands-free turn over the WebSocket, with spoken audio.

    python verify_wake_live.py                 # against whatever server is on 8000

Synthesises the audio locally with Piper or espeak-ng, so generating the speech
costs nothing and needs no microphone. The audio is then streamed to
/ws/session exactly as the browser streams it, with wake_word enabled, and the
protocol events are checked:

  "hey TARS, what is a PACS"  -> wake_detected, then a real answer
  "what is a PACS"            -> wake_ignored, and no turn at all
  "hey TARS"                  -> wake_detected with an acknowledgement, no turn

Recognition has to actually hear the wake word for this to pass, so it exercises
the whole chain rather than the matcher alone. Requires the local speech engines
(pip install -r local/requirements-local.txt) for synthesis; the server itself
may be either edition.
"""

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import numpy as np
from websockets.asyncio.client import connect

# Synthesis always uses the local profile: this test should not spend TTS quota
# just to produce its own input.
os.environ.setdefault('BRAIN_ENV_FILE', str(ROOT.parent / 'local' / '.env.local'))

from brain.config import Settings
from brain.tts_local import LocalTts

URL = os.getenv('BRAIN_WS', 'ws://127.0.0.1:8000/ws/session')
LANGUAGE = os.getenv('WAKE_LANGUAGE', 'en')
VOICE = os.getenv('WAKE_VOICE', 'en_US-ryan-medium')
FRAME_BYTES = 3200  # 100 ms of 16 kHz mono PCM16

passed = failed = 0


def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f'  ok   {label}')
    else:
        failed += 1
        print(f'  FAIL {label}' + (f' -- {detail}' if detail else ''))


def to_16k(pcm24: bytes) -> bytes:
    samples = np.frombuffer(pcm24, dtype='<i2').astype(np.float32)
    count = int(len(samples) * 16000 / 24000)
    positions = np.linspace(0, len(samples), count, endpoint=False)
    return np.clip(np.interp(positions, np.arange(len(samples)), samples),
                   -32768, 32767).astype('<i2').tobytes()


async def say(socket, pcm16: bytes):
    """Stream one utterance, with silence either side so the gate closes."""
    silence = b'\x00' * FRAME_BYTES
    for _ in range(4):
        await socket.send(silence)
    for offset in range(0, len(pcm16), FRAME_BYTES):
        frame = pcm16[offset:offset + FRAME_BYTES]
        if len(frame) % 2:
            frame = frame[:-1]
        if frame:
            await socket.send(frame)
    # Enough trailing quiet to pass the 700 ms hangover and close the utterance.
    for _ in range(12):
        await socket.send(silence)


async def collect(socket, seconds: float, until=None) -> list[dict]:
    """Read JSON events, ignoring binary audio frames.

    Returns as soon as `until` names an event that has arrived. Waiting the full
    window instead would idle out a streaming ASR socket: Deepgram closes a
    connection that receives no audio for about ten seconds, which looked like a
    wake word failure until the timing was fixed.
    """
    events, deadline = [], asyncio.get_running_loop().time() + seconds
    wanted = set(until or ())
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return events
        try:
            raw = await asyncio.wait_for(socket.recv(), remaining)
        except (TimeoutError, asyncio.TimeoutError):
            return events
        if isinstance(raw, str):
            event = json.loads(raw)
            events.append(event)
            if event.get('type') in wanted:
                return events


async def main() -> int:
    settings = Settings.load()
    mouth = LocalTts(settings)

    print(f'Synthesising test speech with {VOICE}')
    addressed = to_16k(await mouth.synthesize('Hey TARS, what is a PACS?', VOICE))
    plain = to_16k(await mouth.synthesize('What is a PACS?', VOICE))
    bare = to_16k(await mouth.synthesize('Hey TARS.', VOICE))
    question_only = to_16k(await mouth.synthesize('What can a PACS do for a farmer?', VOICE))

    token = os.getenv('BRAIN_SESSION_TOKEN') or settings.session_token
    print(f'Connecting to {URL}\n')
    async with connect(URL, max_size=4_000_000) as socket:
        await socket.send(json.dumps({'type': 'auth', 'token': token}))
        ready = json.loads(await asyncio.wait_for(socket.recv(), 15))
        if ready.get('type') != 'ready':
            print(f'FAIL: expected ready, got {ready}')
            return 1
        print(f"server: provider={ready.get('provider')} wake_word={ready.get('wake_word')!r} "
              f"stt={ready.get('stt_engines')}")
        check('the server advertises a wake word', bool(ready.get('wake_word')))

        await socket.send(json.dumps({'type': 'audio_start', 'language': LANGUAGE,
                                      'wake_word': True}))
        opened = await collect(socket, 45, until={'audio_ready'})
        started = [e for e in opened if e.get('type') == 'audio_ready']
        problems = [e for e in opened if e.get('type') == 'error']
        if problems:
            print(f'        server error: {json.dumps(problems[0])}')
        check('audio_ready reports the wake word is armed',
              bool(started) and bool(started[0].get('wake_word')),
              json.dumps(opened[:3]))
        if not started:
            return 1
        print(f"        engine={started[0].get('asr_provider')}\n")

        print('1. "What is a PACS?" — not addressed to TARS')
        await say(socket, plain)
        events = await collect(socket, 60, until={'wake_ignored'})
        kinds = [e.get('type') for e in events]
        ignored = [e for e in events if e.get('type') == 'wake_ignored']
        check('the speech was heard', bool(ignored) or 'transcript' in kinds,
              json.dumps(kinds))
        check('it was ignored rather than answered', 'wake_detected' not in kinds, json.dumps(kinds))
        check('no turn was started', 'turn_done' not in kinds, json.dumps(kinds))
        if ignored:
            print(f"        heard and ignored: {ignored[0].get('text')!r}\n")

        print('2. "Hey TARS, what is a PACS?" — addressed')
        await say(socket, addressed)
        # Bounded by any terminal outcome, not just turn_done: waiting the full
        # window when the wake word is missed leaves the ASR socket idle long
        # enough to be closed, which then buries the real failure under a flood
        # of send errors.
        events = await collect(socket, 150, until={'turn_done', 'wake_ignored'})
        kinds = [e.get('type') for e in events]
        detected = [e for e in events if e.get('type') == 'wake_detected']
        answers = [e for e in events if e.get('type') == 'response']
        check('the wake word was detected', bool(detected), json.dumps(kinds))
        if detected:
            print(f"        detected in: {detected[0].get('text')!r}")
            print(f"        question taken as: {detected[0].get('question')!r}")
            check('the wake phrase was stripped from the question',
                  'tars' not in (detected[0].get('question') or '').lower(),
                  detected[0].get('question'))
        check('a turn ran and produced an answer', bool(answers), json.dumps(kinds))
        if answers:
            print(f"        answer: {(answers[0].get('text') or '')[:110]}\n")

        print('3. "Hey TARS." — wake word alone')
        await say(socket, bare)
        events = await collect(socket, 60, until={'acknowledgement'})
        kinds = [e.get('type') for e in events]
        acks = [e for e in events if e.get('type') == 'acknowledgement']
        check('the bare wake word was detected', 'wake_detected' in kinds, json.dumps(kinds))
        check('it was acknowledged instead of answered', bool(acks), json.dumps(kinds))
        if acks:
            print(f"        acknowledgement: {acks[0].get('text')!r}")
        check('no empty question was sent to the model',
              not [e for e in events if e.get('type') == 'retrieval'], json.dumps(kinds))

        # The case that failed in production. Recognition finalises on a pause, so
        # a natural "Hey TARS, what is a PACS?" often arrives as two utterances.
        # The question must be answered without the name being repeated.
        print('\n4. the question that follows a bare wake word')
        await say(socket, question_only)
        events = await collect(socket, 150, until={'turn_done', 'wake_ignored'})
        kinds = [e.get('type') for e in events]
        answers = [e for e in events if e.get('type') == 'response']
        check('a follow-up question is answered without repeating the name',
              bool(answers), json.dumps(kinds))
        check('the follow-up was not discarded as unaddressed',
              'wake_ignored' not in kinds, json.dumps(kinds))
        if answers:
            print(f"        answer: {(answers[0].get('text') or '')[:110]}")

        await socket.send(json.dumps({'type': 'audio_stop'}))

    print(f'\n{passed}/{passed + failed} checks passed')
    if failed:
        print('Hands-free listening is not working end to end.')
        return 1
    print('Hands-free listening works: only speech addressed to TARS is answered.')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
