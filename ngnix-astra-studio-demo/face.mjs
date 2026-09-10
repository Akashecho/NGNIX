/**
 * The TARS face: two eye panels and a voice bar, drawn on a canvas.
 *
 * TARS has no face in the film — it is a rectangular slab with a bar display —
 * so this keeps to the vocabulary already used in the sidebar markup (`▰ ▰`
 * over `━`) rather than inventing cartoon eyes. The panels carry the expression
 * through their height, brightness and spacing; the bar carries the voice.
 *
 * Everything is driven by state the pipeline already emits, so the face is a
 * readout rather than decoration:
 *
 *   ARMED     waiting for the wake word: half-lidded, alternating pulse
 *   LISTENING microphone open: panels wide, bar tracks your voice level
 *   THINKING  a scanning sweep across both panels
 *   SPEAKING  bar driven by the amplitude of the audio being played
 *   IDLE      slow breathing
 *   OFFLINE   nearly shut, unlit
 *
 * Colours come from CSS custom properties, so it follows the light and dark
 * themes without knowing about either.
 */

const STATES = new Set(['IDLE', 'ARMED', 'LISTENING', 'THINKING', 'SPEAKING', 'OFFLINE']);

// Two looks, same state machine and same geometry rules.
//
//   panels  Film-accurate. TARS is a slab with a bar display, so this is two
//           rectangular panels over a voice bar — the shape the sidebar markup
//           already used (`▰ ▰` over `━`).
//   eyes    Reads as a face to most people: rounded lids with a pupil that
//           glances and a mouth bar. Less faithful to the film, warmer.
const VARIANTS = new Set(['panels', 'eyes']);

// The face is decorative in the strict sense — nothing depends on it — so it
// runs at 30fps rather than 60 and stops entirely when the tab is hidden. On a
// Pi-class device that difference is worth having.
const FRAME_MS = 1000 / 30;

// How far down to move the whole face, as a fraction of the canvas height.
//
// A pure translation of 25% does not fit: the face spans 62% of the height
// (panels) or 71% (eyes), so moving it down 25% would push the voice bar past the
// bottom edge. So the face is translated *and* scaled to fit the space that
// remains below the shift — it ends up lower and very slightly shorter, which is
// what "shift it down 25%" has to mean in a fixed box.
const FACE_SHIFT = 0.25;

// The natural extent of each variant, as drawn, used to work out the fit scale.
const SPANS = {
  panels: {top: 0.16, bottom: 0.78},
  eyes: {top: 0.15, bottom: 0.86},
};

// The face may not extend past this fraction of the height.
const BAR_BOTTOM_LIMIT = 0.96;

export class TarsFace {
  constructor(canvas, options = {}) {
    if (!canvas) throw new Error('TarsFace needs a canvas element');
    this.canvas = canvas;
    this.context = canvas.getContext('2d');
    this.state = 'IDLE';
    this.level = 0;          // 0..1, smoothed
    this.targetLevel = 0;
    this.phase = 0;          // seconds, drives breathing and sweeps
    this.blink = 0;          // 1 = fully shut
    this.nextBlink = 2000 + Math.random() * 3000;
    this.lastFrame = 0;
    this.running = false;
    this.ratio = 1;
    this.reduceMotion = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    this.onStateChange = options.onStateChange ?? null;
    this.variant = VARIANTS.has(options.variant) ? options.variant : 'panels';
    // Where the pupil is looking, in the 'eyes' variant. Drifts while idle and
    // centres when addressed, so the face feels attentive rather than blank.
    this.gaze = 0;
    this.gazeTarget = 0;
    this.nextGaze = 900;
    this.resize();
    this._onResize = () => this.resize();
    this._onVisibility = () => (document.hidden ? this.stop() : this.start());
    globalThis.addEventListener?.('resize', this._onResize);
    document.addEventListener?.('visibilitychange', this._onVisibility);
  }

  /** Match the backing store to the CSS size so the face is not blurry. */
  resize() {
    const ratio = Math.min(globalThis.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect?.() ?? {width: 0, height: 0};
    const width = Math.max(1, Math.round((rect.width || 132) * ratio));
    const height = Math.max(1, Math.round((rect.height || 76) * ratio));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.ratio = ratio;
  }

  setState(next) {
    if (!STATES.has(next) || next === this.state) return;
    this.state = next;
    // Do not carry a stale level into a state that should look immediate.
    if (next !== 'SPEAKING' && next !== 'LISTENING') this.targetLevel = 0;
    this.canvas.dataset.state = next;
    this.canvas.setAttribute('aria-label', `TARS is ${next.toLowerCase()}`);
    this.onStateChange?.(next);
  }

  setVariant(next) {
    if (!VARIANTS.has(next)) return;
    this.variant = next;
    this.canvas.dataset.variant = next;
  }

  /** Input or playback level, 0..1. Anything outside that is clamped. */
  setLevel(level) {
    const value = Number(level);
    this.targetLevel = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.lastFrame = performance.now();
    const loop = now => {
      if (!this.running) return;
      const delta = now - this.lastFrame;
      if (delta >= FRAME_MS) {
        this.lastFrame = now;
        this.step(delta);
        this.draw();
      }
      this.frame = requestAnimationFrame(loop);
    };
    this.frame = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = null;
  }

  destroy() {
    this.stop();
    globalThis.removeEventListener?.('resize', this._onResize);
    document.removeEventListener?.('visibilitychange', this._onVisibility);
  }

  step(delta) {
    this.phase += delta / 1000;
    // Ease towards the target so a spiky RMS reading does not make the bar jitter.
    this.level += (this.targetLevel - this.level) * 0.25;

    // Gaze: drifts while idle, locks forward when being spoken to. Only the
    // 'eyes' variant draws it, but tracking it always keeps the two in step if
    // the variant is switched at runtime.
    if (this.state === 'LISTENING' || this.state === 'SPEAKING') {
      this.gazeTarget = 0;
    } else if (!this.reduceMotion) {
      this.nextGaze -= delta;
      if (this.nextGaze <= 0) {
        this.gazeTarget = (Math.random() - 0.5) * 1.4;
        this.nextGaze = 1200 + Math.random() * 2600;
      }
    }
    this.gaze += (this.gazeTarget - this.gaze) * 0.08;

    if (this.reduceMotion) { this.blink = 0; return; }
    // Blink only when idle or waiting; blinking mid-sentence reads as a glitch.
    if (this.state === 'IDLE' || this.state === 'ARMED') {
      this.nextBlink -= delta;
      if (this.nextBlink <= 0) {
        this.blink = 1;
        this.nextBlink = 2600 + Math.random() * 4200;
      }
    }
    this.blink = Math.max(0, this.blink - delta / 140);
  }

  /** Resolve a themed colour, falling back when the property is absent. */
  colour(name, fallback) {
    const value = getComputedStyle(this.canvas).getPropertyValue(name).trim();
    return value || fallback;
  }

  draw() {
    const ctx = this.context;
    const width = this.canvas.width;
    const height = this.canvas.height;
    ctx.clearRect(0, 0, width, height);

    const palette = {
      ink: this.colour('--face-ink', '#2b2118'),
      glow: this.colour('--face-glow', '#c9662b'),
      dim: this.colour('--face-dim', 'rgba(43,33,24,.22)'),
    };

    // The shift is applied as a transform rather than baked into every
    // coordinate, so each variant still draws in its own natural layout and only
    // one place decides where the face sits.
    const span = SPANS[this.variant] ?? SPANS.panels;
    const top = span.top * height;
    const natural = (span.bottom - span.top) * height;
    const shift = FACE_SHIFT * height;
    const available = BAR_BOTTOM_LIMIT * height - (top + shift);
    const scale = natural > 0 ? Math.min(1, Math.max(0.1, available / natural)) : 1;

    ctx.save();
    // y' = shift + top + (y - top) * scale
    ctx.translate(0, shift + top * (1 - scale));
    ctx.scale(1, scale);
    if (this.variant === 'eyes') this.drawEyes(width, height, palette);
    else this.drawPanels(width, height, palette);
    ctx.restore();
  }

  drawPanels(width, height, {ink, glow, dim}) {
    const ctx = this.context;
    // Two panels side by side, over a full-width bar.
    const margin = width * 0.08;
    const gap = width * 0.08;
    const panelWidth = (width - margin * 2 - gap) / 2;
    const panelMax = height * 0.46;
    const barHeight = Math.max(2 * this.ratio, height * 0.1);
    const barTop = height * 0.68;

    const panelHeight = Math.max(1, panelMax * this.panelOpenness() * (1 - this.blink));
    const panelTop = height * 0.16 + (panelMax - panelHeight) / 2;

    for (let index = 0; index < 2; index += 1) {
      const x = margin + index * (panelWidth + gap);
      ctx.fillStyle = this.panelFill(index, ink, glow, dim);
      this.roundedRect(x, panelTop, panelWidth, panelHeight, Math.min(4 * this.ratio, panelHeight / 2));
      ctx.fill();
    }

    // Thinking sweep: a bright band travelling across both panels.
    if (this.state === 'THINKING' && !this.reduceMotion) {
      const bandWidth = width * 0.16;
      const travel = (this.phase * 0.9) % 1;
      const x = -bandWidth + travel * (width + bandWidth * 2);
      const gradient = ctx.createLinearGradient(x, 0, x + bandWidth, 0);
      gradient.addColorStop(0, 'rgba(255,255,255,0)');
      gradient.addColorStop(0.5, glow);
      gradient.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.save();
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = gradient;
      ctx.fillRect(margin, panelTop, width - margin * 2, panelHeight);
      ctx.restore();
    }

    this.drawBar(margin, barTop, width - margin * 2, barHeight, glow, dim);
  }

  /**
   * The warmer look: two lidded eyes with a pupil, over a mouth bar. The lid is
   * drawn by clipping the eye to the open fraction of its height, so a blink and
   * a squint are the same operation at different amounts.
   */
  drawEyes(width, height, {ink, glow, dim}) {
    const ctx = this.context;
    const margin = width * 0.09;
    const gap = width * 0.12;
    const eyeWidth = (width - margin * 2 - gap) / 2;
    const eyeMax = height * 0.5;
    const barHeight = Math.max(2 * this.ratio, height * 0.08);
    const barTop = height * 0.78;

    const openness = Math.max(0.05, this.panelOpenness() * (1 - this.blink));
    const eyeHeight = Math.max(1, eyeMax * openness);
    const centreY = height * 0.4;
    const lit = this.state === 'LISTENING' || this.state === 'SPEAKING' || this.state === 'THINKING';
    const shell = this.state === 'OFFLINE' ? dim : (lit ? glow : ink);
    const radius = Math.min(eyeHeight / 2, eyeWidth / 2);

    for (let index = 0; index < 2; index += 1) {
      const x = margin + index * (eyeWidth + gap);
      const top = centreY - eyeHeight / 2;

      // Socket, clipped to the lid so the pupil is cut off as the eye closes.
      ctx.save();
      this.roundedRect(x, top, eyeWidth, eyeHeight, radius);
      ctx.clip();
      ctx.fillStyle = this.state === 'OFFLINE' ? dim : 'rgba(0,0,0,.12)';
      ctx.fillRect(x, top, eyeWidth, eyeHeight);

      if (this.state !== 'OFFLINE') {
        const pupil = Math.min(eyeWidth, eyeMax) * (0.24 + this.level * 0.08);
        const pupilX = x + eyeWidth / 2 + this.gaze * eyeWidth * 0.22;
        ctx.fillStyle = shell;
        ctx.beginPath();
        ctx.arc(pupilX, centreY, Math.max(1, pupil), 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      // Lid outline, so the eye still reads when nearly shut.
      ctx.strokeStyle = shell;
      ctx.lineWidth = Math.max(1, 1.4 * this.ratio);
      this.roundedRect(x, top, eyeWidth, eyeHeight, radius);
      ctx.stroke();
    }

    this.drawBar(margin, barTop, width - margin * 2, barHeight, glow, dim);
  }

  drawBar(x, y, width, height, glow, dim) {
    const ctx = this.context;
    ctx.fillStyle = dim;
    this.roundedRect(x, y, width, height, height / 2);
    ctx.fill();
    const active = width * this.barExtent();
    if (active > 0.5) {
      ctx.fillStyle = glow;
      this.roundedRect(x, y, active, height, height / 2);
      ctx.fill();
    }
  }

  /** How open the eye panels are, 0..1, per state. */
  panelOpenness() {
    const breathe = this.reduceMotion ? 0 : Math.sin(this.phase * 1.1) * 0.06;
    switch (this.state) {
      case 'OFFLINE': return 0.12;
      case 'ARMED': return 0.42 + breathe;
      case 'LISTENING': return 0.9 + this.level * 0.1;
      case 'THINKING': return 0.66;
      case 'SPEAKING': return 0.78 + this.level * 0.16;
      default: return 0.6 + breathe;
    }
  }

  panelFill(index, ink, glow, dim) {
    if (this.state === 'OFFLINE') return dim;
    if (this.state === 'LISTENING' || this.state === 'SPEAKING') return glow;
    if (this.state === 'ARMED') {
      // Alternating faint pulse, so "waiting for the wake word" is legible at a
      // glance without being distracting.
      const pulse = this.reduceMotion ? 0.5 : (Math.sin(this.phase * 1.6 + index * Math.PI) + 1) / 2;
      return pulse > 0.6 ? glow : dim;
    }
    return ink;
  }

  /** How much of the voice bar is lit, 0..1. */
  barExtent() {
    switch (this.state) {
      case 'OFFLINE': return 0;
      case 'LISTENING': return Math.max(0.06, this.level);
      case 'SPEAKING': return Math.max(0.12, this.level);
      case 'THINKING': return this.reduceMotion ? 0.5 : (Math.sin(this.phase * 2.4) + 1) / 2;
      default: return 0.04;
    }
  }

  roundedRect(x, y, width, height, radius) {
    const ctx = this.context;
    const r = Math.max(0, Math.min(radius, width / 2, height / 2));
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + width, y, x + width, y + height, r);
    ctx.arcTo(x + width, y + height, x, y + height, r);
    ctx.arcTo(x, y + height, x, y, r);
    ctx.arcTo(x, y, x + width, y, r);
    ctx.closePath();
  }
}

/**
 * Peak amplitude of a PCM16 buffer, 0..1, so the bar can follow TARS's own voice
 * while it speaks. Sampled rather than fully scanned: a chunk can be hundreds of
 * thousands of samples and the bar only needs a level.
 */
export function pcmLevel(buffer) {
  if (!buffer || buffer.byteLength < 2) return 0;
  const samples = new Int16Array(buffer, 0, Math.floor(buffer.byteLength / 2));
  const stride = Math.max(1, Math.floor(samples.length / 512));
  let peak = 0;
  for (let index = 0; index < samples.length; index += stride) {
    const value = Math.abs(samples[index]);
    if (value > peak) peak = value;
  }
  return Math.min(1, peak / 32768);
}
