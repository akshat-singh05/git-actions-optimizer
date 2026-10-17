"""
Keeps the prospective live scorers (``gha-optimizer live-score``) running for days.  Started by the Windows Task Scheduler through
``pythonw.exe`` (no console), installed and removed with ``scripts/live_scorers.ps1`` - see docs/BACKGROUND_RUNBOOK.md.

Why this shape: processes started from a Claude session, through WMI, or as *console* programs under the Task Scheduler all died
(a console program gets a Ctrl-C event, exit code 0xC000013A, when its console goes away).  A console-less Python process owned by
the Task Scheduler is independent of the Claude app and survives it.

Behaviour: one scorer per repository (a 12 h window each, restarted when it ends or crashes, with back-off), a single-instance lock,
a log in ``.gha_optimizer/live/logs/supervisor.log``.  The GitHub token is read from ``gh auth token`` and handed to each scorer in its
environment only; it is never written anywhere.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".gha_optimizer" / "live" / "logs"
LOCK = ROOT / ".gha_optimizer" / "live" / "supervisor.pid"
TARGETS = [
    ("astral-sh/uv", ".github/workflows/ci.yml"),
    ("astral-sh/ruff", ".github/workflows/ci.yaml"),
    ("prometheus/prometheus", ".github/workflows/ci.yml"),
]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WINDOW_MINUTES, POLL_SECONDS = 720, 30
GH_CANDIDATES = ["gh", r"C:\Program Files\GitHub CLI\gh.exe"]


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "supervisor.log", "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")


def pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, creationflags=NO_WINDOW)
    return str(pid) in out.stdout


def take_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip())
            if other != os.getpid() and pid_alive(other):
                return False
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    return True


def external_scorers() -> set[str]:
    """Repositories that already have a running ``live-score`` process (e.g. started by an earlier supervisor)."""
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'live-score' } "
           "| ForEach-Object { $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {repo for repo, _ in TARGETS if f"--repo {repo} " in out + " " or f'--repo "{repo}"' in out}


def github_token() -> str | None:
    for gh in GH_CANDIDATES:
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def start_scorer(repo: str, workflow: str, token: str) -> subprocess.Popen:
    name = repo.replace("/", "__")
    env = dict(os.environ, GITHUB_TOKEN=token, PYTHONIOENCODING="utf-8")
    out = open(LOGS / f"{name}.out.log", "a", encoding="utf-8")
    err = open(LOGS / f"{name}.err.log", "a", encoding="utf-8")
    out.write(f"{datetime.now().isoformat(timespec='seconds')} supervisor starting scorer for {repo} ({WINDOW_MINUTES} min)\n")
    out.flush()
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    return subprocess.Popen(
        [str(py), "-u", "-W", "ignore", "-m", "gha_optimizer.cli", "live-score", "--repo", repo, "--workflow-path", workflow,
         "--minutes", str(WINDOW_MINUTES), "--poll-seconds", str(POLL_SECONDS)],
        cwd=str(ROOT), env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def main() -> int:
    if not take_lock():
        return 0                      # another supervisor is already running: nothing to do
    log(f"supervisor started (pid {os.getpid()})")
    procs: dict[str, subprocess.Popen] = {}
    next_try: dict[str, float] = {}
    fails: dict[str, int] = {}
    try:
        while True:
            already = external_scorers()
            for repo, workflow in TARGETS:
                p = procs.get(repo)
                if p is not None and p.poll() is None:
                    continue
                if p is not None:
                    code = p.poll()
                    log(f"scorer for {repo} exited with code {code}")
                    fails[repo] = fails.get(repo, 0) + 1 if code not in (0, None) else 0
                    procs.pop(repo, None)
                    next_try[repo] = time.time() + min(30 * 2 ** max(fails.get(repo, 0) - 1, 0), 600)   # back-off up to 10 min
                if time.time() < next_try.get(repo, 0):
                    continue
                if repo in already:
                    continue                  # a scorer for this repository is already running: do not start a duplicate
                token = github_token()
                if not token:
                    log(f"no GitHub token (gh auth token failed); retrying {repo} in 5 min")
                    next_try[repo] = time.time() + 300
                    continue
                procs[repo] = start_scorer(repo, workflow, token)
                log(f"started scorer for {repo} (pid {procs[repo].pid})")
            LOCK.write_text(str(os.getpid()))
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        try:
            if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
                LOCK.unlink()
        except OSError:
            pass
        log("supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
