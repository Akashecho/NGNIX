import asyncio
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

import numpy as np

from .policy import expand_query


def official_url(url: str, allow_any: bool = False) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if allow_any:
        # Opt-in escape hatch for local demo corpora. Never enable in production:
        # it removes the guarantee that a citation points at an official source.
        return parsed.scheme in ('http', 'https') and bool(host) and not parsed.username
    return parsed.scheme == 'https' and not parsed.username and parsed.port in (None, 443) and (host.endswith('.gov.in') or host.endswith('.nic.in') or host in {'rbi.org.in', 'www.rbi.org.in'})


def tokens(text):
    return re.findall(r'[\w\u0900-\u097f]+', text.lower())


@dataclass
class Hit:
    id: str
    text: str
    title: str
    url: str
    jurisdiction: str
    updated_at: str
    cosine: float


class Index:
    def __init__(self, chunks, vectors):
        self.chunks = chunks
        self.vectors = np.asarray(vectors, dtype=np.float32)
        self.cache = OrderedDict()
        self.postings = defaultdict(list)
        self.lengths = []
        for index, chunk in enumerate(chunks):
            counts = Counter(tokens(chunk['text']))
            self.lengths.append(sum(counts.values()))
            for token, frequency in counts.items():
                self.postings[token].append((index, frequency))
        self.average_length = sum(self.lengths) / max(1, len(chunks)) or 1
        if chunks:
            if self.vectors.ndim != 2 or self.vectors.shape[0] != len(chunks) or not np.isfinite(self.vectors).all():
                raise ValueError('Invalid corpus vectors')
            norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise ValueError('Zero corpus embedding')
            self.vectors = self.vectors / norms

    @classmethod
    def load_default(cls, settings):
        path = Path(settings.corpus_path)
        if not path.exists():
            if settings.rag_required:
                raise RuntimeError('Official corpus missing. Build RAG_CORPUS_PATH with python -m brain.ingest before starting.')
            return cls([], [])
        data = json.loads(path.read_text(encoding='utf-8'))
        # 'embedding_id' identifies the provider and model; the legacy key is still accepted.
        stored = data.get('embedding_id', data.get('embedding_deployment'))
        if stored != settings.embedding_id:
            raise ValueError(f'Corpus was built with embeddings {stored!r} but the server uses {settings.embedding_id!r}; rebuild the corpus')
        chunks = data.get('chunks', [])
        if not chunks:
            raise ValueError('Corpus is empty')
        for chunk in chunks:
            if not all(isinstance(chunk.get(key), str) and chunk[key].strip() for key in ('text', 'title', 'url', 'jurisdiction', 'updated_at')):
                raise ValueError('Corpus requires text, title, official URL, jurisdiction and updated_at')
            if not official_url(chunk['url'], settings.allow_any_source):
                raise ValueError('Corpus source is not an approved official HTTPS URL')
        return cls(chunks, data['vectors'])

    def rank(self, queries, embeddings, top_k, floor, block_chars):
        query_vectors = np.asarray(embeddings, dtype=np.float32)
        if query_vectors.ndim != 2 or query_vectors.shape[1] != self.vectors.shape[1] or not np.isfinite(query_vectors).all():
            raise ValueError('Query embedding mismatch')
        norms = np.linalg.norm(query_vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError('Zero query embedding')
        similarities = self.vectors @ (query_vectors / norms).T
        # The original query gates evidence. Confusable variants must not silently replace intent.
        original_scores = similarities[:, 0]
        eligible = np.flatnonzero(original_scores >= floor)
        if not len(eligible):
            return []
        fused = defaultdict(float)
        for column, query in enumerate(queries):
            lexical = defaultdict(float)
            for token in set(tokens(query)):
                postings = self.postings.get(token, [])
                idf = math.log(1 + (len(self.chunks) - len(postings) + .5) / (len(postings) + .5))
                for index, frequency in postings:
                    lexical[index] += idf * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * self.lengths[index] / self.average_length))
            dense_order = eligible[np.argsort(-similarities[eligible, column])][:50]
            eligible_set = set(map(int, eligible))
            sparse_order = sorted((index for index in lexical if index in eligible_set), key=lambda i: lexical[i], reverse=True)[:50]
            for ranking in (dense_order, sparse_order):
                for rank, index in enumerate(ranking, 1):
                    fused[int(index)] += 1 / (60 + rank)
        selected = sorted(fused, key=lambda index: (-fused[index], index))[:top_k]
        return [Hit(id=f'S{number}', text=self.chunks[index]['text'][:block_chars], title=self.chunks[index]['title'],
                    url=self.chunks[index]['url'], jurisdiction=self.chunks[index]['jurisdiction'], updated_at=self.chunks[index]['updated_at'],
                    cosine=float(original_scores[index])) for number, index in enumerate(selected, 1)]

    async def search(self, query, providers, settings):
        if not self.chunks:
            return []
        queries = expand_query(query)
        key = tuple(queries)
        embeddings = self.cache.get(key)
        if embeddings is None:
            embeddings = await providers.embed(queries)
            self.cache[key] = embeddings
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        return await asyncio.to_thread(self.rank, queries, embeddings, settings.top_k, settings.confidence_floor, settings.block_chars)
