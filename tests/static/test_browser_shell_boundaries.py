"""M4-01 browser shell authority and generated-client boundaries."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
WEB_SOURCE = PROJECT_ROOT / "web" / "src"


def browser_authored_sources() -> dict[str, str]:
    return {
        path.relative_to(WEB_SOURCE).as_posix(): path.read_text(encoding="utf-8")
        for pattern in ("*.ts", "*.tsx")
        for path in WEB_SOURCE.rglob(pattern)
        if "generated" not in path.parts and path.name != "vite-env.d.ts"
    }


def test_browser_shell_uses_generated_client_without_direct_api_or_storage_access() -> None:
    sources = browser_authored_sources()
    combined = "\n".join(sources.values())
    assert "getWorkspaceBootstrap" in sources["browser/shell.ts"]
    for forbidden in (
        "fetch(",
        "/api/v1/",
        "sqlite",
        "managed_store",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "new EventSource",
        "indexedDB",
    ):
        assert forbidden not in combined


def test_browser_snapshot_is_replaced_only_by_bootstrap_query_response() -> None:
    shell = browser_authored_sources()["browser/shell.ts"]
    assert "this.#snapshots.set(normalized, result.data)" in shell
    assert "authoritative_revision +" not in shell
    assert "queued_count++" not in shell
    assert "turn.status =" not in shell


def test_research_views_use_only_generated_typed_queries() -> None:
    views = browser_authored_sources()["ResearchViews.tsx"]
    for generated_query in (
        "getConversationDetail",
        "getResultIndex",
        "getJournalEntries",
        "getDocumentIndex",
    ):
        assert generated_query in views
    assert "fetch(" not in views
    assert "/api/v1/" not in views


def test_workspace_stream_is_notification_plus_query_not_a_fact_reducer() -> None:
    sources = browser_authored_sources()
    stream = sources["browser/workspace-stream.ts"]
    shell = sources["browser/shell.ts"]
    assert "getWorkspaceStreamHead" in stream
    assert "workspaceEventStreamUrl" in stream
    assert "ReconnectingApiEventStream" in stream
    assert "consumeApiEventStream" in sources["browser/api-event-stream.ts"]
    assert "getWorkspaceBootstrap" in shell
    assert "#refreshFromDurableNotification" in shell
    assert "this.#snapshots.set(workspaceId, result.data)" in shell
    for forbidden in ("queued_count++", "workspace_revision++", "turn.status ="):
        assert forbidden not in stream
        assert forbidden not in shell


def test_browser_session_is_exchanged_once_and_kept_only_in_generated_module_memory() -> None:
    sources = browser_authored_sources()
    session = sources["browser/session.ts"]
    generated = (WEB_SOURCE / "generated" / "api-v1.ts").read_text(encoding="utf-8")
    assert "initializeBrowserSession" in sources["main.tsx"]
    assert "exchangeBrowserSession" in session
    assert "setBrowserSessionToken" in session
    assert "window.history.replaceState" in session
    assert "let browserSessionToken: string | undefined" in generated
    assert "x-stata-browser-session" in generated
    for forbidden in ("localStorage", "sessionStorage", "document.cookie"):
        assert forbidden not in "\n".join(sources.values())


def test_pending_commands_preserve_exact_envelope_and_controls_are_typed() -> None:
    sources = browser_authored_sources()
    coordinator = sources["browser/commands.ts"]
    app = sources["App.tsx"]
    assert "existing.envelope" in coordinator
    assert "delivery_unknown" in coordinator
    assert "submitCommand" in coordinator
    assert 'command_type: "waiting.answer"' in app
    assert 'command_type: "message.submit"' in app
    assert 'command_type: "turn.pause.request"' in app
    assert "waiting_revision" in app
    assert "expected_turn_revision" in app
    for forbidden in ("turn.status = ", "queued_count++", "commit_revision++"):
        assert forbidden not in coordinator
        assert forbidden not in app
