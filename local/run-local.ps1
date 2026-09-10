<#
    Starts the fully-offline TARS edition.

    Nothing in this script touches ngnix-astra-brain-engine\.env, so your Azure
    keys stay where they are. The edition is selected with BRAIN_ENV_FILE, which
    points the backend at local\.env.local instead. Run .\run.ps1 from the repo
    root at any time to get the cloud edition back.

    Everything runs on this machine:
      Ollama            local LLM + embeddings   (port 11434)
      faster-whisper    local speech recognition (in-process)
      Piper / espeak-ng local speech synthesis   (in-process)
      brain engine      (port 8000)
      studio frontend   (port 8080)

    Usage:  .\local\run-local.ps1
            .\local\run-local.ps1 -Rebuild     # force a corpus rebuild
            .\local\run-local.ps1 -Check       # verify the stack, do not start it
#>
param([switch]$Rebuild, [switch]$Check)

$ErrorActionPreference = 'Stop'
$root     = Split-Path $PSScriptRoot -Parent
$engine   = Join-Path $root 'ngnix-astra-brain-engine'
$frontend = Join-Path $root 'ngnix-astra-studio-demo'
$profile  = Join-Path $PSScriptRoot '.env.local'

if (-not (Test-Path $profile)) { throw "Missing profile: $profile" }

function Test-Port([int]$Port) {
    try { (New-Object Net.Sockets.TcpClient).Connect('127.0.0.1', $Port); return $true } catch { return $false }
}

# Reads a setting from the local profile, not from .env.
function Get-Local([string]$Name, [string]$Default = '') {
    foreach ($line in Get-Content $profile) {
        $trimmed = $line.Trim()
        if ($trimmed -like '#*' -or $trimmed -notmatch '=') { continue }
        $key, $value = $trimmed -split '=', 2
        if ($key.Trim() -eq $Name) { return $value.Trim().Trim('"').Trim("'") }
    }
    return $Default
}

Write-Host '== TARS local edition (fully offline) ==' -ForegroundColor Cyan

# The one variable that selects the edition. Every child process inherits it.
$env:BRAIN_ENV_FILE = $profile
Write-Host "[ok]   profile: local\.env.local"

$llm     = Get-Local 'OLLAMA_LLM_MODEL' 'qwen2.5:3b-instruct'
$embed   = Get-Local 'OLLAMA_EMBED_MODEL' 'nomic-embed-text'
$whisper = Get-Local 'WHISPER_MODEL' 'small'
$corpus  = Get-Local 'RAG_CORPUS_PATH' 'corpus.local.json'

# 1. Ollama ---------------------------------------------------------------
if (Test-Port 11434) {
    Write-Host '[ok]   Ollama already running on 11434'
} else {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        throw 'Ollama is not installed. Get it from https://ollama.com/download'
    }
    Write-Host '[..]   starting Ollama'
    Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Port 11434); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Port 11434) { Write-Host '[ok]   Ollama up' } else { throw 'Ollama failed to start.' }
}

$tags = (curl.exe -s -m 10 http://127.0.0.1:11434/api/tags)
foreach ($model in @($llm, $embed)) {
    if ($tags -notlike "*$model*") {
        Write-Host "[..]   pulling $model (first run only)"
        ollama pull $model
    } else {
        Write-Host "[ok]   model present: $model"
    }
}

# 2. Local speech engines -------------------------------------------------
Push-Location $engine
try {
    python -c "import faster_whisper" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "faster-whisper is missing. Install it with: pip install -r local\requirements-local.txt"
    }
    Write-Host "[ok]   ears: faster-whisper '$whisper' (local)"

    python -c "import piper" 2>$null
    $piperOk = ($LASTEXITCODE -eq 0)
} finally { Pop-Location }

if ($piperOk) { Write-Host '[ok]   voice: Piper (en, hi, ml)' }
else { Write-Host '[warn] piper-tts missing; all languages fall back to espeak-ng' -ForegroundColor Yellow }

$espeak = (Get-Command espeak-ng -ErrorAction SilentlyContinue).Source
if (-not $espeak) {
    $espeak = @('C:\Program Files\eSpeak NG\espeak-ng.exe', 'C:\Program Files (x86)\eSpeak NG\espeak-ng.exe') |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if ($espeak) {
    Write-Host '[ok]   voice: espeak-ng (ta, te, bn, mr, gu, kn, pa, or)'
} else {
    Write-Host '[warn] espeak-ng not found: only en, hi and ml will have a voice.' -ForegroundColor Yellow
    Write-Host '       winget install eSpeak-NG.eSpeak-NG' -ForegroundColor Yellow
}

# Odia has no local recognition at all: Whisper has no Odia model.
Write-Host '[note] Odia can be spoken but not heard locally (Whisper has no Odia model).'

# 3. Corpus ---------------------------------------------------------------
# The local edition keeps its own index because the embedding spaces differ.
$corpusPath = Join-Path $engine $corpus
$expected = 'ollama:' + $embed
$stale = $false
if (Test-Path $corpusPath) {
    try {
        $stored = (Get-Content $corpusPath -Raw | ConvertFrom-Json).embedding_id
        if ($stored -ne $expected) {
            Write-Host "[..]   corpus was built with '$stored', this profile needs '$expected'"
            $stale = $true
        }
    } catch { $stale = $true }
}
if ($Rebuild -or $stale -or -not (Test-Path $corpusPath)) {
    Write-Host '[..]   building local RAG corpus (embeds 12 chunks through Ollama)'
    Push-Location $engine
    try { python -m brain.ingest seed_corpus.jsonl } finally { Pop-Location }
}
Write-Host "[ok]   corpus ready ($expected -> $corpus)"

if ($Check) {
    Write-Host ''
    Write-Host 'Check complete; nothing was started.' -ForegroundColor Green
    exit 0
}

# 4. Backend -------------------------------------------------------------
if (Test-Port 8000) {
    Write-Host '[ok]   backend already running on 8000'
    Write-Host '[warn] it may be the Azure edition. Run .\stop.ps1 first to be sure.' -ForegroundColor Yellow
} else {
    Write-Host '[..]   starting brain engine'
    Start-Process -FilePath 'python' -ArgumentList '-m','uvicorn','brain.app:app','--host','127.0.0.1','--port','8000' `
        -WorkingDirectory $engine -WindowStyle Hidden
    for ($i = 0; $i -lt 60 -and -not (Test-Port 8000); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Port 8000)) {
        throw "Backend failed to start. See the error with: cd $engine; `$env:BRAIN_ENV_FILE='$profile'; python -m uvicorn brain.app:app"
    }
}
Write-Host "[ok]   health: $(curl.exe -s -m 20 http://127.0.0.1:8000/health)"

# 5. Frontend ------------------------------------------------------------
if (Test-Port 8080) {
    Write-Host '[ok]   frontend already running on 8080'
} else {
    Write-Host '[..]   serving frontend'
    Start-Process -FilePath 'python' -ArgumentList '-m','http.server','8080','--bind','127.0.0.1' `
        -WorkingDirectory $frontend -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Port 8080); $i++) { Start-Sleep -Milliseconds 500 }
}

Write-Host ''
Write-Host 'Open http://localhost:8080 - this instance makes no network calls.' -ForegroundColor Green
Write-Host 'Stop everything with: .\stop.ps1'
