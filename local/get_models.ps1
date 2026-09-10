<#
    Downloads every model the local edition needs, once, so that afterwards the
    stack runs with the network unplugged.

      .\local\get_models.ps1                 # beta tier for this laptop
      .\local\get_models.ps1 -Tier gpu       # 24 GB RAM / 8 GB VRAM machine
      .\local\get_models.ps1 -Tier pi        # Raspberry Pi 5
      .\local\get_models.ps1 -SkipLlm        # voices and Whisper only

    Sizes on disk, per tier:
      pi     ~1.4 GB   qwen2.5:1.5b + whisper base  + 3 Piper voices
      beta   ~3.0 GB   qwen2.5:3b   + whisper small + 3 Piper voices
      gpu    ~6.5 GB   qwen2.5:7b   + whisper large-v3-turbo + 3 Piper voices
#>
param(
    [ValidateSet('pi', 'beta', 'gpu')][string]$Tier = 'beta',
    [switch]$SkipLlm,
    [switch]$SkipWhisper,
    [switch]$SkipVoices
)

$ErrorActionPreference = 'Stop'
$root   = Split-Path $PSScriptRoot -Parent
$engine = Join-Path $root 'ngnix-astra-brain-engine'
$voices = Join-Path $engine 'models\piper'

# Per-tier model choices. These are the values that go into .env; the README
# explains why each one is the ceiling for that hardware.
$tiers = @{
    pi   = @{ llm = 'qwen2.5:1.5b-instruct'; whisper = 'base';             compute = 'int8' }
    beta = @{ llm = 'qwen2.5:3b-instruct';   whisper = 'small';            compute = 'int8' }
    gpu  = @{ llm = 'qwen2.5:7b-instruct';   whisper = 'large-v3-turbo';   compute = 'float16' }
}
$choice = $tiers[$Tier]

Write-Host "== TARS local models: $Tier tier ==" -ForegroundColor Cyan

# 1. Piper voices ----------------------------------------------------------
# Only Hindi, Malayalam and English have published Piper voices that fit this
# project. Every other language is spoken by espeak-ng, which needs no download.
if (-not $SkipVoices) {
    New-Item -ItemType Directory -Force -Path $voices | Out-Null
    $base = 'https://huggingface.co/rhasspy/piper-voices/resolve/main'
    $wanted = @(
        @{ name = 'en_US-ryan-medium';    path = 'en/en_US/ryan/medium' },
        @{ name = 'hi_IN-pratham-medium'; path = 'hi/hi_IN/pratham/medium' },
        @{ name = 'ml_IN-arjun-medium';   path = 'ml/ml_IN/arjun/medium' }
    )
    foreach ($voice in $wanted) {
        foreach ($extension in @('.onnx', '.onnx.json')) {
            $target = Join-Path $voices ($voice.name + $extension)
            if ((Test-Path $target) -and (Get-Item $target).Length -gt 1kb) {
                Write-Host "[ok]   voice present: $($voice.name)$extension"
                continue
            }
            $url = "$base/$($voice.path)/$($voice.name)$extension"
            Write-Host "[..]   downloading $($voice.name)$extension"
            try {
                Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing -TimeoutSec 600
                $mb = [math]::Round((Get-Item $target).Length / 1MB, 1)
                Write-Host "[ok]   $($voice.name)$extension ($mb MB)"
            } catch {
                Write-Host "[warn] failed: $($voice.name)$extension - $($_.Exception.Message)" -ForegroundColor Yellow
                if (Test-Path $target) { Remove-Item $target -Force }
            }
        }
    }
}

# 2. Whisper -------------------------------------------------------------
# faster-whisper caches into ~/.cache/huggingface on first use. Doing it here
# means the first spoken turn is not a two minute download.
if (-not $SkipWhisper) {
    Write-Host "[..]   fetching Whisper '$($choice.whisper)' ($($choice.compute))"
    $fetch = @"
from faster_whisper import WhisperModel
WhisperModel('$($choice.whisper)', device='cpu', compute_type='int8')
print('cached')
"@
    Push-Location $engine
    try {
        $fetch | python -
        if ($LASTEXITCODE -eq 0) { Write-Host "[ok]   Whisper $($choice.whisper) cached" }
        else { Write-Host '[warn] Whisper download failed. Is faster-whisper installed?' -ForegroundColor Yellow }
    } finally { Pop-Location }
}

# 3. Local LLM and embeddings -------------------------------------------
if (-not $SkipLlm) {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Host '[warn] ollama is not installed. Get it from https://ollama.com/download' -ForegroundColor Yellow
    } else {
        foreach ($model in @($choice.llm, 'nomic-embed-text')) {
            Write-Host "[..]   ollama pull $model"
            ollama pull $model
        }
    }
}

# 4. espeak-ng ----------------------------------------------------------
# A system package, not a model, but the edition cannot speak eight of its
# eleven languages without it, so report its absence loudly. The Windows
# installer does not add itself to PATH, so check where it actually lands.
$espeakPaths = @(
    'C:\Program Files\eSpeak NG\espeak-ng.exe',
    'C:\Program Files (x86)\eSpeak NG\espeak-ng.exe'
)
$espeak = (Get-Command espeak-ng -ErrorAction SilentlyContinue).Source
if (-not $espeak) { $espeak = $espeakPaths | Where-Object { Test-Path $_ } | Select-Object -First 1 }

if ($espeak) {
    Write-Host "[ok]   espeak-ng present (Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Punjabi, Odia)"
    Write-Host "       $espeak"
} else {
    Write-Host '[warn] espeak-ng NOT found. Only English, Hindi and Malayalam will have a voice.' -ForegroundColor Yellow
    Write-Host '       Windows: winget install eSpeak-NG.eSpeak-NG' -ForegroundColor Yellow
    Write-Host '       Pi/Debian: sudo apt install -y espeak-ng' -ForegroundColor Yellow
}

Write-Host ''
Write-Host "Done. Set these in ngnix-astra-brain-engine\.env for the $Tier tier:" -ForegroundColor Green
Write-Host "  OLLAMA_LLM_MODEL=$($choice.llm)"
Write-Host "  WHISPER_MODEL=$($choice.whisper)"
Write-Host "  WHISPER_COMPUTE_TYPE=$($choice.compute)"
Write-Host 'Then start it with: .\local\run-local.ps1'
