/**
 * Unit test for the calibrated noise gate in voice.mjs.
 *
 * Runs the real AudioWorklet processor in Node by stubbing the worklet globals,
 * then feeds synthetic audio: room noise, speech, a door slam. Checks that the
 * floor is measured, that noise is withheld, that speech is forwarded with its
 * attack intact, and that a short pause does not end the utterance.
 *
 *   node voice_test.mjs
 */

import {readFileSync} from 'node:fs';
import {GATE_DEFAULTS} from './voice.mjs';

const source = readFileSync(new URL('./voice.mjs', import.meta.url), 'utf8');
const workletSource = source.split('const WORKLET_SOURCE = `')[1].split('\n`;')[0];

globalThis.sampleRate = 48000;
let Processor;
globalThis.registerProcessor = (name, cls) => { Processor = cls; };
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = {
      postMessage: (data, transfer) => this.__sink(data, transfer),
      onmessage: null,
    };
  }
};
new Function(workletSource)();

const BLOCK = 128;
const FRAME_MS = (1024 / 16000) * 1000;   // 64 ms per emitted frame
let failures = 0, checks = 0;

function check(label, condition, detail = '') {
  checks++;
  console.log(`  ${condition ? 'ok  ' : 'FAIL'} ${label}${detail ? ` - ${detail}` : ''}`);
  if (!condition) failures++;
}

function makeProcessor(gate = {}) {
  const settings = {...GATE_DEFAULTS, ...gate};
  const processor = new Processor({processorOptions: {targetRate: 16000, frameSamples: 1024, gate: settings}});
  const events = {frames: [], gates: [], calibrated: null, levels: 0};
  processor.__sink = data => {
    if (data.calibrated) events.calibrated = data;
    else if (typeof data.gate === 'boolean') events.gates.push(data.gate);
    else if (data.frame) events.frames.push(new Int16Array(data.frame));
    else if (data.level !== undefined) events.levels++;
  };
  return {processor, events, settings};
}

/** Feed `ms` of audio at the given RMS amplitude, in 128-sample blocks. */
function feed(processor, ms, amplitude, {tone = 220} = {}) {
  const total = Math.round(48000 * ms / 1000);
  let phase = processor.__phase || 0;
  for (let start = 0; start < total; start += BLOCK) {
    const block = new Float32Array(Math.min(BLOCK, total - start));
    for (let i = 0; i < block.length; i++) {
      // Sine for speech-like energy, plus a little hiss so RMS is realistic.
      block[i] = amplitude * Math.SQRT2 * Math.sin(2 * Math.PI * tone * phase / 48000)
               + (Math.random() - 0.5) * amplitude * 0.1;
      phase++;
    }
    processor.process([[block]]);
  }
  processor.__phase = phase;
}

console.log('Calibrated noise gate\n');

// --- quiet room -------------------------------------------------------------
{
  console.log('A quiet room, then speech');
  const {processor, events, settings} = makeProcessor();
  feed(processor, 1200, 0.002);                       // room hum during calibration
  check('calibration completed', events.calibrated !== null);
  check('no audio sent while calibrating', events.frames.length === 0, `${events.frames.length} frames`);
  check('measured floor is near the hum',
        events.calibrated.noiseFloor > 0.001 && events.calibrated.noiseFloor < 0.004,
        events.calibrated.noiseFloor.toFixed(5));
  check('threshold sits above the absolute minimum',
        events.calibrated.openThreshold >= settings.minOpenRms,
        events.calibrated.openThreshold.toFixed(5));
  check('threshold is a multiple of the measured floor',
        Math.abs(events.calibrated.openThreshold - events.calibrated.noiseFloor * settings.openFactor) < 1e-6,
        `${events.calibrated.noiseFloor.toFixed(5)} x ${settings.openFactor} = ${events.calibrated.openThreshold.toFixed(5)}`);

  const beforeSpeech = events.frames.length;
  feed(processor, 600, 0.002);                        // more hum after calibration
  check('continuing hum is still withheld', events.frames.length === beforeSpeech,
        `${events.frames.length - beforeSpeech} frames`);

  feed(processor, 800, 0.05);                         // speech
  check('speech opens the gate', events.gates[0] === true, JSON.stringify(events.gates));
  check('speech is forwarded', events.frames.length > 8, `${events.frames.length} frames`);
  const preroll = events.frames.length - Math.floor(800 / FRAME_MS);
  check('pre-roll frames were flushed so the attack is intact',
        preroll >= 1 && preroll <= settings.prerollFrames + 1, `${preroll} extra frames`);
}

// --- noisy room -------------------------------------------------------------
{
  console.log('\nNear silence is clamped, so the gate is not hair-trigger');
  const {processor, events, settings} = makeProcessor();
  feed(processor, 1200, 0.0002);                      // anechoic-quiet input
  check('floor is essentially zero', events.calibrated.noiseFloor < 0.001,
        events.calibrated.noiseFloor.toFixed(6));
  check('threshold clamped up to the absolute minimum',
        events.calibrated.openThreshold === settings.minOpenRms,
        events.calibrated.openThreshold.toFixed(5));
  const before = events.frames.length;
  feed(processor, 400, 0.003);                        // a very faint rustle
  check('a faint rustle below the minimum stays out', events.frames.length === before,
        `${events.frames.length - before} frames`);
}

// --- noisy room -------------------------------------------------------------
{
  console.log('\nA noisy room: the floor rises, quiet noise stays out');
  const {processor, events} = makeProcessor();
  feed(processor, 1200, 0.02);                        // loud fan during calibration
  check('floor tracks the louder room',
        events.calibrated.noiseFloor > 0.015, events.calibrated.noiseFloor.toFixed(5));
  check('threshold rose above the absolute minimum',
        events.calibrated.openThreshold > GATE_DEFAULTS.minOpenRms,
        events.calibrated.openThreshold.toFixed(5));

  const before = events.frames.length;
  feed(processor, 600, 0.02);                         // same fan continues
  check('the fan alone never opens the gate', events.frames.length === before,
        `${events.frames.length - before} frames`);

  feed(processor, 700, 0.15);                         // voice over the fan
  check('a voice above the fan opens the gate', events.frames.length > before,
        `${events.frames.length - before} frames`);
}

// --- hangover ---------------------------------------------------------------
{
  console.log('\nA pause mid-sentence does not end the utterance');
  const {processor, events} = makeProcessor();
  feed(processor, 1000, 0.002);
  feed(processor, 400, 0.06);                         // "what is"
  const gatesAfterFirst = events.gates.length;
  feed(processor, 300, 0.002);                        // 300 ms pause, under hangover
  check('gate stays open through a short pause', events.gates.length === gatesAfterFirst,
        JSON.stringify(events.gates));
  feed(processor, 400, 0.06);                         // "a PACS"
  check('still one continuous utterance', events.gates.filter(Boolean).length === 1,
        JSON.stringify(events.gates));
  feed(processor, 900, 0.002);                        // long silence, over hangover
  check('gate shuts after the hangover', events.gates[events.gates.length - 1] === false,
        JSON.stringify(events.gates));
}

// --- pause while TARS speaks ------------------------------------------------
{
  console.log('\nPausing while TARS speaks blocks its own voice');
  const {processor, events} = makeProcessor();
  feed(processor, 1000, 0.002);
  processor.port.onmessage({data: {paused: true}});
  const before = events.frames.length;
  feed(processor, 900, 0.2);                          // loud playback bleeding in
  check('nothing is forwarded while paused', events.frames.length === before,
        `${events.frames.length - before} frames`);
  check('the meter still reports level while paused', events.levels > 0);
  processor.port.onmessage({data: {paused: false}});
  feed(processor, 500, 0.08);
  check('audio resumes after unpausing', events.frames.length > before,
        `${events.frames.length - before} frames`);
}

// --- recalibration ----------------------------------------------------------
{
  console.log('\nRecalibrating adapts to a changed room');
  const {processor, events} = makeProcessor();
  feed(processor, 1000, 0.002);
  const quietThreshold = events.calibrated.openThreshold;
  processor.port.onmessage({data: {recalibrate: true}});
  feed(processor, 1200, 0.03);                        // air conditioning came on
  check('a second calibration ran', events.calibrated.openThreshold !== quietThreshold
        || events.calibrated.noiseFloor > 0.02, events.calibrated.noiseFloor.toFixed(5));
  check('the new threshold is higher', events.calibrated.openThreshold > quietThreshold,
        `${quietThreshold.toFixed(5)} -> ${events.calibrated.openThreshold.toFixed(5)}`);
}

// --- frame format is unchanged ---------------------------------------------
{
  console.log('\nFrame format still matches what the server accepts');
  const {processor, events} = makeProcessor();
  feed(processor, 1000, 0.002);
  feed(processor, 500, 0.08);
  const frame = events.frames[0];
  check('frames are 1024 samples', frame.length === 1024, String(frame.length));
  check('frames are 2048 bytes, under the 6400 byte cap', frame.byteLength === 2048, String(frame.byteLength));
  check('samples are within PCM16 range',
        frame.every(value => value >= -32768 && value <= 32767));
}

console.log(`\n${checks - failures}/${checks} gate checks passed`);
process.exit(failures ? 1 : 0);
