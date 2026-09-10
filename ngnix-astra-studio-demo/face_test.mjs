/**
 * Tests the TARS face without a browser.
 *
 *   node face_test.mjs
 *
 * face.mjs touches the DOM, so this stubs the handful of APIs it uses and then
 * runs the real module: the state machine, the level smoothing, the per-state
 * geometry and the PCM level helper are all exercised as written. What cannot be
 * checked here is whether it looks right, only that it draws the correct shapes
 * for each state and never emits invalid canvas calls.
 */

const calls = [];

// face.mjs draws inside a translate/scale transform, so the raw coordinates it
// passes are not where anything lands. The stub tracks the vertical part of the
// transform and records transformed values, otherwise the bounds checks below
// would be measuring the wrong numbers and would pass while the face hung off
// the edge of the canvas.
let ty = 0, sy = 1;
const transformStack = [];

function makeContext() {
  const record = name => (...args) => calls.push({name, args});
  // Map a y in current user space to device space: y' = ty + y * sy.
  const mapY = y => ty + y * sy;
  const recordPoint = name => (x, y, ...rest) => calls.push({name, args: [x, mapY(y), ...rest]});
  return {
    fillStyle: '', globalAlpha: 1, strokeStyle: '', lineWidth: 1,
    clearRect: record('clearRect'),
    fillRect: (x, y, w, h) => calls.push({name: 'fillRect', args: [x, mapY(y), w, h * sy]}),
    beginPath: record('beginPath'), closePath: record('closePath'),
    moveTo: recordPoint('moveTo'), arcTo: (x1, y1, x2, y2, r) =>
      calls.push({name: 'arcTo', args: [x1, mapY(y1), x2, mapY(y2), r]}),
    arc: (x, y, r, a, b) => calls.push({name: 'arc', args: [x, mapY(y), r, a, b]}),
    fill: record('fill'), stroke: record('stroke'), clip: record('clip'),
    save: () => { transformStack.push([ty, sy]); calls.push({name: 'save', args: []}); },
    restore: () => { if (transformStack.length) [ty, sy] = transformStack.pop(); calls.push({name: 'restore', args: []}); },
    translate: (x, y) => { ty += y * sy; calls.push({name: 'translate', args: [x, y]}); },
    scale: (x, y) => { sy *= y; calls.push({name: 'scale', args: [x, y]}); },
    createLinearGradient: () => ({addColorStop: record('addColorStop')}),
  };
}

const canvas = {
  width: 132, height: 76, dataset: {},
  attributes: {},
  getContext: () => context,
  getBoundingClientRect: () => ({width: 132, height: 76}),
  setAttribute(name, value) { this.attributes[name] = value; },
};
const context = makeContext();

// Minimal DOM surface used by face.mjs.
globalThis.performance ??= {now: () => Date.now()};
globalThis.devicePixelRatio = 1;
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.matchMedia = () => ({matches: false});
globalThis.getComputedStyle = () => ({getPropertyValue: name => ({
  '--face-ink': '#241d18', '--face-glow': '#b5542e', '--face-dim': 'rgba(0,0,0,.2)',
}[name] ?? '')});
globalThis.document = {hidden: false, addEventListener: () => {}, removeEventListener: () => {}};

const {TarsFace, pcmLevel} = await import('./face.mjs');

let passed = 0, failed = 0;
function check(label, condition, detail = '') {
  if (condition) { passed += 1; console.log(`  ok   ${label}`); }
  else { failed += 1; console.log(`  FAIL ${label}${detail ? ` -- ${detail}` : ''}`); }
}

const face = new TarsFace(canvas);

console.log('Face: state machine');
check('starts idle', face.state === 'IDLE');
face.setState('LISTENING');
check('accepts a known state', face.state === 'LISTENING');
check('publishes the state to the DOM for CSS', canvas.dataset.state === 'LISTENING');
check('keeps the accessible label in step',
  canvas.attributes['aria-label'] === 'TARS is listening', canvas.attributes['aria-label']);
face.setState('NONSENSE');
check('ignores an unknown state', face.state === 'LISTENING');
face.setLevel(0.8);
face.setState('THINKING');
check('clears a stale level when leaving an audio state', face.targetLevel === 0);

console.log('\nFace: level clamping');
for (const [input, expected] of [[0.5, 0.5], [-1, 0], [2, 1], [NaN, 0], ['x', 0], [null, 0]]) {
  face.setLevel(input);
  check(`level ${JSON.stringify(input)} becomes ${expected}`, face.targetLevel === expected,
    String(face.targetLevel));
}

console.log('\nFace: geometry per state');
// Panels must be more open when listening than when merely armed, and nearly
// shut when offline: that difference is the whole expression.
const openness = {};
for (const name of ['OFFLINE', 'ARMED', 'IDLE', 'THINKING', 'SPEAKING', 'LISTENING']) {
  face.state = name;
  face.level = 0;
  face.phase = 0;
  openness[name] = face.panelOpenness();
}
check('offline is the most closed', openness.OFFLINE < openness.ARMED);
check('armed is more closed than idle', openness.ARMED < openness.IDLE);
check('listening is the most open', openness.LISTENING > openness.SPEAKING);
check('every openness is a sane fraction',
  Object.values(openness).every(value => value > 0 && value <= 1.1), JSON.stringify(openness));

face.state = 'LISTENING';
face.level = 0.9;
const loud = face.barExtent();
face.level = 0.05;
const quiet = face.barExtent();
check('the voice bar follows the level', loud > quiet, `${loud} vs ${quiet}`);
check('the bar never exceeds the track',
  ['IDLE','ARMED','LISTENING','THINKING','SPEAKING','OFFLINE'].every(name => {
    face.state = name;
    for (const level of [0, 0.5, 1]) { face.level = level; if (face.barExtent() > 1) return false; }
    return true;
  }));

console.log('\nFace: drawing');
for (const variant of ['panels', 'eyes']) {
  face.setVariant(variant);
  check(`variant ${variant} is selected`, face.variant === variant);
  for (const name of ['IDLE', 'ARMED', 'LISTENING', 'THINKING', 'SPEAKING', 'OFFLINE']) {
    calls.length = 0;
    face.state = name;
    face.draw();
    const drew = calls.filter(call => call.name === 'fill' || call.name === 'fillRect'
      || call.name === 'stroke' || call.name === 'arc').length;
    check(`${variant}/${name} draws something`, drew >= 3, `${drew} paint calls`);
    const bad = calls.filter(call => call.args.some(value => typeof value === 'number' && !Number.isFinite(value)));
    check(`${variant}/${name} emits no NaN geometry`, bad.length === 0, JSON.stringify(bad.slice(0, 2)));
  }
}
face.setVariant('nonsense');
check('an unknown variant is ignored', face.variant === 'eyes', face.variant);
face.setVariant('panels');

console.log('\nFace: everything stays inside the canvas');
// Guards the vertical drop: moving the face down must never push the voice bar
// or an eye off the bottom edge, at any state, variant or aspect ratio.
for (const [w, h] of [[132, 76], [640, 360], [66, 38], [420, 120], [200, 400]]) {
  canvas.width = w; canvas.height = h;
  let worst = null;
  for (const variant of ['panels', 'eyes']) {
    face.setVariant(variant);
    for (const name of ['IDLE', 'ARMED', 'LISTENING', 'THINKING', 'SPEAKING', 'OFFLINE']) {
      face.state = name;
      for (const level of [0, 1]) {
        face.level = level;
        face.blink = 0;
        calls.length = 0;
        face.draw();
        for (const call of calls) {
          // roundedRect goes through moveTo/arcTo; fillRect carries x,y,w,h.
          if (call.name === 'moveTo' || call.name === 'arcTo' || call.name === 'arc') {
            const [x, y] = call.args;
            if (y < -0.5 || y > h + 0.5 || x < -0.5 || x > w + 0.5) worst = {variant, name, call: call.name, x, y};
          }
          if (call.name === 'fillRect') {
            const [x, y, cw, ch] = call.args;
            if (y + ch > h + 0.5 || y < -0.5 || x + cw > w + 0.5) worst = {variant, name, call: 'fillRect', y, ch};
          }
        }
      }
    }
  }
  check(`${w}x${h}: nothing is drawn outside the canvas`, worst === null, JSON.stringify(worst));
}
canvas.width = 132; canvas.height = 76;
face.setVariant('panels');
face.level = 0;

console.log('\nFace: the vertical shift');
// Measured with the eyes wide, so the drawn panel fills its box and its top edge
// is the box top: 16% natural + 25% shift = 41% of the height.
function drawnRange(state, variant) {
  face.setVariant(variant);
  face.state = state;
  face.blink = 0;
  face.level = 0;
  calls.length = 0;
  face.draw();
  const ys = calls.filter(c => ['moveTo','arcTo','arc','fillRect'].includes(c.name))
    .flatMap(c => c.name === 'fillRect' ? [c.args[1], c.args[1] + c.args[3]]
      : c.name === 'arcTo' ? [c.args[1], c.args[3]] : [c.args[1]]);
  return {top: Math.min(...ys), bottom: Math.max(...ys)};
}

const panels = drawnRange('LISTENING', 'panels');
check('panels start at about 41% (16% natural + 25% shift)',
  Math.abs(panels.top / 76 - 0.41) < 0.03, `top at ${(panels.top / 76 * 100).toFixed(1)}%`);
check('panels are scaled to reach the bottom limit, not past it',
  panels.bottom <= 76 * 0.965, `bottom at ${(panels.bottom / 76 * 100).toFixed(1)}%`);

const eyes = drawnRange('LISTENING', 'eyes');
check('eyes are shifted down too',
  eyes.top / 76 > 0.34, `top at ${(eyes.top / 76 * 100).toFixed(1)}%`);
check('eyes stay inside the bottom limit',
  eyes.bottom <= 76 * 0.965, `bottom at ${(eyes.bottom / 76 * 100).toFixed(1)}%`);
face.setVariant('panels');
face.level = 0;

console.log('\nFace: gaze');
face.state = 'IDLE';
face.gaze = 0;
face.gazeTarget = 0;
face.nextGaze = 1;
face.step(50);
check('idle gaze wanders', face.gazeTarget !== 0 || face.nextGaze > 1);
face.state = 'LISTENING';
face.gazeTarget = 0.9;
face.step(50);
check('being addressed centres the gaze', face.gazeTarget === 0);
for (let index = 0; index < 200; index += 1) face.step(33);
check('the gaze eases to its target', Math.abs(face.gaze) < 0.02, String(face.gaze));

console.log('\nFace: blinking');
face.state = 'IDLE';
face.blink = 0;
face.nextBlink = 1;
face.step(50);
check('idle blinks eventually', face.blink > 0);
face.state = 'SPEAKING';
face.blink = 0;
face.nextBlink = 1;
face.step(50);
check('speaking never blinks mid-sentence', face.blink === 0);

console.log('\nFace: level smoothing');
face.state = 'LISTENING';
face.level = 0;
face.setLevel(1);
face.step(33);
check('a jump is eased, not snapped', face.level > 0 && face.level < 1, String(face.level));
for (let index = 0; index < 60; index += 1) face.step(33);
check('it converges on the target', Math.abs(face.level - 1) < 0.01, String(face.level));

console.log('\nFace: PCM level helper');
function pcm(values) {
  const buffer = new ArrayBuffer(values.length * 2);
  new Int16Array(buffer).set(values);
  return buffer;
}
check('silence reads zero', pcmLevel(pcm([0, 0, 0, 0])) === 0);
check('full scale reads one', Math.abs(pcmLevel(pcm([32767, -32768])) - 1) < 0.001);
check('half scale reads about a half', Math.abs(pcmLevel(pcm([16384])) - 0.5) < 0.01);
check('an empty buffer is safe', pcmLevel(new ArrayBuffer(0)) === 0);
check('a null buffer is safe', pcmLevel(null) === 0);
check('an odd-length buffer is safe', pcmLevel(new ArrayBuffer(3)) >= 0);
check('a long buffer is sampled, not scanned',
  pcmLevel(pcm(new Array(200000).fill(8192))) > 0.2);

face.destroy();
check('destroy stops the loop', face.running === false);

console.log(`\n${passed}/${passed + failed} face checks passed`);
process.exit(failed ? 1 : 0);
