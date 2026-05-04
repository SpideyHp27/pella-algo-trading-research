#Requires -Version 5.1
<#
.SYNOPSIS
    Uninstalls all Pella_* scheduled tasks.

.DESCRIPTION
    Deletes any registered task under \Pella\Pella_* via `schtasks /Delete`.
    Idempotent: tasks that don't exist are skipped quietly.

.NOTES
    Sharing tier: INNER ONLY.
#>
[CmdletBinding()]
param([switch]$KeepGenerated)

$ErrorActionPreference = "Stop"

$names = @(
    "Pella_LiveMonitor_Day",
    "Pella_LiveMonitor_Overnight",
    "Pella_EdgeDecay",
    "Pella_PaperReplay",
    "Pella_Discovery",
    "Pella_CrossPollinator"
)

foreach ($n in $names) {
    $tn = "\Pella\" + $n
    & schtasks /Delete /TN $tn /F 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  deleted $n"
    } else {
        Write-Host "  (skip) $n was not registered"
    }
}

if (-not $KeepGenerated) {
    $generated = Join-Path $PSScriptRoot "generated"
    if (Test-Path $generated) {
        Remove-Item -Recurse -Force -Path $generated
        Write-Host "  removed generated/"
    }
}

Write-Host "Done."
