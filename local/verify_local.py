"""Verify the local edition's wiring without a network, a key or a microphone.

Run from the repo root:   python local\\verify_local.py

The pure-logic checks always run: routing tables, engine dispatch, voice
resolution, config validation and the resampler's maths. The engine checks run
only if faster-whisper and piper-tts are importable, and the synthesis checks
only if a voice model or espeak-ng is actually present, so this is useful both
before and after the models are downloaded.

Exits non-zero on any failure.
"""

import asyncio
import math
import os
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / 'ngnix-astra-brain-engine'
sys.path.insert(0, str(ENGINE))

# The local profile is the subject under test, so load it before brain.config
# reads the environment.
os.environ['BRAIN_ENV_FILE'] = str(Path(__file__).resolve().parent / '.env.local')

import numpy as np

from brain import speech as speech_tables
from brain import speech_local
from brain.config import Settings
from brain.tts_local import LocalTts, TARGET_SAMPLE_RATE, parse_wav, to_mono_24k

passed = failed = 0
skipped = []


def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f'  ok   {label}')
    else:
        failed += 1
        print(f'  FAIL {label}' + (f' -- {detail}' if detail else ''))


def section(title):
    print(f'\n{title}')


def skip(label, why):
    skipped.append(label)
    print(f'  skip {label} ({why})')


def wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    byte_rate = sample_rate * channels * 2
    return b''.join((
        b'RIFF', struct.pack('<I', 36 + len(pcm)), b'WAVEfmt ',
        struct.pack('<IHHIIHH', 16, 1, channels, sample_rate, byte_rate, channels * 2, 16),
        b'data', struct.pack('<I', len(pcm)), pcm,
    ))


def tone(samples: int, sample_rate: int, hz: float = 220.0, channels: int = 1) -> bytes:
    values = [
        int(12000 * math.sin(2 * math.pi * hz * index / sample_rate))
        for index in range(samples)
    ]
    if channels > 1:
        values = [value for value in values for _ in range(channels)]
    return b''.join(struct.pack('<h', value) for value in values)


settings = Settings.load()

# ---------------------------------------------------------------------------
section('Profile: the local edition is actually local')
check('LLM_PROVIDER is ollama', settings.llm_provider == 'ollama', settings.llm_provider)
check('STT_PROVIDER is local', settings.stt_provider == 'local', settings.stt_provider)
check('TTS_PROVIDER is local', settings.tts_provider == 'local', settings.tts_provider)
check('settings.offline is true', settings.offline)
check('no Azure OpenAI key in the local profile', not settings.openai_key)
check('no Azure Speech key in the local profile', not settings.speech_key)
check('no Deepgram key in the local profile', not settings.deepgram_key)
check('a microphone is still offered', settings.supports_microphone)
check('the server can still speak', settings.supports_server_tts)
check('local corpus is a separate index', settings.corpus_path == 'corpus.local.json', settings.corpus_path)
check('embedding id names the local model',
      settings.embedding_id == f'ollama:{settings.ollama_embed_model}', settings.embedding_id)
check('stt_engines reports the local engine',
      settings.stt_engines == (f'local-whisper:{settings.whisper_model}',), str(settings.stt_engines))

# ---------------------------------------------------------------------------
section('Ears: recognition routes locally and never off-machine')
for language in ('hi', 'en', 'ta', 'te', 'bn', 'mr', 'gu', 'kn', 'ml', 'pa'):
    plan = speech_tables.stt_plan(language, settings)
    check(f'{language} is heard by the local engine',
          plan == [('local', speech_local.WHISPER_LANGUAGES[language])], str(plan))

check('Odia has no local recognition at all', speech_tables.stt_plan('or', settings) == [])
check('Odia is the only recognition gap', speech_local.WHISPER_LANGUAGE_GAPS == ('or',))
check('no Whisper language table entry is unknown to Whisper',
      set(speech_local.WHISPER_LANGUAGES) == set(speech_tables.AZURE_STT_LOCALES) - {'or'})

# The point of the offline edition: even with cloud keys present, audio must not
# leave the machine while STT_PROVIDER=local.
with_keys = Settings.load().__class__(**{
    **{field: getattr(settings, field) for field in settings.__dataclass_fields__},
    'deepgram_key': 'x' * 32, 'speech_key': 'y' * 32,
})
check('a stray cloud key does not add a cloud failover',
      speech_tables.stt_plan('hi', with_keys) == [('local', 'hi')],
      str(speech_tables.stt_plan('hi', with_keys)))

# ---------------------------------------------------------------------------
section('Voice: every language the UI offers can be spoken')
table = speech_tables.voice_table(settings)
check('all eleven languages have a local voice',
      set(table) == set(speech_tables.AZURE_STT_LOCALES), str(sorted(set(speech_tables.AZURE_STT_LOCALES) - set(table))))
check('Hindi uses the Piper neural voice', table.get('hi') == 'hi_IN-pratham-medium', table.get('hi'))
check('Malayalam uses the Piper neural voice', table.get('ml') == 'ml_IN-arjun-medium', table.get('ml'))
check('English uses the Piper neural voice', table.get('en') == 'en_US-ryan-medium', table.get('en'))
check('Odia falls to espeak-ng', table.get('or') == 'or+m3', table.get('or'))
check('Tamil falls to espeak-ng', table.get('ta') == 'ta+m3', table.get('ta'))
check('Telugu falls to espeak-ng (no Piper model published)', table.get('te') == 'te+m3', table.get('te'))

check('resolve_voice picks the local voice, not an Azure name',
      speech_tables.resolve_voice('hi', None, settings) == 'hi_IN-pratham-medium')
check('resolve_voice honours a client override',
      speech_tables.resolve_voice('hi', 'en_US-ryan-medium', settings) == 'en_US-ryan-medium')
check('pinning espeak overrides Piper',
      speech_local.local_tts_plan('hi', 'espeak') == ('espeak', 'hi+m3'))
check('pinning piper refuses a language it lacks',
      speech_local.local_tts_plan('ta', 'piper') is None)

mouth = LocalTts(settings)
check('a Piper voice name dispatches to Piper', mouth.resolve('hi_IN-pratham-medium')[0] == 'piper')
check('an espeak voice name dispatches to espeak', mouth.resolve('or+m3')[0] == 'espeak')

# ---------------------------------------------------------------------------
section('Audio: the local mouth matches the pipeline contract')
# The whole pipeline assumes 24 kHz mono PCM16: RobotVoice, PlaybackClock and the
# browser's playback worklet all hard-code it.
source = tone(22050, 22050)
converted = to_mono_24k(source, 22050)
check('22.05 kHz is resampled to 24 kHz',
      abs(len(converted) // 2 - TARGET_SAMPLE_RATE) <= 2, f'{len(converted) // 2} samples')
check('resampled audio is complete 16-bit samples', len(converted) % 2 == 0)
check('resampling preserves amplitude',
      8000 < int(np.abs(np.frombuffer(converted, dtype="<i2")).max()) <= 12200,
      str(int(np.abs(np.frombuffer(converted, dtype='<i2')).max())))

stereo = tone(1000, 24000, channels=2)
check('stereo is downmixed to mono', len(to_mono_24k(stereo, 24000, 2)) == 2000)
check('24 kHz mono passes through untouched',
      to_mono_24k(tone(500, 24000), 24000) == tone(500, 24000))

pcm, rate, channels = parse_wav(wav(tone(300, 22050), 22050))
check('a RIFF/WAVE header is parsed', rate == 22050 and channels == 1 and len(pcm) == 600)

# espeak-ng on some builds writes a LIST chunk before the data chunk.
padded = wav(tone(300, 22050), 22050)
listed = padded[:36] + b'LIST' + struct.pack('<I', 4) + b'INFO' + padded[36:]
_, listed_rate, _ = parse_wav(listed)
check('an unknown chunk before the audio is skipped', listed_rate == 22050)

for label, payload in (('not a RIFF stream', b'nope' * 8), ('a header with no audio', wav(b'', 22050))):
    try:
        parse_wav(payload)
        check(f'{label} is rejected', False)
    except ValueError:
        check(f'{label} is rejected', True)

try:
    to_mono_24k(b'\x01', 24000)
    check('an odd-length PCM buffer is rejected', False)
except ValueError:
    check('an odd-length PCM buffer is rejected', True)

# ---------------------------------------------------------------------------
section('Config: the profile is validated, not merely read')
for name, value in (('STT_PROVIDER', 'telepathy'), ('TTS_PROVIDER', 'shouting'),
                    ('TTS_LOCAL_ENGINE', 'bagpipes'), ('WHISPER_DEVICE', 'tpu'),
                    ('WHISPER_COMPUTE_TYPE', 'float8'), ('WHISPER_BEAM_SIZE', '99')):
    original = os.environ.get(name)
    os.environ[name] = value
    try:
        Settings.load()
        check(f'{name}={value} is rejected', False)
    except ValueError:
        check(f'{name}={value} is rejected', True)
    finally:
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original

original = os.environ.get('BRAIN_ENV_FILE')
os.environ['BRAIN_ENV_FILE'] = str(ROOT / 'no-such-profile.env')
try:
    Settings.load()
    check('a missing BRAIN_ENV_FILE is an error, not a silent fallback', False)
except ValueError:
    check('a missing BRAIN_ENV_FILE is an error, not a silent fallback', True)
finally:
    os.environ['BRAIN_ENV_FILE'] = original

# ---------------------------------------------------------------------------
section('Engines: installed and loadable')
try:
    import faster_whisper  # noqa: F401
    check('faster-whisper imports', True)
except ImportError as error:
    skip('faster-whisper imports', str(error)[:60])

try:
    import piper  # noqa: F401
    check('piper imports', True)
except ImportError as error:
    skip('piper imports', str(error)[:60])

status = mouth.status()
print(f'  info Piper voices installed: {status["piper_voices_installed"] or "none"}')
print(f'  info espeak-ng available: {status["espeak_available"]}')
print(f'  info languages with a working voice right now: {len(status["languages_spoken"])}/11')

# ---------------------------------------------------------------------------
section('Synthesis: real audio from whichever engine is present')


async def speak(text, voice):
    return await mouth.synthesize(text, voice)


if status['espeak_available']:
    for language, voice, phrase in (('or', 'or+m3', 'ପାକ୍ସ କଣ'), ('ta', 'ta+m3', 'PACS என்றால் என்ன'),
                                    ('en', 'en-us+m3', 'All systems operational.')):
        try:
            pcm = asyncio.run(speak(phrase, voice))
            check(f'espeak-ng speaks {language}', len(pcm) > 2400 and len(pcm) % 2 == 0, f'{len(pcm)} bytes')
        except Exception as error:
            check(f'espeak-ng speaks {language}', False, str(error)[:120])
else:
    skip('espeak-ng synthesis', 'espeak-ng is not installed')

for language, voice in (('en', 'en_US-ryan-medium'), ('hi', 'hi_IN-pratham-medium')):
    if not mouth.piper.available(voice):
        skip(f'Piper speaks {language}', f'{voice} not downloaded')
        continue
    try:
        pcm = asyncio.run(speak('PACS is a village level cooperative.' if language == 'en'
                                else 'पैक्स एक गाँव स्तर की सहकारी समिति है।', voice))
        check(f'Piper speaks {language}', len(pcm) > 4800 and len(pcm) % 2 == 0, f'{len(pcm)} bytes')
    except Exception as error:
        check(f'Piper speaks {language}', False, str(error)[:120])

# A missing Piper model must degrade to espeak-ng rather than silence the robot.
if status['espeak_available']:
    try:
        pcm = asyncio.run(speak('Fallback check.', 'te_IN-nonexistent-medium'))
        check('a missing Piper model falls back to espeak-ng', len(pcm) > 2400, f'{len(pcm)} bytes')
    except Exception as error:
        check('a missing Piper model falls back to espeak-ng', False, str(error)[:120])
else:
    skip('Piper to espeak-ng fallback', 'espeak-ng is not installed')

# ---------------------------------------------------------------------------
section('Prompt: sized for a CPU model, persona intact')
from dataclasses import asdict

from brain.asr_local import MAXIMUM_PROMPT_CHARS, domain_prompt
from brain.persona import character_block, compact_character_block, persona_block
from brain.policy import Persona

dials = asdict(Persona())
full, compact = character_block(dials), compact_character_block(dials)
check('the local profile selects the compact persona',
      settings.effective_prompt_style == 'compact', settings.effective_prompt_style)
check('persona_block honours the style', persona_block(dials, 'compact') == compact)
check('the compact persona fits its latency budget', len(compact) <= 2800,
      f'{len(compact)} chars, about {len(compact) // 4} tokens')
check('the compact persona is much smaller than the full one', len(compact) < len(full) / 2.5,
      f'{len(compact)} vs {len(full)} chars')
# Everything below is what must survive the shrink.
check('compact keeps the TARS identity', 'TARS' in compact and 'military-surplus' in compact)
check('compact keeps the dial values', 'sarcasm' in compact and 'Current settings' in compact)
check('compact keeps the dial scales', 'HUMOUR' in compact and 'VERBOSITY' in compact)
check('compact keeps the drop-the-humour safety rule',
      'HARD RULE' in compact and 'worried' in compact and 'humour stops completely' in compact)
check('compact keeps the wit off the user', 'never at the user' in compact)
check('compact keeps the garbled-input rule', 'did not catch' in compact)
check('compact still carries worked examples', compact.count('TARS:') >= 6)
check('compact carries no negative examples for a small model to copy',
      'WRONG' not in compact)
check('an Azure profile would still get the full persona',
      Settings.load().__class__(**{**{f: getattr(settings, f) for f in settings.__dataclass_fields__},
                                   'llm_provider': 'azure', 'prompt_style': 'auto'}).effective_prompt_style == 'full')

prompt = domain_prompt(settings.keyterms)
check('Whisper is given the domain vocabulary', prompt and 'PACS' in prompt, str(prompt)[:80])
check('the domain prompt stays short enough not to be continued',
      len(prompt) <= MAXIMUM_PROMPT_CHARS, f'{len(prompt)} chars')
check('empty ASR_KEYTERMS disables prompting', domain_prompt(()) is None)

# ---------------------------------------------------------------------------
section('Indic NLP: script and number normalisation')
from brain import indic
from brain.policy import normalise_transcript

check('the Indic dependencies are installed for the offline edition', indic.available,
      'pip install -r local/requirements-local.txt')

if indic.available:
    state = indic.status()
    check('a script normaliser exists for all ten Indic languages',
          len(state['scripts_normalised']) == 10, str(state['scripts_normalised']))
    check('number words are generated for the eight languages policy.py lacks',
          len(state['numbers_generated']) == 8, str(state['numbers_generated']))
    check('Hindi and Marathi keep the hand-written table',
          set(state['numbers_hand_written']) == {'hi', 'mr'})

    from num_to_words import num_to_word
    for language, value in (('ta', 25), ('te', 7), ('bn', 12), ('gu', 30),
                            ('kn', 5), ('ml', 40), ('pa', 9), ('or', 60)):
        spoken = num_to_word(value, lang=language)
        got = normalise_transcript(spoken, language)
        check(f'{language}: {spoken!r} becomes {value}', got == str(value), f'got {got!r}')

    tamil_fifty = num_to_word(50, lang='ta')
    check('a persona dial spoken in Tamil numerals is understood',
          '50' in normalise_transcript(f'sarcasm {tamil_fifty}', 'ta'),
          normalise_transcript(f'sarcasm {tamil_fifty}', 'ta'))

    check('a compound number is not broken by its own parts',
          normalise_transcript(num_to_word(25, lang='ta'), 'ta') == '25',
          normalise_transcript(num_to_word(25, lang='ta'), 'ta'))

    for language, phrase in (('hi', 'पैक्स क्या है'), ('ta', 'PACS என்றால் என்ன'),
                             ('or', 'ପାକ୍ସ କଣ')):
        result = indic.normalise_script(phrase, language)
        check(f'{language}: script normalisation preserves the words', bool(result.strip()),
              repr(result))

    check('Hindi numbers still use the hand-written table',
          normalise_transcript('सारकाज़म पच्चीस', 'hi').endswith('25'),
          normalise_transcript('सारकाज़म पच्चीस', 'hi'))
    check('the PACS substitution still applies',
          'PACS' in normalise_transcript('पैक्स क्या है', 'hi'))
    check('English numbers still convert',
          normalise_transcript('set sarcasm to forty five', 'en').endswith('45'),
          normalise_transcript('set sarcasm to forty five', 'en'))
    check('normalise_transcript still works without a language argument',
          'PACS' in normalise_transcript('पैक्स क्या है'))
else:
    skip('Indic normalisation checks', 'indic-nlp-library not installed')

# ---------------------------------------------------------------------------
section('Robot voice: the TARS colouring still applies to local audio')
from brain.voice import RobotVoice, speech_chunks

filter_ = RobotVoice(amount=settings.robot_amount, clarity=settings.voice_clarity,
                     autotune=settings.voice_autotune)
plain = tone(4800, TARGET_SAMPLE_RATE)
coloured = filter_.process(plain)
check('colouring preserves the sample count', len(coloured) == len(plain))
check('colouring actually changes the audio', coloured != plain)
check('colouring stays inside 16-bit range',
      int(np.abs(np.frombuffer(coloured, dtype='<i2')).max()) <= 32767)
check('long replies are chunked for streaming',
      len(speech_chunks('PACS is a cooperative. ' * 40)) > 1)

# ---------------------------------------------------------------------------
print(f'\n{passed}/{passed + failed} checks passed' + (f', {len(skipped)} skipped' if skipped else ''))
if failed:
    print('The local edition is NOT ready.')
    sys.exit(1)
print('The local edition is wired: mic -> Whisper -> Ollama -> Piper/espeak-ng -> PCM.')
if skipped:
    print('Skipped checks need: pip install -r local/requirements-local.txt, '
          'then .\\local\\get_models.ps1')
