import {languages, paintedLotus, selectDemoReply, formatSpecs} from './core.mjs';
import {MicPreview, InterfaceAudio} from './audio.mjs';
import {MicrophoneStream, SpeechPlayer} from './voice.mjs';
import {BrainClient} from './client.mjs';
import {TarsFace, pcmLevel} from './face.mjs';
import {backend} from './dev-config.mjs';

const $ = id => document.getElementById(id);
const audio = new InterfaceAudio();
const state = {language:languages[0],screen:'ritual',fromStudio:false,busy:false,humor:25,messages:[],generation:0,
  serverTts:false,micEnabled:false,listening:false,voiceTurn:false,asrProvider:null,
  calibrated:false,gateOpen:false,wakeWord:false,wakePhrase:'hey tars'};
let replyTimer, thoughtTimer, toastTimer, audioBlob;
const brain = new BrainClient({url:backend.url,token:backend.token});
// Two faces share one state: the small readout in the sidebar and the large one
// in face mode. They are created lazily because their canvases only exist once
// the studio markup is in the DOM, and kept in a list so every update reaches
// both without either becoming the source of truth.
const faces = [];
function faceViews() {
  if (!faces.length) {
    for (const id of ['tars-face','tars-face-big']) {
      const canvas = $(id);
      if (!canvas) continue;
      const view = new TarsFace(canvas);
      view.start();
      faces.push(view);
    }
  }
  return faces;
}
function faceState(next) { for (const view of faceViews()) view.setState(next); }
function faceLevel(level) { for (const view of faceViews()) view.setLevel(level); }
/** Single place that decides what the face should be showing when idle. */
function paintFace() {
  if (!brain.connected) return faceState('OFFLINE');
  if (state.busy) return;                    // turn states are driven by events
  if (state.listening) return faceState(state.wakeWord ? 'ARMED' : 'LISTENING');
  faceState('IDLE');
}
/** The one-line status under the big face. */
function setFaceStatus(text) {
  const label = $('face-state');
  if (label) label.textContent = text;
}
let liveModel = 'the model';
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

function lotusInto(element, size, breathe = false) {
  // Only locally generated SVG markup is inserted; conversation text uses textContent.
  element.innerHTML = paintedLotus({size,seed:88,mode:breathe ? 'breathe' : ''});
}
document.querySelectorAll('[data-lotus]').forEach(element => lotusInto(element, Number(element.dataset.lotus), element.hasAttribute('data-breathe')));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(message) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; },2800);
}
function showScreen(name) {
  state.screen = name;
  for (const screen of ['ritual','languages','studio','face-mode']) $(screen).hidden = screen !== name;
  audio.cancelSpeech();
  // The big canvas has no size until its screen is shown, so it is measured on
  // entry rather than only on window resize.
  if (name === 'face-mode') requestAnimationFrame(() => { for (const view of faceViews()) view.resize(); });
  const focusTarget = {ritual:'begin',languages:'language-title',studio:'studio-greeting','face-mode':'face-tap'}[name];
  requestAnimationFrame(() => (name === 'studio' && state.messages.length ? $('prompt') : $(focusTarget)).focus());
}
function setLanguage(language) {
  state.language = language;
  for (const button of $('language-grid').children) button.setAttribute('aria-pressed',String(button.dataset.id === language.id));
  $('greeting-preview').textContent = language.greeting;
  $('greeting-preview').lang = language.id;
  $('language-preview').textContent = `Studio prompt will initialize in ${language.name} (${language.region}).`;
  $('enter').textContent = `Enter Studio in ${language.name} →`;
  $('active-language').textContent = `${language.name} · ${language.region}`;
  $('studio-greeting').textContent = language.greeting;
  $('studio-greeting').lang = language.id;
  $('prompt').placeholder = `Ask in ${language.name}…`;
}
for (const language of languages) {
  const button = el('button','language-tab'); button.type = 'button'; button.dataset.id = language.id;
  const native = el('span','native',language.native); native.lang = language.id;
  button.append(native,el('strong','',language.name),el('small','',language.region));
  button.addEventListener('click',() => { setLanguage(language); audio.chime('mid'); });
  $('language-grid').append(button);
}
setLanguage(state.language);
$('begin').addEventListener('click',() => {
  $('begin').disabled = true; audio.chime('bell'); $('begin').classList.add('blooming');
  setTimeout(() => { showScreen('languages'); $('begin').disabled = false; $('begin').classList.remove('blooming'); },reducedMotion.matches ? 0 : 700);
});
$('back').addEventListener('click',() => showScreen('ritual'));
$('enter').addEventListener('click',() => { audio.chime('high'); showScreen('studio'); });
$('home').addEventListener('click',event => { event.preventDefault(); cancelPending(); showScreen('ritual'); });
$('change-language').addEventListener('click',() => { state.fromStudio = true; showScreen('languages'); });

// ---- Face mode -----------------------------------------------------------
// Tap TARS to talk, or arm hands-free and address him by name.
//
// Two entry points on purpose: the sidebar robot card, and a header button. The
// sidebar is `display:none` below 700px, so on a phone the card alone left face
// mode unreachable — the header button is the one that always works.
function openFaceMode() {
  audio.chime('mid');
  showScreen('face-mode');
  setFaceStatus(brain.connected ? 'Ready' : 'Offline');
  setFaceMicLabel();
  paintFace();
}
$('face-open').addEventListener('click',openFaceMode);
$('face-open-header').addEventListener('click',openFaceMode);
$('face-back').addEventListener('click',() => showScreen('studio'));

function setFaceMicLabel() {
  const button = $('face-listen');
  if (button) button.textContent = state.listening ? 'Stop listening' : 'Tap to talk';
  const toggle = $('face-handsfree');
  if (toggle) {
    const on = Boolean($('hands-free')?.checked);
    toggle.textContent = `Hands-free: ${on ? 'on' : 'off'}`;
    toggle.setAttribute('aria-pressed',String(on));
  }
}

async function toggleFaceListening() {
  if (state.listening) { stopListening(); }
  else { await startListening(); }
  setFaceMicLabel();
}
// Tapping the face itself is the primary gesture; the button is the same action
// spelled out for anyone who does not realise the face is tappable.
$('face-tap').addEventListener('click',toggleFaceListening);
$('face-listen').addEventListener('click',toggleFaceListening);

// Hands-free is one setting shared with the settings dialog, so the two controls
// can never disagree about whether the wake word is armed.
$('face-handsfree').addEventListener('click',async () => {
  const source = $('hands-free');
  if (!source) return;
  source.checked = !source.checked;
  setFaceMicLabel();
  const wake = source.checked;
  setFaceStatus(wake ? `Waiting for “${state.wakePhrase}”` : 'Ready');
  // Restart listening so the server is told about the new mode.
  if (state.listening) { stopListening(); await startListening(); setFaceMicLabel(); }
});
$('hands-free')?.addEventListener('change',setFaceMicLabel);
$('sound').addEventListener('click',() => {
  audio.mute(); $('sound').textContent = audio.enabled ? '🔔' : '🔕';
  $('sound').setAttribute('aria-pressed',String(audio.enabled));
  $('sound').setAttribute('aria-label',audio.enabled ? 'Mute interface sounds' : 'Enable interface sounds');
});
$('theme').addEventListener('click',() => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme').textContent = dark ? '☀' : '◐';
  $('theme').setAttribute('aria-pressed',String(dark));
  $('theme').setAttribute('aria-label',dark ? 'Switch to light theme' : 'Switch to dark theme');
  document.querySelector('meta[name=theme-color]').content = dark ? '#201d19' : '#f0e9dd';
});

function updateComposer() {
  $('send').disabled = state.busy || !$('prompt').value.trim();
  $('prompt').style.height = 'auto';
  $('prompt').style.height = `${Math.min($('prompt').scrollHeight,130)}px`;
}
function scrollToLatest() {
  const area = $('chat-scroll'); area.scrollTo({top:area.scrollHeight,behavior:reducedMotion.matches ? 'instant' : 'smooth'});
}
function nearBottom() { const area = $('chat-scroll'); return area.scrollHeight - area.scrollTop - area.clientHeight < 150; }
function cancelPending() {
  // Bumping the generation first means the rejected turn is ignored rather than
  // falling through to a scripted reply.
  state.generation++; clearTimeout(replyTimer); clearInterval(thoughtTimer);
  if (brain.connected) brain.interrupt();
  speech.stop();
  state.voiceTurn = false;
  state.busy = false; $('thinking').hidden = true; audio.cancelSpeech(); updateComposer();
}
function renderMessage(role, text, reply) {
  const message = el('article',`message ${role}`);
  message.setAttribute('aria-label',role === 'user' ? 'You' : 'TARS demo answer');
  const bubble = el('div','bubble');
  if (role === 'user') bubble.textContent = text;
  else {
    const label = el('div','assistant-label'); const icon = el('span'); lotusInto(icon,16);
    label.append(icon,el('span','','TARS / demo')); bubble.append(label,el('p','',text));
    const card = el('section','spec-card'); card.append(el('span','pill',reply.tag),el('h3','',reply.title),el('p','',reply.description));
    const metrics = el('div','metrics');
    for (const [name,value] of reply.metrics) {
      const metric = el('div','metric'); metric.append(el('small','',name),el('strong','',value)); metrics.append(metric);
    }
    const copy = el('button','text-button','Copy summary ↗'); copy.type = 'button';
    copy.addEventListener('click',async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(formatSpecs(reply)); toast('✓ Demo summary copied');
      } catch { toast('Clipboard unavailable. Select the card text to copy it manually.'); }
    });
    card.append(metrics,copy); bubble.append(card);
    const context = el('details'); context.append(el('summary','','Answer context · not a reasoning trace'));
    const list = el('ul'); reply.context.forEach(step => list.append(el('li','',step))); context.append(list); bubble.append(context);
  }
  message.append(bubble); $('messages').append(message);
}
function renderLiveAnswer(answer) {
  const message = el('article','message assistant');
  message.setAttribute('aria-label','TARS answer');
  const bubble = el('div','bubble');
  const label = el('div','assistant-label'); const icon = el('span'); lotusInto(icon,16);
  label.append(icon,el('span','',`TARS / ${answer.mode === 'ADVISORY' ? 'sourced' : 'chat'}`));
  bubble.append(label,el('p','',answer.text));
  const sources = Array.isArray(answer.sources) ? answer.sources : [];
  if (sources.length) {
    const details = el('details','source-list');
    details.append(el('summary','',answer.grounded ? `Sources · ${sources.length}` : `Passages consulted · ${sources.length}`));
    const list = el('ul');
    for (const source of sources) {
      const item = el('li');
      item.append(el('strong','',`[${source.id}] ${source.title}`),el('br'));
      // Only http(s) links are made clickable, so a corpus entry cannot inject a javascript: URL.
      if (/^https?:\/\//i.test(source.url ?? '')) {
        const link = el('a','',source.url);
        link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        item.append(link);
      } else {
        item.append(el('span','',source.url ?? ''));
      }
      item.append(el('br'),el('small','',`${source.jurisdiction ?? ''} · updated ${source.updated_at ?? ''}`));
      list.append(item);
    }
    details.append(list); bubble.append(details);
  }
  const web = Array.isArray(answer.web_sources) ? answer.web_sources : [];
  if (web.length) {
    const details = el('details','source-list');
    details.append(el('summary','',`From the web · ${web.length}`));
    const list = el('ul');
    for (const source of web) {
      const item = el('li');
      if (/^https?:\/\//i.test(source.url ?? '')) {
        const link = el('a','',source.title || source.url);
        link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        item.append(link);
        item.append(el('br'),el('small','',new URL(source.url).hostname));
      } else {
        item.append(el('span','',source.title || ''));
      }
      list.append(item);
    }
    details.append(list); bubble.append(details);
  }
  const seconds = Math.round((answer.elapsed_ms ?? 0) / 100) / 10;
  const verification = answer.grounded ? '✓ grounded in retrieved sources'
    : answer.verification === 'uncited_answer' ? '⚠ answered from the passages but not citation-checked'
    : '⚠ not source-verified';
  bubble.append(el('p','answer-meta',`${verification} · answered in ${seconds}s by ${liveModel}`));
  message.append(bubble); $('messages').append(message);
}

function beginTurn(text) {
  $('empty').hidden = true;
  state.streamed = '';
  state.messages.push({role:'user',text,language:state.language.id,at:new Date().toISOString()});
  renderMessage('user',text);
  $('prompt').value = ''; state.busy = true; updateComposer();
  $('thinking').hidden = false; $('thinking-label').textContent = 'Shaping an answer…';
  const phrases = ['Shaping an answer…','Finding the simple words…','A little clarity, coming up…','Putting it together…']; let index = 0;
  thoughtTimer = setInterval(() => { $('thinking-label').textContent = phrases[++index % phrases.length]; },2400);
  if ($('filler').checked && !$('settings').open) audio.filler();
  scrollToLatest();
}
function endTurn() {
  clearInterval(thoughtTimer); state.busy = false; $('thinking').hidden = true; updateComposer();
}
function demoReply(text, generation) {
  const reply = selectDemoReply(text,state.humor);
  replyTimer = setTimeout(() => {
    if (generation !== state.generation) return;
    const follow = nearBottom(); endTurn(); audio.cancelSpeech();
    state.messages.push({role:'assistant',text:reply.text,language:'en',demo:true,at:new Date().toISOString()});
    renderMessage('assistant',reply.text,reply);
    if (follow) scrollToLatest();
  },1800 + Math.random() * 700);
}
async function send(prompt) {
  const text = prompt.trim().slice(0,2000);
  if (!text || state.busy) return;
  beginTurn(text);
  const generation = state.generation;
  if (brain.connected) {
    // speak:true routes the reply through Azure Speech neural TTS on the server,
    // which streams PCM back over the same socket. Without a server voice the
    // browser speaks the text itself.
    const speakOnServer = state.serverTts && $('speak-replies').checked;
    try {
      const answer = await brain.ask(text,state.language.id,{speak:speakOnServer});
      if (generation !== state.generation) return;
      const follow = nearBottom(); endTurn(); if (!speakOnServer) audio.cancelSpeech();
      state.messages.push({role:'assistant',text:answer.text,language:state.language.id,live:true,model:liveModel,at:new Date().toISOString()});
      renderLiveAnswer(answer);
      if (!speakOnServer && $('speak-replies').checked) audio.speak(answer.text,state.language.id);
      if (follow) scrollToLatest();
      return;
    } catch (error) {
      if (generation !== state.generation) return;
      toast(error.message);
      if (!backend.demoFallback) { endTurn(); return; }
      // The pending indicator stays up; demoReply finishes the turn.
      demoReply(text,generation);
      return;
    }
  }
  // No live model available: fall back to the scripted prototype replies.
  demoReply(text,generation);
}
$('composer').addEventListener('submit',event => { event.preventDefault(); send($('prompt').value); });
$('prompt').addEventListener('input',updateComposer);
$('prompt').addEventListener('keydown',event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send($('prompt').value); }
});
for (const prompt of ['What can my PACS help with?','Help me understand crop insurance','How do I raise a complaint?','What should I check before a loan?']) {
  const button = el('button','suggestion'); button.append(el('span','',prompt),el('span','','↗'));
  button.addEventListener('click',() => send(prompt)); $('suggestions').append(button);
}
// New conversation. Bound to both the sidebar button and a header button,
// because the sidebar is hidden below 700px and this would otherwise be
// unreachable on a phone.
function newConversation() {
  if (state.messages.length && !confirm('Clear this conversation? Export it first if you want to keep a copy.')) return;
  cancelPending(); state.messages = []; $('messages').replaceChildren(); $('empty').hidden = false; $('prompt').value = ''; updateComposer(); $('prompt').focus();
}
$('new-chat').addEventListener('click',newConversation);
$('new-chat-header').addEventListener('click',newConversation);
function download(blob, filename) {
  const url = URL.createObjectURL(blob), link = el('a'); link.href = url; link.download = filename;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),30000);
}
$('export').addEventListener('click',() => {
  if (!state.messages.length) { toast('Your conversation will appear here after your first question.'); return; }
  download(new Blob([JSON.stringify({application:'Astra TARS prototype',demo:true,scope:'This browser tab only',messages:state.messages},null,2)],{type:'application/json'}),'astra-conversation.json');
  toast('Conversation exported to your device');
});

const microphone = new MicPreview({
  onLevel(level) { $('meter-fill').style.width = `${level}%`; $('meter-fill').parentElement.setAttribute('aria-valuenow',String(level)); },
  onStatus(text, active) {
    $('mic-status').textContent = text; $('mic-test').textContent = active ? 'Stop microphone test' : 'Start 20-second mic test';
    for (const id of ['noise','echo','gain']) $(id).disabled = active;
  },
  onReady(blob) { audioBlob = blob; $('download-audio').disabled = !blob; }
});
function openSettings() { audio.cancelSpeech(); if (!$('settings').open) $('settings').showModal(); }
for (const id of ['settings-open','voice-open','robot-open']) $(id).addEventListener('click',openSettings);
$('settings-close').addEventListener('click',() => $('settings').close());
$('settings').addEventListener('close',() => microphone.dispose());
$('mic-test').addEventListener('click',() => {
  audio.cancelSpeech();
  if (microphone.active) { microphone.dispose(); return; }
  microphone.start({noise:$('noise').checked,echo:$('echo').checked,gain:$('gain').checked});
});
$('download-audio').addEventListener('click',() => {
  if (!audioBlob) return;
  const extension = audioBlob.type.includes('mp4') ? 'm4a' : audioBlob.type.includes('ogg') ? 'ogg' : 'webm';
  download(audioBlob,`astra-local-mic-test.${extension}`); toast('Local sample saved. No audio was uploaded.');
});
$('humor').addEventListener('input',() => { state.humor = Number($('humor').value); $('humor-value').value = `${state.humor}%`; });
$('filler').addEventListener('change',() => {
  if (!$('filler').checked) audio.cancelSpeech();
  else if (!globalThis.speechSynthesis) { $('filler').checked = false; toast('Device speech is not supported in this browser.'); }
});
window.addEventListener('pagehide',() => { microphone.dispose(); cancelPending(); });
document.addEventListener('visibilitychange',() => { if (document.hidden) { microphone.dispose(); audio.cancelSpeech(); } });


/* ---------------------------------------------------------------------------
 * Voice pipeline
 *
 * Microphone → 16 kHz PCM frames → backend (Deepgram nova-3, or Azure Speech
 * for the languages nova-3 does not cover) → Azure OpenAI → Azure Speech neural
 * TTS → 24 kHz PCM streamed back here.
 * ------------------------------------------------------------------------- */

const speech = new SpeechPlayer({
  onFinished: ({turnId, utteranceId}) => {
    brain.playbackDone(turnId, utteranceId);
    // Reopen the microphone once TARS has finished speaking.
    if (state.listening && $('half-duplex').checked) talk.resume();
  },
});

const talk = new MicrophoneStream({
  onFrame: frame => brain.sendAudio(frame),
  onLevel(level) {
    const fill = $('meter-fill');
    if (fill) { fill.style.width = `${level}%`; fill.parentElement.setAttribute('aria-valuenow',String(level)); }
    // The meter is 0..100; the face wants 0..1.
    faceLevel(level / 100);
  },  onGate(open) {
    // Only your voice, above the measured room noise, is sent.
    state.gateOpen = open;
    setMicState();
  },
  onCalibrated({noiseFloor, openThreshold}) {
    state.calibrated = true;
    setMicState();
    const floor = Math.round(noiseFloor * 1000) / 1000;
    const threshold = Math.round(openThreshold * 1000) / 1000;
    $('mic-status').textContent = `Calibrated to this room · noise floor ${floor} · speaks above ${threshold}. Only audio above that is sent.`;
    toast('Calibrated to the room — go ahead and speak');
  },
  onError(message) { toast(message); stopListening(); },
});

function setMicState() {
  const button = $('mic');
  const enabled = state.micEnabled && brain.connected;
  button.disabled = !enabled;
  button.setAttribute('aria-pressed',String(state.listening));
  button.classList.toggle('listening',state.listening);
  button.classList.toggle('calibrating',state.listening && !state.calibrated);
  button.classList.toggle('hearing',state.listening && state.calibrated && state.gateOpen);
  button.textContent = state.listening ? (state.calibrated ? '■' : '…') : '♩';
  const label = !enabled ? 'Microphone unavailable'
    : !state.listening ? 'Speak to TARS'
    : !state.calibrated ? 'Measuring room noise…'
    : state.gateOpen ? 'Hearing you — click to stop' : 'Listening — click to stop';
  button.setAttribute('aria-label',label);
  button.title = enabled ? label : 'Speech recognition is not configured on the server';
  // Face mode has its own button for the same action, so it is updated from the
  // same place rather than being left to drift.
  setFaceMicLabel();
  const listen = $('face-listen');
  if (listen) listen.disabled = !enabled;
  const tap = $('face-tap');
  if (tap) tap.disabled = !enabled;
}

async function startListening() {
  if (state.listening || !brain.connected || !state.micEnabled) return;
  // Hands-free keeps the microphone open and answers only when addressed.
  state.wakeWord = Boolean($('hands-free')?.checked);
  try {
    brain.startAudio(state.language.id, null, state.wakeWord);
  } catch (error) { toast(error.message); return; }
  state.calibrated = false;
  state.gateOpen = false;
  $('mic-status').textContent = state.wakeWord
    ? `Measuring the room — then say "${state.wakePhrase}" to get my attention.`
    : 'Measuring the room for a moment — stay quiet, then speak.';
  const started = await talk.start({noise:$('noise').checked,echo:$('echo').checked,gain:$('gain').checked});
  if (!started) { brain.stopAudio(); return; }
  state.listening = true;
  setMicState();
  paintFace();
  audio.chime('high');
}

function stopListening({flush = true} = {}) {
  if (!state.listening && !talk.active) return;
  talk.stop();
  state.listening = false;
  state.calibrated = false;
  state.gateOpen = false;
  if (flush) brain.stopAudio(); else brain.audio = false;
  // The interim-transcript indicator belongs to listening, not to a turn.
  if (!state.busy) $('thinking').hidden = true;
  setMicState();
  faceLevel(0);
  paintFace();
}

$('mic').addEventListener('click',() => { state.listening ? stopListening() : startListening(); });
$('recalibrate')?.addEventListener('click',() => {
  if (!state.listening) { toast('Start the microphone first.'); return; }
  state.calibrated = false; state.gateOpen = false; setMicState();
  $('mic-status').textContent = 'Re-measuring the room — stay quiet for a moment.';
  talk.recalibrate();
});

// A spoken turn is started by the server the moment the transcript is final, so
// the UI is driven by events here rather than by the ask() promise.
brain.on('transcript', event => {
  if (event.final === false) {
    if (!state.busy) $('thinking-label').textContent = event.text ? `“${event.text}”` : 'Listening…';
    $('thinking').hidden = false;
    const heard = $('face-heard');
    if (heard && event.text) heard.textContent = `“${event.text}”`;
    return;
  }
  if (event.origin !== 'user' || state.busy) return;
  state.voiceTurn = true;
  beginTurn(event.text);
});

brain.on('response', event => {
  // Face mode shows the answer under the face; the chat panel keeps its own copy.
  const said = $('face-said');
  if (said) said.textContent = (event.text ?? '').replace(/\[S\d+\]/g,'').trim();
  if (!state.voiceTurn) return;   // ask() renders its own answer
  const follow = nearBottom();
  state.messages.push({role:'assistant',text:event.text,language:state.language.id,live:true,model:liveModel,voice:true,at:new Date().toISOString()});
  renderLiveAnswer(event);
  if (!state.serverTts && $('speak-replies').checked) audio.speak(event.text,state.language.id);
  if (follow) scrollToLatest();
});

brain.on('turn_done', () => { if (state.voiceTurn) { state.voiceTurn = false; endTurn(); } });

brain.on('audio_chunk', ({header, data}) => {
  // Mute the microphone for the duration of playback so TARS's own voice is
  // never transcribed back as a new question.
  if ($('half-duplex').checked && state.listening) talk.pause();
  if (header.sequence === 0) speech.begin({turnId:header.turn_id,utteranceId:header.utterance_id});
  if (!speech.enqueue(data)) toast('This browser cannot play streamed audio. The reply is shown as text.');
  // Let the face's voice bar follow TARS's own amplitude rather than a fake
  // animation, so the mouth matches what is actually being said.
  faceLevel(pcmLevel(data));
});
brain.on('audio_end', () => { speech.end(); faceLevel(0); });
brain.on('stop_audio', () => { speech.stop(); if (state.listening && $('half-duplex').checked) talk.resume(); });
brain.on('voice_unavailable', event => {
  speech.stop();
  if (state.listening && $('half-duplex').checked) talk.resume();
  toast(event.message);
});
brain.on('audio_ready', event => {
  // The server reports which engine it actually started: Deepgram nova-3, or
  // Azure Speech when nova-3 has no model for this language or failed to open.
  state.asrProvider = event.asr_provider;
  setMicState();
  const gap = event.asr_provider === 'azure-speech' ? ' (Azure Speech covers this language)' : '';
  toast(`Listening · ${event.asr_provider} · ${event.asr_language}${gap}`);
});
brain.on('error', event => {
  // Any failure while the microphone is open ends the listening session rather
  // than leaving the browser streaming frames the server is rejecting.
  if (state.listening) { stopListening({flush:event.code === 'turn_limit'}); toast(event.message); return; }
  // A spoken turn has no ask() promise to reject, so it is settled here.
  if (state.voiceTurn) { state.voiceTurn = false; speech.stop(); endTurn(); toast(event.message); }
});


/* ---------------------------------------------------------------------------
 * Live model connection
 * ------------------------------------------------------------------------- */

function setLiveStatus(text, tone) {
  const label = $('live-status-text');
  const wrapper = $('live-status');
  if (label) label.textContent = text;
  if (wrapper) wrapper.dataset.tone = tone;
  const pill = $('mode-pill');
  if (pill) pill.textContent = tone === 'live' ? 'LIVE' : 'DEMO';
}

brain.on('state', event => {
  // The face follows the pipeline even when no turn is in flight, so it is a
  // readout of what TARS is actually doing rather than of the chat UI.
  if (event.state === 'LISTENING') faceState(state.wakeWord ? 'ARMED' : 'LISTENING');
  else if (event.state === 'IDLE') paintFace();
  else faceState(event.state);
  const spoken = {THINKING:'Thinking', SPEAKING:'Speaking',
    LISTENING:state.wakeWord ? `Waiting for “${state.wakePhrase}”` : 'Listening', IDLE:'Ready'};
  if (spoken[event.state]) setFaceStatus(spoken[event.state]);
  if (!state.busy) return;
  const labels = {THINKING:`Thinking with ${liveModel}…`,SPEAKING:'Speaking…',LISTENING:'Listening…'};
  if (labels[event.state]) $('thinking-label').textContent = labels[event.state];
});

// Hands-free: speech that was heard but not addressed to TARS. Shown briefly in
// the mic status rather than added to the conversation.
brain.on('wake_ignored', event => {
  const status = $('mic-status');
  if (status) status.textContent = `Heard "${(event.text ?? '').slice(0, 60)}" — say "${state.wakePhrase}" first.`;
  const heard = $('face-heard');
  if (heard) heard.textContent = `heard “${(event.text ?? '').slice(0, 70)}” — not for me`;
});
brain.on('wake_detected', event => {
  audio.chime('high');
  faceState('LISTENING');
  const status = $('mic-status');
  if (status) status.textContent = 'Addressed — listening.';
  const heard = $('face-heard');
  if (heard) heard.textContent = event.question ? `“${event.question}”` : 'yes?';
  setFaceStatus('Addressed');
});
brain.on('acknowledgement', event => {
  const status = $('mic-status');
  if (status) status.textContent = `TARS: ${event.text} (go ahead)`;
  const said = $('face-said');
  if (said) said.textContent = event.text;
});
brain.on('retrieval', event => {
  if (state.busy) $('thinking-label').textContent = `Reading ${event.source_count} source${event.source_count === 1 ? '' : 's'}…`;
});
// The answer now streams in as the model writes it, instead of appearing at the end.
brain.on('response_delta', event => {
  if (!state.busy) return;
  state.streamed = (state.streamed ?? '') + (event.text ?? '');
  const label = $('thinking-label');
  const trimmed = state.streamed.replace(/\[S\d+\]/g, '').replace(/\s+/g, ' ').trim();
  if (trimmed) label.textContent = trimmed.length > 160 ? `…${trimmed.slice(-160)}` : trimmed;
});
brain.on('citation_retry', () => {
  if (state.busy) $('thinking-label').textContent = 'Adding the source references…';
});
brain.on('closed', ({wasReady}) => {
  stopListening({flush:false});
  speech.stop();
  state.micEnabled = false;
  setMicState();
  setLiveStatus(backend.demoFallback ? 'Model offline · scripted replies' : 'Model offline','offline');
  if (wasReady) toast('Lost the connection to the model. Reconnect from settings.');
});

async function connectBrain({quiet = false} = {}) {
  setLiveStatus('Connecting to the model…','pending');
  try {
    const ready = await brain.connect();
    liveModel = ready.model || 'the model';
    state.serverTts = Boolean(ready.server_tts);
    state.micEnabled = Boolean(ready.microphone?.enabled) && talk.supported;
    setMicState();
    const engines = (ready.stt_engines ?? []).join(' → ') || 'no speech recognition';
    setLiveStatus(`Live · ${liveModel}${ready.corpus_loaded ? '' : ' · no corpus'}`,'live');
    // Adopt the server's wake word so the UI never advertises a phrase the
    // backend will not answer to.
    if (ready.wake_word) {
      state.wakePhrase = ready.wake_word;
      const label = $('wake-phrase');
      if (label) label.textContent = ready.wake_word;
      const hint = $('face-wake');
      if (hint) hint.textContent = ready.wake_word;
    }
    const handsFree = $('hands-free');
    if (handsFree && ready.wake_word_required) handsFree.checked = true;
    paintFace();
    // The voice engine depends on the edition: Azure Speech in the cloud build,
    // Piper/espeak-ng in the offline one, or the browser when neither is configured.
    const voiceEngine = !ready.server_tts ? 'this browser'
      : ready.tts_provider === 'local' ? 'Piper / espeak-ng (on-device)'
      : 'Azure Speech neural TTS';
    $('pipeline-summary')?.replaceChildren(document.createTextNode(
      `Brain: ${ready.provider} / ${liveModel}. Ears: ${engines}. Voice: ${voiceEngine}.`
      + (ready.offline ? ' Fully offline.' : '')));
    if (!quiet) toast(`Connected to ${liveModel}`);
    return true;
  } catch (error) {
    state.serverTts = false;
    state.micEnabled = false;
    setMicState();
    setLiveStatus(backend.demoFallback ? 'Model offline · scripted replies' : 'Model offline','offline');
    if (!quiet) toast(error.message);
    return false;
  }
}

/**
 * Adopt deployment configuration from /api/config when it exists.
 *
 * Locally there is no such endpoint, so the fetch fails and dev-config.mjs stays
 * in force — the no-build local workflow is unchanged. On Vercel the function
 * returns the backend URL and token from environment variables, so neither has
 * to be committed. `configured: false` means no backend was supplied and the app
 * stays in scripted demo mode.
 */
async function adoptDeploymentConfig() {
  try {
    const response = await fetch('/api/config', {cache: 'no-store'});
    if (!response.ok) return;
    const config = await response.json();
    if (config.warning) console.warn(`[config] ${config.warning}`);
    if (config.configured) {
      brain.url = config.url;
      brain.token = config.token;
    }
    if (typeof config.demoFallback === 'boolean') backend.demoFallback = config.demoFallback;
  } catch {
    // No endpoint, or offline: dev-config.mjs is the fallback by design.
  }
}

$('reconnect')?.addEventListener('click',() => connectBrain());
window.addEventListener('pagehide',() => { stopListening({flush:false}); speech.dispose(); brain.close(); });
document.addEventListener('visibilitychange',() => { if (document.hidden) stopListening(); });
setMicState();
adoptDeploymentConfig().then(() => connectBrain({quiet:true}));
