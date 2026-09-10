import asyncio
from collections import deque
from dataclasses import dataclass, field
import math
import re
import time

import numpy as np


def speech_chunks(text: str, maximum: int = 240) -> list[str]:
    text = re.sub(r'\[S\d+\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    chunks = []
    while text:
        if len(text) <= maximum:
            chunks.append(text)
            break
        boundary = max(text.rfind(mark, 14, maximum) for mark in ('. ', '? ', '! ', '। '))
        if boundary >= 14:
            boundary += 1
        else:
            boundary = text.rfind(' ', 14, maximum)
        if boundary < 14:
            boundary = maximum
        chunks.append(text[:boundary].strip())
        text = text[boundary:].strip()
    return chunks


class OrderedTtsPipeline:
    def __init__(self, synthesize, prefetch: int = 2):
        if not 1 <= prefetch <= 2:
            raise ValueError('At most two syntheses may be in flight')
        self.synthesize, self.prefetch = synthesize, prefetch

    async def stream(self, chunks):
        iterator = iter(chunks)
        pending = deque()
        def enqueue():
            try:
                text = next(iterator)
            except StopIteration:
                return False
            pending.append(asyncio.create_task(self.synthesize(text)))
            return True
        for _ in range(self.prefetch):
            if not enqueue():
                break
        current = None
        try:
            while pending:
                current = pending.popleft()
                pcm = await current
                current = None
                enqueue()
                yield pcm
        finally:
            tasks = list(pending) + ([current] if current is not None else [])
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


class RobotVoice:
    RUMBLE_WIDTH = 241
    PRESENCE_WIDTH = 9
    HISTORY = 288
    RING_HZ = 110.0
    LFO_HZ = 0.7
    CHORUS_BASE_MS = 4.0
    CHORUS_DEPTH_MS = 1.6

    def __init__(self, amount: float = .18, sample_rate: int = 24000,
                 clarity: float = .6, autotune: float = .35):
        if not 0 <= amount <= .5:
            raise ValueError('Keep robotic colouring between 0 and 0.5')
        if not 0 <= clarity <= 1.5:
            raise ValueError('Keep clarity between 0 and 1.5')
        if not 0 <= autotune <= 1:
            raise ValueError('Keep autotune between 0 and 1')
        self.amount, self.sample_rate = amount, sample_rate
        self.clarity, self.autotune = clarity, autotune
        self.ring_phase = 0
        self.lfo_phase = 0
        self.makeup = 1.06 / (1 - .45 * amount)
        self.history = np.zeros(self.HISTORY, dtype=np.float64)
        self.shaped_history = np.zeros(self.HISTORY, dtype=np.float64)

    @staticmethod
    def _moving_average(signal, width):
        totals = np.cumsum(np.concatenate(([0.0], signal)))
        return (totals[width:] - totals[:-width]) / width

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError('PCM must contain complete signed 16-bit samples')
        if not pcm:
            return pcm
        if not (self.amount or self.clarity or self.autotune):
            return pcm

        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768
        count = len(samples)

        padded = np.concatenate((self.history, samples))
        rumble = self._moving_average(padded, self.RUMBLE_WIDTH)[-count:]
        presence = self._moving_average(padded, self.PRESENCE_WIDTH)[-count:]
        voiced = (samples - rumble) + self.clarity * (samples - presence)

        index = np.arange(count) + self.ring_phase
        oscillator = np.cos(2 * math.pi * self.RING_HZ * index / self.sample_rate)
        shaped = voiced * (1 - self.amount + self.amount * oscillator)
        pre_mix = shaped

        if self.autotune:
            lfo = np.sin(2 * math.pi * self.LFO_HZ * (np.arange(count) + self.lfo_phase) / self.sample_rate)
            delay = (self.CHORUS_BASE_MS + self.CHORUS_DEPTH_MS * lfo) * self.sample_rate / 1000
            shaped_padded = np.concatenate((self.shaped_history, pre_mix))
            positions = self.HISTORY + np.arange(count) - delay
            detuned = np.interp(positions, np.arange(len(shaped_padded)), shaped_padded)
            mix = .5 * self.autotune
            shaped = pre_mix * (1 - mix) + detuned * mix

        drive = 1 + self.amount * .6
        coloured = np.tanh(shaped * self.makeup * drive) / drive

        self.history = padded[-self.HISTORY:].copy()
        self.shaped_history = np.concatenate((self.shaped_history, pre_mix))[-self.HISTORY:].copy()
        self.ring_phase = (self.ring_phase + count) % self.sample_rate
        self.lfo_phase = (self.lfo_phase + count) % (self.sample_rate * 100)
        return np.clip(coloured * 32768, -32768, 32767).astype('<i2').tobytes()


@dataclass
class PlaybackClock:
    utterance_id: str
    deadline: float = 0
    final_sent: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def enqueue(self, byte_count: int, now: float | None = None):
        if byte_count < 0 or byte_count % 2:
            raise ValueError('Invalid PCM length')
        now = time.monotonic() if now is None else now
        self.deadline = max(now, self.deadline) + byte_count / (24000 * 2)

    def acknowledge(self, utterance_id: str):
        if utterance_id == self.utterance_id and self.final_sent:
            self.done.set()

    async def wait(self):
        try:
            await asyncio.wait_for(self.done.wait(), max(0, self.deadline - time.monotonic()) + .25)
        except TimeoutError:
            pass
