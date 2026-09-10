"""Verify the deployed cloud backend over a real WSS connection.

    python verify_deployed.py <wss-url> [token]

Checks the parts that only break in a deployment: TLS on the socket, the origin
allowlist, the auth frame, and one real grounded turn. Run it after
deploy-backend.ps1 and before pointing the frontend at a new backend.
"""

import asyncio
import json
import os
from pathlib import Path
import sys

from websockets.asyncio.client import connect

URL = sys.argv[1] if len(sys.argv) > 1 else os.getenv('BRAIN_WS_URL', '')
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ''
if not TOKEN:
    cached = Path(os.getenv('TEMP', '.')) / 'tars_production_token.txt'
    TOKEN = cached.read_text(encoding='utf-8').strip() if cached.is_file() else ''
ORIGIN = os.getenv('BRAIN_ORIGIN', 'https://ngnix-astra-studio-demo.vercel.app')
QUESTION = os.getenv('QUESTION', 'What is a PACS?')

if not URL or not TOKEN:
    print('usage: python verify_deployed.py wss://host/ws/session [token]')
    raise SystemExit(2)

passed = failed = 0


def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f'  ok   {label}')
    else:
        failed += 1
        print(f'  FAIL {label}' + (f' -- {detail}' if detail else ''))


async def turn(origin, token, expect_reject=False):
    """One session. Returns the events, or None if the socket was refused."""
    try:
        async with connect(URL, additional_headers={'Origin': origin},
                           max_size=4_000_000, open_timeout=25) as socket:
            await socket.send(json.dumps({'type': 'auth', 'token': token}))
            first = json.loads(await asyncio.wait_for(socket.recv(), 25))
            if first.get('type') != 'ready':
                return [first]
            if expect_reject:
                return [first]
            await socket.send(json.dumps({'type': 'text', 'text': QUESTION,
                                          'language': 'en', 'speak': False}))
            events = [first]
            while True:
                raw = await asyncio.wait_for(socket.recv(), 90)
                if not isinstance(raw, str):
                    continue
                event = json.loads(raw)
                events.append(event)
                if event.get('type') in {'turn_done', 'error'}:
                    return events
    except Exception as error:
        return None if expect_reject else [{'type': 'connection_failed', 'detail': str(error)[:160]}]


async def main() -> int:
    print(f'{URL}\norigin {ORIGIN}\n')
    check('the socket is TLS', URL.startswith('wss://'), URL)

    events = await turn(ORIGIN, TOKEN)
    if not events:
        check('the allowed origin can connect', False, 'refused')
        return 1
    ready = events[0]
    if ready.get('type') == 'connection_failed':
        check('the allowed origin can connect', False, ready.get('detail'))
        return 1

    check('the allowed origin can connect', ready.get('type') == 'ready', json.dumps(ready)[:120])
    check('the deployed brain is Azure', ready.get('provider') == 'azure', str(ready.get('provider')))
    check('the corpus loaded in the container', bool(ready.get('corpus_loaded')))
    check('a server voice is available', bool(ready.get('server_tts')))
    check('the microphone is offered', bool(ready.get('microphone')))
    check('the wake word is advertised', bool(ready.get('wake_word')), str(ready.get('wake_word')))
    print(f"        model={ready.get('model')} stt={ready.get('stt_engines')}")

    answers = [e for e in events if e.get('type') == 'response']
    failures = [e for e in events if e.get('type') == 'error']
    check('a real turn produced an answer', bool(answers),
          json.dumps(failures[:1]) if failures else str([e.get('type') for e in events]))
    if answers:
        text = (answers[0].get('text') or '').strip()
        print(f'        answer: {text[:130]}')
        check('the answer is not empty', len(text) > 20, text[:60])

    # The origin allowlist is the only thing stopping another site from opening
    # this socket with a token scraped from the page, so it must actually reject.
    refused = await turn('https://not-allowed.example.com', TOKEN, expect_reject=True)
    check('a foreign origin is refused', refused is None or refused[0].get('type') != 'ready',
          json.dumps(refused[0])[:120] if refused else 'refused')

    bad = await turn(ORIGIN, 'x' * 40, expect_reject=True)
    check('a wrong token is refused', bad is None or bad[0].get('type') != 'ready',
          json.dumps(bad[0])[:120] if bad else 'refused')

    print(f'\n{passed}/{passed + failed} checks passed')
    if failed:
        print('The deployment is not serving turns correctly.')
        return 1
    print('The deployed backend is live: TLS socket, origin allowlist, real grounded turn.')
    return 0


raise SystemExit(asyncio.run(main()))
