from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from stata_research_agent.interfaces import service_main
from stata_research_agent.interfaces.service_main import (
    ServiceLayout,
    ServiceStartupError,
    main,
    release_probe,
)


def layout(tmp_path: Path) -> ServiceLayout:
    static = tmp_path / "web"
    static.mkdir()
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    executor = tmp_path / "stata-mcp.exe"
    executor.write_bytes(b"fixture")
    stata_home = tmp_path / "Stata18"
    stata_home.mkdir()
    skills = tmp_path / "skills" / "empirical-research-main"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: empirical-research-main\nversion: 1.0.0\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    return ServiceLayout(
        tmp_path / "workspaces",
        tmp_path / "runtime",
        static,
        tmp_path / "skills",
        executor,
        stata_home,
    )


def test_release_probe_is_read_only(tmp_path: Path) -> None:
    candidate = layout(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = release_probe(candidate)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert result["read_only"] is True
    assert result["stata_mcp_executor_present"] is True
    assert before == after
    assert not candidate.workspace_root.exists()
    assert not candidate.runtime_directory.exists()


def test_release_probe_rejects_missing_runtime_member(tmp_path: Path) -> None:
    candidate = layout(tmp_path)
    candidate.mcp_executor.unlink()

    with pytest.raises(ServiceStartupError, match="Python runtime is missing"):
        release_probe(candidate)


def test_cli_probe_emits_machine_readable_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = layout(tmp_path)

    return_code = main(
        [
            "--probe",
            "--workspace-root",
            str(candidate.workspace_root),
            "--runtime-directory",
            str(candidate.runtime_directory),
            "--static-directory",
            str(candidate.static_directory),
            "--skills-directory",
            str(candidate.skills_directory),
            "--mcp-executor",
            str(candidate.mcp_executor),
            "--stata-home",
            str(candidate.stata_home),
        ]
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert '"schema_version": "stata-research-agent.release-probe/v1"' in captured.out
    assert captured.err == ""
    assert not candidate.workspace_root.exists()
    assert not candidate.runtime_directory.exists()


def test_cli_treats_ctrl_c_as_clean_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(service_main.asyncio, "run", interrupted)

    assert main(["--no-browser"]) == 0


def test_optional_mineru_adapter_requires_explicit_version(tmp_path: Path) -> None:
    candidate = layout(tmp_path)
    executable = tmp_path / "mineru.exe"
    executable.write_bytes(b"fixture")
    unpinned = replace(candidate, mineru_executable=executable)

    with pytest.raises(ServiceStartupError, match="pinned version"):
        service_main._knowledge_adapters(unpinned)

    pinned = replace(
        candidate, mineru_executable=executable, mineru_version="4.x-test"
    )
    embedding, mineru = service_main._knowledge_adapters(pinned)
    assert embedding is None
    assert mineru is not None and mineru.tier == "basic"
