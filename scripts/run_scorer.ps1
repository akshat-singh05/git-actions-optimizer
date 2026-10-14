<#
.SYNOPSIS
  Run ONE live scorer in the foreground of its own (detached) process; started by scripts/live_scorers.ps1, not by hand.

.DESCRIPTION
  The GitHub token is read here with `gh auth token` into this process's environment only (never a file, never a log).
  stdout/stderr go to .gha_optimizer/live/logs/<owner>__<repo>.{out,err}.log (appended, so a restart does not erase history).
#>
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Workflow,
    [int]$Minutes = 720,
    [int]$PollSeconds = 30
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$token = (gh auth token)
if (-not $token) { Add-Content ".gha_optimizer\live\logs\run_scorer.err.log" "$(Get-Date -Format s) gh auth token returned nothing for $Repo"; exit 3 }
$env:GITHUB_TOKEN = $token
$name = $Repo -replace "/", "__"
$out = Join-Path $root ".gha_optimizer\live\logs\$name.out.log"
$err = Join-Path $root ".gha_optimizer\live\logs\$name.err.log"
"$(Get-Date -Format s) starting scorer for $Repo ($Minutes min)" | Add-Content $out
$py = Join-Path $root ".venv\Scripts\python.exe"
# cmd.exe does the redirection: PowerShell 5.1 turns every stderr line of a native command into an error record
$line = '"' + $py + '" -u -W ignore -m gha_optimizer.cli live-score --repo ' + $Repo + ' --workflow-path ' + $Workflow +
        ' --minutes ' + $Minutes + ' --poll-seconds ' + $PollSeconds + ' 1>>"' + $out + '" 2>>"' + $err + '"'
cmd.exe /c $line
"$(Get-Date -Format s) scorer for $Repo exited with code $LASTEXITCODE" | Add-Content $out
