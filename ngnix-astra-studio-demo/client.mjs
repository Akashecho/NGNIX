/**
 * WebSocket client for the Astra brain engine.
 *
 * Protocol: open the socket, send an auth frame as the very first message, then
 * wait for a `ready` event before sending anything else. The server closes with
 * code 1008 on a bad token or a disallowed Origin.
 */

const AUTH_TIMEOUT_MS = 10000;
const TURN_TIMEOUT_MS = 180000;

export class BrainClient {
  constructor({url, token}) {
    this.url = url;
    this.token = token;
    this.socket = null;
    this.ready = false;
    this.info = null;
    this.listeners = new Map();
    this.turn = null;
    this.pendingAudio = null;
    this.audio = false;
  }

  on(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
    return this;
  }

  emit(type, payload) {
    for (const handler of this.listeners.get(type) ?? []) {
      try { handler(payload); } catch { /* a listener must never break the socket loop */ }
    }
  }

  get connected() {
    return this.ready && this.socket?.readyState === WebSocket.OPEN;
  }

  connect() {
    return new Promise((resolve, reject) => {
      let socket;
      try {
        socket = new WebSocket(this.url);
      } catch (error) {
        reject(new Error(`Invalid backend URL: ${error.message}`));
        return;
      }
      socket.binaryType = 'arraybuffer';
      const timer = setTimeout(() => {
        socket.close();
        reject(new Error('The backend did not respond in time.'));
      }, AUTH_TIMEOUT_MS);

      socket.onopen = () => socket.send(JSON.stringify({type: 'auth', token: this.token}));

      socket.onmessage = event => {
        // Speech arrives as a JSON `audio` header immediately followed by one
        // binary frame of PCM. Pair them here so listeners get both at once.
        if (typeof event.data !== 'string') {
          const header = this.pendingAudio;
          this.pendingAudio = null;
          if (header) this.emit('audio_chunk', {header, data: event.data});
          return;
        }
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === 'audio') {
          this.pendingAudio = message;
          return;
        }
        if (!this.ready && message.type === 'ready') {
          this.ready = true;
          this.info = message;
          this.socket = socket;
          clearTimeout(timer);
          resolve(message);
        }
        this.route(message);
      };

      socket.onclose = event => {
        clearTimeout(timer);
        const wasReady = this.ready;
        this.ready = false;
        this.socket = null;
        this.failTurn(new Error('The connection to the backend closed.'));
        if (!wasReady) {
          reject(new Error(event.code === 1008
            ? 'Backend rejected the connection. Check the session token and that this page is served from an allowed origin.'
            : 'Could not reach the backend. Is the server running?'));
        }
        this.emit('closed', {code: event.code, wasReady});
      };

      socket.onerror = () => { /* onclose always follows and carries the detail */ };
    });
  }

  route(message) {
    this.emit(message.type, message);
    this.emit('*', message);
    if (!this.turn) return;
    if (message.type === 'response') {
      this.turn.response = message;
    } else if (message.type === 'error') {
      // A failed turn never emits turn_done, so settle here.
      this.failTurn(new Error(message.message || 'The request could not be completed.'));
    } else if (message.type === 'turn_done') {
      const turn = this.turn;
      this.turn = null;
      clearTimeout(turn.timer);
      turn.resolve(turn.response ?? {text: '', sources: []});
    }
  }

  failTurn(error) {
    if (!this.turn) return;
    const turn = this.turn;
    this.turn = null;
    clearTimeout(turn.timer);
    turn.reject(error);
  }

  ask(text, language = 'hi', {speak = false} = {}) {
    if (!this.connected) return Promise.reject(new Error('Not connected to the backend.'));
    if (this.turn) return Promise.reject(new Error('A question is already in progress.'));
    return new Promise((resolve, reject) => {
      this.turn = {
        resolve, reject, response: null,
        timer: setTimeout(() => this.failTurn(new Error('The model took too long to answer.')), TURN_TIMEOUT_MS),
      };
      try {
        // speak:true asks the server for Azure Speech neural TTS; the browser
        // only falls back to local speech when the server has no voice.
        this.socket.send(JSON.stringify({type: 'text', text, language, speak}));
      } catch (error) {
        this.failTurn(new Error(`Could not send the question: ${error.message}`));
      }
    });
  }

  /**
   * Open the microphone stream. The server picks the ASR engine for this
   * language (Deepgram nova-3, or Azure Speech for its language gaps) and
   * replies with an `audio_ready` event naming the engine it started.
   */
  startAudio(language = 'hi', voice = null, wakeWord = null) {
    if (!this.connected) throw new Error('Not connected to the backend.');
    this.audio = true;
    this.socket.send(JSON.stringify({type: 'audio_start', language, ...(voice ? {voice} : {}),
      // Omitted entirely when null, so the server's WAKE_WORD_REQUIRED default applies.
      ...(wakeWord === null ? {} : {wake_word: Boolean(wakeWord)})}));
  }

  /** Send one frame of mono PCM16 at 16 kHz. */
  sendAudio(frame) {
    if (!this.connected || !this.audio) return false;
    this.socket.send(frame);
    return true;
  }

  stopAudio() {
    if (!this.audio) return;
    this.audio = false;
    if (this.connected) this.socket.send(JSON.stringify({type: 'audio_stop'}));
  }

  /** Acknowledge that an utterance finished playing, so the server can advance. */
  playbackDone(turnId, utteranceId) {
    if (!this.connected || !turnId || !utteranceId) return;
    this.socket.send(JSON.stringify({type: 'playback_done', turn_id: turnId, utterance_id: utteranceId}));
  }

  interrupt() {
    if (this.connected) this.socket.send(JSON.stringify({type: 'interrupt'}));
    this.failTurn(new Error('cancelled'));
  }

  close() {
    this.failTurn(new Error('cancelled'));
    this.audio = false;
    this.ready = false;
    this.socket?.close(1000);
    this.socket = null;
  }
}
