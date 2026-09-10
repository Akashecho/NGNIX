/**
 * Microphone capture and streamed speech playback for the voice pipeline.
 *
 * Capture: getUserMedia at whatever rate the device gives, resampled to the
 * 16 kHz mono PCM16 frames the backend forwards to Deepgram or Azure Speech.
 * Playback: the 24 kHz mono PCM16 the backend streams back from Azure Speech
 * neural TTS, queued so segments play gapless and in order.
 *
 * Capture runs through a calibrated noise gate. The first second of audio is
 * measured but not sent, to learn the room's noise floor; after that only audio
 * clearly above that floor is forwarded. Without the gate a fan or a nearby
 * conversation reaches the recogniser, raises spurious speech events and cuts
 * the reply short.
 *
 * An AudioWorklet does the capture so the audio thread is never blocked; the
 * worklet source is inlined as a Blob URL to keep the frontend build-free.
 */

const CAPTURE_RATE = 16000;
const PLAYBACK_RATE = 24000;
// 1024 samples at 16 kHz = 64 ms = 2048 bytes, well under the 6400 byte server cap.
const FRAME_SAMPLES = 1024;

export const GATE_DEFAULTS = {
  // Time spent listening to the room before any audio is sent.
  calibrationMs: 900,
  // The gate opens at openFactor x the measured noise floor and closes at
  // closeFactor x. The spread is hysteresis: it stops the gate chattering on
  // syllable boundaries.
  openFactor: 4,
  closeFactor: 2,
  // Absolute limits, so a silent room does not produce a hair-trigger gate and
  // a very loud room cannot raise the bar above ordinary speech.
  minOpenRms: 0.006,
  maxOpenRms: 0.09,
  // The gate stays open this long after the level drops, so word endings and
  // short pauses mid-sentence are not clipped.
  hangoverMs: 700,
  // Frames held back while the gate is shut and flushed when it opens, so the
  // attack of the first word survives.
  prerollFrames: 3,
};

const WORKLET_SOURCE = `
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const settings = options.processorOptions;
    this.targetRate = settings.targetRate;
    this.frameSamples = settings.frameSamples;
    this.gate = settings.gate;
    this.frameMs = (this.frameSamples / this.targetRate) * 1000;

    this.ratio = sampleRate / this.targetRate;
    this.position = 0;
    // Last sample of the previous block, so interpolation stays continuous
    // across process() boundaries instead of clamping at each block edge.
    this.carry = 0;
    this.pending = new Float32Array(this.frameSamples);
    this.filled = 0;

    this.calibrating = true;
    this.calibrationMs = 0;
    this.noiseSamples = [];
    this.noiseFloor = 0;
    this.openThreshold = this.gate.minOpenRms;
    this.closeThreshold = this.gate.minOpenRms / 2;
    this.open = false;
    this.quietMs = 0;
    this.preroll = [];
    this.paused = false;

    this.port.onmessage = event => {
      if (typeof event.data.paused === 'boolean') {
        this.paused = event.data.paused;
        if (this.paused) this.shut();
      }
      if (event.data.recalibrate) this.beginCalibration();
    };
  }

  beginCalibration() {
    this.calibrating = true;
    this.calibrationMs = 0;
    this.noiseSamples = [];
    this.shut();
  }

  shut() {
    if (this.open) this.port.postMessage({gate: false});
    this.open = false;
    this.quietMs = 0;
    this.preroll.length = 0;
  }

  setThresholds(floor) {
    const clamp = value => Math.min(this.gate.maxOpenRms, Math.max(this.gate.minOpenRms, value));
    this.noiseFloor = floor;
    this.openThreshold = clamp(floor * this.gate.openFactor);
    this.closeThreshold = Math.max(this.gate.minOpenRms / 2, floor * this.gate.closeFactor);
  }

  endCalibration() {
    this.calibrating = false;
    // Median, not mean: one cough during calibration must not raise the floor.
    const sorted = this.noiseSamples.slice().sort((a, b) => a - b);
    const floor = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
    this.setThresholds(Math.max(floor, 1e-5));
    this.port.postMessage({
      calibrated: true, noiseFloor: this.noiseFloor,
      openThreshold: this.openThreshold, closeThreshold: this.closeThreshold,
    });
  }

  emit(samples, rms, peak) {
    const frame = new Int16Array(this.frameSamples);
    for (let i = 0; i < this.frameSamples; i++) {
      const clamped = Math.max(-1, Math.min(1, samples[i]));
      frame[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.port.postMessage({frame: frame.buffer, rms, peak}, [frame.buffer]);
  }

  handleFrame(samples) {
    let sum = 0, peak = 0;
    for (let i = 0; i < this.frameSamples; i++) {
      const value = samples[i];
      sum += value * value;
      const magnitude = Math.abs(value);
      if (magnitude > peak) peak = magnitude;
    }
    const rms = Math.sqrt(sum / this.frameSamples);

    if (this.calibrating) {
      this.noiseSamples.push(rms);
      this.calibrationMs += this.frameMs;
      this.port.postMessage({level: peak, rms, calibrating: true});
      if (this.calibrationMs >= this.gate.calibrationMs) this.endCalibration();
      return;
    }

    this.port.postMessage({level: peak, rms, open: this.open});
    if (this.paused) return;

    if (!this.open) {
      // Track the floor while quiet, so the gate follows a changing room.
      if (rms < this.openThreshold) this.setThresholds(this.noiseFloor * 0.95 + rms * 0.05);
      // Hold recent frames so the first word is not lost when the gate opens.
      this.preroll.push(samples.slice());
      while (this.preroll.length > this.gate.prerollFrames) this.preroll.shift();
      if (rms >= this.openThreshold) {
        this.open = true;
        this.quietMs = 0;
        this.port.postMessage({gate: true});
        for (const held of this.preroll) this.emit(held, rms, peak);
        this.preroll.length = 0;
      }
      return;
    }

    this.quietMs = rms >= this.closeThreshold ? 0 : this.quietMs + this.frameMs;
    if (this.quietMs >= this.gate.hangoverMs) {
      this.shut();
      return;
    }
    this.emit(samples, rms, peak);
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || !channel.length) return true;
    const length = channel.length;
    // Virtual stream is [carry, ...channel]; index 0 is the carried sample.
    const at = index => (index === 0 ? this.carry : channel[index - 1]);
    // Linear interpolation down to the target rate. Cheap, and adequate for
    // speech recognition at 16 kHz. Stop while index+1 is still in range.
    while (this.position < length) {
      const index = Math.floor(this.position);
      const fraction = this.position - index;
      const current = at(index);
      this.pending[this.filled++] = current + (at(index + 1) - current) * fraction;
      if (this.filled === this.frameSamples) {
        this.handleFrame(this.pending);
        this.filled = 0;
      }
      this.position += this.ratio;
    }
    this.carry = channel[length - 1];
    this.position -= length;
    return true;
  }
}
registerProcessor('tars-capture', CaptureProcessor);
`;

let workletUrl;
function workletModuleUrl() {
  workletUrl ||= URL.createObjectURL(new Blob([WORKLET_SOURCE], {type: 'application/javascript'}));
  return workletUrl;
}

export class MicrophoneStream {
  /**
   * @param onFrame       receives an ArrayBuffer of mono PCM16 at 16 kHz
   * @param onLevel       receives 0..100 for the level meter
   * @param onError       receives a human-readable message
   * @param onGate        receives true when your voice opens the gate, false when it shuts
   * @param onCalibrated  receives {noiseFloor, openThreshold} once the room is measured
   * @param gate          overrides for GATE_DEFAULTS
   */
  constructor({onFrame, onLevel = () => {}, onError = () => {}, onGate = () => {},
               onCalibrated = () => {}, gate = {}} = {}) {
    Object.assign(this, {onFrame, onLevel, onError, onGate, onCalibrated});
    this.gate = {...GATE_DEFAULTS, ...gate};
    this.active = false;
    this.calibrating = false;
    this.open = false;
    this.paused = false;
    this.thresholds = null;
    this.framesSent = 0;
    this.framesGated = 0;
  }

  get supported() {
    return Boolean(globalThis.isSecureContext && navigator.mediaDevices?.getUserMedia
      && (globalThis.AudioContext || globalThis.webkitAudioContext));
  }

  async start({noise = true, echo = true, gain = true} = {}) {
    if (this.active) return true;
    if (!this.supported) {
      this.onError('Microphone unavailable. Open this page on HTTPS or localhost in a supported browser.');
      return false;
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {noiseSuppression: {ideal: noise}, echoCancellation: {ideal: echo}, autoGainControl: {ideal: gain},
                channelCount: {ideal: 1}},
        video: false,
      });
      const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
      this.context = new AudioContextClass();
      await this.context.resume();
      await this.context.audioWorklet.addModule(workletModuleUrl());
      this.source = this.context.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.context, 'tars-capture', {
        numberOfInputs: 1, numberOfOutputs: 0,
        processorOptions: {targetRate: CAPTURE_RATE, frameSamples: FRAME_SAMPLES, gate: this.gate},
      });
      this.calibrating = true;
      this.framesSent = this.framesGated = 0;
      this.node.port.onmessage = event => {
        if (!this.active) return;
        const data = event.data;
        if (data.calibrated) {
          this.calibrating = false;
          this.thresholds = {noiseFloor: data.noiseFloor, openThreshold: data.openThreshold,
                             closeThreshold: data.closeThreshold};
          this.onCalibrated(this.thresholds);
          return;
        }
        if (typeof data.gate === 'boolean') {
          this.open = data.gate;
          this.onGate(data.gate);
          return;
        }
        if (data.frame) {
          this.framesSent++;
          this.onFrame(data.frame);
          return;
        }
        // Level-only message: meter still moves while the gate is shut, so a
        // user can see the microphone is alive and that their voice is too quiet.
        if (data.level !== undefined) {
          if (!data.open && !data.calibrating) this.framesGated++;
          this.onLevel(Math.min(100, Math.round(data.level * 140)));
        }
      };
      // The worklet has no outputs, so nothing is ever routed to the speakers:
      // that is what keeps the microphone from feeding back into playback.
      this.source.connect(this.node);
      this.stream.getAudioTracks()[0].onended = () => this.stop();
      this.active = true;
      return true;
    } catch (error) {
      this.stop();
      const messages = {
        NotAllowedError: 'Microphone permission denied. Allow microphone access to speak to TARS.',
        NotFoundError: 'No microphone found. Connect a microphone and try again.',
        NotReadableError: 'Microphone is busy. Close other recording apps and retry.',
      };
      this.onError(messages[error.name] || `Could not start the microphone: ${error.message}`);
      return false;
    }
  }

  /**
   * Stop forwarding audio without dropping the microphone. Used while TARS is
   * speaking so its own voice cannot be transcribed back as a new question.
   */
  pause() {
    if (!this.active || this.paused) return;
    this.paused = true;
    this.node?.port.postMessage({paused: true});
  }

  resume() {
    if (!this.active || !this.paused) return;
    this.paused = false;
    this.node?.port.postMessage({paused: false});
  }

  /** Re-measure the room, e.g. after the user moves or the noise changes. */
  recalibrate() {
    if (!this.active) return;
    this.calibrating = true;
    this.node?.port.postMessage({recalibrate: true});
  }

  stop() {
    this.active = false;
    this.paused = false;
    this.open = false;
    this.calibrating = false;
    if (this.node) { this.node.port.onmessage = null; this.node.disconnect(); }
    this.source?.disconnect();
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {});
    this.node = this.source = this.stream = this.context = null;
    this.onLevel(0);
  }
}

export class SpeechPlayer {
  /**
   * Plays the ordered PCM segments the server streams for one utterance.
   * @param onFinished  called with {turnId, utteranceId} once audio has drained
   */
  constructor({onFinished = () => {}} = {}) {
    this.onFinished = onFinished;
    this.nextStart = 0;
    this.sources = new Set();
    this.current = null;
    this.expected = 0;
    this.played = 0;
    this.ended = false;
  }

  get supported() {
    return Boolean(globalThis.AudioContext || globalThis.webkitAudioContext);
  }

  context_() {
    const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
    this.context ||= new AudioContextClass({sampleRate: PLAYBACK_RATE});
    this.context.resume().catch(() => {});
    return this.context;
  }

  /** Begin a new utterance, discarding anything still queued from the last one. */
  begin({turnId, utteranceId}) {
    if (this.current && (this.current.turnId !== turnId || this.current.utteranceId !== utteranceId)) this.stop();
    this.current = {turnId, utteranceId};
    this.ended = false;
    this.expected = 0;
    this.played = 0;
  }

  /** Enqueue one PCM16 segment. Returns false if playback is unsupported. */
  enqueue(arrayBuffer) {
    if (!this.supported || !this.current || !arrayBuffer.byteLength) return false;
    const context = this.context_();
    const samples = new Int16Array(arrayBuffer);
    const buffer = context.createBuffer(1, samples.length, PLAYBACK_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    // Schedule against a running cursor so consecutive segments abut exactly.
    const startAt = Math.max(context.currentTime + 0.04, this.nextStart);
    source.start(startAt);
    this.nextStart = startAt + buffer.duration;
    this.expected++;
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      source.disconnect();
      this.played++;
      this.settle();
    };
    return true;
  }

  /** The server sent audio_end: no further segments are coming. */
  end() {
    this.ended = true;
    this.settle();
  }

  settle() {
    if (!this.ended || !this.current || this.played < this.expected) return;
    const finished = this.current;
    this.current = null;
    this.nextStart = 0;
    this.onFinished(finished);
  }

  stop() {
    for (const source of this.sources) {
      try { source.onended = null; source.stop(); source.disconnect(); } catch { /* already finished */ }
    }
    this.sources.clear();
    this.current = null;
    this.nextStart = 0;
    this.expected = this.played = 0;
    this.ended = false;
  }

  dispose() {
    this.stop();
    if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {});
    this.context = null;
  }
}
