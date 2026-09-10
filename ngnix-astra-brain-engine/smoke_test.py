"""End-to-end smoke test for the brain engine.

Opens a real WebSocket session, authenticates, asks one question and prints every
event until the turn completes. Run the server first, then:

    python smoke_test.py "What is a PACS?"

Set SMOKE_LANGUAGE=hi for a Hindi turn. Set SMOKE_SPEAK=1 to request Azure Speech
neural TTS, which additionally checks the streamed audio: each JSON `audio`
header must be followed by one binary PCM frame, and the client acknowledges each
finished utterance with `playback_done` the way the browser does.
"""
import asyncio
import json
import os
import sys

from websockets.asyncio.client import connect

from brain.config import Settings

QUESTION = sys.argv[1] if len(sys.argv) > 1 else 'What is a PACS?'
LANGUAGE = os.getenv('SMOKE_LANGUAGE', 'en')
SPEAK = os.getenv('SMOKE_SPEAK', '').lower() in {'1', 'true', 'yes'}


async def main() -> int:
    settings = Settings.load()
    url = os.getenv('SMOKE_URL', 'ws://127.0.0.1:8000/ws/session')
    print(f'connecting to {url}')
    async with connect(url, max_size=4_000_000) as socket:
        await socket.send(json.dumps({'type': 'auth', 'token': settings.session_token}))

        ready = json.loads(await asyncio.wait_for(socket.recv(), 15))
        if ready.get('type') != 'ready':
            print(f'FAIL: expected ready, got {ready}')
            return 1
        print(f"ready: corpus_loaded={ready.get('corpus_loaded')} server_tts={ready.get('server_tts')} "
              f"stt={ready.get('stt_engines')}")
        if SPEAK and not ready.get('server_tts'):
            print('FAIL: SMOKE_SPEAK requested but the server reports no voice (set AZURE_SPEECH_KEY)')
            return 1

        await socket.send(json.dumps({'type': 'text', 'text': QUESTION, 'language': LANGUAGE, 'speak': SPEAK}))
        print(f'asked: {QUESTION}{" (spoken)" if SPEAK else ""}\n')

        answer, failed = None, None
        # Audio arrives as a JSON header immediately followed by one binary frame.
        pending_header, audio_bytes, segments, unpaired = None, 0, 0, 0
        utterances = []
        while True:
            raw = await asyncio.wait_for(socket.recv(), 180)
            if not isinstance(raw, str):
                if pending_header is None:
                    unpaired += 1
                    print(f'  <unpaired binary frame: {len(raw)} bytes>')
                    continue
                expected = pending_header.get('byte_length')
                match = 'ok' if expected == len(raw) else f'MISMATCH expected {expected}'
                print(f"  audio seq={pending_header['sequence']} purpose={pending_header['purpose']} "
                      f"{len(raw)} bytes {pending_header['duration_ms']:.0f} ms [{match}]")
                if expected != len(raw):
                    unpaired += 1
                audio_bytes += len(raw)
                segments += 1
                pending_header = None
                continue
            event = json.loads(raw)
            kind = event.get('type')
            if kind == 'audio':
                pending_header = event
            elif kind == 'audio_end':
                print(f"  audio_end: {event['segments']} segments, purpose={event['purpose']}")
                utterances.append(event)
                # Acknowledge playback so the server stops waiting on the clock.
                await socket.send(json.dumps({'type': 'playback_done', 'turn_id': event['turn_id'],
                                              'utterance_id': event['utterance_id']}))
            elif kind == 'state':
                print(f"  state -> {event['state']}")
            elif kind == 'retrieval':
                print(f"  retrieval: {event['source_count']} sources in {event['elapsed_ms']}ms")
            elif kind == 'voice_unavailable':
                failed = event
                print(f"  VOICE UNAVAILABLE: {event['message']}")
            elif kind == 'response':
                answer = event
                print(f"\n  mode={event['mode']} grounded={event['grounded']} elapsed={event['elapsed_ms']}ms")
                print(f"  sources={[s['title'] for s in event.get('sources', [])]}")
                print(f"\nANSWER:\n{event['text']}\n")
            elif kind == 'error':
                failed = event
                print(f"  ERROR {event['code']}: {event['message']}")
            elif kind == 'turn_done':
                print(f"  turn_done in {event['elapsed_ms']}ms")
                break

    if failed:
        print('RESULT: FAIL (server reported an error)')
        return 1
    if not answer or not answer['text'].strip():
        print('RESULT: FAIL (empty answer)')
        return 1
    if SPEAK:
        seconds = audio_bytes / (24000 * 2)
        print(f'  spoken: {segments} segments, {audio_bytes} bytes, {seconds:.2f} s of 24 kHz mono PCM16')
        if not segments:
            print('RESULT: FAIL (speak requested but no audio was streamed)')
            return 1
        if unpaired:
            print(f'RESULT: FAIL ({unpaired} audio frames were unpaired or the wrong length)')
            return 1
        if not utterances:
            print('RESULT: FAIL (no audio_end event)')
            return 1
        if seconds < 1:
            print(f'RESULT: FAIL (only {seconds:.2f} s of audio for a {len(answer["text"])} character answer)')
            return 1
    print('RESULT: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
