import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import time
from typing import Annotated, Literal, Union

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .body import BodyHub
from .config import Settings
from .dialog import Dialog
from .providers import build_providers
from .rag import Index
from .session import Session
from .speech import DEEPGRAM_LANGUAGE_GAPS, voice_table


Language = Literal['hi', 'en', 'ta', 'te', 'bn', 'mr', 'gu', 'kn', 'ml', 'pa', 'or']


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Text(Event):
    type: Literal['text']
    text: str = Field(min_length=1, max_length=2000)
    language: Language = 'hi'
    speak: bool = False
    voice: str | None = Field(default=None, max_length=100)


class AudioStart(Event):
    type: Literal['audio_start']
    language: Language = 'hi'
    voice: str | None = Field(default=None, max_length=100)
    sample_rate: Literal[16000] = 16000
    encoding: Literal['pcm_s16le'] = 'pcm_s16le'
    channels: Literal[1] = 1
    # Hands-free listening. None keeps the server's WAKE_WORD_REQUIRED default, so
    # a client that knows nothing about wake words behaves exactly as before.
    wake_word: bool | None = None


class Simple(Event):
    type: Literal['audio_stop', 'interrupt', 'history', 'ping', 'dialog_cancel']


class PlaybackDone(Event):
    type: Literal['playback_done']
    turn_id: str = Field(min_length=1, max_length=64)
    utterance_id: str = Field(min_length=1, max_length=64)


class DialogStart(Event):
    type: Literal['dialog_start']
    flow: Literal['eligibility', 'grievance', 'financial_literacy']


class DialogAnswer(Event):
    type: Literal['dialog_answer']
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=1000)
    language: Language = 'hi'
    speak: bool = False
    voice: str | None = Field(default=None, max_length=100)


ClientEvent = TypeAdapter(Annotated[Union[Text, AudioStart, Simple, PlaybackDone, DialogStart, DialogAnswer], Field(discriminator='type')])


@asynccontextmanager
async def lifespan(app):
    settings = Settings.load()
    settings.validate_server()
    index = Index.load_default(settings)
    # The read timeout tracks the turn budget rather than being a fixed 30s: a
    # local model on CPU spends far longer than a cloud deployment on the same
    # prompt, and a read timeout shorter than BRAIN_TURN_TIMEOUT makes that
    # budget unreachable — the turn dies on the socket before the timeout the
    # operator configured ever applies.
    read_timeout = max(30.0, settings.turn_timeout)
    async with httpx.AsyncClient(timeout=httpx.Timeout(read_timeout, connect=8), limits=httpx.Limits(max_connections=24, max_keepalive_connections=12)) as client:
        app.state.settings, app.state.index = settings, index
        app.state.providers, app.state.body = build_providers(settings, client), BodyHub()
        app.state.sessions = {}
        yield
        await asyncio.gather(*(session.close() for session in list(app.state.sessions.values())), return_exceptions=True)


app = FastAPI(title='Astra Brain', version='0.1.0', lifespan=lifespan)


@app.get('/health')
async def health():
    settings = app.state.settings
    model = settings.ollama_llm_model if settings.llm_provider == 'ollama' else settings.llm_deployment
    payload = {'status': 'ready', 'corpus_loaded': bool(app.state.index.chunks), 'provider_connectivity': 'not_checked',
            'provider': settings.llm_provider, 'model': model, 'chunks': len(app.state.index.chunks),
            'citation_mode': settings.citation_mode, 'server_tts': settings.supports_server_tts,
            'tts_provider': settings.tts_provider, 'offline': settings.offline,
            'stt_engines': list(settings.stt_engines), 'stt_preference': settings.stt_provider,
            'microphone': settings.supports_microphone,
            'language_gaps_covered_by_azure': list(DEEPGRAM_LANGUAGE_GAPS) if settings.speech_key else []}
    if settings.local_ears:
        from .speech_local import WHISPER_LANGUAGE_GAPS

        payload['languages_not_heard_locally'] = list(WHISPER_LANGUAGE_GAPS)
    if settings.local_speech:
        # Reports which Piper voices are actually on disk, so a missing model
        # download shows up here instead of as a silent espeak-ng substitution.
        from .tts_local import LocalTts

        payload['local_tts'] = LocalTts(settings).status()
    if settings.offline:
        from . import indic

        payload['indic_nlp'] = indic.status()
    return payload


async def authenticate(socket, body=False):
    settings = app.state.settings
    origin = socket.headers.get('origin')
    if origin and origin not in settings.allowed_origins:
        await socket.close(code=1008)
        return None
    await socket.accept()
    try:
        # Credentials live in the first frame, never in a query string or logs.
        raw = await asyncio.wait_for(socket.receive_text(), 8)
        if len(raw) > 2048:
            raise ValueError('Authentication frame too large')
        hello = json.loads(raw)
        if not isinstance(hello, dict) or hello.get('type') != 'auth' or not isinstance(hello.get('token'), str):
            raise ValueError('Authentication required')
        if not hmac.compare_digest(hello['token'].encode(), settings.session_token.encode()):
            raise ValueError('Invalid token')
        if body and (not isinstance(hello.get('session_id'), str) or hello['session_id'] not in app.state.sessions):
            raise ValueError('Pair with an active phone session')
        return hello
    except (ValueError, TimeoutError, WebSocketDisconnect, RuntimeError):
        await socket.close(code=1008)
        return None


@app.websocket('/ws/session')
async def conversation(socket: WebSocket):
    if await authenticate(socket) is None:
        return
    if len(app.state.sessions) >= app.state.settings.max_sessions:
        await socket.close(code=1013)
        return
    session = Session(socket, app.state.settings, app.state.providers, app.state.index, app.state.body)
    app.state.sessions[session.id] = session
    dialog = None
    frame_count, window_start = 0, time.monotonic()
    try:
        await session.emit('ready', protocol_version=1, text_mode=True, corpus_loaded=bool(app.state.index.chunks),
            microphone={'encoding': 'pcm_s16le', 'sample_rate': 16000, 'channels': 1,
                        'enabled': app.state.settings.supports_microphone, 'max_frame_bytes': 6400},
            playback={'encoding': 'pcm_s16le', 'sample_rate': 24000, 'channels': 1},
            persistence='memory_only', asr_model=app.state.settings.asr_model, asr_language=app.state.settings.asr_language,
            stt_engines=list(app.state.settings.stt_engines), stt_preference=app.state.settings.stt_provider,
            azure_stt_languages=list(DEEPGRAM_LANGUAGE_GAPS),
            voices=voice_table(app.state.settings) if app.state.settings.supports_server_tts else {},
            provider=app.state.settings.llm_provider, server_tts=app.state.settings.supports_server_tts,
            tts_provider=app.state.settings.tts_provider, offline=app.state.settings.offline,
            wake_word=app.state.settings.wake_word,
            wake_word_required=app.state.settings.wake_word_required,
            model=app.state.settings.ollama_llm_model if app.state.settings.llm_provider == 'ollama' else app.state.settings.llm_deployment)
        while True:
            packet = await asyncio.wait_for(socket.receive(), 180)
            if packet['type'] == 'websocket.disconnect':
                break
            now = time.monotonic()
            if now - window_start >= 1:
                window_start, frame_count = now, 0
            frame_count += 1
            if frame_count > 100:
                await socket.close(code=1008)
                break
            try:
                if packet.get('bytes') is not None:
                    if not session.asr:
                        raise ValueError('Send audio_start first; text mode does not open ASR')
                    await session.asr.send(packet['bytes'])
                    continue
                raw = packet.get('text') or ''
                if len(raw) > 12000:
                    raise ValueError('Event too large')
                event = ClientEvent.validate_json(raw)
                if event.type == 'text':
                    await session.stop_audio(flush=False)
                    await session.start_turn(event.text, event.language, event.speak, event.voice)
                elif event.type == 'audio_start':
                    await session.start_audio(event.language, event.voice, event.wake_word)
                elif event.type == 'audio_stop':
                    await session.stop_audio()
                elif event.type == 'interrupt':
                    await session.interrupt()
                elif event.type == 'playback_done':
                    session.playback_done(event.turn_id, event.utterance_id)
                elif event.type == 'history':
                    await session.emit('history', messages=list(session.history), truncated_context=True, persisted=False)
                elif event.type == 'ping':
                    await session.emit('pong')
                elif event.type == 'dialog_start':
                    dialog = Dialog(event.flow)
                    await session.emit('dialog', **dialog.snapshot())
                elif event.type == 'dialog_answer':
                    if dialog is None:
                        raise ValueError('Start a dialog first')
                    snapshot = dialog.answer(event.field, event.value)
                    await session.emit('dialog', **snapshot)
                    if snapshot['complete']:
                        await session.start_turn(dialog.query(), event.language, event.speak, event.voice)
                elif event.type == 'dialog_cancel':
                    dialog = None
                    await session.emit('dialog_cancelled')
            except (ValidationError, ValueError):
                await session.emit('error', code='invalid_event', message='Invalid event or audio format. Check field types, limits and the required PCM format.')
            except Exception:
                await session.emit('error', code='provider_unavailable', message='The service is unavailable. Retry or use text mode.')
    except (WebSocketDisconnect, TimeoutError, RuntimeError):
        pass
    finally:
        app.state.sessions.pop(session.id, None)
        try:
            await session.close()
        except (WebSocketDisconnect, RuntimeError):
            pass


@app.websocket('/ws/body')
async def body_socket(socket: WebSocket):
    hello = await authenticate(socket, body=True)
    if hello is None:
        return
    session_id = hello['session_id']
    capabilities = hello.get('gestures', [])
    if not isinstance(capabilities, list) or len(capabilities) > 7 or any(value not in ('nod', 'shake', 'lean_in', 'point', 'wave', 'shrug', 'settle') for value in capabilities):
        await socket.close(code=1008)
        return
    attached = False
    try:
        app.state.body.attach(session_id, socket, capabilities)
        attached = True
        await socket.send_json({'type': 'paired', 'session_id': session_id, 'protocol_version': 1})
        owner = app.state.sessions.get(session_id)
        if owner:
            await owner.emit('body_status', connected=True, gestures=capabilities)
        count, window = 0, time.monotonic()
        while True:
            packet = await asyncio.wait_for(socket.receive(), 60)
            if packet['type'] == 'websocket.disconnect':
                break
            if packet.get('bytes') is not None:
                raise ValueError('Body socket is JSON-only')
            raw = packet.get('text') or ''
            if len(raw) > 2048:
                raise ValueError('Body event too large')
            if time.monotonic() - window > 1:
                window, count = time.monotonic(), 0
            count += 1
            if count > 20:
                raise ValueError('Body event rate exceeded')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Expected JSON object')
            if data.get('type') == 'gesture_ack':
                if data.get('status') not in ('completed', 'rejected') or not isinstance(data.get('command_id'), str):
                    raise ValueError('Invalid acknowledgement')
                app.state.body.acknowledge(session_id, data['command_id'], data['status'])
            elif data.get('type') == 'telemetry':
                angles = data.get('angles', [])
                if not isinstance(angles, list) or len(angles) != 2 or any(type(angle) not in (int, float) or not 0 <= angle <= 180 for angle in angles):
                    raise ValueError('Expected two finite servo angles from 0–180')
                owner = app.state.sessions.get(session_id)
                if owner:
                    await owner.emit('body_telemetry', angles=angles, reported_by='body')
            elif data.get('type') == 'ping':
                async with app.state.body.locks[session_id]:
                    await socket.send_json({'type': 'pong'})
            else:
                raise ValueError('Unsupported body event')
    except (ValueError, TimeoutError, WebSocketDisconnect, RuntimeError):
        try:
            await socket.close(code=1008)
        except RuntimeError:
            pass
    finally:
        if attached:
            app.state.body.detach(session_id)
            owner = app.state.sessions.get(session_id)
            if owner:
                try:
                    await owner.emit('body_status', connected=False)
                except (WebSocketDisconnect, RuntimeError):
                    pass
