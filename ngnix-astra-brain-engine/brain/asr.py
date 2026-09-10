"""Streaming microphone input.

Deepgram nova-3 is the default ears. `open_asr` picks the engine for the turn's
language from brain/speech.py and falls over to the next candidate — normally
Azure Speech — when an engine cannot start.
"""

import asyncio
import json
import time
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from .asr_azure import AzureSpeechInput
from .asr_local import LocalWhisperInput
from .speech import DOMAIN_KEYTERMS, stt_plan


class DeepgramInput:
    provider = 'deepgram'

    # Comfortably inside Deepgram's ~10 second idle timeout.
    KEEPALIVE_SECONDS = 5

    def __init__(self, settings, on_transcript, on_speech, on_error, language=None, client=None):
        self.settings = settings
        self.on_transcript, self.on_speech, self.on_error = on_transcript, on_speech, on_error
        # Per-turn language parameter; falls back to the configured default.
        self.language = language or settings.asr_language
        self.socket = None
        self.receiver = None
        self.keepalive = None
        self.parts = []
        self.closed = False
        self.bytes_received = 0
        self.last_sent = time.monotonic()
        self.keyterms = tuple(settings.keyterms)

    def query(self, keyterms=()):
        parameters = {'model': self.settings.asr_model, 'language': self.language,
            'encoding': 'linear16', 'sample_rate': 16000, 'channels': 1,
            'interim_results': 'true', 'smart_format': 'true', 'endpointing': 300,
            'utterance_end_ms': 1000, 'vad_events': 'true'}
        if keyterms:
            # Each term must be its own `keyterm` parameter; doseq handles that.
            parameters['keyterm'] = list(keyterms)
        return urlencode(parameters, doseq=True)

    async def _open(self, keyterms):
        return await connect('wss://api.deepgram.com/v1/listen?' + self.query(keyterms),
            additional_headers={'Authorization': 'Token ' + self.settings.deepgram_key},
            open_timeout=10, ping_interval=20, ping_timeout=20, max_size=256000)

    async def start(self):
        if not self.settings.deepgram_key:
            raise RuntimeError('Deepgram is not configured')
        try:
            self.socket = await self._open(self.keyterms)
        except Exception:
            if not self.keyterms:
                raise
            # Never lose the ears over a vocabulary hint: retry plain. Deepgram
            # rejects the request outright if the keyterm budget is exceeded.
            self.keyterms = ()
            self.socket = await self._open(())
        self.receiver = asyncio.create_task(self._receive())
        self.keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self):
        """Hold the socket open while no audio is being sent.

        Deepgram closes a connection that receives no audio for about ten
        seconds. That is reached routinely in hands-free mode: the microphone is
        muted for the whole of playback, so a reply longer than ten seconds
        silently killed the ears and the next wake word was never heard. A
        KeepAlive frame costs nothing and is not billed as audio.
        """
        try:
            while not self.closed and self.socket:
                await asyncio.sleep(self.KEEPALIVE_SECONDS)
                idle = time.monotonic() - self.last_sent
                if self.closed or not self.socket or idle < self.KEEPALIVE_SECONDS:
                    continue
                await self.socket.send(json.dumps({'type': 'KeepAlive'}))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed keepalive is not worth failing the turn over; the receiver
            # task reports a genuinely dead socket.
            pass

    async def send(self, pcm):
        if self.closed or not self.socket:
            raise RuntimeError('Start audio before sending microphone frames')
        if not pcm or len(pcm) > 6400 or len(pcm) % 2:
            raise ValueError('Send at most 200 ms of mono PCM16 at 16000 Hz per frame')
        self.bytes_received += len(pcm)
        if self.bytes_received > 16000 * 2 * 60:
            raise ValueError('Restart microphone input after 60 seconds')
        self.last_sent = time.monotonic()
        await self.socket.send(pcm)

    async def _flush(self):
        if self.parts:
            text = ' '.join(self.parts).strip()
            self.parts.clear()
            if text:
                await self.on_transcript(text, True)

    async def _receive(self):
        try:
            async for raw in self.socket:
                if not isinstance(raw, str):
                    continue
                event = json.loads(raw)
                kind = event.get('type')
                if kind == 'SpeechStarted':
                    await self.on_speech()
                elif kind == 'Results':
                    alternatives = event.get('channel', {}).get('alternatives', [])
                    text = alternatives[0].get('transcript', '') if alternatives else ''
                    if event.get('is_final') and text:
                        self.parts.append(text)
                    elif text:
                        await self.on_transcript(' '.join([*self.parts, text]), False)
                    if event.get('speech_final') or event.get('from_finalize'):
                        await self._flush()
                elif kind == 'UtteranceEnd':
                    await self._flush()
                elif kind == 'Error':
                    raise RuntimeError('ASR provider returned an error')
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed:
                await self.on_error('Speech recognition disconnected; you can still use text mode.')

    async def stop(self, flush=True):
        self.closed = True
        if self.keepalive and not self.keepalive.done():
            self.keepalive.cancel()
            await asyncio.gather(self.keepalive, return_exceptions=True)
        self.keepalive = None
        if not self.socket:
            return
        try:
            if flush:
                await self.socket.send(json.dumps({'type': 'Finalize'}))
                await self.socket.send(json.dumps({'type': 'CloseStream'}))
                if self.receiver:
                    try:
                        await asyncio.wait_for(asyncio.shield(self.receiver), 3)
                    except TimeoutError:
                        pass
                await self._flush()
        finally:
            if self.receiver and not self.receiver.done():
                self.receiver.cancel()
                await asyncio.gather(self.receiver, return_exceptions=True)
            await self.socket.close()
            self.socket = None


ENGINES = {'deepgram': DeepgramInput, 'azure': AzureSpeechInput, 'local': LocalWhisperInput}


async def open_asr(settings, language, on_transcript, on_speech, on_error, client=None):
    """Start the best available ASR engine for a language.

    Tries each candidate from `stt_plan` in order, so a Deepgram outage or a
    language nova-3 lacks ends up on Azure Speech instead of failing the turn.
    Returns the started engine; its `provider` attribute says which one answered.
    Raises RuntimeError if every candidate fails or none is configured.
    """
    candidates = stt_plan(language, settings)
    if not candidates:
        raise RuntimeError('No speech recognition engine is configured for this language')
    failures = []
    for provider, parameter in candidates:
        engine = ENGINES[provider](settings, on_transcript, on_speech, on_error,
                                   language=parameter, client=client)
        try:
            await engine.start()
            return engine
        except Exception as error:
            failures.append(f'{provider}: {error}')
            try:
                await engine.stop(flush=False)
            except Exception:
                pass
    raise RuntimeError('Speech recognition unavailable (' + '; '.join(failures) + ')')
