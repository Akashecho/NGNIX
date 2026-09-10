import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRET_KEYS = (
    'AZURE_OPENAI_KEY', 'AZURE_OPENAI_API_KEY', 'AZURE_SPEECH_KEY',
    'AZURE_SPEECH_KEY_FALLBACK', 'DEEPGRAM_API_KEY', 'SEARCH_API_KEY',
)

secrets = {}
env_file = ROOT / 'ngnix-astra-brain-engine' / '.env'
if env_file.is_file():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        if '=' not in line or line.strip().startswith('#'):
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in SECRET_KEYS and len(value) >= 20:
            secrets[key] = value

token_file = Path(__file__).resolve().parent / 'local' / '_none'
import os
prod = Path(os.getenv('TEMP', '.')) / 'tars_production_token.txt'
if prod.is_file():
    secrets['BRAIN_SESSION_TOKEN_PRODUCTION'] = prod.read_text(encoding='utf-8').strip()

staged = subprocess.run(['git', 'ls-files'],
                        capture_output=True, text=True, cwd=ROOT).stdout.split()
print(f'{len(staged)} tracked files, {len(secrets)} live secrets to look for')

if not secrets:
    print('No live secrets found to check against. Is ngnix-astra-brain-engine/.env present?')

leaks = []
for name in staged:
    path = ROOT / name
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for label, value in secrets.items():
        if value and value in text:
            leaks.append((name, label))

generic = re.compile(r'(?:api[_-]?key|secret|password|token)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{32,})')
suspicious = []
for name in staged:
    path = ROOT / name
    if not path.is_file() or path.suffix in {'.md'}:
        continue
    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for match in generic.finditer(text):
        value = match.group(1)
        if value.startswith('tars-local-dev-only'):
            continue
        suspicious.append((name, value[:12] + '...'))

if leaks:
    print('\nLEAK: a live credential appears in staged content')
    for name, label in leaks:
        print(f'  {name}  <- {label}')
if suspicious:
    print('\nLong secret-shaped strings worth an eye:')
    for name, value in sorted(set(suspicious)):
        print(f'  {name}  {value}')

if leaks:
    sys.exit(1)
print('\nClean: no live credential from .env appears in any staged file.')
