export class MicPreview {
  constructor({onLevel, onStatus, onReady}) {
    Object.assign(this, {onLevel, onStatus, onReady});
    this.epoch = 0;
    this.active = false;
  }

  async start({noise, echo, gain}) {
    this.dispose();
    const epoch = this.epoch;
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      this.onStatus('Microphone unavailable. Open this page on HTTPS or localhost in a supported browser.', false);
      return;
    }
    this.active = true;
    this.onStatus('Waiting for microphone permission…', true);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio:{noiseSuppression:{ideal:noise},echoCancellation:{ideal:echo},autoGainControl:{ideal:gain}},video:false});
      if (epoch !== this.epoch) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!AudioContextClass) throw new Error('Audio metering is unsupported in this browser.');
      this.context = new AudioContextClass();
      await this.context.resume();
      if (epoch !== this.epoch) return;
      this.source = this.context.createMediaStreamSource(stream);
      this.analyser = this.context.createAnalyser();
      this.analyser.fftSize = 1024;
      this.source.connect(this.analyser);
      // Never connect the microphone to speakers: this prevents preview feedback.
      const samples = new Float32Array(this.analyser.fftSize);
      const tick = () => {
        if (!this.active || epoch !== this.epoch) return;
        this.analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(samples.reduce((sum,value) => sum + value * value, 0) / samples.length);
        this.onLevel(Math.min(100, Math.round(rms * 350)));
        this.frame = requestAnimationFrame(tick);
      };
      tick();
      const settings = stream.getAudioTracks()[0].getSettings();
      const status = key => settings[key] === true ? 'on' : settings[key] === false ? 'off' : 'not reported';
      let codec = 'Recording unsupported; meter only';
      if (globalThis.MediaRecorder) {
        const mimeType = ['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4'].find(type => MediaRecorder.isTypeSupported(type));
        const recorder = new MediaRecorder(stream, mimeType ? {mimeType} : {});
        this.recorder = recorder;
        const chunks = [];
        recorder.ondataavailable = event => { if (epoch === this.epoch && event.data.size) chunks.push(event.data); };
        recorder.onstop = () => {
          if (epoch === this.epoch && chunks.length) this.onReady(new Blob(chunks,{type:recorder.mimeType || chunks[0].type}));
        };
        recorder.onerror = () => { this.stop(); this.onStatus('Audio recording failed. Try another browser or microphone.',false); };
        recorder.start(250);
        codec = recorder.mimeType || 'browser default codec';
      }
      this.onStatus(`Local test · Noise suppression: ${status('noiseSuppression')} · Echo cancellation: ${status('echoCancellation')} · Gain control: ${status('autoGainControl')} · ${codec}. Stops after 20 seconds.`, true);
      this.timer = setTimeout(() => this.stop(), 20000);
      stream.getAudioTracks()[0].onended = () => this.stop();
    } catch (error) {
      if (epoch !== this.epoch) return;
      this.dispose();
      const messages = {NotAllowedError:'Microphone permission denied. Allow microphone access in your browser to test.',NotFoundError:'No microphone found. Connect a microphone and try again.',NotReadableError:'Microphone is busy or unavailable. Close other recording apps and retry.'};
      this.onStatus(messages[error.name] || 'Could not start microphone processing. Try a supported browser and check your audio device.',false);
    }
  }

  stop() {
    this.active = false;
    clearTimeout(this.timer);
    cancelAnimationFrame(this.frame);
    if (this.recorder?.state === 'recording') this.recorder.stop();
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    this.source?.disconnect();
    if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {});
    this.stream = null;
    this.source = null;
    this.context = null;
    this.onLevel(0);
    this.onStatus('Microphone off. A recorded sample, if supported, can be saved locally.',false);
  }

  dispose() {
    this.epoch++;
    this.stop();
    this.recorder = null;
    this.onReady(null);
  }
}

export class InterfaceAudio {
  constructor() { this.enabled = true; }
  chime(kind = 'mid') {
    if (!this.enabled) return;
    const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!Context) return;
    try {
      this.context ||= new Context();
      this.context.resume().catch(() => {});
      const start = this.context.currentTime;
      const frequency = {bell:523.25,mid:392,high:659.25}[kind] || 392;
      [1,2.01,3.98].forEach((ratio,index) => {
        const oscillator = this.context.createOscillator(), gain = this.context.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency * ratio;
        gain.gain.setValueAtTime(.0001,start);
        gain.gain.exponentialRampToValueAtTime(.045 / (index + 1),start + .012);
        gain.gain.exponentialRampToValueAtTime(.0001,start + .85);
        oscillator.connect(gain).connect(this.context.destination);
        oscillator.start(start); oscillator.stop(start + .9);
        oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
      });
    } catch { /* Interface sound is optional; never block navigation. */ }
  }
  filler() {
    if (!this.enabled || !globalThis.speechSynthesis) return;
    this.cancelSpeech();
    const line = new SpeechSynthesisUtterance('One moment.');
    line.lang = 'en-IN'; line.rate = .9; line.volume = .6;
    globalThis.speechSynthesis.speak(line);
  }
  /**
   * Speak an answer locally. Used instead of server-side TTS, which is not
   * available with the local model provider. Citation markers are stripped so
   * the listener does not hear "bracket S one".
   */
  speak(text, language = 'en') {
    if (!globalThis.speechSynthesis) return false;
    const spoken = String(text).replace(/\[S\d+\]/g, '').replace(/\s+/g, ' ').trim();
    if (!spoken) return false;
    this.cancelSpeech();
    const utterance = new SpeechSynthesisUtterance(spoken.slice(0, 4000));
    utterance.lang = /-/.test(language) ? language : `${language}-IN`;
    utterance.rate = .95;
    const voice = globalThis.speechSynthesis.getVoices().find(candidate => candidate.lang === utterance.lang)
      || globalThis.speechSynthesis.getVoices().find(candidate => candidate.lang?.startsWith(`${language}-`));
    if (voice) utterance.voice = voice;
    globalThis.speechSynthesis.speak(utterance);
    return true;
  }
  cancelSpeech() { globalThis.speechSynthesis?.cancel(); }
  mute() { this.enabled = !this.enabled; this.cancelSpeech(); if (!this.enabled) this.context?.suspend().catch(() => {}); }
}
