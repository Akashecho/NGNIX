#!/bin/sh
# Start the cloud edition backend.
#
# The server refuses to start without a RAG index, and the index records which
# embedding model built it. It cannot be baked into the image because building it
# needs live Azure credentials, which belong in the runtime environment and not
# in a layer. So it is built here on first boot, and rebuilt if the configured
# embedding model no longer matches.
set -eu

CORPUS="${RAG_CORPUS_PATH:-corpus.index.json}"
EXPECTED="${AZURE_OPENAI_EMBED_DEPLOYMENT:-text-embedding-3-large}"

needs_build=1
if [ -f "$CORPUS" ]; then
    stored=$(python -c "
import json,sys
try:
    print(json.load(open('$CORPUS', encoding='utf-8')).get('embedding_id',''))
except Exception:
    print('')
" 2>/dev/null || echo '')
    if [ "$stored" = "$EXPECTED" ]; then
        needs_build=0
        echo "[entrypoint] corpus present ($stored)"
    else
        echo "[entrypoint] corpus was built with '$stored', this deployment needs '$EXPECTED'"
    fi
fi

if [ "$needs_build" = "1" ]; then
    echo "[entrypoint] building RAG corpus"
    python -m brain.ingest seed_corpus.jsonl
fi

echo "[entrypoint] starting on port ${PORT:-8000}"
exec python -m uvicorn brain.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
