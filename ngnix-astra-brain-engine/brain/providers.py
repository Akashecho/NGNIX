from dataclasses import dataclass
from html import escape
import json
import re
import secrets
from urllib.parse import quote

import httpx

THINK_BLOCK = re.compile(r'<think>.*?</think>', re.DOTALL)


@dataclass
class Completion:
    text: str
    calls: list[dict]


async def azure_speech_synthesize(settings, client: httpx.AsyncClient, text: str, voice: str) -> bytes:
    """Azure Speech neural TTS returning raw 24 kHz mono PCM16.

    The mouth is independent of the brain: this is used whichever LLM provider is
    selected, as long as AZURE_SPEECH_KEY is set. If a fallback resource in
    another region is configured, a refusal from the primary region is retried
    there before the turn is given up on.
    """
    resources = settings.speech_resources
    if not resources:
        raise RuntimeError('Azure Speech is not configured')
    if not re.fullmatch(r'[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural', voice):
        raise ValueError('Invalid Azure voice name')
    language = '-'.join(voice.split('-')[:2])
    rate = round((getattr(settings, 'voice_rate', 1.15) - 1) * 100)
    pitch = round(getattr(settings, 'voice_pitch', -10))
    ssml = (f'<speak version="1.0" xml:lang="{language}"><voice name="{voice}">'
            f'<prosody pitch="{pitch:+d}%" rate="{rate:+d}%">{escape(text)}</prosody></voice></speak>')

    last_error = None
    for key, region in resources:
        if not re.fullmatch(r'[a-z0-9-]+', region):
            raise ValueError('Invalid Azure Speech region')
        try:
            response = await client.post(f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1',
                headers={'Ocp-Apim-Subscription-Key': key, 'Content-Type': 'application/ssml+xml',
                         'X-Microsoft-OutputFormat': 'raw-24khz-16bit-mono-pcm'}, content=ssml.encode())
            response.raise_for_status()
            if not response.content or len(response.content) % 2 or len(response.content) > 24000 * 2 * 90:
                raise ValueError('Invalid PCM response')
            return response.content
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
    raise last_error


class SpeechMixin:
    """Shared mouth for both brains.

    TTS_PROVIDER selects the engine independently of LLM_PROVIDER, so an Azure
    brain can speak through Piper and a local brain through Azure Speech. The
    local engine is constructed on first use: importing it pulls in onnxruntime,
    which the Azure edition has no reason to pay for.
    """

    _local_tts = None

    @property
    def supports_tts(self) -> bool:
        return self.settings.supports_server_tts

    @property
    def local_tts(self):
        if self._local_tts is None:
            from .tts_local import LocalTts

            self._local_tts = LocalTts(self.settings)
        return self._local_tts

    async def synthesize(self, text: str, voice: str) -> bytes:
        if getattr(self.settings, 'local_speech', False):
            return await self.local_tts.synthesize(text, voice)
        return await azure_speech_synthesize(self.settings, self.client, text, voice)


class AzureProviders(SpeechMixin):
    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    def url(self, deployment, operation):
        if not self.settings.openai_key or not self.settings.openai_endpoint:
            raise RuntimeError('Azure OpenAI is not configured')
        return f'{self.settings.openai_endpoint}/openai/deployments/{quote(deployment, safe="")}/{operation}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self.client.post(self.url(self.settings.embedding_deployment, 'embeddings'),
            params={'api-version': self.settings.api_version}, headers={'api-key': self.settings.openai_key},
            json={'input': texts})
        response.raise_for_status()
        data = sorted(response.json()['data'], key=lambda item: item['index'])
        if len(data) != len(texts):
            raise ValueError('Incomplete embedding response')
        return [item['embedding'] for item in data]

    async def complete(self, messages, tools=None, on_delta=None) -> Completion:
        body = {'messages': messages, 'temperature': self.settings.temperature, 'max_tokens': 350, 'stream': True}
        if tools:
            body['tools'], body['tool_choice'] = tools, 'auto'
        text, calls = [], {}
        async with self.client.stream('POST', self.url(self.settings.llm_deployment, 'chat/completions'),
                params={'api-version': self.settings.api_version}, headers={'api-key': self.settings.openai_key}, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith('data: '):
                    continue
                payload = line[6:]
                if payload == '[DONE]':
                    break
                packet = json.loads(payload)
                if 'error' in packet:
                    raise RuntimeError('Azure completion failed')
                for choice in packet.get('choices', []):
                    delta = choice.get('delta', {})
                    if delta.get('content'):
                        text.append(delta['content'])
                        if on_delta:
                            await on_delta(delta['content'])
                    for fragment in delta.get('tool_calls', []):
                        index = fragment['index']
                        if index >= 8:
                            raise ValueError('Too many tool calls')
                        call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        if fragment.get('id'):
                            call['id'] = fragment['id']
                        fn = fragment.get('function', {})
                        call['function']['name'] += fn.get('name', '')
                        call['function']['arguments'] += fn.get('arguments', '')
                        if len(call['function']['arguments']) > 4096:
                            raise ValueError('Tool arguments too large')
                if sum(map(len, text)) > 10000:
                    raise ValueError('Completion too large')
        return Completion(''.join(text), [calls[key] for key in sorted(calls)])


class OllamaProviders(SpeechMixin):
    """Local model backend speaking Ollama's native API.

    Presents the same surface as AzureProviders so Session needs no branching:
    tool calls are normalised to the OpenAI shape (string arguments plus an id)
    because tools.py and session.py both assume that format. Speech output still
    goes through Azure Speech when a key is configured.
    """

    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self._think_supported = settings.ollama_think

    @property
    def base(self) -> str:
        return self.settings.ollama_endpoint.rstrip('/')

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self.client.post(f'{self.base}/api/embed',
            json={'model': self.settings.ollama_embed_model, 'input': texts})
        response.raise_for_status()
        vectors = response.json().get('embeddings') or []
        if len(vectors) != len(texts):
            raise ValueError('Incomplete embedding response')
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                raise ValueError('Malformed embedding response')
        return vectors

    def _payload(self, messages, tools, think):
        body = {
            'model': self.settings.ollama_llm_model,
            'messages': messages,
            'stream': True,
            'options': {'temperature': .2, 'num_predict': 400},
        }
        # Reasoning traces are suppressed: a small model would otherwise spend the
        # whole token budget thinking and return an empty answer.
        if not think:
            body['think'] = False
        if tools:
            body['tools'] = tools
        return body

    async def complete(self, messages, tools=None, on_delta=None) -> Completion:
        try:
            return await self._stream(messages, tools, self._think_supported, on_delta)
        except httpx.HTTPStatusError as error:
            # Older Ollama builds reject the "think" field outright; retry once without it.
            if error.response.status_code != 400 or self._think_supported:
                raise
            self._think_supported = True
            return await self._stream(messages, tools, True, on_delta)

    async def _stream(self, messages, tools, think, on_delta) -> Completion:
        text, calls = [], []
        async with self.client.stream('POST', f'{self.base}/api/chat', json=self._payload(messages, tools, think)) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                packet = json.loads(line)
                if packet.get('error'):
                    raise RuntimeError('Ollama completion failed')
                message = packet.get('message') or {}
                content = message.get('content') or ''
                if content:
                    text.append(content)
                    if on_delta:
                        await on_delta(content)
                for fragment in message.get('tool_calls') or []:
                    function = fragment.get('function') or {}
                    arguments = function.get('arguments')
                    if len(calls) >= 8:
                        raise ValueError('Too many tool calls')
                    calls.append({
                        'id': 'call_' + secrets.token_hex(8),
                        'type': 'function',
                        'function': {
                            'name': function.get('name', ''),
                            # Ollama returns a decoded object; downstream expects a JSON string.
                            'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False),
                        },
                    })
                if sum(map(len, text)) > 10000:
                    raise ValueError('Completion too large')
                if packet.get('done'):
                    break
        answer = THINK_BLOCK.sub('', ''.join(text)).strip()
        return Completion(answer, calls)


def build_providers(settings, client: httpx.AsyncClient):
    """Select a provider implementation from configuration."""
    if settings.llm_provider == 'ollama':
        return OllamaProviders(settings, client)
    return AzureProviders(settings, client)
