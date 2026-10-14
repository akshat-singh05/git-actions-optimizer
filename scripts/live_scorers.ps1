<#
.SYNOPSIS
  Keep the prospective live scorers (gha-optimizer live-score) running; meant to be left open in a terminal tab.

.DESCRIPTION
  Default: a supervisor that stays in the foreground, starts a scorer for each repository of the live test if none is running,
  checks every minute, restarts one whose window ended or that died, and prints one status line per check.  Stop it with Ctrl-C
  (the scorers keep running; use -Stop to stop them too).

  Why a terminal tab and not a background task: processes started by a Claude session, by a scheduled Claude task, through WMI or
  through the Windows Task Scheduler all died here within seconds or when their session ended (the first scorers left gaps in the
  ledgers).  A process tree started in the user's own interactive terminal lives as long as that terminal.

  -Status : print what is running and the ledger sizes, then exit.
  -Stop   : stop every scorer (and its wrapper) and exit.
  Each scorer appends to its own hash-chained ledger under .gha_optimizer/live/ and never re-scores a run already in it;
  the GitHub token is read by scripts/run_scorer.ps1 with `gh auth token` into that process's environment only.
#>
param(
    [int]$Minutes = 720,
    [int]$PollSeconds = 30,
    [switch]$Status,
    [switch]$Stop
)

$root = Split-Path -Parent $PSScriptRoot
$targets = @(
    @{ repo = "astral-sh/uv";            workflow = ".github/workflows/ci.yml" },
    @{ repo = "astral-sh/ruff";          workflow = ".github/workflows/ci.yaml" },
    @{ repo = "prometheus/prometheus";   workflow = ".github/workflows/ci.yml" }
)
function Get-Procs([string]$repo) {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -in @("python.exe", "powershell.exe")) -and $_.CommandLine -match "(live-score|run_scorer\.ps1)" -and
        $_.CommandLine -match ("(--repo|-Repo) ""?" + [regex]::Escape($repo) + """?(\s|$)") }
}
function Ledger-Lines([string]$repo) {
    $f = Join-Path $root (".gha_optimizer\live\" + ($repo -replace "/", "__") + ".jsonl")
    if (Test-Path $f) { (Get-Content $f | Measure-Object -Line).Lines } else { 0 }
}

if ($Stop) {
    # the supervisor first, otherwise it would restart what is stopped
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object { $_.CommandLine -match "live_scorers\.ps1" -and $_.ProcessId -ne $PID -and $_.CommandLine -notmatch "-Stop|-Status" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "stopped supervisor (pid $($_.ProcessId))" }
    foreach ($t in $targets) { foreach ($p in @(Get-Procs $t.repo)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; "stopped $($t.repo) (pid $($p.ProcessId))" } }
    return
}
if ($Status) {
    foreach ($t in $targets) { "{0,-24} {1,-9} ledger entries: {2}" -f $t.repo, $(if (Get-Procs $t.repo) { "running" } else { "NOT running" }), (Ledger-Lines $t.repo) }
    return
}

if (-not (gh auth token)) { throw "gh auth token returned nothing: run `gh auth login` first" }
New-Item -ItemType Directory -Force (Join-Path $root ".gha_optimizer\live\logs") | Out-Null
"live-scorer supervisor started $(Get-Date -Format s); Ctrl-C leaves the scorers running, -Stop stops them"
while ($true) {
    foreach ($t in $targets) {
        if (-not (Get-Procs $t.repo)) {
            Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -WorkingDirectory $root -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$root\scripts\run_scorer.ps1`"", "-Repo", "`"$($t.repo)`"",
                "-Workflow", "`"$($t.workflow)`"", "-Minutes", "$Minutes", "-PollSeconds", "$PollSeconds") | Out-Null
            "$(Get-Date -Format s) started $($t.repo)"
        }
    }
    Start-Sleep -Seconds 60
    "$(Get-Date -Format s) " + ((@($targets | ForEach-Object { "{0}: {1} ({2})" -f ($_.repo -split "/")[1], $(if (Get-Procs $_.repo) { "up" } else { "DOWN" }), (Ledger-Lines $_.repo) })) -join "   ")
}
