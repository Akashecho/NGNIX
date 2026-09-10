from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from .speech import DOMAIN_KEYTERMS


def boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {'true', 'false', '1', '0'}:
        raise ValueError(f'{name} must be true or false')
    return value in {'true', '1'}


def parse_keyterms(value: str) -> tuple[str, ...]:
    """Keyterm prompts for Deepgram: 'auto' uses the domain list, '' disables."""
    setting = (value or '').strip()
    if setting.lower() == 'auto':
        return DOMAIN_KEYTERMS
    return tuple(term.strip() for term in setting.split(',') if term.strip())


def load_env_file(filename: str = '.env') -> None:
    """Load KEY=VALUE lines from a local .env file.

    Real environment variables always win, so this never overrides a value the
    operator set deliberately. Searches the working directory and its parents so
    the server can be started from either the repo root or the package folder.

    BRAIN_ENV_FILE names a different profile to load instead, which is how the
    two editions coexist: the cloud keys stay in .env while the offline edition
    runs from local/.env.local, and neither launcher overwrites the other's
    configuration. A path that is set but missing is an error rather than a
    silent fallback to .env, because that would quietly start the wrong edition.
    """
    override = os.getenv('BRAIN_ENV_FILE', '').strip()
    if override:
        candidate = Path(override)
        if not candidate.is_file():
            raise ValueError(f'BRAIN_ENV_FILE points at {override}, which does not exist')
        read_env_file(candidate)
        return
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / filename
        if not candidate.is_file():
            continue
        read_env_file(candidate)
        return


def read_env_file(path: Path) -> None:
    """Apply one KEY=VALUE file to the environment without overriding it."""
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    session_token: str
    allowed_origins: tuple[str, ...]
    openai_endpoint: str
    openai_key: str
    api_version: str
    llm_deployment: str
    embedding_deployment: str
    speech_key: str
    speech_region: str
    speech_key_fallback: str
    speech_region_fallback: str
    deepgram_key: str
    asr_model: str
    asr_language: str
    stt_provider: str
    keyterms: tuple[str, ...]
    voice: str
    corpus_path: str
    rag_required: bool
    confidence_floor: float
    top_k: int
    block_chars: int
    tools_enabled: bool
    robot_amount: float
    voice_clarity: float
    voice_autotune: float
    voice_rate: float
    voice_pitch: float
    max_sessions: int
    turn_timeout: float
    # Provider selection and local-model support.
    llm_provider: str = 'azure'
    ollama_endpoint: str = 'http://127.0.0.1:11434'
    ollama_llm_model: str = 'qwen3.5:0.8b'
    ollama_embed_model: str = 'nomic-embed-text'
    ollama_think: bool = False
    # Offline speech. TTS_PROVIDER picks the mouth independently of the brain, so
    # a local model can still speak through Azure and vice versa.
    tts_provider: str = 'azure'
    tts_local_engine: str = 'auto'
    piper_model_dir: str = 'models/piper'
    espeak_binary: str = 'espeak-ng'
    whisper_model: str = 'small'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    whisper_cpu_threads: int = 0
    whisper_beam_size: int = 1
    # 'full' or 'compact'. Prompt length dominates local latency, so a local brain
    # defaults to the compact persona; 'auto' resolves per provider.
    prompt_style: str = 'auto'
    # Hands-free listening. The wake word is matched against the transcript, so it
    # needs no extra model and works in every language the ASR covers.
    wake_word: str = 'hey tars'
    wake_word_required: bool = False
    citation_mode: str = 'strict'
    allow_any_source: bool = False
    assistant_mode: str = 'grounded'
    temperature: float = 0.4
    search_api_key: str = ''

    @property
    def open_domain(self) -> bool:
        return self.assistant_mode == 'open'

    @property
    def embedding_id(self) -> str:
        """Identity of the embedding space, used to detect a stale corpus."""
        if self.llm_provider == 'ollama':
            return f'ollama:{self.ollama_embed_model}'
        return self.embedding_deployment

    @property
    def effective_prompt_style(self) -> str:
        """'full' or 'compact', resolving 'auto' by provider.

        A cloud deployment evaluates a long prompt in well under a second, so it
        keeps the full persona with all its worked examples. A local model on CPU
        pays roughly a second per sixty prompt tokens, which makes the same block
        cost tens of seconds a turn, so it gets the compact one.
        """
        if self.prompt_style != 'auto':
            return self.prompt_style
        return 'compact' if self.llm_provider == 'ollama' else 'full'

    @property
    def local_speech(self) -> bool:
        """True when the mouth runs on this machine rather than in Azure."""
        return self.tts_provider == 'local'

    @property
    def local_ears(self) -> bool:
        """True when recognition can run locally for at least some languages."""
        return self.stt_provider == 'local'

    @property
    def offline(self) -> bool:
        """True when no turn needs the network: local brain, ears and voice."""
        return self.llm_provider == 'ollama' and self.local_speech and self.local_ears

    @property
    def supports_server_tts(self) -> bool:
        """Whether the server can speak at all, by either engine."""
        return self.local_speech or bool(self.speech_key)

    @property
    def speech_resources(self) -> tuple[tuple[str, str], ...]:
        """Azure Speech (key, region) pairs to try in order.

        A second resource in another region is optional. When configured, both
        recognition and synthesis retry against it if the primary region refuses
        the request, which covers a regional outage or a throttled key.
        """
        resources = [(self.speech_key, self.speech_region)] if self.speech_key else []
        if self.speech_key_fallback and self.speech_region_fallback:
            resources.append((self.speech_key_fallback, self.speech_region_fallback))
        return tuple(resources)

    @property
    def supports_microphone(self) -> bool:
        return self.local_ears or bool(self.deepgram_key or self.speech_key)

    @property
    def stt_engines(self) -> tuple[str, ...]:
        """ASR engines that are actually configured, in preference order."""
        if self.local_ears:
            return (f'local-whisper:{self.whisper_model}',)
        engines = []
        if self.deepgram_key:
            engines.append(f'deepgram:{self.asr_model}')
        if self.speech_key:
            engines.append('azure-speech')
        if self.stt_provider == 'azure':
            engines.reverse()
        return tuple(engines)

    @classmethod
    def load(cls):
        load_env_file()
        endpoint = os.getenv('AZURE_OPENAI_ENDPOINT', '').rstrip('/')
        if endpoint and (urlparse(endpoint).scheme != 'https' or not urlparse(endpoint).hostname):
            raise ValueError('AZURE_OPENAI_ENDPOINT must be an HTTPS URL')
        ollama_endpoint = os.getenv('OLLAMA_ENDPOINT', 'http://127.0.0.1:11434').rstrip('/')
        parsed_ollama = urlparse(ollama_endpoint)
        if parsed_ollama.scheme not in {'http', 'https'} or not parsed_ollama.hostname:
            raise ValueError('OLLAMA_ENDPOINT must be an HTTP(S) URL')
        config = cls(
            session_token=os.getenv('BRAIN_SESSION_TOKEN', ''),
            allowed_origins=tuple(x.strip() for x in os.getenv('BRAIN_ALLOWED_ORIGINS', 'http://localhost:5173,http://localhost:8080').split(',') if x.strip()),
            openai_endpoint=endpoint,
            # AZURE_OPENAI_API_KEY is accepted as an alias for the name used by the Azure portal.
            openai_key=os.getenv('AZURE_OPENAI_KEY', '') or os.getenv('AZURE_OPENAI_API_KEY', ''),
            api_version=os.getenv('AZURE_OPENAI_API_VERSION', '2024-10-21'),
            llm_deployment=os.getenv('AZURE_OPENAI_LLM_DEPLOYMENT', 'gpt-4.1-nano'),
            embedding_deployment=os.getenv('AZURE_OPENAI_EMBED_DEPLOYMENT', 'text-embedding-3-large'),
            speech_key=os.getenv('AZURE_SPEECH_KEY', ''), speech_region=os.getenv('AZURE_SPEECH_REGION', 'centralindia'),
            speech_key_fallback=os.getenv('AZURE_SPEECH_KEY_FALLBACK', ''),
            speech_region_fallback=os.getenv('AZURE_SPEECH_REGION_FALLBACK', ''),
            deepgram_key=os.getenv('DEEPGRAM_API_KEY', ''), asr_model=os.getenv('ASR_MODEL', 'nova-3'),
            # 'auto' picks the language parameter per turn from brain/speech.py.
            asr_language=os.getenv('ASR_LANGUAGE', 'auto'),
            stt_provider=os.getenv('STT_PROVIDER', 'auto').strip().lower(),
            # Domain vocabulary boosted in nova-3. 'auto' uses the cooperative
            # terms in brain/speech.py; a comma-separated list replaces them and
            # an empty value disables keyterm prompting.
            keyterms=parse_keyterms(os.getenv('ASR_KEYTERMS', 'auto')),
            voice=os.getenv('TARS_VOICE', 'hi-IN-MadhurNeural'),
            corpus_path=os.getenv('RAG_CORPUS_PATH', 'corpus.index.json'), rag_required=boolean('RAG_REQUIRED', True),
            confidence_floor=float(os.getenv('RAG_CONFIDENCE_FLOOR', '.32')), top_k=int(os.getenv('RAG_TOP_K', '4')),
            block_chars=int(os.getenv('RAG_BLOCK_CHARS', '700')), tools_enabled=boolean('TOOLS_ENABLED', True),
            robot_amount=float(os.getenv('ROBOT_AMOUNT', '.18')),
            voice_clarity=float(os.getenv('VOICE_CLARITY', '.6')),
            voice_autotune=float(os.getenv('VOICE_AUTOTUNE', '.35')),
            voice_rate=float(os.getenv('VOICE_RATE', '1.15')),
            voice_pitch=float(os.getenv('VOICE_PITCH', '-10')), max_sessions=int(os.getenv('BRAIN_MAX_SESSIONS', '8')),
            turn_timeout=float(os.getenv('BRAIN_TURN_TIMEOUT', '60')),
            llm_provider=os.getenv('LLM_PROVIDER', 'azure').strip().lower(),
            ollama_endpoint=ollama_endpoint,
            ollama_llm_model=os.getenv('OLLAMA_LLM_MODEL', 'qwen3.5:0.8b'),
            ollama_embed_model=os.getenv('OLLAMA_EMBED_MODEL', 'nomic-embed-text'),
            ollama_think=boolean('OLLAMA_THINK', False),
            tts_provider=os.getenv('TTS_PROVIDER', 'azure').strip().lower(),
            tts_local_engine=os.getenv('TTS_LOCAL_ENGINE', 'auto').strip().lower(),
            piper_model_dir=os.getenv('PIPER_MODEL_DIR', 'models/piper'),
            espeak_binary=os.getenv('ESPEAK_BINARY', 'espeak-ng'),
            whisper_model=os.getenv('WHISPER_MODEL', 'small').strip(),
            whisper_device=os.getenv('WHISPER_DEVICE', 'cpu').strip().lower(),
            whisper_compute_type=os.getenv('WHISPER_COMPUTE_TYPE', 'int8').strip().lower(),
            whisper_cpu_threads=int(os.getenv('WHISPER_CPU_THREADS', '0')),
            whisper_beam_size=int(os.getenv('WHISPER_BEAM_SIZE', '1')),
            prompt_style=os.getenv('PROMPT_STYLE', 'auto').strip().lower(),
            wake_word=os.getenv('WAKE_WORD', 'hey tars').strip(),
            wake_word_required=boolean('WAKE_WORD_REQUIRED', False),
            citation_mode=os.getenv('RAG_CITATION_MODE', 'strict').strip().lower(),
            allow_any_source=boolean('RAG_ALLOW_ANY_SOURCE', False),
            assistant_mode=os.getenv('ASSISTANT_MODE', 'grounded').strip().lower(),
            temperature=float(os.getenv('LLM_TEMPERATURE', '0.4')),
            search_api_key=os.getenv('SEARCH_API_KEY', ''),
        )
        if config.llm_provider not in {'ollama', 'azure'}:
            raise ValueError('LLM_PROVIDER must be ollama or azure')
        if config.stt_provider not in {'auto', 'deepgram', 'azure', 'local'}:
            raise ValueError('STT_PROVIDER must be auto, deepgram, azure or local')
        if config.tts_provider not in {'azure', 'local'}:
            raise ValueError('TTS_PROVIDER must be azure or local')
        if config.prompt_style not in {'auto', 'full', 'compact'}:
            raise ValueError('PROMPT_STYLE must be auto, full or compact')
        if config.tts_local_engine not in {'auto', 'piper', 'espeak'}:
            raise ValueError('TTS_LOCAL_ENGINE must be auto, piper or espeak')
        if config.whisper_device not in {'cpu', 'cuda', 'auto'}:
            raise ValueError('WHISPER_DEVICE must be cpu, cuda or auto')
        if config.whisper_compute_type not in {'int8', 'int8_float16', 'int8_float32', 'float16', 'float32'}:
            raise ValueError('WHISPER_COMPUTE_TYPE must be int8, int8_float16, int8_float32, float16 or float32')
        if not 0 <= config.whisper_cpu_threads <= 64:
            raise ValueError('WHISPER_CPU_THREADS must be between 0 and 64')
        if not 1 <= config.whisper_beam_size <= 10:
            raise ValueError('WHISPER_BEAM_SIZE must be between 1 and 10')
        if not re.fullmatch(r'[a-z0-9-]+', config.speech_region):
            raise ValueError('AZURE_SPEECH_REGION must be a lowercase region name such as centralindia')
        if config.citation_mode not in {'strict', 'lenient', 'flag'}:
            raise ValueError('RAG_CITATION_MODE must be strict, lenient or flag')
        if config.assistant_mode not in {'grounded', 'open'}:
            raise ValueError('ASSISTANT_MODE must be grounded or open')
        if not 0 <= config.temperature <= 1.5:
            raise ValueError('LLM_TEMPERATURE must be between 0 and 1.5')
        if not 0 <= config.robot_amount <= .5:
            raise ValueError('ROBOT_AMOUNT must be between 0 and 0.5')
        if not 0 <= config.voice_clarity <= 1.5:
            raise ValueError('VOICE_CLARITY must be between 0 and 1.5')
        if not 0 <= config.voice_autotune <= 1:
            raise ValueError('VOICE_AUTOTUNE must be between 0 and 1')
        if not 0.5 <= config.voice_rate <= 2:
            raise ValueError('VOICE_RATE must be between 0.5 and 2')
        if not -50 <= config.voice_pitch <= 50:
            raise ValueError('VOICE_PITCH must be between -50 and 50')
        if not 0 <= config.confidence_floor <= 1 or not 1 <= config.top_k <= 10:
            raise ValueError('Invalid retrieval threshold or top K')
        if not 100 <= config.block_chars <= 4000 or not 1 <= config.max_sessions <= 100:
            raise ValueError('Invalid context or resource limits')
        # The ceiling is generous because the offline edition runs recognition,
        # generation and synthesis on one CPU: a Pi-class machine can legitimately
        # spend two minutes on a turn that Azure answers in four seconds.
        if not 5 <= config.turn_timeout <= 300:
            raise ValueError('BRAIN_TURN_TIMEOUT must be between 5 and 300 seconds')
        # The offline edition must be offline in fact, not just by intention.
        #
        # Real environment variables deliberately outrank the profile file, which
        # means a machine with AZURE_OPENAI_API_KEY or AZURE_SPEECH_KEY exported
        # at the user level would hand those credentials to an edition whose whole
        # purpose is to touch no network. Nothing would visibly break — the
        # routing already sends every turn to the local engines — but the
        # guarantee would be aspirational rather than enforced, and one future
        # code path that reads a key would silently start making calls.
        #
        # So when the profile asks for a local brain, local ears and a local
        # voice, the cloud credentials are discarded here, at the single point
        # where configuration becomes fact.
        if config.llm_provider == 'ollama' and config.stt_provider == 'local' and config.tts_provider == 'local':
            config = replace(config, openai_key='', speech_key='', speech_key_fallback='', deepgram_key='')
        return config

    def validate_server(self):
        if len(self.session_token) < 24:
            raise ValueError('Set BRAIN_SESSION_TOKEN to a random secret of at least 24 characters')
        if not self.allowed_origins or '*' in self.allowed_origins:
            raise ValueError('Configure explicit BRAIN_ALLOWED_ORIGINS')
        if self.llm_provider == 'azure' and (not self.openai_endpoint or not self.openai_key):
            raise ValueError('LLM_PROVIDER=azure requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY')
        if self.stt_provider == 'deepgram' and not self.deepgram_key:
            raise ValueError('STT_PROVIDER=deepgram requires DEEPGRAM_API_KEY')
        if self.stt_provider == 'azure' and not self.speech_key:
            raise ValueError('STT_PROVIDER=azure requires AZURE_SPEECH_KEY')
