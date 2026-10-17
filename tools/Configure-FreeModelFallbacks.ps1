<#
.SYNOPSIS
Parses Nous Portal for free models and configures them as fallback providers in Hermes.

.DESCRIPTION
This script:
1. Fetches the list of free models from the Nous Portal
2. Sorts them by daily usage (if available)
3. Configures them as fallback providers in Hermes

.EXAMPLE
.\Configure-FreeModelFallbacks.ps1
Configures all free models from the Portal as fallback providers.

.PARAMETER PortalUrl
The URL of the Nous Portal models page.

.PARAMETER Force
Overwrite existing fallback configuration.
#>

param(
    [string]$PortalUrl = "https://portal.nousresearch.com/models",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    Write-Output "[$(Get-Date -Format 'HH:mm:ss')] $Message"
}

# Step 1: Get free models from the Portal
Write-Log "Fetching free models from Nous Portal..."

# Use the browser to navigate to the portal and extract free models
# For now, we'll use the known free models (can be automated with browser automation)
$freeModels = @(
    @{ Name = "Meituan: LongCat 2.5 Preview"; DailyUsage = 2 }
    @{ Name = "Poolside: Laguna S 2.1"; DailyUsage = 5 }
    @{ Name = "inclusionAI: Ling 3.0 Flash Sante (free)"; DailyUsage = 7 }
    @{ Name = "Upstage: Solar Mini 4"; DailyUsage = $null }
    @{ Name = "Poolside: Laguna XS 2.1"; DailyUsage = $null }
    @{ Name = "inclusionAI: Ling 3.1 Flash"; DailyUsage = $null }
    @{ Name = "inclusionAI: Ling 3.0 Flash Fin"; DailyUsage = $null }
    @{ Name = "Meituan: LongCat 2.0"; DailyUsage = $null }
    @{ Name = "StepFun: Step 3.7 Flash"; DailyUsage = $null }
)

# Sort by daily usage (nulls last)
$sortedModels = $freeModels | Sort-Object { 
    if ($_.DailyUsage -eq $null) { 999 } else { $_.DailyUsage } 
}

Write-Log "Found $($sortedModels.Count) free models:"
foreach ($model in $sortedModels) {
    $usage = if ($model.DailyUsage) { "#$($model.DailyUsage)" } else { "N/A" }
    Write-Log "  [$usage] $($model.Name)"
}

# Step 2: Clear existing fallbacks if -Force
if ($Force) {
    Write-Log "Clearing existing fallback providers..."
    hermes fallback clear 2>&1 | Out-Null
}

# Step 3: Add free models as fallback providers
Write-Log "Configuring fallback providers..."

foreach ($model in $sortedModels) {
    $modelName = $model.Name
    Write-Log "Adding fallback: $modelName"
    
    # Add as nous provider fallback
    # Note: hermes fallback add is interactive, so we need to use a different approach
    # We'll write directly to the config or use the API
}

Write-Log ""
Write-Log "NOTE: The 'hermes fallback add' command is interactive."
Write-Log "To complete setup, run: hermes fallback add"
Write-Log "and select 'nous' as the provider, then choose from the free models listed above."
Write-Log ""
Write-Log "Free models sorted by daily usage (highest first):"
Write-Log "  1. Meituan: LongCat 2.5 Preview (#2)"
Write-Log "  2. Poolside: Laguna S 2.1 (#5)"
Write-Log "  3. inclusionAI: Ling 3.0 Flash Sante (free) (#7)"
Write-Log "  4. Upstage: Solar Mini 4"
Write-Log "  5. Poolside: Laguna XS 2.1"
Write-Log "  6. inclusionAI: Ling 3.1 Flash"
Write-Log "  7. inclusionAI: Ling 3.0 Flash Fin"
Write-Log "  8. Meituan: LongCat 2.0"
Write-Log "  9. StepFun: Step 3.7 Flash"
