"""Azure Speech recognition for the languages Deepgram nova-3 cannot hear.

nova-3 has no Malayalam or Odia model, and a streaming socket can fail to open
for any language. Both cases land here.

Azure's REST recognition endpoint returns only final results, so this client
does its own endpointing: microphone frames are buffered, an energy gate marks
where speech starts and stops, and each closed utterance is posted for
recognition. That produces the same final-transcript callbacks the Deepgram
client produces, so Session needs no branching — the trade-off is no interim
text while the speaker is mid-sentence.
"""

import asyncio
import re

import httpx
import numpy as np

from .speech import wav_container

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
# Root-mean-square level, on a -1..1 scale, at or above which a frame counts as
# speech. The gate adapts: the floor is estimated from the quiet frames actually
# received, and the threshold is a multiple of it, clamped to this range. A fixed
# threshold either cuts off a quiet speaker or lets a noisy room through.
MINIMUM_THRESHOLD = 0.012
MAXIMUM_THRESHOLD = 0.09
THRESHOLD_FACTOR = 3.5
# Trailing quiet needed to close an utterance, and the least speech worth sending.
SILENCE_HANGOVER_MS = 700
MINIMUM_SPEECH_MS = 320
# One utterance may not exceed the REST endpoint's 60 second limit.
MAXIMUM_UTTERANCE_MS = 55000


class AzureSpeechInput:
    """Microphone sink with the same surface as DeepgramInput."""

    provider = 'azure-speech'

    def __init__(self, settings, on_transcript, on_speech, on_error, language='hi-IN', client=None):
        if not re.fullmatch(r'[a-z]{2,3}-[A-Z]{2}', language):
            raise ValueError('Azure recognition locale must look like ml-IN')
        self.settings = settings
        self.on_transcript, self.on_speech, self.on_error = on_transcript, on_speech, on_error
        self.language = language
        self.client = client
        self.buffer = bytearray()
        self.speech_ms = 0
        self.silence_ms = 0
        self.speaking = False
        self.closed = False
        self.bytes_received = 0
        self.pending: set[asyncio.Task] = set()
        # Starts so that the threshold equals MINIMUM_THRESHOLD, then follows the
        # room upward as quiet frames arrive.
        self.noise_floor = MINIMUM_THRESHOLD / THRESHOLD_FACTOR

    @property
    def threshold(self) -> float:
        return min(MAXIMUM_THRESHOLD, max(MINIMUM_THRESHOLD, self.noise_floor * THRESHOLD_FACTOR))

    @property
    def url(self) -> str:
        return self.endpoint(self.settings.speech_region)

    @staticmethod
    def endpoint(region: str) -> str:
        if not re.fullmatch(r'[a-z0-9-]+', region):
            raise ValueError('Invalid Azure Speech region')
        return f'https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1'

    async def start(self):
        if not self.settings.speech_resources:
            raise RuntimeError('Azure Speech is not configured')
        if self.client is None:
            raise RuntimeError('Azure Speech recognition needs an HTTP client')
        self.closed = False

    async def send(self, pcm: bytes):
        if self.closed:
            raise RuntimeError('Start audio before sending microphone frames')
        if not pcm or len(pcm) > 6400 or len(pcm) % 2:
            raise ValueError('Send at most 200 ms of mono PCM16 at 16000 Hz per frame')
        self.bytes_received += len(pcm)
        if self.bytes_received > BYTES_PER_SECOND * 60:
            raise ValueError('Restart microphone input after 60 seconds')

        duration_ms = len(pcm) / 32  # 16000 samples/s * 2 bytes = 32 bytes per ms
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        level = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0

        if level >= self.threshold:
            if not self.speaking:
                self.speaking = True
                await self.on_speech()
            self.speech_ms += duration_ms
            self.silence_ms = 0
            self.buffer.extend(pcm)
        elif self.speaking:
            # Keep the trailing quiet: word endings live in it.
            self.silence_ms += duration_ms
            self.buffer.extend(pcm)
            if self.silence_ms >= SILENCE_HANGOVER_MS:
                await self._close_utterance()
        else:
            # Quiet frame outside an utterance: let it teach the noise floor.
            # Silence before speech is otherwise discarded, so the audio posted
            # for recognition stays short.
            self.noise_floor = self.noise_floor * 0.9 + level * 0.1

        if self.speech_ms + self.silence_ms >= MAXIMUM_UTTERANCE_MS:
            await self._close_utterance()

    async def _close_utterance(self):
        audio, speech_ms = bytes(self.buffer), self.speech_ms
        self.buffer.clear()
        self.speech_ms = self.silence_ms = 0
        self.speaking = False
        if speech_ms < MINIMUM_SPEECH_MS or not audio:
            return
        # Recognition runs off the receive path so microphone frames are never blocked.
        task = asyncio.create_task(self._recognize(audio))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _recognize(self, audio: bytes):
        body = wav_container(audio, SAMPLE_RATE)
        try:
            payload, last_error = None, None
            # A refusal from the primary region is retried against the fallback
            # resource, if one is configured.
            for key, region in self.settings.speech_resources:
                try:
                    response = await self.client.post(
                        self.endpoint(region),
                        params={'language': self.language, 'format': 'simple', 'profanity': 'raw'},
                        headers={
                            'Ocp-Apim-Subscription-Key': key,
                            'Content-Type': f'audio/wav; codecs=audio/pcm; samplerate={SAMPLE_RATE}',
                            'Accept': 'application/json',
                        },
                        content=body,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except httpx.HTTPError as error:
                    last_error = error
            if payload is None:
                raise last_error or RuntimeError('Azure Speech recognition failed')
            if not isinstance(payload, dict):
                raise ValueError('Unexpected recognition response')
            # RecognitionStatus is Success, NoMatch, InitialSilenceTimeout or Error.
            # Only Success carries text; NoMatch is normal for a cough or a knock.
            if payload.get('RecognitionStatus') == 'Success':
                text = (payload.get('DisplayText') or '').strip()
                if text:
                    await self.on_transcript(text, True)
            elif payload.get('RecognitionStatus') not in (None, 'NoMatch', 'InitialSilenceTimeout', 'BabbleTimeout'):
                raise RuntimeError('Azure Speech returned a recognition error')
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed:
                await self.on_error('Speech recognition failed for this phrase. Try again or use text mode.')

    async def stop(self, flush=True):
        self.closed = True
        try:
            if flush:
                # The speaker released the button: recognise whatever is buffered,
                # then wait for in-flight recognitions so their text still arrives.
                self.closed = False
                self.silence_ms = max(self.silence_ms, SILENCE_HANGOVER_MS)
                await self._close_utterance()
                self.closed = True
                if self.pending:
                    await asyncio.wait(set(self.pending), timeout=8)
        finally:
            self.closed = True
            for task in list(self.pending):
                if not task.done():
                    task.cancel()
            if self.pending:
                await asyncio.gather(*list(self.pending), return_exceptions=True)
            self.buffer.clear()
            self.speech_ms = self.silence_ms = 0
            self.speaking = False
