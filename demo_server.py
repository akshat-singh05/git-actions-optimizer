"""Compatibility entry point: ``python demo_server.py --repo owner/repo [--warm]`` is ``gha-optimizer dashboard ...``.

The dashboard (UI ``gha_optimizer/dashboard_ui/index.html`` + API + service) lives in ``gha_optimizer/``; there is no second implementation.
"""
import sys

from gha_optimizer.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "dashboard")
    main()
