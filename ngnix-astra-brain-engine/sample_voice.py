"""Listen to the TARS voice without a browser.

Synthesises a phrase with Azure Speech neural TTS, applies the same robot
colouring the client hears, and writes a playable WAV per language.

    python sample_voice.py                       # one sample per language
    python sample_voice.py hi "आप कैसे हैं?"      # one language, custom text
    python sample_voice.py --plain                # skip the robot filter

Files land in voice-samples/ next to this script.
"""

import asyncio
from pathlib import Path
import sys

import httpx

from brain.config import Settings
from brain.providers import azure_speech_synthesize
from brain.speech import AZURE_VOICES, wav_container
from brain.voice import RobotVoice

PHRASES = {
    'hi': 'नमस्ते, मैं TARS हूँ। पैक्स एक गाँव स्तर की सहकारी ऋण समिति है।',
    'en': 'Hello, I am TARS. A PACS is a village level cooperative credit society.',
    'ta': 'வணக்கம், நான் TARS. PACS என்பது ஒரு கிராம அளவிலான கூட்டுறவு கடன் சங்கம்.',
    'te': 'నమస్కారం, నేను TARS. PACS ఒక గ్రామ స్థాయి సహకార రుణ సంఘం.',
    'bn': 'নমস্কার, আমি TARS। PACS একটি গ্রাম পর্যায়ের সমবায় ঋণ সমিতি।',
    'mr': 'नमस्कार, मी TARS आहे. PACS ही गाव पातळीवरील सहकारी पतसंस्था आहे.',
    'gu': 'નમસ્તે, હું TARS છું. PACS એ ગામ સ્તરની સહકારી ધિરાણ મંડળી છે.',
    'kn': 'ನಮಸ್ಕಾರ, ನಾನು TARS. PACS ಒಂದು ಗ್ರಾಮ ಮಟ್ಟದ ಸಹಕಾರಿ ಸಾಲ ಸಂಘ.',
    'ml': 'നമസ്കാരം, ഞാൻ TARS ആണ്. PACS ഒരു ഗ്രാമതല സഹകരണ വായ്പാ സംഘമാണ്.',
    'pa': 'ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ TARS ਹਾਂ। PACS ਇੱਕ ਪਿੰਡ ਪੱਧਰ ਦੀ ਸਹਿਕਾਰੀ ਕਰਜ਼ਾ ਸਭਾ ਹੈ।',
    'or': 'ନମସ୍କାର, ମୁଁ TARS। PACS ଏକ ଗ୍ରାମ ସ୍ତରୀୟ ସହଯୋଗୀ ଋଣ ସମିତି।',
}


async def main() -> int:
    settings = Settings.load()
    if not settings.speech_key:
        print('AZURE_SPEECH_KEY is required')
        return 2

    arguments = [value for value in sys.argv[1:] if not value.startswith('--')]
    plain = '--plain' in sys.argv
    compare = '--compare' in sys.argv
    if arguments:
        language = arguments[0]
        if language not in AZURE_VOICES:
            print(f'unknown language {language!r}; choose from {", ".join(AZURE_VOICES)}')
            return 2
        wanted = {language: arguments[1] if len(arguments) > 1 else PHRASES[language]}
    else:
        wanted = PHRASES

    output = Path(__file__).parent / 'voice-samples'
    output.mkdir(exist_ok=True)
    presets = ([('raw', 0, 0, 0)] if plain else
               [('flat', .18, 0.0, 0.0), ('clear', .18, .6, 0.0),
                ('clear-autotune', .18, .6, .35), ('more-autotune', .18, .8, .7)] if compare else
               [('', settings.robot_amount, settings.voice_clarity, settings.voice_autotune)])
    print(f'region={settings.speech_region} presets={[p[0] or "current" for p in presets]} -> {output}')

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        for language, phrase in wanted.items():
            voice = AZURE_VOICES[language]
            try:
                pcm = await azure_speech_synthesize(settings, client, phrase, voice)
            except Exception as error:
                print(f'  {language:3s} {voice:24s} FAILED {error!r}')
                continue
            for name, amount, clarity, autotune in presets:
                shaped = pcm
                if amount or clarity or autotune:
                    shaped = RobotVoice(amount, 24000, clarity, autotune).process(pcm)
                suffix = f'-{name}' if name else ''
                path = output / f'tars-{language}{suffix}.wav'
                path.write_bytes(wav_container(shaped, 24000))
                print(f'  {language:3s} {voice:24s} {len(shaped) / 48000:5.2f} s  {path.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
