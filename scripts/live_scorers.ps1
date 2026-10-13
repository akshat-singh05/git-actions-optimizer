<#
.SYNOPSIS
  Start the prospective live scorers (gha-optimizer live-score) for the repositories of the live test, unless they already run.

.DESCRIPTION
  Idempotent: a repository whose scorer is already running is left alone, so this can be run by hand or by the watchdog task
  (docs/BACKGROUND_RUNBOOK.md).  Each scorer appends to its own hash-chained ledger under .gha_optimizer/live/ and never
  re-scores a run that is already in the ledger.  The GitHub token comes from `gh auth token` and lives only in the
  environment of the scorer processes; it is never written to a file.

.PARAMETER Minutes
  Length of each scorer window (default 720 = 12 h); the watchdog starts a new window when one ends.
.PARAMETER Stop
  Stop the running scorers instead of starting them.
#>
param(
    [int]$Minutes = 720,
    [int]$PollSeconds = 30,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$targets = @(
    @{ repo = "astral-sh/uv";            workflow = ".github/workflows/ci.yml" },
    @{ repo = "astral-sh/ruff";          workflow = ".github/workflows/ci.yaml" },
    @{ repo = "prometheus/prometheus";   workflow = ".github/workflows/ci.yml" }
)

function Get-Scorer([string]$repo) {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "live-score" -and $_.CommandLine -match ("--repo ""?" + [regex]::Escape($repo) + """?(\s|$)") }
}

if ($Stop) {
    foreach ($t in $targets) { Get-Scorer $t.repo | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped $($t.repo) (pid $($_.ProcessId))" } }
    return
}

$env:GITHUB_TOKEN = (gh auth token)
if (-not $env:GITHUB_TOKEN) { throw "gh auth token returned nothing: run `gh auth login` first" }
Set-Location $root
$logs = Join-Path $root ".gha_optimizer\live\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

foreach ($t in $targets) {
    if (Get-Scorer $t.repo) { "already running: $($t.repo)"; continue }
    $name = $t.repo -replace "/", "__"
    $out = Join-Path $logs "$name.out.log"
    $err = Join-Path $logs "$name.err.log"
    $argList = @("-u", "-W", "ignore", "-m", "gha_optimizer.cli", "live-score", "--repo", $t.repo, "--workflow-path", $t.workflow,
              "--minutes", "$Minutes", "--poll-seconds", "$PollSeconds")
    $p = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $root -WindowStyle Hidden `
         -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    "started $($t.repo) (pid $($p.Id)) for $Minutes min; log: $out"
}
$env:GITHUB_TOKEN = $null
