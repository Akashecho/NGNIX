"""Offline verification for the Azure pipeline.

Exercises the real code paths with a stubbed transport, so the wiring can be
checked without Azure OpenAI, Azure Speech or Deepgram credentials:

  mic frames -> ASR routing/failover -> Azure OpenAI -> Azure Speech TTS -> PCM

Run from ngnix-astra-brain-engine:  python verify_pipeline.py
Exits non-zero on the first failure.
"""

import asyncio
import dataclasses
import json
import time
import os
import struct
import sys
import traceback

import httpx

# Populate the environment before brain.config reads it, and stop it finding the
# repo .env: this harness must not depend on whatever the operator has set.
os.environ.update({
    'BRAIN_SESSION_TOKEN': 'verify-only-token-0123456789abcdef',
    'BRAIN_ALLOWED_ORIGINS': 'http://localhost:8080',
    'LLM_PROVIDER': 'azure',
    'AZURE_OPENAI_ENDPOINT': 'https://verify.openai.azure.com',
    'AZURE_OPENAI_KEY': 'verify-openai-key',
    'AZURE_OPENAI_LLM_DEPLOYMENT': 'gpt-4.1-nano',
    'AZURE_OPENAI_EMBED_DEPLOYMENT': 'text-embedding-3-large',
    'AZURE_SPEECH_KEY': 'verify-speech-key',
    'AZURE_SPEECH_REGION': 'centralindia',
    # Explicitly blank so the operator's real .env cannot leak into this suite.
    'AZURE_SPEECH_KEY_FALLBACK': '',
    'AZURE_SPEECH_REGION_FALLBACK': '',
    'ASR_KEYTERMS': 'auto',
    'DEEPGRAM_API_KEY': 'verify-deepgram-key',
    'STT_PROVIDER': 'auto',
    'ASR_LANGUAGE': 'auto',
    'RAG_REQUIRED': 'false',
    'RAG_ALLOW_ANY_SOURCE': 'true',
    'RAG_CITATION_MODE': 'lenient',
    'ASSISTANT_MODE': 'grounded',
    'TOOLS_ENABLED': 'false',
})

from brain import speech as speech_tables                      # noqa: E402
from brain.asr import DeepgramInput, open_asr                   # noqa: E402
from brain.asr_azure import AzureSpeechInput                    # noqa: E402
from brain.config import Settings                               # noqa: E402
from brain.providers import azure_speech_synthesize, build_providers  # noqa: E402
from brain.voice import RobotVoice, speech_chunks               # noqa: E402

FAILURES = []
CHECKS = 0


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f'  ok   {label}')
    else:
        print(f'  FAIL {label}{" - " + detail if detail else ""}')
        FAILURES.append(label)


def tone(milliseconds, amplitude=9000, sample_rate=16000):
    """Mono PCM16 sine-ish buffer used as fake microphone input."""
    import math
    count = int(sample_rate * milliseconds / 1000)
    return b''.join(struct.pack('<h', int(amplitude * math.sin(i * 0.06))) for i in range(count))

def silence(milliseconds, sample_rate=16000):
    return b'\x00\x00' * int(sample_rate * milliseconds / 1000)


class FakeDeepgramSocket:
    """Stands in for the Deepgram websocket so no network call is made."""

    def __init__(self, url):
        self.url = url
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Deepgram would push results here; this receiver simply idles until the
        # client cancels it during stop().
        await asyncio.sleep(3600)
        raise StopAsyncIteration


async def noop_transcript(text, final):
    pass


async def noop_speech():
    pass


async def noop_error(message):
    pass


def collector(bucket):
    """Async callback that records its argument."""
    async def callback(*args):
        bucket.append(args[0] if len(args) == 1 else args)
    return callback


class Recorder:
    """httpx transport that fakes Azure OpenAI and Azure Speech."""

    def __init__(self, *, recognition='Success', text='PACS ek gaon-star sahkari samiti hai [S1].'):
        self.requests = []
        self.recognition = recognition
        self.text = text

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if '/embeddings' in url:
            payload = json.loads(request.content)
            return httpx.Response(200, json={'data': [
                {'index': index, 'embedding': [0.1, 0.2, 0.3]} for index in range(len(payload['input']))]})
        if '/chat/completions' in url:
            frames = [
                {'choices': [{'delta': {'content': self.text}}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            ]
            body = ''.join(f'data: {json.dumps(frame)}\n\n' for frame in frames) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})
        if 'tts.speech.microsoft.com' in url:
            # 200 ms of 24 kHz mono PCM16.
            return httpx.Response(200, content=b'\x01\x00' * 4800,
                                  headers={'content-type': 'audio/basic'})
        if 'stt.speech.microsoft.com' in url:
            return httpx.Response(200, json={'RecognitionStatus': self.recognition,
                                             'DisplayText': 'മലയാളം പരീക്ഷണം' if self.recognition == 'Success' else ''})
        return httpx.Response(404, json={'error': f'unexpected call to {url}'})


async def verify_routing(settings):
    print('\nLanguage routing (brain/speech.py)')
    check('nova-3 uses code-switching for Hindi',
          speech_tables.deepgram_language('hi', 'auto') == 'multi')
    check('nova-3 uses code-switching for English',
          speech_tables.deepgram_language('en', 'auto') == 'multi')
    check('nova-3 uses a monolingual model for Tamil',
          speech_tables.deepgram_language('ta', 'auto') == 'ta')
    check('nova-3 has no Malayalam model',
          speech_tables.deepgram_language('ml', 'auto') is None)
    check('nova-3 has no Odia model',
          speech_tables.deepgram_language('or', 'auto') is None)
    check('gaps are exactly Malayalam and Odia',
          speech_tables.DEEPGRAM_LANGUAGE_GAPS == ('ml', 'or'),
          str(speech_tables.DEEPGRAM_LANGUAGE_GAPS))
    check('ASR_LANGUAGE override beats the table',
          speech_tables.deepgram_language('ta', 'multi') == 'multi')

    check('Hindi plan is Deepgram first, Azure as failover',
          speech_tables.stt_plan('hi', settings) == [('deepgram', 'multi'), ('azure', 'hi-IN')],
          str(speech_tables.stt_plan('hi', settings)))
    check('Malayalam plan goes straight to Azure Speech',
          speech_tables.stt_plan('ml', settings) == [('azure', 'ml-IN')],
          str(speech_tables.stt_plan('ml', settings)))

    azure_first = dataclasses.replace(settings, stt_provider='azure')
    check('STT_PROVIDER=azure reverses the order',
          speech_tables.stt_plan('hi', azure_first) == [('azure', 'hi-IN'), ('deepgram', 'multi')],
          str(speech_tables.stt_plan('hi', azure_first)))

    pinned = dataclasses.replace(settings, stt_provider='deepgram')
    check('STT_PROVIDER=deepgram still sends Malayalam to Azure',
          speech_tables.stt_plan('ml', pinned) == [('azure', 'ml-IN')],
          str(speech_tables.stt_plan('ml', pinned)))
    check('STT_PROVIDER=deepgram keeps Hindi on Deepgram only',
          speech_tables.stt_plan('hi', pinned) == [('deepgram', 'multi')],
          str(speech_tables.stt_plan('hi', pinned)))

    keyless = dataclasses.replace(settings, deepgram_key='', speech_key='')
    check('no keys means no ASR plan', speech_tables.stt_plan('hi', keyless) == [])

    print('\nVoices (brain/speech.py)')
    check('every UI language has an Azure voice',
          set(speech_tables.AZURE_VOICES) == set(speech_tables.AZURE_STT_LOCALES),
          str(set(speech_tables.AZURE_STT_LOCALES) - set(speech_tables.AZURE_VOICES)))
    import re
    bad = [voice for voice in speech_tables.AZURE_VOICES.values()
           if not re.fullmatch(r'[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural', voice)]
    check('voice names pass the synthesizer validation', not bad, str(bad))
    check('voice_for honours a client override',
          speech_tables.voice_for('hi', 'hi-IN-SwaraNeural') == 'hi-IN-SwaraNeural')
    check('voice_for maps Malayalam', speech_tables.voice_for('ml') == 'ml-IN-MidhunNeural')
    check('voice_for returns empty for an unknown language', speech_tables.voice_for('zz') == '')


async def verify_brain(settings):
    print('\nBrain: Azure OpenAI (stubbed transport)')
    recorder = Recorder()
    async with httpx.AsyncClient(transport=recorder.transport()) as client:
        providers = build_providers(settings, client)
        check('azure provider selected', type(providers).__name__ == 'AzureProviders')
        check('server TTS advertised', providers.supports_tts)

        vectors = await providers.embed(['what is a PACS?', 'crop insurance'])
        check('embeddings returned per input', len(vectors) == 2 and vectors[0] == [0.1, 0.2, 0.3])

        deltas = []
        async def on_delta(value):
            deltas.append(value)
        completion = await providers.complete(
            [{'role': 'user', 'content': 'What is a PACS?'}], None, on_delta)
        check('streamed completion text assembled', completion.text.startswith('PACS ek gaon'), completion.text)
        check('deltas were streamed to the callback', deltas and ''.join(deltas) == completion.text)
        check('no tool calls when none are offered', completion.calls == [])

        request = next(r for r in recorder.requests if '/chat/completions' in str(r.url))
        check('chat call targets the deployment path',
              '/openai/deployments/gpt-4.1-nano/chat/completions' in str(request.url), str(request.url))
        check('chat call sends the api-key header', request.headers.get('api-key') == 'verify-openai-key')
        check('chat call requests streaming', json.loads(request.content)['stream'] is True)


async def verify_mouth(settings):
    print('\nMouth: Azure Speech neural TTS (stubbed transport)')
    recorder = Recorder()
    async with httpx.AsyncClient(transport=recorder.transport()) as client:
        providers = build_providers(settings, client)
        pcm = await providers.synthesize('एक क्षण।', 'hi-IN-MadhurNeural')
        check('PCM returned', len(pcm) == 9600 and len(pcm) % 2 == 0, str(len(pcm)))

        request = next(r for r in recorder.requests if 'tts.speech' in str(r.url))
        check('TTS hits the configured region',
              str(request.url) == 'https://centralindia.tts.speech.microsoft.com/cognitiveservices/v1', str(request.url))
        check('TTS asks for 24 kHz mono PCM',
              request.headers.get('X-Microsoft-OutputFormat') == 'raw-24khz-16bit-mono-pcm')
        check('TTS sends the speech key', request.headers.get('Ocp-Apim-Subscription-Key') == 'verify-speech-key')
        body = request.content.decode()
        check('SSML names the voice', 'hi-IN-MadhurNeural' in body and 'xml:lang="hi-IN"' in body, body)

        for language, voice in speech_tables.AZURE_VOICES.items():
            try:
                await providers.synthesize('test', voice)
            except Exception as error:  # noqa: BLE001
                check(f'{language} voice accepted by the synthesizer', False, str(error))
                break
        else:
            check('all 11 language voices accepted by the synthesizer', True)

        try:
            await providers.synthesize('test', 'not-a-voice')
            check('invalid voice rejected', False)
        except ValueError:
            check('invalid voice rejected', True)

        # The robot colouring runs on the PCM before it reaches the client.
        coloured = RobotVoice(settings.robot_amount).process(pcm)
        check('robot colouring preserves PCM length', len(coloured) == len(pcm))
        check('speech is split into speakable chunks',
              len(speech_chunks('First sentence. ' * 40)) > 1)
        check('citation markers are stripped before speaking',
              '[S1]' not in speech_chunks('A PACS is a society [S1].')[0])

    print('\nMouth: regional failover')
    paired = dataclasses.replace(settings, speech_key_fallback='fallback-key',
                                 speech_region_fallback='eastus')
    check('both regions are offered in order',
          paired.speech_resources == (('verify-speech-key', 'centralindia'), ('fallback-key', 'eastus')),
          str(paired.speech_resources))
    check('no fallback configured means a single resource',
          settings.speech_resources == (('verify-speech-key', 'centralindia'),),
          str(settings.speech_resources))

    class RefusePrimary(Recorder):
        def handle(self, request):
            if 'centralindia' in str(request.url):
                self.requests.append(request)
                return httpx.Response(429, json={'error': 'throttled'})
            return super().handle(request)

    refuser = RefusePrimary()
    async with httpx.AsyncClient(transport=refuser.transport()) as client:
        pcm = await azure_speech_synthesize(paired, client, 'test', 'hi-IN-MadhurNeural')
        check('a throttled primary region falls over to the fallback', len(pcm) == 9600, str(len(pcm)))
        regions = [str(r.url) for r in refuser.requests if 'tts.speech' in str(r.url)]
        check('the fallback region was actually used',
              len(regions) == 2 and 'centralindia' in regions[0] and 'eastus' in regions[1], str(regions))

        heard, problems = [], []
        async def on_transcript(text, final):
            heard.append(text)
        engine = AzureSpeechInput(paired, on_transcript, noop_speech, collector(problems),
                                  language='ml-IN', client=client)
        await engine.start()
        for _ in range(10):
            await engine.send(tone(100))
        await engine.stop()
        check('recognition also falls over to the fallback region', heard == ['മലയാളം പരീക്ഷണം'], str(heard))
        check('the failover raised no user-visible error', problems == [], str(problems))
        stt_regions = [str(r.url) for r in refuser.requests if 'stt.speech' in str(r.url)]
        check('recognition tried the primary region first',
              len(stt_regions) == 2 and 'centralindia' in stt_regions[0] and 'eastus' in stt_regions[1],
              str(stt_regions))

    both_down = Recorder()
    both_down.handle = lambda request: httpx.Response(503, json={'error': 'down'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(both_down.handle)) as client:
        try:
            await azure_speech_synthesize(paired, client, 'test', 'hi-IN-MadhurNeural')
            check('both regions failing raises', False)
        except httpx.HTTPError:
            check('both regions failing raises', True)


async def verify_ears(settings):
    print('\nEars: Azure Speech recognition (stubbed transport)')
    recorder = Recorder()
    async with httpx.AsyncClient(transport=recorder.transport()) as client:
        transcripts, speech_events, errors = [], [], []
        async def on_transcript(text, final):
            transcripts.append((text, final))
        engine = AzureSpeechInput(settings, on_transcript, collector(speech_events), collector(errors),
                                  language='ml-IN', client=client)
        await engine.start()

        # Leading silence, then 1 s of speech, then enough quiet to close the turn.
        for _ in range(3):
            await engine.send(silence(100))
        for _ in range(10):
            await engine.send(tone(100))
        check('speech onset reported once', len(speech_events) == 1, str(speech_events))
        for _ in range(8):
            await engine.send(silence(100))
        await engine.stop()

        check('final transcript delivered', transcripts and transcripts[0][1] is True, str(transcripts))
        check('transcript text passed through',
              transcripts and transcripts[0][0] == 'മലയാളം പരീക്ഷണം', str(transcripts))
        check('no errors raised', errors == [], str(errors))

        request = next(r for r in recorder.requests if 'stt.speech' in str(r.url))
        check('recognition uses the short-audio endpoint',
              str(request.url).startswith('https://centralindia.stt.speech.microsoft.com/speech/recognition/'
                                          'conversation/cognitiveservices/v1'), str(request.url))
        check('recognition requests the Malayalam locale', 'language=ml-IN' in str(request.url), str(request.url))
        check('recognition declares 16 kHz WAV',
              'samplerate=16000' in request.headers.get('Content-Type', ''), request.headers.get('Content-Type', ''))
        check('recognition sends the speech key',
              request.headers.get('Ocp-Apim-Subscription-Key') == 'verify-speech-key')
        audio = request.content
        check('body is a RIFF/WAVE container', audio[:4] == b'RIFF' and audio[8:12] == b'WAVE')
        channels, = struct.unpack('<H', audio[22:24])
        rate, = struct.unpack('<I', audio[24:28])
        declared, = struct.unpack('<I', audio[40:44])
        check('WAV header says 16 kHz mono', rate == 16000 and channels == 1, f'{rate} Hz, {channels} ch')
        check('WAV data length matches the payload', declared == len(audio) - 44, f'{declared} vs {len(audio) - 44}')
        check('leading silence was dropped before upload',
              declared < 16000 * 2 * 2.0, f'{declared} bytes')

        print('\nEars: frame validation')
        fresh = AzureSpeechInput(settings, noop_transcript, noop_speech, noop_error,
                                 language='or-IN', client=client)
        await fresh.start()
        try:
            await fresh.send(b'\x00')
            check('odd-length frame rejected', False)
        except ValueError:
            check('odd-length frame rejected', True)
        try:
            await fresh.send(b'\x00\x00' * 4000)
            check('oversized frame rejected', False)
        except ValueError:
            check('oversized frame rejected', True)
        try:
            AzureSpeechInput(settings, noop_transcript, noop_speech, noop_error, language='malayalam', client=client)
            check('malformed locale rejected', False)
        except ValueError:
            check('malformed locale rejected', True)
        await fresh.stop(flush=False)

        print('\nEars: adaptive noise gate')
        loud = AzureSpeechInput(settings, noop_transcript, noop_speech, noop_error,
                                language='ml-IN', client=client)
        await loud.start()
        check('gate starts at the minimum threshold',
              abs(loud.threshold - 0.012) < 1e-9, f'{loud.threshold:.4f}')
        # A room humming just under the fixed threshold: previously every spike
        # slightly above 0.012 counted as speech.
        for _ in range(30):
            await loud.send(tone(100, amplitude=400))     # RMS about 0.0086
        check('the threshold rose to follow the room', loud.threshold > 0.02, f'{loud.threshold:.4f}')
        check('the threshold stays within its ceiling', loud.threshold <= 0.09, f'{loud.threshold:.4f}')
        check('the room hum never opened an utterance', not loud.speaking)
        check('the room hum was not buffered for upload', len(loud.buffer) == 0, str(len(loud.buffer)))
        # A spike that would have passed the old fixed gate must now be ignored.
        await loud.send(tone(100, amplitude=650))         # RMS about 0.014
        check('a spike above the old fixed threshold is now ignored',
              not loud.speaking and len(loud.buffer) == 0,
              f'speaking={loud.speaking} buffered={len(loud.buffer)}')
        for _ in range(10):
            await loud.send(tone(100, amplitude=12000))   # a voice, clearly above
        check('a real voice still opens an utterance', loud.speaking)
        check('the voice is buffered for upload', len(loud.buffer) > 0, str(len(loud.buffer)))
        await loud.stop(flush=False)

        print('\nEars: NoMatch handling')
        quiet = Recorder(recognition='NoMatch')
        async with httpx.AsyncClient(transport=quiet.transport()) as quiet_client:
            heard, problems = [], []
            nomatch = AzureSpeechInput(settings, collector(heard), noop_speech, collector(problems),
                                       language='or-IN', client=quiet_client)
            await nomatch.start()
            for _ in range(10):
                await nomatch.send(tone(100))
            await nomatch.stop()
            check('NoMatch produces no transcript', heard == [], str(heard))
            check('NoMatch is not treated as an error', problems == [], str(problems))


async def verify_failover(settings):
    print('\nEars: Deepgram nova-3 (stubbed socket)')
    import brain.asr as asr_module

    opened = []
    async def fake_connect(url, **kwargs):
        opened.append((url, kwargs))
        return FakeDeepgramSocket(url)

    original_connect = asr_module.connect
    asr_module.connect = fake_connect
    recorder = Recorder()
    try:
        async with httpx.AsyncClient(transport=recorder.transport()) as client:
            engine = await open_asr(settings, 'ta', noop_transcript, noop_speech, noop_error, client=client)
            check('Tamil chooses Deepgram', engine.provider == 'deepgram', engine.provider)
            check('Tamil uses the nova-3 Tamil model', engine.language == 'ta', engine.language)
            url, kwargs = opened[-1]
            check('socket targets the Deepgram listen endpoint',
                  url.startswith('wss://api.deepgram.com/v1/listen?'), url)
            check('socket requests nova-3', 'model=nova-3' in url, url)
            check('socket requests the Tamil model', 'language=ta&' in url or url.endswith('language=ta'), url)
            check('socket declares 16 kHz linear16 mono',
                  'encoding=linear16' in url and 'sample_rate=16000' in url and 'channels=1' in url, url)
            check('socket asks for interim results and VAD events',
                  'interim_results=true' in url and 'vad_events=true' in url, url)
            check('socket authorises with the Deepgram key',
                  kwargs['additional_headers']['Authorization'] == 'Token verify-deepgram-key')
            check('each keyterm is sent as its own parameter',
                  url.count('keyterm=') == len(settings.keyterms),
                  f'{url.count("keyterm=")} of {len(settings.keyterms)}')
            check('the PACS keyterm is present', 'keyterm=PACS' in url, url)
            check('Devanagari keyterms are percent-encoded',
                  'keyterm=%E0%A4%AA%E0%A5%88%E0%A4%95%E0%A5%8D%E0%A4%B8' in url, url)
            check('no keyterm carries a weight or comma',
                  ':0.' not in url and 'keyterm=PACS%2C' not in url, url)
            await engine.send(tone(100))
            check('frames reach the socket', len(engine.socket.sent) == 1)
            try:
                await engine.send(b'\x00\x00' * 4000)
                check('oversized frame rejected', False)
            except ValueError:
                check('oversized frame rejected', True)

            # Deepgram closes a connection that receives no audio for about ten
            # seconds. Hands-free mode reaches that every reply, because the
            # microphone is muted for the whole of playback, so the socket must be
            # held open explicitly or the next wake word is never heard.
            check('a keepalive task runs alongside the receiver',
                  engine.keepalive is not None and not engine.keepalive.done())
            engine.KEEPALIVE_SECONDS = 0.01
            engine.last_sent -= 5
            before = len(engine.socket.sent)
            await asyncio.sleep(0.08)
            keepalives = [frame for frame in engine.socket.sent[before:]
                          if isinstance(frame, str) and 'KeepAlive' in frame]
            check('an idle socket is sent KeepAlive', bool(keepalives),
                  str(engine.socket.sent[before:][:2]))
            check('KeepAlive is a JSON control frame, not audio',
                  all(json.loads(frame).get('type') == 'KeepAlive' for frame in keepalives))
            # While audio is flowing there is nothing to keep alive, and a
            # keepalive interleaved with speech would be wasted traffic.
            engine.KEEPALIVE_SECONDS = 0.02
            sent_before = len(engine.socket.sent)
            for _ in range(6):
                await engine.send(tone(10))
                await asyncio.sleep(0.01)
            fresh = [frame for frame in engine.socket.sent[sent_before:]
                     if isinstance(frame, str) and 'KeepAlive' in frame]
            check('a busy socket is not sent KeepAlive', not fresh, str(fresh[:2]))

            await engine.stop(flush=False)
            check('stopping cancels the keepalive task', engine.keepalive is None)

            hindi = await open_asr(settings, 'hi', noop_transcript, noop_speech, noop_error, client=client)
            check('Hindi uses nova-3 code-switching', hindi.language == 'multi', hindi.language)
            check('Hindi prefers Deepgram while it is healthy', hindi.provider == 'deepgram', hindi.provider)
            await hindi.stop(flush=False)

            malayalam = await open_asr(settings, 'ml', noop_transcript, noop_speech, noop_error, client=client)
            check('Malayalam bypasses Deepgram entirely', malayalam.provider == 'azure-speech', malayalam.provider)
            await malayalam.stop(flush=False)

            print('\nEars: keyterm prompting degrades safely')
            # Deepgram rejects the whole request if the keyterm budget is blown,
            # so a refused open must retry without them rather than lose the ears.
            attempts = []
            async def picky_connect(url, **kwargs):
                attempts.append(url)
                if 'keyterm=' in url:
                    raise RuntimeError('Keyterm limit exceeded')
                return FakeDeepgramSocket(url)
            asr_module.connect = picky_connect
            try:
                engine = await open_asr(settings, 'ta', noop_transcript, noop_speech, noop_error, client=client)
                check('a rejected keyterm request retries without keyterms',
                      engine.provider == 'deepgram' and len(attempts) == 2
                      and 'keyterm=' in attempts[0] and 'keyterm=' not in attempts[1],
                      f'{len(attempts)} attempts')
                check('the retry keeps the language and model',
                      'model=nova-3' in attempts[1] and 'language=ta' in attempts[1], attempts[1])
                await engine.stop(flush=False)
            finally:
                asr_module.connect = fake_connect

            print('\nEars: keyterms can be disabled')
            plain = dataclasses.replace(settings, keyterms=())
            engine = await open_asr(plain, 'ta', noop_transcript, noop_speech, noop_error, client=client)
            check('ASR_KEYTERMS empty sends no keyterm parameter',
                  'keyterm=' not in opened[-1][0], opened[-1][0])
            await engine.stop(flush=False)

        print('\nFailover: Deepgram outage')
        original_start = DeepgramInput.start
        async def failing_start(self):
            raise RuntimeError('simulated Deepgram outage')
        DeepgramInput.start = failing_start
        try:
            async with httpx.AsyncClient(transport=recorder.transport()) as client:
                engine = await open_asr(settings, 'hi', noop_transcript, noop_speech, noop_error, client=client)
                check('Hindi falls over to Azure Speech', engine.provider == 'azure-speech', engine.provider)
                check('failover uses the Hindi locale', engine.language == 'hi-IN', engine.language)
                await engine.stop(flush=False)

                no_azure = dataclasses.replace(settings, speech_key='')
                try:
                    await open_asr(no_azure, 'hi', noop_transcript, noop_speech, noop_error, client=client)
                    check('outage with no fallback raises', False)
                except RuntimeError as error:
                    check('outage with no fallback raises', 'unavailable' in str(error).lower(), str(error))

                try:
                    await open_asr(no_azure, 'ml', noop_transcript, noop_speech, noop_error, client=client)
                    check('Malayalam without Azure raises', False)
                except RuntimeError as error:
                    check('Malayalam without Azure raises', 'configured' in str(error).lower(), str(error))
        finally:
            DeepgramInput.start = original_start
    finally:
        asr_module.connect = original_connect


async def verify_config():
    print('\nConfiguration')
    settings = Settings.load()
    settings.validate_server()
    check('Azure is the default brain', settings.llm_provider == 'azure')
    check('nova-3 is the default ASR model', settings.asr_model == 'nova-3')
    check('ASR language routing is automatic', settings.asr_language == 'auto')
    check('server TTS enabled by the speech key alone', settings.supports_server_tts)
    check('microphone enabled', settings.supports_microphone)
    check('engines reported in preference order',
          settings.stt_engines == ('deepgram:nova-3', 'azure-speech'), str(settings.stt_engines))
    check('embedding identity is the Azure deployment',
          settings.embedding_id == 'text-embedding-3-large', settings.embedding_id)

    os.environ['STT_PROVIDER'] = 'azure'
    reversed_settings = Settings.load()
    check('azure preference reorders the reported engines',
          reversed_settings.stt_engines == ('azure-speech', 'deepgram:nova-3'), str(reversed_settings.stt_engines))
    os.environ['STT_PROVIDER'] = 'auto'

    os.environ['STT_PROVIDER'] = 'nonsense'
    try:
        Settings.load()
        check('invalid STT_PROVIDER rejected', False)
    except ValueError:
        check('invalid STT_PROVIDER rejected', True)
    os.environ['STT_PROVIDER'] = 'auto'

    os.environ['AZURE_SPEECH_REGION'] = 'Central India'
    try:
        Settings.load()
        check('malformed speech region rejected', False)
    except ValueError:
        check('malformed speech region rejected', True)
    os.environ['AZURE_SPEECH_REGION'] = 'centralindia'

    # AZURE_OPENAI_API_KEY is an accepted alias, so both names must be cleared.
    saved_alias = os.environ.get('AZURE_OPENAI_API_KEY')
    os.environ['AZURE_OPENAI_KEY'] = ''
    os.environ['AZURE_OPENAI_API_KEY'] = ''
    try:
        Settings.load().validate_server()
        check('azure brain without a key rejected', False)
    except ValueError:
        check('azure brain without a key rejected', True)

    os.environ['AZURE_OPENAI_API_KEY'] = 'alias-key'
    check('AZURE_OPENAI_API_KEY works as an alias for AZURE_OPENAI_KEY',
          Settings.load().openai_key == 'alias-key', Settings.load().openai_key)

    os.environ['AZURE_OPENAI_KEY'] = 'verify-openai-key'
    if saved_alias is None:
        os.environ.pop('AZURE_OPENAI_API_KEY', None)
    else:
        os.environ['AZURE_OPENAI_API_KEY'] = saved_alias
    check('AZURE_OPENAI_KEY takes precedence over the alias',
          Settings.load().openai_key == 'verify-openai-key')

    print('\nLocal fallback brain keeps the Azure mouth')
    os.environ['LLM_PROVIDER'] = 'ollama'
    local = Settings.load()
    async with httpx.AsyncClient(transport=Recorder().transport()) as client:
        providers = build_providers(local, client)
        check('ollama provider selected', type(providers).__name__ == 'OllamaProviders')
        check('ollama brain still advertises Azure TTS', providers.supports_tts)
        pcm = await providers.synthesize('test', 'ta-IN-ValluvarNeural')
        check('ollama brain can speak through Azure Speech', len(pcm) == 9600)
    check('local embedding identity differs from Azure',
          local.embedding_id == 'ollama:nomic-embed-text', local.embedding_id)
    os.environ['LLM_PROVIDER'] = 'azure'

    return Settings.load()


async def verify_grounding():
    print('\nGrounding policy (brain/policy.py)')
    from brain.policy import citations_valid
    ids = {'S1', 'S2', 'S3'}
    one = 'A PACS is a society [S1]. It lends to farmers.'
    every = 'A PACS is a society [S1]. It lends to farmers [S2].'

    check('strict rejects a partly cited answer', not citations_valid(one, ids, 'strict'))
    check('strict accepts a fully cited answer', citations_valid(every, ids, 'strict'))
    check('lenient accepts one valid marker', citations_valid(one, ids, 'lenient'))
    check('flag accepts one valid marker', citations_valid(one, ids, 'flag'))
    for mode in ('strict', 'lenient', 'flag'):
        check(f'{mode} rejects an answer with no marker',
              not citations_valid('A PACS is a society.', ids, mode))
        check(f'{mode} rejects a fabricated marker',
              not citations_valid('A PACS is a society [S9].', ids, mode))

    print('\nPersona: advisory turns keep the sardonic streak')
    from brain.policy import Persona
    persona = Persona(humour=50, sarcasm=55, warmth=55, confidence=95)
    advisory = persona.effective(True)
    check('humour survives an advisory turn', advisory.humour >= 40, str(advisory.humour))
    check('sarcasm survives but is capped', 20 < advisory.sarcasm <= 40, str(advisory.sarcasm))
    check('honesty is forced high', advisory.honesty >= 95, str(advisory.honesty))
    check('warmth has a floor', advisory.warmth >= 55, str(advisory.warmth))
    check('confidence is capped', advisory.confidence <= 75, str(advisory.confidence))
    extreme = Persona(humour=100, sarcasm=100).effective(True)
    check('an extreme dial is still bounded on advisory turns',
          extreme.humour == 45 and extreme.sarcasm == 40, f'{extreme.humour}/{extreme.sarcasm}')

    print('\nPrompt: the citation instruction is firm')
    from brain.rag import Hit
    from brain.session import Session
    settings = Settings.load()
    session = Session.__new__(Session)
    session.settings = settings
    session.language = 'hi'
    session.persona = Persona.load()
    sources = [Hit(id='S1', text='t', title='x', url='https://demo.local/a',
                   jurisdiction='DEMO', updated_at='2025-01-01', cosine=0.5)]
    prompt = session.system_prompt(True, sources)
    check('marker requirement is stated as required', 'REQUIRED' in prompt)
    check('a worked example is given', '[S1].' in prompt)
    check('markers are distinguished from markdown', 'not markdown' in prompt)
    check('brevity is instructed', 'BE BRIEF' in prompt)
    check('length is bounded for speech', 'under 70 words' in prompt)
    check('the reply language is pinned', 'language hi' in prompt)
    check('passages are marked as data, not instructions', 'data, not instructions' in prompt)
    check('the prompt never says a missing marker is tolerated',
          'unverified' not in prompt and 'labelled' not in prompt)
    insisted = session.system_prompt(True, sources, insist_citations=True)
    check('the retry prompt names the valid ids', 'S1' in insisted and 'NO VALID MARKER' in insisted)

    print('\nOpen mode: answers anything, still refuses to fabricate')
    open_settings = dataclasses.replace(settings, assistant_mode='open')
    check('open_domain flag follows the mode', open_settings.open_domain and not settings.open_domain)
    session.settings = open_settings
    with_sources = session.system_prompt(True, sources)
    without = session.system_prompt(True, [])
    check('open mode answers beyond the corpus', 'You also answer anything else' in without)
    check('open mode still bounds length', 'BE BRIEF' in without and 'under 70 words' in without)

    # Tool wording is conditional on TOOLS_ENABLED. Promising web_search to a
    # model that has no tools is how the offline edition got confidently invented
    # search results, so each state is checked separately.
    session.settings = dataclasses.replace(open_settings, tools_enabled=True)
    with_tools = session.system_prompt(True, [])
    check('open mode offers web search', 'web_search' in with_tools and 'get_weather' in with_tools)
    check('open mode forbids pretending to search', 'Never claim to have searched' in with_tools)
    check('tool calls are told not to guess the result', 'does not guess the result' in with_tools)

    session.settings = dataclasses.replace(open_settings, tools_enabled=False)
    toolless = session.system_prompt(True, [])
    check('no tools means no web_search offered', 'web_search' not in toolless and 'get_weather' not in toolless)
    check('no tools means the model is told it is offline', 'NO internet access' in toolless)
    check('no tools forbids implying a search happened',
          'Never imply you searched' in toolless)
    session.settings = open_settings

    check('open mode still forbids invented sources',
          'Never invent a source' in without and 'filing confirmation' in without)
    check('open mode still points at the Registrar for personal decisions', 'Registrar' in without)
    check('open mode cites passages when they are supplied',
          '[Snumber]' in with_sources and 'Retrieved passages' in with_sources)
    check('open mode omits citation wording when there are no passages',
          '[Snumber]' not in without and 'Retrieved passages' not in without)
    check('the sardonic register is described', 'sardonic' in without and 'deadpan' in without)
    check('the wit is never aimed at the user', 'never at the user' in without)
    check('distress switches the wit off', 'not optional' in without and 'worried' in without)
    check('register exemplars anchor the tone', 'expensive rectangle' in without)
    check('exemplars are marked as register, not lines to reuse', 'never reuse the lines' in without)
    check('the persona dials reach the prompt', 'sarcasm 55' in without or 'sarcasm 40' in without,
          [line for line in without.split('\n') if 'Current settings' in line][:1])
    check('wrong/right contrast pairs are present', without.count('WRONG:') >= 3 and without.count('RIGHT:') >= 3)
    check('nonsense input is told to ask for a repeat rather than invent a meaning',
          'do not guess a meaning' in without and 'blue fish' in without)
    check('a bare greeting is exempted from the nonsense rule', 'is NOT nonsense' in without)
    check('numeric settings are answered plainly when asked', 'Sarcasm is at 55' in without)
    session.settings = settings

    print('\nOpen mode: tool permissions')
    from brain.tools import Registry, ToolContext
    registry = Registry()
    grounded_ctx = ToolContext(True, 's', 'q', None, dataclasses.replace(settings, tools_enabled=True),
                               None, None, None, None, [])
    open_ctx = ToolContext(True, 's', 'q', None,
                           dataclasses.replace(settings, tools_enabled=True, assistant_mode='open'),
                           None, None, None, None, [])
    check('grounded advisory turns cannot search the web',
          'web_search' not in registry.permitted(grounded_ctx))
    check('open advisory turns can search the web', 'web_search' in registry.permitted(open_ctx))
    check('open advisory turns can read a page', 'fetch_page' in registry.permitted(open_ctx))
    check('open advisory turns can check weather', 'get_weather' in registry.permitted(open_ctx))
    check('a disallowed tool is still refused at dispatch',
          (await registry.dispatch('web_search', '{"query":"x"}', grounded_ctx)).get('error') is not None)

    print('\nOpen mode: page fetching is guarded against SSRF')
    from brain.tools import public_https_url
    for blocked in ('http://example.com', 'https://localhost/x', 'https://127.0.0.1/x',
                    'https://169.254.169.254/latest/meta-data/', 'https://10.0.0.5/x',
                    'https://192.168.1.1/x', 'https://[::1]/x', 'https://example.com:8080/x'):
        check(f'blocked: {blocked}', not await public_https_url(blocked))


async def verify_research(settings):
    print('\nResearch tool: search then read pages (stubbed web)')
    from brain.tools import Registry, ToolContext

    def handler(request):
        url = str(request.url)
        if 'api.search.brave.com' in url:
            return httpx.Response(200, json={'web': {'results': [
                {'title': 'Result one', 'url': 'https://example.com/one', 'description': 'First snippet.'},
                {'title': 'Result two', 'url': 'https://www.iana.org/two', 'description': 'Second snippet.'},
                {'title': 'Blocked host', 'url': 'https://127.0.0.1/three', 'description': 'Should be skipped.'},
            ]}})
        if 'wikipedia.org/w/api.php' in url and 'list=search' in url:
            return httpx.Response(200, json={'query': {'search': [
                {'title': 'Quantum computing', 'snippet': 'A <span class="searchmatch">quantum</span> computer is'},
                {'title': 'Qubit', 'snippet': 'A qubit is the basic unit'},
                {'title': 'Shor algorithm', 'snippet': 'An algorithm for factoring'},
            ]}})
        if 'wikipedia.org/w/api.php' in url and 'prop=extracts' in url:
            return httpx.Response(200, json={'query': {'pages': [
                {'extract': 'A quantum computer processes information using quantum states. ' * 6}]}})
        if 'api.duckduckgo.com' in url:
            return httpx.Response(200, json={
                'Heading': 'Paris', 'AbstractText': 'Paris is the capital of France.',
                'AbstractURL': 'https://example.com/paris', 'Answer': '', 'Definition': '',
                'RelatedTopics': [{'Text': 'Eiffel Tower', 'FirstURL': 'https://www.iana.org/eiffel'}]})
        if 'example.com' in url or 'iana.org' in url:
            return httpx.Response(200, text='<html><body><script>x()</script><p>'
                                            + 'Readable page content about the topic. ' * 12 + '</p></body></html>')
        return httpx.Response(404)

    registry = Registry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        keyed = dataclasses.replace(settings, tools_enabled=True, assistant_mode='open',
                                    search_api_key='brave-test-key')
        ctx = ToolContext(True, 's', 'q', None, keyed, None, None, client, None, [], [], 'en')
        out = await registry.dispatch('research', json.dumps({'query': 'test query'}), ctx)
        check('brave is used when a key is set', out.get('provider') == 'brave', str(out.get('provider')))
        pages = out.get('pages') or []
        check('pages were fetched and read', len(pages) == 2, f'{len(pages)} pages')
        check('page text was extracted', all(len(page['text']) > 100 for page in pages))
        check('script contents were stripped', all('x()' not in page['text'] for page in pages))
        check('a loopback result was skipped',
              all('127.0.0.1' not in page['url'] for page in pages))
        check('one page per host', len({page['url'] for page in pages}) == len(pages))
        check('web sources were recorded for the client', len(ctx.web_sources) == 2, str(ctx.web_sources))
        check('snippets are returned alongside the pages', len(out.get('snippets') or []) == 3)
        check('page text is labelled untrusted', 'untrusted' in out.get('warning', ''))

        keyless = dataclasses.replace(settings, tools_enabled=True, assistant_mode='open', search_api_key='')
        ctx2 = ToolContext(True, 's', 'q', None, keyless, None, None, client, None, [], [], 'en')
        out2 = await registry.dispatch('research', json.dumps({'query': 'quantum computing'}), ctx2)
        check('wikipedia is the keyless default', out2.get('provider') == 'wikipedia', str(out2.get('provider')))
        check('wikipedia search markup is stripped from snippets',
              all('<span' not in item.get('snippet', '') for item in out2.get('snippets') or []))
        check('wikipedia extracts are read as page text',
              any('quantum states' in page['text'] for page in out2.get('pages') or []))
        check('wikipedia urls are built from titles',
              any('/wiki/Quantum_computing' in page['url'] for page in out2.get('pages') or []))

        hindi = ToolContext(True, 's', 'q', None, keyless, None, None, client, None, [], [], 'hi')
        check('a hindi turn searches the hindi wikipedia',
              registry.wiki_host(hindi) == 'hi.wikipedia.org', registry.wiki_host(hindi))
        check('an unsupported language falls back to english wikipedia',
              registry.wiki_host(ToolContext(True, 's', 'q', None, keyless, None, None, client,
                                            None, [], [], 'zz')) == 'en.wikipedia.org')

        ctx3 = ToolContext(True, 's', 'q', None, keyed, None, None, client, None, [], [], 'en')
        out3 = await registry.dispatch('web_search', json.dumps({'query': 'test'}), ctx3)
        check('web_search returns snippets without reading pages',
              out3.get('results') and 'pages' not in out3, str(list(out3)))
        check('max_pages is bounded',
              (await registry.dispatch('research', json.dumps({'query': 'x', 'max_pages': 9}), ctx3)).get('error')
              is not None)


async def main():
    print('TARS Azure pipeline verification (no live credentials used)')
    settings = await verify_config()
    await verify_routing(settings)
    await verify_grounding()
    await verify_research(settings)
    await verify_brain(settings)
    await verify_mouth(settings)
    await verify_ears(settings)
    await verify_failover(settings)

    print(f'\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed')
    if FAILURES:
        print('failed: ' + ', '.join(FAILURES))
        return 1
    print('The pipeline is wired: mic -> Deepgram/Azure Speech -> Azure OpenAI -> Azure Speech TTS -> PCM.')
    print('Live keys are still required for a real turn; this only proves the wiring.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)

