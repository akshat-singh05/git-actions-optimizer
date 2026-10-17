<#
.SYNOPSIS
  Fallback for demo day: run the dashboard inside the Linux Docker image when Windows Smart App Control blocks scikit-learn.

.DESCRIPTION
  Smart App Control must stay on; this does not touch it.  The dashboard runs in the image `git-actions-optimizer-worker:latest`
  (Linux, where the blocked native extensions are not an issue) at http://localhost:8501.
    1. starts Docker Desktop if the daemon is not answering and waits for it,
    2. creates the container with the project mounted at /app (code, git mirrors and result caches),
    3. COPIES the SQLite database into the container instead of mounting it: SQLite file locking does not work across the
       Windows bind mount and a live ingest on the same file can corrupt it (seen on 2026-10-17: "disk I/O error"),
    4. starts the dashboard, warms the selected repository and opens the browser.
  The copy is a snapshot taken now: re-run the script after a new ingest.  Allow a few minutes for the first computation of a
  repository (the disk cache in .gha_optimizer/dashboard_cache is reused only when its fingerprint still matches).

.PARAMETER Repo
  Repository shown first (default python-poetry/poetry).
.PARAMETER Port
  Host port (default 8501).
.PARAMETER NoBrowser
  Do not open the browser.
#>
param(
    [string]$Repo = "python-poetry/poetry",
    [int]$Port = 8501,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$name = "gha-dashboard"
$image = "git-actions-optimizer-worker:latest"

function Test-Docker { & docker info --format "{{.ServerVersion}}" 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }

if (-not (Test-Docker)) {
    $exe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $exe)) { throw "Docker Desktop is not installed at $exe" }
    "Starting Docker Desktop ..."
    Start-Process $exe
    for ($i = 0; $i -lt 60 -and -not (Test-Docker); $i++) { Start-Sleep 5 }
    if (-not (Test-Docker)) { throw "the Docker daemon did not come up within 5 minutes: open Docker Desktop and accept any prompt" }
}
& docker image inspect $image 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { throw "image $image is missing: run  docker compose build  once while online" }

& docker rm -f $name 2>$null | Out-Null
$db = Join-Path $root "gha_optimizer.db"
if (-not (Test-Path $db)) { throw "no database at ${db} - ingest a repository first (gha-optimizer ingest ...)" }

$cmd = "git config --global --add safe.directory '*' && python -m gha_optimizer.cli dashboard --repo $Repo --warm --host 0.0.0.0 --port 8501 --snapshot-root /tmp/ghas"
& docker create --name $name -p "${Port}:8501" -v "${root}:/app" -w /app -e PYTHONPATH=/app -e DATABASE_URL=sqlite:////tmp/gha_optimizer.db `
    $image sh -c $cmd | Out-Null
"Copying the database into the container ($([math]::Round((Get-Item $db).Length / 1MB)) MB) ..."
& docker cp $db "${name}:/tmp/gha_optimizer.db"
& docker start $name | Out-Null

$url = "http://localhost:$Port"
for ($i = 0; $i -lt 90; $i++) { try { $null = Invoke-RestMethod "$url/api/v1/dashboard/repos" -TimeoutSec 2; break } catch { Start-Sleep 1 } }
"Dashboard (in Docker) at $url   - logs: docker logs -f $name   - stop: docker rm -f $name"
if (-not $NoBrowser) { Start-Process $url }
