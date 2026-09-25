"""Outer composition root for the production Windows loopback service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from stata_research_agent import __version__
from stata_research_agent.application.diagnostic_service import DiagnosticService
from stata_research_agent.application.diagnostic_tracing import DiagnosticTracer
from stata_research_agent.application.diagnostics import (
    DiagnosticEventCandidate,
    default_diagnostic_registry,
)
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.provider_credentials import ProviderCredentialService
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.application.streaming import EphemeralModelDeltaHub
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.interfaces.cross_encoder_reranker import (
    AdaptiveFusionEvidenceReranker,
    AdaptiveRerankPolicy,
    CrossEncoderEvidenceReranker,
)
from stata_research_agent.interfaces.diagnostic_bundle_builder import DiagnosticBundleBuilder
from stata_research_agent.interfaces.filesystem_diagnostics import FilesystemDiagnosticSink
from stata_research_agent.interfaces.literature_catalog import MineruCliParser
from stata_research_agent.interfaces.loopback_launcher import LoopbackLauncherClient
from stata_research_agent.interfaces.memory_maintenance_runner import (
    ProductionMemoryMaintenanceRunner,
)
from stata_research_agent.interfaces.production_turn_runner import ProductionTurnRunner
from stata_research_agent.interfaces.provider_pricing import load_provider_pricing_catalog
from stata_research_agent.interfaces.secured_loopback_instance import SecuredLoopbackInstance
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.interfaces.windows_credential_store import WindowsCredentialStore
from stata_research_agent.interfaces.windows_runtime_control import (
    RuntimeAlreadyActiveError,
    WindowsRuntimeControl,
)
from stata_research_agent.interfaces.windows_sandbox_executor import WindowsSandboxExecutor
from stata_research_agent.interfaces.workspace_skills import FilesystemMainSkillCatalog
from stata_research_agent.persistence.diagnostic_projection import WorkspaceDiagnosticProjection
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)
from stata_research_agent.runtime.application_runtime import WorkspaceTurnSupervisor
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime


class ServiceStartupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceLayout:
    workspace_root: Path
    runtime_directory: Path
    static_directory: Path
    skills_directory: Path
    mcp_executor: Path | None
    stata_home: Path
    sandbox_wxc: Path | None = None
    sandbox_python: Path | None = None
    sandbox_powershell: Path | None = None
    embedding_model: str | None = None
    embedding_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    embedding_cache: Path | None = None
    reranker_model: str | None = None
    reranker_revision: str | None = None
    reranker_cache: Path | None = None
    reranker_device: str = "cpu"
    mineru_executable: Path | None = None
    mineru_version: str | None = None


def source_or_frozen_static_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root) / "web"
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def source_or_frozen_skills_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root) / "skills"
    return Path(__file__).resolve().parents[3] / "skills"


def default_service_layout() -> ServiceLayout:
    local_app_data = os.environ.get("LOCALAPPDATA")
    user_profile = os.environ.get("USERPROFILE")
    if not local_app_data or not user_profile:
        raise ServiceStartupError("Windows per-user directories are unavailable")
    executable = Path(sys.executable).resolve()
    # The release carries a normal CPython runtime plus the locked MCP wheel.  The
    # application is ``payload/app/stata-research-agent.exe`` and the executor is
    # ``payload/executor/python.exe``; a frozen MCP executable is intentionally not
    # part of the supported layout because PyStata imports native Stata modules at
    # runtime.
    default_mcp = (
        executable.parent.parent / "executor" / "python.exe"
        if getattr(sys, "frozen", False)
        else executable
    )
    configured_wxc = os.environ.get("SRA_WXC_EXECUTABLE")
    powershell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    return ServiceLayout(
        Path(user_profile) / "Documents" / "Stata Research Agent",
        Path(local_app_data) / "StataResearchAgent" / "runtime",
        source_or_frozen_static_directory(),
        source_or_frozen_skills_directory(),
        default_mcp,
        Path(os.environ.get("STATA_HOME", r"C:\Program Files\Stata18")),
        None if not configured_wxc else Path(configured_wxc),
        default_mcp,
        powershell,
        os.environ.get("SRA_EMBEDDING_MODEL"),
        os.environ.get("SRA_EMBEDDING_REVISION", "614241f622f53c4eeff9890bdc4f31cfecc418b3"),
        (
            None
            if not os.environ.get("SRA_EMBEDDING_CACHE")
            else Path(str(os.environ["SRA_EMBEDDING_CACHE"]))
        ),
        os.environ.get("SRA_RERANKER_MODEL"),
        os.environ.get("SRA_RERANKER_REVISION"),
        (
            None
            if not os.environ.get("SRA_RERANKER_CACHE")
            else Path(str(os.environ["SRA_RERANKER_CACHE"]))
        ),
        os.environ.get("SRA_RERANKER_DEVICE", "cpu"),
        (
            None
            if not os.environ.get("SRA_MINERU_EXECUTABLE")
            else Path(str(os.environ["SRA_MINERU_EXECUTABLE"]))
        ),
        os.environ.get("SRA_MINERU_VERSION"),
    )


def _knowledge_adapters(
    layout: ServiceLayout,
) -> tuple[
    SentenceTransformerEmbeddingGateway | None,
    MineruCliParser | None,
    AdaptiveFusionEvidenceReranker | None,
]:
    embedding = None
    if layout.embedding_model is not None:
        embedding = SentenceTransformerEmbeddingGateway(
            model_name=layout.embedding_model,
            model_revision=layout.embedding_revision,
            cache_folder=layout.embedding_cache,
        )
    mineru = None
    if layout.mineru_executable is not None:
        if layout.mineru_version is None:
            raise ServiceStartupError("configured MinerU requires a pinned version")
        mineru = MineruCliParser(layout.mineru_executable, version=layout.mineru_version)
    reranker = None
    if layout.reranker_model is not None:
        if layout.reranker_revision is None:
            raise ServiceStartupError("configured reranker requires a pinned revision")
        reranker_cache = layout.reranker_cache or layout.embedding_cache
        if reranker_cache is None:
            raise ServiceStartupError("configured reranker requires a model cache directory")
        neural_reranker = CrossEncoderEvidenceReranker(
            model_name=layout.reranker_model,
            model_revision=layout.reranker_revision,
            cache_folder=str(reranker_cache),
            device=layout.reranker_device,
            batch_size=32 if layout.reranker_device.startswith("cuda") else 8,
            max_length=512,
            top_n=32,
            lazy_load=True,
        )
        reranker = AdaptiveFusionEvidenceReranker(
            neural_reranker,
            policy=AdaptiveRerankPolicy(minimum_rank_disagreement=6),
        )
    return embedding, mineru, reranker


def release_probe(layout: ServiceLayout) -> dict[str, object]:
    """Read-only activation probe; it must not create runtime or Workspace state."""
    static_index = layout.static_directory.resolve() / "index.html"
    if not static_index.is_file():
        raise ServiceStartupError("bundled browser assets are incomplete")
    if layout.mcp_executor is None or not layout.mcp_executor.resolve().is_file():
        raise ServiceStartupError("bundled Stata MCP Python runtime is missing")
    try:
        FilesystemMainSkillCatalog(layout.skills_directory).resolve(layout.workspace_root)
    except Exception as error:
        raise ServiceStartupError("bundled main Skill is missing or invalid") from error
    if not layout.stata_home.resolve().is_dir():
        raise ServiceStartupError("Stata installation directory is missing")
    return {
        "schema_version": "stata-research-agent.release-probe/v1",
        "application_version": __version__,
        "browser_assets_present": True,
        "main_skill_present": True,
        "stata_mcp_executor_present": True,
        "stata_home_present": True,
        "read_only": True,
    }


def _open_existing(runtime_directory: Path) -> bool:
    location = LoopbackLauncherClient(WindowsRuntimeControl(runtime_directory)).connect_existing()
    if location is None:
        return False
    return bool(webbrowser.open(location.url, new=2))


async def _open_browser_after_start(server: uvicorn.Server, runtime_directory: Path) -> None:
    for _ in range(200):
        if server.started:
            opened = await asyncio.to_thread(_open_existing, runtime_directory)
            if not opened:
                print(
                    "Browser launch was unavailable; the service remains active.",
                    file=sys.stderr,
                )
            return
        if server.should_exit:
            return
        await asyncio.sleep(0.05)
    print("Browser launch timed out; the service remains active.", file=sys.stderr)


async def serve(layout: ServiceLayout, *, open_browser: bool, log_level: str) -> None:
    layout.workspace_root.mkdir(parents=True, exist_ok=True)
    try:
        secured = SecuredLoopbackInstance.open(layout.runtime_directory)
    except RuntimeAlreadyActiveError:
        if open_browser and await asyncio.to_thread(_open_existing, layout.runtime_directory):
            return
        raise ServiceStartupError("an existing service owns the runtime lock") from None
    with secured:
        diagnostic_gate = SensitiveOutputGate()
        try:
            diagnostic_sink: FilesystemDiagnosticSink | None = FilesystemDiagnosticSink(
                layout.runtime_directory.parent / "control" / "diagnostics"
            )
        except OSError:
            diagnostic_sink = None
        raw_build_id = os.environ.get("SRA_BUILD_ID", f"build-{__version__}")
        build_id = (
            "".join(
                character if character.isalnum() or character in "._-" else "_"
                for character in raw_build_id
            )[:128]
            or "build-unknown"
        )
        diagnostic_service: DiagnosticService | None = None
        tracer: DiagnosticTracer | None = None
        if diagnostic_sink is not None:
            try:
                diagnostic_sink.enforce_retention()
            except OSError:
                pass
            diagnostic_service = DiagnosticService(
                diagnostic_sink,
                default_diagnostic_registry(),
                diagnostic_gate,
                release_id=__version__,
                build_id=build_id,
                instance_id=secured.discovery.instance_id,
            )
            tracer = DiagnosticTracer(diagnostic_service)
            diagnostic_service.record(
                DiagnosticEventCandidate(
                    "process.lifecycle",
                    9,
                    "INFO",
                    "PROCESS_STARTED",
                    "service_main",
                    "main_service",
                    {"state_code": "started", "generation": 1},
                )
            )
        credential_connection = GlobalCredentialDatabase(
            layout.runtime_directory.parent / "control" / "provider-credentials.sqlite3"
        ).open()
        try:
            credential_repository = SqliteProviderCredentialRepository(credential_connection)
            provider_credentials = ProviderCredentialService(
                credential_repository,
                WindowsCredentialStore(),
            )
            provider_credentials.recover()
            model_configuration = WorkspaceModelConfigurationService(credential_repository)
            provider_pricing = load_provider_pricing_catalog(
                layout.runtime_directory.parent / "control" / "provider-pricing.json"
            )

            def runtime_factory(working_directory: Path) -> StdioStataRuntime:
                assert layout.mcp_executor is not None
                return StdioStataRuntime(
                    python_executable=layout.mcp_executor,
                    mcp_source_root=None,
                    working_directory=working_directory,
                    stata_home=layout.stata_home,
                )

            host = WorkspaceHost(layout.workspace_root)
            pool = WorkspaceExecutionPool(runtime_factory)
            assert layout.mcp_executor is not None
            sandbox_factory = None
            if layout.sandbox_wxc is not None:
                if layout.sandbox_python is None or layout.sandbox_powershell is None:
                    raise ServiceStartupError(
                        "sandbox Python and PowerShell must accompany the WXC runtime"
                    )

                def sandbox_factory(execution_root: Path) -> WindowsSandboxExecutor:
                    assert layout.sandbox_wxc is not None
                    assert layout.sandbox_python is not None
                    assert layout.sandbox_powershell is not None
                    return WindowsSandboxExecutor(
                        wxc_executable=layout.sandbox_wxc,
                        python_executable=layout.sandbox_python,
                        powershell_executable=layout.sandbox_powershell,
                        execution_root=execution_root,
                    )

            model_delta_hub = EphemeralModelDeltaHub()
            embedding_gateway, literature_pdf_parser, evidence_reranker = (
                _knowledge_adapters(layout)
            )
            turn_runner = ProductionTurnRunner(
                host,
                pool,
                model_configuration,
                provider_credentials,
                layout.mcp_executor,
                main_skills=FilesystemMainSkillCatalog(layout.skills_directory),
                sandbox_factory=sandbox_factory,
                model_delta_hub=model_delta_hub,
                embedding_gateway=embedding_gateway,
                evidence_reranker=evidence_reranker,
                literature_pdf_parser=literature_pdf_parser,
                diagnostics=diagnostic_service,
                tracer=tracer,
            )
            supervisor = WorkspaceTurnSupervisor(
                host,
                turn_runner,
                ProductionMemoryMaintenanceRunner(
                    host,
                    model_configuration,
                    provider_credentials,
                ),
            )

            diagnostic_bundle_factory: (
                Callable[[WorkspaceId | None], DiagnosticBundleBuilder] | None
            ) = None
            if diagnostic_sink is not None:

                def build_diagnostic_bundle(
                    workspace_id: WorkspaceId | None,
                ) -> DiagnosticBundleBuilder:
                    projection = (
                        None
                        if workspace_id is None
                        else WorkspaceDiagnosticProjection(host.database(workspace_id))
                    )
                    return DiagnosticBundleBuilder(
                        diagnostic_sink,
                        diagnostic_gate,
                        layout.runtime_directory.parent / "control" / "bundle-staging",
                        system_profile={
                            "release_id": __version__,
                            "build_id": build_id,
                            "supported_profile": True,
                        },
                        workspace_projection=projection,
                    )

                diagnostic_bundle_factory = build_diagnostic_bundle

            app = create_app(
                host,
                static_directory=layout.static_directory,
                turn_dispatcher=supervisor,
                local_session_authority=secured.local_session_authority,
                provider_credentials=provider_credentials,
                model_configuration=model_configuration,
                model_delta_hub=model_delta_hub,
                provider_pricing=provider_pricing,
                diagnostic_bundle_factory=diagnostic_bundle_factory,
            )
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=secured.discovery.port,
                log_level=log_level,
            )
            server = uvicorn.Server(config)
            browser_task = (
                asyncio.create_task(_open_browser_after_start(server, layout.runtime_directory))
                if open_browser
                else None
            )
            try:
                await server.serve(sockets=[secured.server_socket])
            finally:
                if browser_task is not None:
                    browser_task.cancel()
                    await asyncio.gather(browser_task, return_exceptions=True)
                await supervisor.close()
                await pool.close()
                if diagnostic_service is not None:
                    diagnostic_service.record(
                        DiagnosticEventCandidate(
                            "process.lifecycle",
                            9,
                            "INFO",
                            "PROCESS_STOPPED",
                            "service_main",
                            "main_service",
                            {"state_code": "stopped", "generation": 1},
                        )
                    )
        finally:
            credential_connection.close()


def _parser(defaults: ServiceLayout) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stata-research-agent")
    parser.add_argument("--workspace-root", type=Path, default=defaults.workspace_root)
    parser.add_argument("--runtime-directory", type=Path, default=defaults.runtime_directory)
    parser.add_argument("--static-directory", type=Path, default=defaults.static_directory)
    parser.add_argument("--skills-directory", type=Path, default=defaults.skills_directory)
    parser.add_argument("--mcp-executor", type=Path, default=defaults.mcp_executor)
    parser.add_argument("--stata-home", type=Path, default=defaults.stata_home)
    parser.add_argument("--sandbox-wxc", type=Path, default=defaults.sandbox_wxc)
    parser.add_argument("--sandbox-python", type=Path, default=defaults.sandbox_python)
    parser.add_argument("--sandbox-powershell", type=Path, default=defaults.sandbox_powershell)
    parser.add_argument("--embedding-model", default=defaults.embedding_model)
    parser.add_argument("--embedding-revision", default=defaults.embedding_revision)
    parser.add_argument("--embedding-cache", type=Path, default=defaults.embedding_cache)
    parser.add_argument("--reranker-model", default=defaults.reranker_model)
    parser.add_argument("--reranker-revision", default=defaults.reranker_revision)
    parser.add_argument("--reranker-cache", type=Path, default=defaults.reranker_cache)
    parser.add_argument("--reranker-device", default=defaults.reranker_device)
    parser.add_argument("--mineru-executable", type=Path, default=defaults.mineru_executable)
    parser.add_argument("--mineru-version", default=defaults.mineru_version)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--log-level", default="warning")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        defaults = default_service_layout()
        arguments = _parser(defaults).parse_args(argv)
        if arguments.version:
            print(__version__)
            return 0
        layout = ServiceLayout(
            workspace_root=arguments.workspace_root,
            runtime_directory=arguments.runtime_directory,
            static_directory=arguments.static_directory,
            skills_directory=arguments.skills_directory,
            mcp_executor=arguments.mcp_executor,
            stata_home=arguments.stata_home,
            sandbox_wxc=arguments.sandbox_wxc,
            sandbox_python=arguments.sandbox_python,
            sandbox_powershell=arguments.sandbox_powershell,
            embedding_model=arguments.embedding_model,
            embedding_revision=arguments.embedding_revision,
            embedding_cache=arguments.embedding_cache,
            reranker_model=arguments.reranker_model,
            reranker_revision=arguments.reranker_revision,
            reranker_cache=arguments.reranker_cache,
            reranker_device=arguments.reranker_device,
            mineru_executable=arguments.mineru_executable,
            mineru_version=arguments.mineru_version,
        )
        if arguments.probe:
            print(json.dumps(release_probe(layout), sort_keys=True))
            return 0
        asyncio.run(
            serve(
                layout,
                open_browser=not arguments.no_browser,
                log_level=arguments.log_level,
            )
        )
        return 0
    except (OSError, ServiceStartupError, ValueError) as error:
        print(f"Stata Research Agent could not start: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Ctrl+C is a normal local-service shutdown, not a product crash.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
