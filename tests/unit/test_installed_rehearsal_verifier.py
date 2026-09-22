from pathlib import Path

import pytest

from tools.verify_installed_rehearsal import verify_install


def test_installed_verifier_rejects_missing_active_pointer(tmp_path: Path) -> None:
    import asyncio

    with pytest.raises(FileNotFoundError):
        asyncio.run(verify_install(tmp_path, "local-0.0.1", tmp_path / "Stata"))
