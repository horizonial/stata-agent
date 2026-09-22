from __future__ import annotations

from pathlib import Path

import pytest

from tools.run_real_product_stress import _command, _sha256


def test_stress_command_captures_table_data_and_graph_in_attempt_staging() -> None:
    command = _command(3, "price", "weight")
    assert "regress price weight" in command
    assert "esttab using \"<ATTEMPT_STAGING>/tables/model.rtf\"" in command
    assert 'save "<ATTEMPT_STAGING>/data/analysis.dta"' in command
    assert 'graph export "<ATTEMPT_STAGING>/figures/scatter.png"' in command


def test_stress_hash_streams_real_payload(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"research" * 1000)
    assert _sha256(payload) == "3cd30240001f2cf6515f17d32a954557810349fd7a627b7e5b1db506f76790e1"


@pytest.mark.parametrize("cycles, workspaces", [(0, 2), (1, 1)])
def test_stress_rejects_non_stress_configuration(
    tmp_path: Path, cycles: int, workspaces: int
) -> None:
    from tools.run_real_product_stress import _run_stata_stress

    with pytest.raises(ValueError, match="at least one cycle and two Workspaces"):
        import asyncio

        asyncio.run(
            _run_stata_stress(
                tmp_path,
                cycles=cycles,
                workspace_count=workspaces,
                timeout_seconds=30,
                stata_home=tmp_path,
                candidate=None,
            )
        )
