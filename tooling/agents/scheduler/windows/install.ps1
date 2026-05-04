#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the Pella agent stack as Windows Scheduled Tasks.

.DESCRIPTION
    Substitutes __PELLA_PYTHON__ / __PELLA_LAB_ROOT__ / __PELLA_USER__ in
    each XML template under ./tasks, writes the rendered XML to
    ./generated/, and registers each one with `schtasks /Create /XML`.

    Run from this directory:

        cd C:\Lab\NT8Bridge\scheduler\windows
        .\install.ps1            # install all tasks
        .\install.ps1 -DryRun    # render to .\generated\ but don't register
        .\install.ps1 -Replace   # delete + recreate each task (idempotent)

.PARAMETER PythonPath
    Override Python interpreter. Default: $env:PELLA_PYTHON or `python`
    (resolved via Get-Command).

.PARAMETER LabRoot
    Override Pella project root. Default: $env:PELLA_LAB_ROOT or "C:\Lab".

.PARAMETER User
    Override task principal (Domain\User). Default: $env:PELLA_USER or
    the current user as reported by `whoami`.

.PARAMETER DryRun
    Render the XMLs but do not register tasks.

.PARAMETER Replace
    Delete any existing Pella_* tasks before creating, so re-runs are safe.

.NOTES
    Sharing tier: INNER ONLY.
#>
[CmdletBinding()]
param(
    [string]$PythonPath = $env:PELLA_PYTHON,
    [string]$LabRoot    = $env:PELLA_LAB_ROOT,
    [string]$User       = $env:PELLA_USER,
    [switch]$DryRun,
    [switch]$Replace
)

$ErrorActionPreference = "Stop"

# ----- Resolve substitutions -----------------------------------------------
if (-not $PythonPath) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Could not resolve Python; pass -PythonPath or set PELLA_PYTHON." }
    $PythonPath = $cmd.Source
}
if (-not (Test-Path $PythonPath)) {
    throw "PythonPath does not exist: $PythonPath"
}

if (-not $LabRoot) { $LabRoot = "C:\Lab" }
if (-not (Test-Path $LabRoot)) {
    throw "LabRoot does not exist: $LabRoot"
}

if (-not $User) {
    $User = (whoami).Trim()
}

# Verify the agent scripts actually exist before scheduling them
$agents = @(
    "NT8Bridge\tools\live_monitor_agent.py",
    "NT8Bridge\tools\edge_decay_watchdog.py",
    "NT8Bridge\tools\paper_replay_agent.py",
    "NT8Bridge\tools\discovery_agent.py",
    "NT8Bridge\tools\cross_pollinator.py"
)
foreach ($a in $agents) {
    $p = Join-Path $LabRoot $a
    if (-not (Test-Path $p)) { throw "Agent script not found: $p" }
}

Write-Host "Pella scheduler install:"
Write-Host "  PythonPath = $PythonPath"
Write-Host "  LabRoot    = $LabRoot"
Write-Host "  User       = $User"
Write-Host "  DryRun     = $DryRun"
Write-Host "  Replace    = $Replace"
Write-Host ""

# ----- Render templates ----------------------------------------------------
$here       = $PSScriptRoot
$templates  = Join-Path $here "tasks"
$generated  = Join-Path $here "generated"
if (-not (Test-Path $generated)) { New-Item -ItemType Directory -Path $generated | Out-Null }

$xmlFiles = Get-ChildItem -Path $templates -Filter "Pella_*.xml" | Sort-Object Name
if ($xmlFiles.Count -eq 0) {
    throw "No task templates found under $templates"
}

$rendered = @()
foreach ($xml in $xmlFiles) {
    $taskName = $xml.BaseName
    $content  = Get-Content -Raw -Path $xml.FullName
    $content  = $content.Replace("__PELLA_PYTHON__", $PythonPath)
    $content  = $content.Replace("__PELLA_LAB_ROOT__", $LabRoot)
    $content  = $content.Replace("__PELLA_USER__", $User)
    $outPath  = Join-Path $generated ($xml.Name)
    # Templates declare encoding="UTF-8". schtasks /XML on Windows 10+ accepts
    # UTF-8 with BOM (which is what `Set-Content -Encoding utf8` writes in PS 5.1).
    Set-Content -Path $outPath -Value $content -Encoding utf8
    Write-Host "  rendered $taskName -> $outPath"
    $rendered += [pscustomobject]@{ Name = $taskName; Path = $outPath }
}
Write-Host ""

if ($DryRun) {
    Write-Host "DryRun: skipping schtasks. Inspect generated XMLs in $generated"
    return
}

# ----- Register --------------------------------------------------------
foreach ($t in $rendered) {
    if ($Replace) {
        # Delete if exists; ignore "task does not exist" exit code
        $del = & schtasks /Delete /TN ("\Pella\" + $t.Name) /F 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  deleted existing $($t.Name)"
        }
    }
    Write-Host "  registering $($t.Name) ..."
    & schtasks /Create /TN ("\Pella\" + $t.Name) /XML $t.Path | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /Create failed for $($t.Name) (exit $LASTEXITCODE). Re-run with -Replace if the task already exists."
    }
}

Write-Host ""
Write-Host "Done. Inspect with: schtasks /Query /FO LIST | Select-String 'Pella_'"
