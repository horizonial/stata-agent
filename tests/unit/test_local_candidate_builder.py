from __future__ import annotations

from pathlib import Path

from tools.build_local_candidate import _canonical_build_source


def test_canonical_build_source_covers_code_builder_and_web_payload(tmp_path: Path) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    service = tmp_path / "tools" / "service_frozen_entry.py"
    builder = tmp_path / "tools" / "build_local_candidate.py"
    web = tmp_path / "web" / "dist" / "index.html"
    for path, content in (
        (source, "source"),
        (service, "service"),
        (builder, "builder"),
        (web, "web"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    first, count = _canonical_build_source(tmp_path)
    source.write_text("changed", encoding="utf-8")
    second, second_count = _canonical_build_source(tmp_path)

    assert count == second_count == 4
    assert first != second
