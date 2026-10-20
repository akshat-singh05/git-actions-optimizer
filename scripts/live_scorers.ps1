<#
.SYNOPSIS
  Install, inspect and remove the durable prospective live-test runner (gha-optimizer live-score for uv, ruff and prometheus).

.DESCRIPTION
  Default (no switch): register the Windows Task Scheduler task "GHA-Optimizer-LiveSupervisor" if missing and start it.
  The task runs scripts/live_supervisor.py with pythonw.exe (no console window), starts at logon, re-checks every 15 minutes and
  is independent of the Claude app and of any terminal tab; the supervisor itself keeps one scorer per repository alive
  (12 h windows, restart with back-off) and takes a single-instance lock.  Needs no administrator rights.

  Why pythonw: a *console* program started by the Task Scheduler, by WMI or by a Claude session was killed within seconds or when
  the session ended (exit code 0xC000013A, a Ctrl-C event when the console goes away); a console-less process is not.

  -Status : task state, running supervisor / scorers, ledger sizes and the last supervisor log lines.
  -Stop   : unregister the task and stop the supervisor and the scorers (use this when the live test is over).
  -Disable owner/repo [owner/repo ...] : stop the scorer of just these repositories and tell the supervisor not to restart them (the others keep running).
  The GitHub token comes from `gh auth token` inside the supervisor and lives only in each scorer's environment.
#>
param(
    [switch]$Status,
    [switch]$Stop,
    [string[]]$Disable
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "GHA-Optimizer-LiveSupervisor"
$targets = "astral-sh/uv", "astral-sh/ruff", "prometheus/prometheus"

function Get-LiveProcs {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -in @("python.exe", "pythonw.exe")) -and $_.CommandLine -match "(live-score|live_supervisor\.py)" }
}
function Ledger-Lines([string]$repo) {
    $f = Join-Path $root (".gha_optimizer\live\" + ($repo -replace "/", "__") + ".jsonl")
    if (Test-Path $f) { (Get-Content $f | Measure-Object -Line).Lines } else { 0 }
}

if ($Disable) {
    # `powershell -File ... -Disable a,b` delivers ONE string "a,b": split it
    $Disable = @($Disable | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $f = Join-Path $root ".gha_optimizer\live\disabled_repos.txt"
    New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null
    foreach ($r in $Disable) {
        Add-Content -Path $f -Value $r
        foreach ($p in @(Get-LiveProcs | Where-Object { $_.CommandLine -match [regex]::Escape("--repo $r ") })) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            "stopped scorer pid $($p.ProcessId) of $r"
        }
        "disabled $r (the supervisor will not restart it)"
    }
    return
}

if ($Status) {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    "task $taskName : " + $(if ($t) { $t.State } else { "NOT registered" })
    $procs = @(Get-LiveProcs)
    "supervisor running: " + [bool]($procs | Where-Object { $_.CommandLine -match "live_supervisor\.py" })
    foreach ($r in $targets) {
        $up = [bool]($procs | Where-Object { $_.CommandLine -match [regex]::Escape("--repo $r ") })
        "{0,-24} {1,-11} ledger entries: {2}" -f $r, $(if ($up) { "running" } else { "NOT running" }), (Ledger-Lines $r)
    }
    $dis = Join-Path $root ".gha_optimizer\live\disabled_repos.txt"
    if (Test-Path $dis) { "disabled (not restarted): " + ((Get-Content $dis) -join ", ") }
    $log = Join-Path $root ".gha_optimizer\live\logs\supervisor.log"
    if (Test-Path $log) { "--- supervisor.log (last 5)"; Get-Content $log -Tail 5 }
    return
}

if ($Stop) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        "unregistered $taskName"
    }
    # the supervisor first (it would restart the scorers), then the scorers
    foreach ($p in @(Get-LiveProcs | Sort-Object { if ($_.CommandLine -match "live_supervisor") { 0 } else { 1 } })) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        "stopped pid $($p.ProcessId)"
    }
    Remove-Item (Join-Path $root ".gha_optimizer\live\supervisor.pid") -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $root ".gha_optimizer\live\disabled_repos.txt") -ErrorAction SilentlyContinue
    return
}

if (-not (gh auth token)) { throw "gh auth token returned nothing: run `gh auth login` first" }
New-Item -ItemType Directory -Force (Join-Path $root ".gha_optimizer\live\logs") | Out-Null
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-W ignore `"$root\scripts\live_supervisor.py`"" -WorkingDirectory $root
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 30))
    )
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Description "Prospective live test of the GHA optimizer (docs/BACKGROUND_RUNBOOK.md). Remove with: scripts/live_scorers.ps1 -Stop" | Out-Null
    "registered $taskName"
}
Start-ScheduledTask -TaskName $taskName
"started $taskName; check with: scripts/live_scorers.ps1 -Status"
