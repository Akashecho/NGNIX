"""Offline text to speech: Piper where a neural voice exists, espeak-ng elsewhere.

Both backends return exactly what the Azure path returns — raw 24 kHz mono
PCM16 — so brain/voice.py can colour it, OrderedTtsPipeline can stream it and
PlaybackClock can time it without knowing which engine spoke.

Neither backend is async, and both are CPU-bound, so every synthesis runs in a
worker thread. On a Pi-class machine a sentence of Piper audio costs a
noticeable fraction of a second; blocking the event loop would stall the
WebSocket and the microphone with it.
"""

import asyncio
from pathlib import Path
import shutil
import struct
import subprocess

import numpy as np

from .speech_local import (
    ESPEAK_PITCH,
    ESPEAK_RATE,
    ESPEAK_VOICES,
    PIPER_VOICES,
    local_tts_plan,
)

TARGET_SAMPLE_RATE = 24000
# One utterance may not exceed 90 seconds of audio, matching the ceiling the
# Azure path enforces on its own responses.
MAXIMUM_SAMPLES = TARGET_SAMPLE_RATE * 90


def parse_wav(data: bytes) -> tuple[bytes, int, int]:
    """Extract (pcm, sample_rate, channels) from a RIFF/WAVE byte string.

    espeak-ng writes a WAV to stdout and Piper's older API writes into a wave
    file object, so both paths hand back a container rather than raw samples.
    Only the two fields that matter are read; unknown chunks are skipped, which
    matters because espeak-ng emits a LIST chunk before the data on some builds.
    """
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('Not a RIFF/WAVE stream')
    offset, sample_rate, channels, bits = 12, 0, 1, 16
    pcm = b''
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        body = data[offset + 8:offset + 8 + size]
        if chunk_id == b'fmt ' and len(body) >= 16:
            _, channels, sample_rate, _, _, bits = struct.unpack('<HHIIHH', body[:16])
        elif chunk_id == b'data':
            pcm = body
        # Chunks are word-aligned: an odd size is followed by a pad byte.
        offset += 8 + size + (size % 2)
    if not pcm or not sample_rate:
        raise ValueError('WAVE stream carries no audio')
    if bits != 16:
        raise ValueError(f'Expected 16-bit samples, got {bits}')
    return pcm, sample_rate, channels


def to_mono_24k(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Downmix to mono and resample to 24 kHz.

    Linear interpolation is deliberate. The sources are 22.05 kHz and the target
    is 24 kHz, a ratio near 1.09, so the resampler is barely stretching the
    signal and a windowed-sinc filter would cost CPU on a Pi for a difference
    nothing survives the robot filter to hear.
    """
    if len(pcm) % 2:
        raise ValueError('PCM must contain complete signed 16-bit samples')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    if channels > 1:
        usable = len(samples) - (len(samples) % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    if not len(samples):
        return b''
    if sample_rate != TARGET_SAMPLE_RATE:
        count = int(round(len(samples) * TARGET_SAMPLE_RATE / sample_rate))
        if count <= 0:
            return b''
        # endpoint=False keeps successive chunks phase-continuous, so a sentence
        # split across syntheses does not click at the seam.
        positions = np.linspace(0, len(samples), count, endpoint=False)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    if len(samples) > MAXIMUM_SAMPLES:
        raise ValueError('Synthesised audio exceeds the 90 second ceiling')
    return np.clip(samples, -32768, 32767).astype('<i2').tobytes()


class PiperEngine:
    """Neural Piper voices loaded from local .onnx files.

    Voices are cached per name: loading one costs an ONNX session, and a
    conversation that switches language mid-way would otherwise pay it twice.
    """

    # The engine root, i.e. the folder holding the `brain` package. A relative
    # PIPER_MODEL_DIR is resolved against it rather than against the working
    # directory, so the voices are found whether the process was started from the
    # repo root, from the engine folder, or by a test harness.
    ENGINE_ROOT = Path(__file__).resolve().parent.parent

    def __init__(self, model_directory: str):
        given = Path(model_directory)
        if given.is_absolute():
            self.directory = given
        else:
            # Prefer the working directory if the voices are genuinely there,
            # so an operator can still point at a local folder deliberately.
            self.directory = given if given.is_dir() else self.ENGINE_ROOT / given
        self._voices: dict[str, object] = {}
        self._lock = asyncio.Lock()

    def model_path(self, voice: str) -> Path:
        return self.directory / f'{voice}.onnx'

    def available(self, voice: str) -> bool:
        return self.model_path(voice).is_file() and self.model_path(voice).with_suffix('.onnx.json').is_file()

    def _load(self, voice: str):
        from piper import PiperVoice

        path = self.model_path(voice)
        if not path.is_file():
            raise RuntimeError(
                f'Piper voice {voice} is missing from {self.directory}. '
                'Run local/get_models.ps1 to download it.'
            )
        return PiperVoice.load(str(path))

    def _synthesize(self, loaded, text: str) -> bytes:
        """Render text with whichever Piper API this version exposes.

        piper-tts 1.3 streams AudioChunk objects; earlier releases write into a
        wave file. Supporting both keeps a pip upgrade from breaking the voice.
        """
        if hasattr(loaded, 'synthesize') and not hasattr(loaded, 'synthesize_wav'):
            parts = []
            for chunk in loaded.synthesize(text):
                audio = getattr(chunk, 'audio_int16_bytes', None)
                if audio is None:
                    array = np.asarray(getattr(chunk, 'audio_float_array', []), dtype=np.float32)
                    audio = np.clip(array * 32767, -32768, 32767).astype('<i2').tobytes()
                parts.append(audio)
                rate = getattr(chunk, 'sample_rate', None) or loaded.config.sample_rate
            return to_mono_24k(b''.join(parts), rate)

        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(loaded.config.sample_rate)
            if hasattr(loaded, 'synthesize_wav'):
                loaded.synthesize_wav(text, handle)
            else:
                loaded.synthesize(text, handle)
        pcm, rate, channels = parse_wav(buffer.getvalue())
        return to_mono_24k(pcm, rate, channels)

    async def synthesize(self, text: str, voice: str) -> bytes:
        if voice not in self._voices:
            async with self._lock:
                if voice not in self._voices:
                    self._voices[voice] = await asyncio.to_thread(self._load, voice)
        loaded = self._voices[voice]
        return await asyncio.to_thread(self._synthesize, loaded, text)


class EspeakEngine:
    """espeak-ng, the universal fallback and the only local Odia voice."""

    # The Windows installer does not put espeak-ng on PATH, and the Homebrew and
    # Debian layouts differ again, so a PATH miss is checked against the places
    # each platform actually installs it before giving up.
    WELL_KNOWN = (
        r'C:\Program Files\eSpeak NG\espeak-ng.exe',
        r'C:\Program Files (x86)\eSpeak NG\espeak-ng.exe',
        '/usr/bin/espeak-ng',
        '/usr/local/bin/espeak-ng',
        '/opt/homebrew/bin/espeak-ng',
    )

    def __init__(self, binary: str = 'espeak-ng'):
        self.binary = binary
        self._resolved: str | None = None

    @property
    def path(self) -> str | None:
        if self._resolved:
            return self._resolved
        found = shutil.which(self.binary)
        if not found:
            for candidate in self.WELL_KNOWN:
                if Path(candidate).is_file():
                    found = candidate
                    break
        self._resolved = found
        return found

    def available(self, voice: str = '') -> bool:
        return self.path is not None

    def _run(self, text: str, voice: str) -> bytes:
        executable = self.path
        if not executable:
            raise RuntimeError(
                'espeak-ng is not installed. On Debian/Raspberry Pi OS: '
                'sudo apt install espeak-ng. On Windows: winget install eSpeak-NG.eSpeak-NG'
            )
        # Text goes on stdin, never interpolated into the command line, so a
        # reply containing quotes or shell metacharacters cannot alter the call.
        result = subprocess.run(
            [executable, '-v', voice, '-s', str(ESPEAK_RATE), '-p', str(ESPEAK_PITCH), '--stdout'],
            input=text.encode('utf-8'), capture_output=True, timeout=60, check=False,
        )
        if result.returncode != 0 or not result.stdout:
            detail = result.stderr.decode('utf-8', 'replace').strip()[:200]
            raise RuntimeError(f'espeak-ng failed for voice {voice}: {detail or "no audio"}')
        pcm, rate, channels = parse_wav(result.stdout)
        return to_mono_24k(pcm, rate, channels)

    async def synthesize(self, text: str, voice: str) -> bytes:
        return await asyncio.to_thread(self._run, text, voice)


class LocalTts:
    """Local mouth with the same call signature as azure_speech_synthesize.

    Chooses the engine from the voice name so a per-turn client override still
    works: anything Piper has a model for goes to Piper, anything that looks
    like an espeak voice goes to espeak-ng.
    """

    def __init__(self, settings):
        self.settings = settings
        self.piper = PiperEngine(getattr(settings, 'piper_model_dir', 'models/piper'))
        self.espeak = EspeakEngine(getattr(settings, 'espeak_binary', 'espeak-ng'))
        self.prefer = getattr(settings, 'tts_local_engine', 'auto')

    @property
    def espeak_voices(self) -> set[str]:
        return set(ESPEAK_VOICES.values())

    def resolve(self, voice: str) -> tuple[str, str]:
        """Map a voice name to the engine that owns it."""
        if voice in PIPER_VOICES.values() or voice.count('-') >= 2:
            return 'piper', voice
        if voice in self.espeak_voices or '+' in voice or len(voice) <= 6:
            return 'espeak', voice
        return 'piper', voice

    async def synthesize(self, text: str, voice: str) -> bytes:
        if not voice:
            raise ValueError('No local voice is configured for this language')
        engine, name = self.resolve(voice)
        if engine == 'piper':
            if self.piper.available(name):
                return await self.piper.synthesize(text, name)
            # A missing model must not silence the robot: fall back to espeak-ng
            # in the same language rather than dropping the turn's audio.
            language = name.split('_')[0]
            fallback = ESPEAK_VOICES.get(language)
            if fallback and self.espeak.available():
                return await self.espeak.synthesize(text, fallback)
            raise RuntimeError(
                f'Piper voice {name} is not downloaded and espeak-ng is unavailable as a fallback'
            )
        return await self.espeak.synthesize(text, name)

    def status(self) -> dict:
        """What the local mouth can actually do right now, for /health."""
        piper_ready = sorted(
            language for language, voice in PIPER_VOICES.items() if self.piper.available(voice)
        )
        espeak_ready = self.espeak.available()
        spoken = set(piper_ready) | (set(ESPEAK_VOICES) if espeak_ready else set())
        return {
            'engine_preference': self.prefer,
            'piper_voices_installed': piper_ready,
            'espeak_available': espeak_ready,
            'languages_spoken': sorted(spoken),
        }


def local_voice_plan(language: str, prefer: str = 'auto'):
    """Re-exported so callers need only import this module."""
    return local_tts_plan(language, prefer)
