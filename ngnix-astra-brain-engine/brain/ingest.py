import argparse
import asyncio
from datetime import date
import json
import os
from pathlib import Path

import httpx

from .config import Settings
from .providers import build_providers
from .rag import Index, official_url


def chunk_text(text: str, size: int = 700, overlap: int = 100):
    text = ' '.join(text.split())
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind(' ', start + size // 2, end)
            if boundary > start:
                end = boundary
        yield text[start:end]
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        boundary = text.find(' ', next_start, end)
        start = boundary + 1 if boundary >= next_start else next_start


async def build(input_path: Path, output_path: Path | None = None):
    settings = Settings.load()
    # Resolved from settings rather than from a default computed at argument-parse
    # time: RAG_CORPUS_PATH usually arrives from the profile file, which is only
    # read once Settings.load() runs. Reading os.environ earlier silently wrote
    # every edition's index to the same file and mixed two embedding spaces.
    if output_path is None:
        output_path = Path(settings.corpus_path)
    chunks = []
    with input_path.open(encoding='utf-8') as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            document = json.loads(line)
            required = ('text', 'title', 'url', 'jurisdiction', 'updated_at')
            if not all(isinstance(document.get(key), str) and document[key].strip() for key in required):
                raise ValueError(f'Line {line_number}: missing source text or provenance')
            if not official_url(document['url'], settings.allow_any_source):
                raise ValueError(f'Line {line_number}: official HTTPS source required')
            date.fromisoformat(document['updated_at'])
            for text in chunk_text(document['text'], settings.block_chars):
                chunks.append({**{key: document[key] for key in required if key != 'text'}, 'text': text})
    if not chunks:
        raise ValueError('Refusing to create an empty corpus')
    vectors = []
    async with httpx.AsyncClient(timeout=120) as client:
        providers = build_providers(settings, client)
        for offset in range(0, len(chunks), 32):
            batch = [chunk['text'] for chunk in chunks[offset:offset + 32]]
            vectors.extend(await providers.embed(batch))
            print(f'Embedded {min(offset + 32, len(chunks))}/{len(chunks)} chunks')
    Index(chunks, vectors)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + '.tmp')
    temporary.write_text(json.dumps({'embedding_id': settings.embedding_id, 'chunks': chunks, 'vectors': vectors}, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, output_path)
    print(f'Indexed {len(chunks)} chunks into {output_path} using embeddings {settings.embedding_id}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Embed reviewed official documents from JSONL: text, title, url, jurisdiction, updated_at (YYYY-MM-DD).')
    parser.add_argument('input', type=Path)
    # No default: an omitted --output means "wherever RAG_CORPUS_PATH says", which
    # is only known after the profile is loaded inside build().
    parser.add_argument('--output', type=Path, default=None,
                        help='Override the output index path (default: RAG_CORPUS_PATH)')
    args = parser.parse_args()
    asyncio.run(build(args.input, args.output))
