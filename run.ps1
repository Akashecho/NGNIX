<#
    Starts the TARS stack for local development.

    With LLM_PROVIDER=azure (the default) the reasoning model runs in Azure
    OpenAI, so nothing needs to be downloaded and Ollama is not started:
      1. Astra brain engine       (port 8000)
      2. Static studio frontend   (port 8080)

    With LLM_PROVIDER=ollama the local model server is started first and the
    required models are pulled if missing.

    Usage:  .\run.ps1
            .\run.ps1 -Rebuild     # also rebuild the RAG corpus first
#>
param([switch]$Rebuild)

$ErrorActionPreference = 'Stop'
$root     = $PSScriptRoot
$engine   = Join-Path $root 'ngnix-astra-brain-engine'
$frontend = Join-Path $root 'ngnix-astra-studio-demo'

function Test-Port([int]$Port) {
    try { (New-Object Net.Sockets.TcpClient).Connect('127.0.0.1', $Port); return $true } catch { return $false }
}

# Reads a KEY=VALUE from .env. A real environment variable wins, matching the
# precedence the backend itself applies.
function Get-Setting([string]$Name, [string]$Default = '') {
    $live = [Environment]::GetEnvironmentVariable($Name)
    if ($live) { return $live }
    $envFile = Join-Path $engine '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            $trimmed = $line.Trim()
            if ($trimmed -like '#*' -or $trimmed -notmatch '=') { continue }
            $key, $value = $trimmed -split '=', 2
            if ($key.Trim() -eq $Name) { return $value.Trim().Trim('"').Trim("'") }
        }
    }
    return $Default
}

Write-Host '== TARS local stack ==' -ForegroundColor Cyan

$provider = (Get-Setting 'LLM_PROVIDER' 'azure').ToLower()
$sttPreference = (Get-Setting 'STT_PROVIDER' 'auto').ToLower()
$deepgramKey = Get-Setting 'DEEPGRAM_API_KEY'
$speechKey   = Get-Setting 'AZURE_SPEECH_KEY'

Write-Host "[..]   brain: $provider"

# 1. Brain ------------------------------------------------------------------
if ($provider -eq 'azure') {
    $endpoint = Get-Setting 'AZURE_OPENAI_ENDPOINT'
    # AZURE_OPENAI_API_KEY is the name the Azure portal uses; the backend accepts either.
    $openaiKey = Get-Setting 'AZURE_OPENAI_KEY'
    if (-not $openaiKey) { $openaiKey = Get-Setting 'AZURE_OPENAI_API_KEY' }
    if (-not $endpoint) {
        throw "LLM_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT. Set it in ngnix-astra-brain-engine\.env, or switch LLM_PROVIDER=ollama to run locally."
    }
    if ($endpoint -like '*your-resource*') {
        throw "AZURE_OPENAI_ENDPOINT is still the placeholder ($endpoint). Put your real resource endpoint in ngnix-astra-brain-engine\.env."
    }
    if (-not $openaiKey) {
        throw "LLM_PROVIDER=azure needs AZURE_OPENAI_KEY (or AZURE_OPENAI_API_KEY). Set it in ngnix-astra-brain-engine\.env, or switch LLM_PROVIDER=ollama to run locally."
    }
    Write-Host "[ok]   Azure OpenAI deployment: $(Get-Setting 'AZURE_OPENAI_LLM_DEPLOYMENT' 'gpt-4.1-nano') at $endpoint"
} else {
    if (Test-Port 11434) {
        Write-Host '[ok]   Ollama already running on 11434'
    } else {
        Write-Host '[..]   starting Ollama'
        Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 0; $i -lt 20 -and -not (Test-Port 11434); $i++) { Start-Sleep -Milliseconds 500 }
        if (Test-Port 11434) { Write-Host '[ok]   Ollama up' } else { throw 'Ollama failed to start. Is it installed?' }
    }
    $tags = (curl.exe -s -m 10 http://127.0.0.1:11434/api/tags)
    foreach ($model in @((Get-Setting 'OLLAMA_LLM_MODEL' 'qwen3.5:0.8b'), (Get-Setting 'OLLAMA_EMBED_MODEL' 'nomic-embed-text'))) {
        if ($tags -notlike "*$model*") {
            Write-Host "[..]   pulling missing model $model"
            ollama pull $model
        } else {
            Write-Host "[ok]   model present: $model"
        }
    }
}

# 2. Speech -----------------------------------------------------------------
# Neither key is fatal: the stack still answers typed questions.
if ($deepgramKey) { Write-Host "[ok]   ears: Deepgram $(Get-Setting 'ASR_MODEL' 'nova-3')" }
if ($speechKey)   { Write-Host '[ok]   ears fallback + voice: Azure Speech' }
if (-not $deepgramKey -and -not $speechKey) {
    Write-Host '[warn] no DEEPGRAM_API_KEY and no AZURE_SPEECH_KEY: the microphone is disabled and replies are spoken by the browser.' -ForegroundColor Yellow
} elseif (-not $deepgramKey) {
    Write-Host '[warn] no DEEPGRAM_API_KEY: all speech recognition goes to Azure Speech.' -ForegroundColor Yellow
} elseif (-not $speechKey) {
    Write-Host '[warn] no AZURE_SPEECH_KEY: no server voice, and Malayalam/Odia cannot be transcribed (nova-3 has no model for them).' -ForegroundColor Yellow
}
if ($sttPreference -eq 'deepgram' -and -not $deepgramKey) { throw 'STT_PROVIDER=deepgram needs DEEPGRAM_API_KEY.' }
if ($sttPreference -eq 'azure' -and -not $speechKey) { throw 'STT_PROVIDER=azure needs AZURE_SPEECH_KEY.' }

# 3. Corpus -----------------------------------------------------------------
# The corpus stores the embedding model that built it and the server refuses to
# start on a mismatch, so a provider switch requires a rebuild.
$corpus = Join-Path $engine 'corpus.index.json'
$expected = if ($provider -eq 'ollama') { 'ollama:' + (Get-Setting 'OLLAMA_EMBED_MODEL' 'nomic-embed-text') }
            else { Get-Setting 'AZURE_OPENAI_EMBED_DEPLOYMENT' 'text-embedding-3-large' }
$stale = $false
if (Test-Path $corpus) {
    try {
        $stored = (Get-Content $corpus -Raw | ConvertFrom-Json).embedding_id
        if ($stored -ne $expected) {
            Write-Host "[..]   corpus was built with '$stored' but this provider uses '$expected'"
            $stale = $true
        }
    } catch { $stale = $true }
}
if ($Rebuild -or $stale -or -not (Test-Path $corpus)) {
    Write-Host '[..]   building RAG corpus'
    Push-Location $engine
    try { python -m brain.ingest seed_corpus.jsonl } finally { Pop-Location }
}
Write-Host "[ok]   corpus ready ($expected)"

# 4. Backend ---------------------------------------------------------------
if (Test-Port 8000) {
    Write-Host '[ok]   backend already running on 8000'
} else {
    Write-Host '[..]   starting brain engine'
    Start-Process -FilePath 'python' -ArgumentList '-m','uvicorn','brain.app:app','--host','127.0.0.1','--port','8000' `
        -WorkingDirectory $engine -WindowStyle Hidden
    for ($i = 0; $i -lt 40 -and -not (Test-Port 8000); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Port 8000)) { throw 'Backend failed to start. Run it in the foreground to see the error: cd ngnix-astra-brain-engine; python -m uvicorn brain.app:app' }
}
Write-Host "[ok]   health: $(curl.exe -s -m 10 http://127.0.0.1:8000/health)"

# 5. Frontend --------------------------------------------------------------
if (Test-Port 8080) {
    Write-Host '[ok]   frontend already running on 8080'
} else {
    Write-Host '[..]   serving frontend'
    # Port 8080 matters: it must match BRAIN_ALLOWED_ORIGINS in .env.
    Start-Process -FilePath 'python' -ArgumentList '-m','http.server','8080','--bind','127.0.0.1' `
        -WorkingDirectory $frontend -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Port 8080); $i++) { Start-Sleep -Milliseconds 500 }
}

Write-Host ''
Write-Host 'Open http://localhost:8080 in your browser.' -ForegroundColor Green
Write-Host 'Stop everything with: .\stop.ps1'
