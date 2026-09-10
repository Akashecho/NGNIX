<#
    Deploys the cloud edition backend to Azure Container Apps and wires the
    Vercel frontend to it.

        .\deploy-backend.ps1
        .\deploy-backend.ps1 -SkipBuild      # reuse the existing image, just update config

    Why a script rather than a few commands: the Azure keys must not appear on a
    command line, where they land in shell history and in the deployment record.
    Everything is read from ngnix-astra-brain-engine\.env and pushed as Container
    Apps *secrets*, which environment variables then reference with secretref:.

    A fresh BRAIN_SESSION_TOKEN is generated rather than reusing the development
    one, because dev-config.mjs is a static file and is publicly served by Vercel.
#>
param(
    [string]$ResourceGroup = 'rg-tars-sih',
    [string]$Location      = 'centralindia',
    [string]$AppName       = 'tars-brain',
    [string]$FrontendUrl   = 'https://ngnix-astra-studio-demo.vercel.app',
    [switch]$SkipBuild
)

# Deliberately NOT 'Stop'. The az CLI writes warnings to stderr — the containerapp
# extension announces itself on every call — and with 'Stop' PowerShell turns any
# native stderr output into a terminating error, so the deploy died on a warning.
# Every az call below checks $LASTEXITCODE explicitly instead.
$ErrorActionPreference = 'Continue'
$engine = Join-Path (Split-Path $PSScriptRoot -Parent) 'ngnix-astra-brain-engine'
if (-not (Test-Path $engine)) { $engine = Join-Path $PSScriptRoot 'ngnix-astra-brain-engine' }
$envFile = Join-Path $engine '.env'
if (-not (Test-Path $envFile)) { throw "Missing $envFile" }

# Read KEY=VALUE without printing anything.
$settings = @{}
foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if ($trimmed -like '#*' -or $trimmed -notmatch '=') { continue }
    $key, $value = $trimmed -split '=', 2
    $settings[$key.Trim()] = $value.Trim().Trim('"').Trim("'")
}

function Need([string]$Name) {
    if (-not $settings[$Name]) { throw "$Name is missing from $envFile" }
    return $settings[$Name]
}

$openaiKey   = Need 'AZURE_OPENAI_KEY'
$openaiHost  = Need 'AZURE_OPENAI_ENDPOINT'
$speechKey   = Need 'AZURE_SPEECH_KEY'
$speechRegion = Need 'AZURE_SPEECH_REGION'
$deepgramKey = $settings['DEEPGRAM_API_KEY']
$speechKeyFallback = $settings['AZURE_SPEECH_KEY_FALLBACK']
$speechRegionFallback = $settings['AZURE_SPEECH_REGION_FALLBACK']

# A production token, distinct from the committed development one.
$tokenFile = Join-Path $env:TEMP 'tars_production_token.txt'
if (Test-Path $tokenFile) {
    $sessionToken = (Get-Content $tokenFile -Raw).Trim()
    Write-Host '[ok]   reusing the production token from this session'
} else {
    $sessionToken = (python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    Set-Content -Path $tokenFile -Value $sessionToken -NoNewline
    Write-Host "[ok]   generated a fresh BRAIN_SESSION_TOKEN ($($sessionToken.Length) chars)"
}
if ($sessionToken -eq $settings['BRAIN_SESSION_TOKEN']) {
    throw 'The production token must not equal the development token in .env'
}

Write-Host "[..]   deploying $AppName to $ResourceGroup ($Location)"

if (-not $SkipBuild) {
    # `up` builds the image in Azure Container Registry, so no local Docker build
    # is needed, and creates the Container Apps environment on first run.
    # Note: `up` does not accept --only-show-errors, unlike the other commands.
    az containerapp up `
        --name $AppName `
        --resource-group $ResourceGroup `
        --location $Location `
        --source $engine `
        --ingress external `
        --target-port 8000
    if ($LASTEXITCODE -ne 0) { throw 'Container build or create failed' }
    Write-Host '[ok]   image built and app created'
}

# Secrets first, so the environment variables below can reference them and no
# value is ever passed as a plain env var.
Write-Host '[..]   setting secrets'
$secretArgs = @(
    "openai-key=$openaiKey",
    "speech-key=$speechKey",
    "session-token=$sessionToken"
)
if ($deepgramKey) { $secretArgs += "deepgram-key=$deepgramKey" }
if ($speechKeyFallback) { $secretArgs += "speech-key-fallback=$speechKeyFallback" }

az containerapp secret set --name $AppName --resource-group $ResourceGroup `
    --secrets $secretArgs --only-show-errors | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to set secrets' }
Write-Host "[ok]   $($secretArgs.Count) secrets stored"

Write-Host '[..]   setting configuration'
$envVars = @(
    'AZURE_OPENAI_KEY=secretref:openai-key',
    "AZURE_OPENAI_ENDPOINT=$openaiHost",
    "AZURE_OPENAI_API_VERSION=$($settings['AZURE_OPENAI_API_VERSION'])",
    "AZURE_OPENAI_LLM_DEPLOYMENT=$($settings['AZURE_OPENAI_LLM_DEPLOYMENT'])",
    "AZURE_OPENAI_EMBED_DEPLOYMENT=$($settings['AZURE_OPENAI_EMBED_DEPLOYMENT'])",
    'AZURE_SPEECH_KEY=secretref:speech-key',
    "AZURE_SPEECH_REGION=$speechRegion",
    "TARS_VOICE=$($settings['TARS_VOICE'])",
    'BRAIN_SESSION_TOKEN=secretref:session-token',
    # Exact origin allowlist: wildcards are rejected by the backend by design.
    "BRAIN_ALLOWED_ORIGINS=$FrontendUrl",
    "STT_PROVIDER=$($settings['STT_PROVIDER'])",
    "ASR_MODEL=$($settings['ASR_MODEL'])",
    "ASR_LANGUAGE=$($settings['ASR_LANGUAGE'])",
    "ASR_KEYTERMS=$($settings['ASR_KEYTERMS'])",
    'LLM_PROVIDER=azure',
    "ASSISTANT_MODE=$($settings['ASSISTANT_MODE'])",
    "RAG_CITATION_MODE=$($settings['RAG_CITATION_MODE'])",
    "RAG_ALLOW_ANY_SOURCE=$($settings['RAG_ALLOW_ANY_SOURCE'])",
    "RAG_TOP_K=$($settings['RAG_TOP_K'])",
    "RAG_BLOCK_CHARS=$($settings['RAG_BLOCK_CHARS'])",
    "RAG_CONFIDENCE_FLOOR=$($settings['RAG_CONFIDENCE_FLOOR'])",
    "TOOLS_ENABLED=$($settings['TOOLS_ENABLED'])",
    "LLM_TEMPERATURE=$($settings['LLM_TEMPERATURE'])",
    "BRAIN_TURN_TIMEOUT=$($settings['BRAIN_TURN_TIMEOUT'])",
    "BRAIN_MAX_SESSIONS=$($settings['BRAIN_MAX_SESSIONS'])",
    "WAKE_WORD=$($settings['WAKE_WORD'])",
    "PERSONA_HUMOUR=$($settings['PERSONA_HUMOUR'])",
    "PERSONA_SARCASM=$($settings['PERSONA_SARCASM'])",
    "PERSONA_HONESTY=$($settings['PERSONA_HONESTY'])",
    "PERSONA_WARMTH=$($settings['PERSONA_WARMTH'])",
    "PERSONA_VERBOSITY=$($settings['PERSONA_VERBOSITY'])",
    "PERSONA_FORMALITY=$($settings['PERSONA_FORMALITY'])",
    "PERSONA_CONFIDENCE=$($settings['PERSONA_CONFIDENCE'])",
    "VOICE_CLARITY=$($settings['VOICE_CLARITY'])",
    "VOICE_AUTOTUNE=$($settings['VOICE_AUTOTUNE'])",
    "ROBOT_AMOUNT=$($settings['ROBOT_AMOUNT'])",
    "VOICE_RATE=$($settings['VOICE_RATE'])",
    "VOICE_PITCH=$($settings['VOICE_PITCH'])"
)
if ($deepgramKey) { $envVars += 'DEEPGRAM_API_KEY=secretref:deepgram-key' }
if ($speechKeyFallback) {
    $envVars += 'AZURE_SPEECH_KEY_FALLBACK=secretref:speech-key-fallback'
    $envVars += "AZURE_SPEECH_REGION_FALLBACK=$speechRegionFallback"
}
# Drop any empty assignments so a blank .env line cannot clear a default.
$envVars = $envVars | Where-Object { $_ -notmatch '=$' }

az containerapp update --name $AppName --resource-group $ResourceGroup `
    --set-env-vars $envVars `
    --min-replicas 1 --max-replicas 2 `
    --only-show-errors | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to update configuration' }
Write-Host "[ok]   $($envVars.Count) settings applied, min-replicas 1"

$fqdn = az containerapp show --name $AppName --resource-group $ResourceGroup `
    --query 'properties.configuration.ingress.fqdn' -o tsv --only-show-errors
Write-Host ''
Write-Host "Backend:  https://$fqdn" -ForegroundColor Green
Write-Host "Health:   https://$fqdn/health"
Write-Host "Socket:   wss://$fqdn/ws/session"
Write-Host ''
Write-Host 'Next: point Vercel at it with .\deploy-frontend.ps1' -ForegroundColor Cyan
Write-Host "The production token is in $tokenFile (delete it when done)."
