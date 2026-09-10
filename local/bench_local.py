"""Measure where a local turn spends its time, so tuning is based on numbers.

Usage:  python local\\bench_local.py [model]
"""
import json
import sys
import time
import urllib.request

MODEL = sys.argv[1] if len(sys.argv) > 1 else 'qwen2.5:3b-instruct'


def call(system, user, predict=60):
    body = json.dumps({
        'model': MODEL,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'stream': False, 'think': False, 'options': {'temperature': 0.2, 'num_predict': predict},
    }).encode()
    request = urllib.request.Request('http://127.0.0.1:11434/api/chat', body,
                                     {'Content-Type': 'application/json'})
    started = time.time()
    payload = json.load(urllib.request.urlopen(request, timeout=900))
    return time.time() - started, payload


print(f'model: {MODEL}\n')
print(f'{"prompt tokens":>14} {"wall":>8} {"prompt eval":>12} {"gen":>10} {"prompt tok/s":>13} {"gen tok/s":>10}')

for repeats in (1, 100, 400, 800):
    system = 'PACS is a village level cooperative credit society. ' * repeats
    wall, payload = call(system, 'Reply with one short sentence.')
    prompt_tokens = payload.get('prompt_eval_count') or 0
    prompt_seconds = (payload.get('prompt_eval_duration') or 0) / 1e9
    gen_tokens = payload.get('eval_count') or 0
    gen_seconds = (payload.get('eval_duration') or 0) / 1e9
    print(f'{prompt_tokens:>14} {wall:>7.1f}s {prompt_seconds:>11.1f}s {gen_seconds:>9.1f}s '
          f'{prompt_tokens / max(prompt_seconds, 0.01):>13.0f} {gen_tokens / max(gen_seconds, 0.01):>10.1f}')
