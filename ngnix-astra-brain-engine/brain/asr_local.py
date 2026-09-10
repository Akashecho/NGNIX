"""Offline speech recognition with Whisper, via faster-whisper (CTranslate2).

Presents the same surface as DeepgramInput and AzureSpeechInput so Session needs
no branching. Like the Azure client, Whisper is a batch recogniser rather than a
streaming one, so this class does its own endpointing: microphone frames are
buffered, an adaptive energy gate marks where speech starts and stops, and each
closed utterance is transcribed. The trade-off is the same one the Azure path
makes — no interim text while the speaker is mid-sentence.

The model is loaded once per process and shared by every session. Loading is
slow and costs real memory, and a Pi has neither to spare.
"""

import asyncio
import threading

import numpy as np

from .asr_azure import (
    BYTES_PER_SECOND,
    MAXIMUM_THRESHOLD,
    MAXIMUM_UTTERANCE_MS,
    MINIMUM_SPEECH_MS,
    MINIMUM_THRESHOLD,
    SAMPLE_RATE,
    SILENCE_HANGOVER_MS,
    THRESHOLD_FACTOR,
)
from .speech_local import WHISPER_LANGUAGES

# One loaded model per (name, device, compute type), guarded so two sessions
# starting at once cannot load it twice.
_models: dict[tuple, object] = {}
_models_lock = threading.Lock()


def load_model(name: str, device: str = 'cpu', compute_type: str = 'int8', cpu_threads: int = 0):
    """Load and cache a faster-whisper model.

    Raises RuntimeError with an actionable message when the package is missing,
    because that is the most likely failure on a fresh machine.
    """
    key = (name, device, compute_type)
    with _models_lock:
        if key in _models:
            return _models[key]
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            'faster-whisper is not installed. Install the local edition requirements: '
            'pip install -r local/requirements-local.txt'
        ) from error
    model = WhisperModel(name, device=device, compute_type=compute_type, cpu_threads=cpu_threads)
    with _models_lock:
        return _models.setdefault(key, model)


def unload_models():
    """Drop cached models. Used by tests and by a model change at runtime."""
    with _models_lock:
        _models.clear()


# Whisper is conditioned by a short text prefix rather than by a keyterm list, so
# the domain vocabulary is presented as a sentence fragment. It is kept short on
# purpose: a long initial_prompt makes Whisper continue the prompt's style instead
# of transcribing, which shows up as invented text on quiet audio.
MAXIMUM_PROMPT_CHARS = 220


def domain_prompt(keyterms: tuple[str, ...]) -> str | None:
    """Build Whisper's initial_prompt from the configured domain vocabulary.

    Returns None when prompting is disabled (ASR_KEYTERMS empty), matching how the
    Deepgram path treats an empty keyterm list.
    """
    if not keyterms:
        return None
    collected: list[str] = []
    length = 0
    for term in keyterms:
        if length + len(term) + 2 > MAXIMUM_PROMPT_CHARS:
            break
        collected.append(term)
        length += len(term) + 2
    if not collected:
        return None
    return ', '.join(collected) + '.'


class LocalWhisperInput:
    """Microphone sink that transcribes locally with Whisper."""

    provider = 'local-whisper'

    def __init__(self, settings, on_transcript, on_speech, on_error, language='en', client=None):
        # `language` arrives as a Whisper code from the routing plan. Guard it so
        # a bad override cannot reach the model as an unsupported token.
        if language and language not in set(WHISPER_LANGUAGES.values()):
            raise ValueError(f'Whisper has no model for language {language!r}')
        self.settings = settings
        self.on_transcript, self.on_speech, self.on_error = on_transcript, on_speech, on_error
        self.language = language or 'en'
        self.model = None
        self.buffer = bytearray()
        self.speech_ms = 0
        self.silence_ms = 0
        self.speaking = False
        self.closed = False
        self.bytes_received = 0
        self.pending: set[asyncio.Task] = set()
        self.noise_floor = MINIMUM_THRESHOLD / THRESHOLD_FACTOR
        self.initial_prompt = domain_prompt(settings.keyterms)

    @property
    def threshold(self) -> float:
        return min(MAXIMUM_THRESHOLD, max(MINIMUM_THRESHOLD, self.noise_floor * THRESHOLD_FACTOR))

    async def start(self):
        # Loading blocks for seconds on first use, so keep it off the event loop.
        self.model = await asyncio.to_thread(
            load_model,
            self.settings.whisper_model,
            self.settings.whisper_device,
            self.settings.whisper_compute_type,
            self.settings.whisper_cpu_threads,
        )
        self.closed = False

    async def send(self, pcm: bytes):
        if self.closed:
            raise RuntimeError('Start audio before sending microphone frames')
        if not pcm or len(pcm) > 6400 or len(pcm) % 2:
            raise ValueError('Send at most 200 ms of mono PCM16 at 16000 Hz per frame')
        self.bytes_received += len(pcm)
        if self.bytes_received > BYTES_PER_SECOND * 60:
            raise ValueError('Restart microphone input after 60 seconds')

        duration_ms = len(pcm) / 32
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
            self.silence_ms += duration_ms
            self.buffer.extend(pcm)
            if self.silence_ms >= SILENCE_HANGOVER_MS:
                await self._close_utterance()
        else:
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
        task = asyncio.create_task(self._recognize(audio))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    def _transcribe(self, audio: bytes) -> str:
        """Run Whisper on one utterance. Called in a worker thread."""
        samples = np.frombuffer(audio, dtype='<i2').astype(np.float32) / 32768
        segments, _ = self.model.transcribe(
            samples,
            language=self.language,
            beam_size=self.settings.whisper_beam_size,
            # Whisper's counterpart to Deepgram's keyterm prompting. Without it
            # spoken "PACS" comes back as "packs", the retrieval query misses the
            # passage that answers it, and the grounding policy abstains on a
            # question the corpus can answer — the same failure the Azure edition
            # documents for "पैक्स" transcribed as "Bags".
            initial_prompt=self.initial_prompt,
            # The energy gate already removed the silence, and Whisper's own VAD
            # would occasionally discard a short quiet reply like "haan".
            vad_filter=False,
            # Whisper hallucinates fluent text from noise. A low temperature with
            # no fallback, plus these thresholds, makes it return nothing instead.
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        return ' '.join(segment.text.strip() for segment in segments).strip()

    async def _recognize(self, audio: bytes):
        try:
            text = await asyncio.to_thread(self._transcribe, audio)
            if text:
                await self.on_transcript(text, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed:
                await self.on_error('Local speech recognition failed for this phrase. Try again or use text mode.')

    async def stop(self, flush=True):
        self.closed = True
        try:
            if flush:
                # Transcribe whatever is buffered, then wait for in-flight work so
                # its text still reaches the client.
                self.closed = False
                self.silence_ms = max(self.silence_ms, SILENCE_HANGOVER_MS)
                await self._close_utterance()
                self.closed = True
                if self.pending:
                    await asyncio.wait(set(self.pending), timeout=30)
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
