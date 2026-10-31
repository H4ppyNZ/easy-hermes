# Set-EnvKeys.ps1
# Prompts securely for provider API keys and stores them in the Windows USER
# environment (HKEY_CURRENT_USER\Environment). Values are never echoed.
# Run from a fresh PowerShell window afterwards; env vars are read at process start.

param()

$ErrorActionPreference = 'Stop'

$groups = @(
    @{ Names = @('OPENROUTER_API_KEY'); Optional = $true;  Desc = 'OpenRouter (optional; raises rate limit)' },
    @{ Names = @('NVIDIA_API_KEY','NGC_API_KEY'); Optional = $false; Desc = 'NVIDIA NIM (required for polling)' },
    @{ Names = @('GOOGLE_API_KEY','GEMINI_API_KEY'); Optional = $false; Desc = 'Google AI Studio / Gemini (required)' },
    @{ Names = @('OLLAMA_API_KEY','OLLAMA_CLOUD_API_KEY'); Optional = $true;  Desc = 'Ollama Cloud (optional)' },
    @{ Names = @('NOVITA_API_KEY','NOVITA_KEY','NOVITA_AI_API_KEY'); Optional = $false; Desc = 'Novita AI (required)' },
    @{ Names = @('CLOUDFLARE_API_TOKEN','CLOUDFLARE_API_KEY','CLOUDFLARE_WORKERS_AI_TOKEN'); Optional = $false; Desc = 'Cloudflare Workers AI API token (required for polling; Workers AI read/run scope)' },
    @{ Names = @('CLOUDFLARE_ACCOUNT_ID','CF_ACCOUNT_ID','CLOUDFLARE_ACCOUNT'); Optional = $false; Desc = 'Cloudflare Account ID (required for polling; dashboard -> AI -> Workers AI -> Use REST API)' }
)

function Read-Secret([string]$prompt) {
    $ss = Read-Host -Prompt $prompt -AsSecureString
    if ($ss.Length -eq 0) { return $null }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

Write-Host "Set provider API keys in Windows USER environment."
Write-Host "Blank input keeps the existing value (if any). Values are never echoed."
Write-Host ""

foreach ($g in $groups) {
    $primary = $g.Names[0]
    $current = [Environment]::GetEnvironmentVariable($primary, 'User')
    $state = if ($current) { "(currently set)" } else { "(not set)" }
    $req = if ($g.Optional) { "optional" } else { "required" }
    Write-Host ("{0} [{1}] {2}" -f $primary, $req, $state)
    Write-Host ("  {0}" -f $g.Desc)
    $value = Read-Secret "  Value"
    if ([string]::IsNullOrWhiteSpace($value)) {
        if ($current) { Write-Host "  kept existing" } else { Write-Host "  skipped" }
        continue
    }
    foreach ($name in $g.Names) {
        [Environment]::SetEnvironmentVariable($name, $value, 'User')
        Set-Item -Path "env:$name" -Value $value
    }
    Write-Host ("  set {0}" -f ($g.Names -join ', '))
}

Write-Host ""
Write-Host "Done. Open a new PowerShell/terminal window so processes pick up the new values."
Write-Host "Verify with:  [Environment]::GetEnvironmentVariable('NVIDIA_API_KEY','User')"
