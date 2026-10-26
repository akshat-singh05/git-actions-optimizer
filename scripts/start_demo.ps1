<#
.SYNOPSIS
  One command to get the working demo ready: the dashboard on every ingested repository, warmed, in your browser.

.DESCRIPTION
  Run it in a terminal tab and leave it open (Ctrl-C stops the dashboard).  It
    1. checks that the machine can run the models (scikit-learn imports; Windows Smart App Control must stay on),
    2. starts `gha-optimizer dashboard --warm` with short snapshot paths (apache/dubbo needs them on Windows),
    3. waits until the server answers, opens http://localhost:8501 and prints which repositories are ready.
  Everything shown comes from the local database of real ingested runs; nothing is simulated.
  Allow about 6-8 minutes for the first warm-up of all repositories (results are cached on disk afterwards).

.PARAMETER Repo
  Repository shown first (default python-poetry/poetry).
.PARAMETER NoBrowser
  Do not open the browser.
#>
param(
    [string]$Repo = "python-poetry/poetry",
    [switch]$NoBrowser
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

& $py -W ignore -c "import sklearn.ensemble, fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) { throw "scikit-learn / fastapi could not be imported. If Windows Smart App Control blocked it, do NOT disable it: retry in a minute or use the Docker image (scripts/start_demo_docker.ps1)." }

$port = 8501
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "port $port is already in use: a dashboard is probably running already - open http://localhost:$port" }

# open the browser and report readiness from a background job once the server answers
$watcher = Start-Job -ArgumentList $port, $NoBrowser.IsPresent -ScriptBlock {
    param($port, $noBrowser)
    $url = "http://localhost:$port/api/v1/dashboard/repos"
    for ($i = 0; $i -lt 120; $i++) { try { $null = Invoke-RestMethod $url -TimeoutSec 2; break } catch { Start-Sleep 1 } }
    if (-not $noBrowser) { Start-Process "http://localhost:$port" }
}

"Starting the dashboard on http://localhost:$port (first repository: $Repo). Warming all repositories in the background - the selector shows (ready) as each finishes."
& $py -W ignore -m gha_optimizer.cli dashboard --repo $Repo --warm --snapshot-root D:/ghas --port $port
Stop-Job $watcher -ErrorAction SilentlyContinue
