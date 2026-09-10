"""Check that every element app.mjs looks up actually exists in index.html.

    python check_ids.py

A missing id is silent in the browser — `$('typo')` just returns null and the
feature quietly does nothing — so it is worth failing loudly here instead.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
html = (HERE / 'index.html').read_text(encoding='utf-8')
app = (HERE / 'app.mjs').read_text(encoding='utf-8')

defined = set(re.findall(r'id="([^"]+)"', html))
used = set(re.findall(r"\$\('([^']+)'\)", app))

missing = sorted(name for name in used if name not in defined)
unused = sorted(name for name in defined if name not in used)

print(f'{len(defined)} ids in index.html, {len(used)} looked up by app.mjs')
if missing:
    print('MISSING from index.html:')
    for name in missing:
        print(f'  {name}')
else:
    print('every id app.mjs looks up exists')
if unused:
    print(f'defined but never looked up (may be styled or static): {", ".join(unused)}')

sys.exit(1 if missing else 0)
