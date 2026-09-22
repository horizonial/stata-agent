"""Secured loopback host used by the M5 browser capability acceptance test."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from stata_research_agent.application.local_session import LocalSessionAuthority
from stata_research_agent.interfaces.api import WorkspaceHost, create_app

PROJECT_ROOT = Path(__file__).parents[2]
LAUNCHER_SECRET = "m5_e2e_launcher_" + "a" * 40


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    exact_host = f"{args.host}:{args.port}"
    authority = LocalSessionAuthority(
        instance_id="instance_m5_browser_e2e",
        exact_host=exact_host,
        launcher_secret=LAUNCHER_SECRET,
    )
    app = create_app(
        WorkspaceHost(args.workspace_root),
        static_directory=PROJECT_ROOT / "web" / "dist",
        local_session_authority=authority,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
