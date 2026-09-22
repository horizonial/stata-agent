"""PyInstaller entrypoint for the stable application/service executable."""

from __future__ import annotations

import multiprocessing

from stata_research_agent.interfaces.service_main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
