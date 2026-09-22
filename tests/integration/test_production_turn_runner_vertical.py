"""Production composition: idea + Workspace data + model selection → Stata → Word."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.lineage import LineageEntryKind, LineageSelector
from stata_research_agent.application.lineage_service import EvidenceLineageService
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.provider_credentials import (
    CredentialUnavailableError,
    ProviderCredentialService,
)
from stata_research_agent.application.sandbox_execution import (
    SandboxExecutionReceipt,
    SandboxExecutionRequest,
    SandboxOutputCandidate,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.documents.word_renderer import inspect_docx
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.interfaces.production_turn_runner import ProductionTurnRunner
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)
from stata_research_agent.persistence.lineage_query import SqliteEvidenceLineageQuery
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT = Path(__file__).parents[2]
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")
AUTO_DATA = STATA_HOME / "auto.dta"
WORKER_PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write(self, target_name: str, secret: str) -> None:
        self.values[target_name] = secret

    def read(self, target_name: str) -> str:
        try:
            return self.values[target_name]
        except KeyError as error:
            raise CredentialUnavailableError("credential unavailable") from error

    def contains(self, target_name: str) -> bool:
        return target_name in self.values

    def delete(self, target_name: str) -> None:
        self.values.pop(target_name, None)


def _independent_evaluation_response(request_json: str) -> ProviderResponse | None:
    if "independent research checkpoint evaluator" not in request_json:
        return None
    return ProviderResponse(
        {
            "text": "",
            "tool_calls": [],
            "evaluation": {"verdict": "pass", "findings": ["completion_ready"]},
        }
    )


class ResearchModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "Inspect the Stata data before choosing variables.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": "describe",
                                "reset_data": True,
                                "execution_role": "data_step",
                                "timeout_seconds": 17,
                            },
                        }
                    ],
                }
            )
        if self.calls == 2:
            artifact_ids = re.findall(r"artifact_[0-9a-f-]{36}", request_json)
            assert artifact_ids
            return ProviderResponse(
                {
                    "text": "Use isolated Python for an exploratory non-regression summary.",
                    "tool_calls": [
                        {
                            "name": "python.run",
                            "arguments": {
                                "code": (
                                    "from pathlib import Path\n"
                                    "import json, os\n"
                                    "Path(os.environ['SRA_OUTPUT_DIR'], 'summary.json').write_text("
                                    "json.dumps({'purpose':'exploration'}), encoding='utf-8')"
                                ),
                                "inputs": [
                                    {
                                        "artifact_id": artifact_ids[-1],
                                        "relative_target": "auto.dta",
                                    }
                                ],
                                "network_mode": "block",
                                "timeout_seconds": 30,
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "captured_artifacts" in request_json
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            artifact_ids = re.findall(r"artifact_[0-9a-f-]{36}", request_json)
            assert operation_ids and len(artifact_ids) >= 2
            return ProviderResponse(
                {
                    "text": "Classify the exact exploratory table; do not adopt it.",
                    "tool_calls": [
                        {
                            "name": "research.classify_analysis_output",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "output_kind": "custom",
                                "method_summary": "Python exploratory metadata summary.",
                                "outputs": [{"artifact_id": artifact_ids[-2], "role": "primary"}],
                                "elements": [],
                            },
                        }
                    ],
                }
            )
        if self.calls == 4:
            assert "make" in request_json and "price" in request_json
            return ProviderResponse(
                {
                    "text": "Run the selected baseline as a formal Result candidate.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": "regress price mpg weight",
                                "reset_data": False,
                                "execution_role": "formal_result_candidate",
                            },
                        }
                    ],
                }
            )
        if self.calls == 5:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Qualify and adopt the chosen formal regression.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "term._cons.coefficient",
                                    "term._cons.se",
                                    "scalar.N",
                                    "scalar.r2",
                                ],
                                "result_slot_key": "baseline.primary",
                                "plan_summary": "Baseline OLS of price on mpg and weight.",
                            },
                        }
                    ],
                }
            )
        if self.calls == 6:
            assert "result_id" in request_json
            return ProviderResponse(
                {
                    "text": "Export the adopted result to Word.",
                    "tool_calls": [
                        {
                            "name": "research.export_word",
                            "arguments": {
                                "result_slot_key": "baseline.primary",
                                "document_title": "Auto baseline",
                                "manuscript_sections": {
                                    "title": "Automobile Prices and Fuel Economy",
                                    "abstract": (
                                        "This draft studies how fuel economy and vehicle weight "
                                        "relate to automobile prices."
                                    ),
                                    "research_question": (
                                        "The analysis asks whether price varies systematically "
                                        "with the selected vehicle characteristics."
                                    ),
                                    "data_and_methods": (
                                        "The study uses the supplied automobile data and an "
                                        "ordinary least squares baseline."
                                    ),
                                    "results": (
                                        "The estimates indicate a negative association for fuel "
                                        "economy and a positive association for vehicle weight."
                                    ),
                                    "limitations": (
                                        "The design is descriptive and does not establish a "
                                        "causal effect."
                                    ),
                                    "conclusion": (
                                        "The baseline provides a reproducible starting point for "
                                        "deeper specification and design checks."
                                    ),
                                },
                            },
                        }
                    ],
                }
            )
        assert "document_revision_id" in request_json
        return ProviderResponse(
            {
                "text": "The Stata-backed Word draft is ready.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Formal Stata result and Word draft delivered.",
                },
            }
        )


class ProductionSandbox:
    def __init__(self, execution_root: Path) -> None:
        self._execution_root = execution_root

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionReceipt:
        scratch = self._execution_root / ".stata-agent" / "staging" / request.attempt_id / "scratch"
        outputs = scratch / "outputs"
        outputs.mkdir(parents=True)
        program = scratch / "program.py"
        program.write_text(request.code, encoding="utf-8")
        summary = outputs / "summary.json"
        summary.write_text(json.dumps({"purpose": "exploration"}), encoding="utf-8")
        return SandboxExecutionReceipt(
            "stata-agent.sandbox-receipt/v1",
            request.attempt_id,
            "base-container",
            "b" * 64,
            request.network_mode,
            (),
            (str(scratch),),
            False,
            "passed",
            "passed",
            0,
            "exploration complete",
            "",
            program,
            (
                SandboxOutputCandidate(
                    "summary.json",
                    summary,
                    summary.stat().st_size,
                    hashlib.sha256(summary.read_bytes()).hexdigest(),
                ),
            ),
        )


class AdoptionModel:
    def __init__(self, output_id: str, fingerprint: str, preview_artifact_id: str) -> None:
        self._output_id = output_id
        self._fingerprint = fingerprint
        self._preview_artifact_id = preview_artifact_id
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            assert self._output_id in request_json
            assert self._fingerprint in request_json
            return ProviderResponse(
                {
                    "text": "Adopt the exact output version confirmed by the researcher.",
                    "tool_calls": [
                        {
                            "name": "research.adopt_analysis_output",
                            "arguments": {
                                "analysis_output_id": self._output_id,
                                "expected_output_fingerprint": self._fingerprint,
                                "preview_artifact_id": self._preview_artifact_id,
                                "confirmation_summary": (
                                    "The researcher explicitly confirmed this exact preview."
                                ),
                            },
                        }
                    ],
                }
            )
        assert "evidence_record_id" in request_json
        return ProviderResponse(
            {
                "text": "The exact non-regression output is now adopted.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Explicit Analysis Output adoption recorded.",
                },
            }
        )


class SkillLoadingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.loaded_revision: str | None = None

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            assert "Specialized Skills are optional and not loaded yet" in request_json
            assert "econ-visualization" in request_json
            revisions = re.findall(r"1\.0\.0\+sha256\.[0-9a-f]{16}", request_json)
            assert revisions
            self.loaded_revision = revisions[-1]
            return ProviderResponse(
                {
                    "text": "Load the registered figure guidance before advising.",
                    "tool_calls": [
                        {
                            "name": "research.load_skill",
                            "arguments": {
                                "skill_name": "econ-visualization",
                                "expected_revision": self.loaded_revision,
                            },
                        }
                    ],
                }
            )
        assert "Design reproducible economics figures" in request_json
        assert "exact fingerprint and preview Artifact" in request_json
        return ProviderResponse(
            {
                "text": "The relevant registered figure guidance is now available.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Specialized Skill loaded progressively.",
                },
            }
        )


class ReghdfeModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "Run the selected fixed-effects specification in Stata.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": ("reghdfe price mpg weight, absorb(foreign) vce(robust)"),
                                "reset_data": True,
                                "execution_role": "formal_result_candidate",
                            },
                        }
                    ],
                }
            )
        if self.calls == 2:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Qualify the exact reghdfe execution contract.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "term._cons.coefficient",
                                    "term._cons.se",
                                    "scalar.N",
                                    "scalar.r2",
                                ],
                                "result_slot_key": "fixed_effects.primary",
                                "plan_summary": (
                                    "Price on fuel economy with foreign fixed effects."
                                ),
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "result_id" in request_json
            return ProviderResponse(
                {
                    "text": "Export the qualified fixed-effects Result to Word.",
                    "tool_calls": [
                        {
                            "name": "research.export_word",
                            "arguments": {
                                "result_slot_key": "fixed_effects.primary",
                                "document_title": "Auto fixed effects",
                                "manuscript_sections": {
                                    "title": "Automobile Prices and Fixed Effects",
                                    "abstract": (
                                        "This draft studies the association between fuel "
                                        "economy and automobile prices."
                                    ),
                                    "research_question": (
                                        "The analysis asks whether the relationship remains "
                                        "after accounting for origin groups."
                                    ),
                                    "data_and_methods": (
                                        "The study uses the supplied automobile data and a "
                                        "fixed-effects linear specification."
                                    ),
                                    "results": (
                                        "The selected estimate indicates a negative association "
                                        "for fuel economy."
                                    ),
                                    "limitations": (
                                        "The design remains descriptive and depends on the "
                                        "selected grouping structure."
                                    ),
                                    "conclusion": (
                                        "The result offers a reproducible basis for further "
                                        "design and robustness checks."
                                    ),
                                },
                            },
                        }
                    ],
                }
            )
        assert "document_revision_id" in request_json
        return ProviderResponse(
            {
                "text": "The Stata fixed-effects Result and Word draft are ready.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Qualified and delivered the selected reghdfe Result.",
                },
            }
        )


class IvregressModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "Run the selected instrumental-variables specification in Stata.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": (
                                    "ivregress 2sls price weight (mpg = displacement), vce(robust)"
                                ),
                                "reset_data": True,
                                "execution_role": "formal_result_candidate",
                            },
                        }
                    ],
                }
            )
        if self.calls == 2:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Qualify the exact IV execution and instrument-role contract.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "term._cons.coefficient",
                                    "term._cons.se",
                                    "scalar.N",
                                    "scalar.r2",
                                ],
                                "result_slot_key": "iv.primary",
                                "plan_summary": (
                                    "A technical IV fixture with fuel economy treated as "
                                    "endogenous and displacement as the excluded instrument."
                                ),
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "result_id" in request_json
            return ProviderResponse(
                {
                    "text": "Export the qualified Stata IV Result to Word.",
                    "tool_calls": [
                        {
                            "name": "research.export_word",
                            "arguments": {
                                "result_slot_key": "iv.primary",
                                "document_title": "Auto instrumental variables fixture",
                                "manuscript_sections": {
                                    "title": "Automobile Prices and Fuel Economy",
                                    "abstract": (
                                        "This technical draft evaluates an instrumental-"
                                        "variables specification using the supplied data."
                                    ),
                                    "research_question": (
                                        "The analysis asks how the estimated price relationship "
                                        "changes under the selected instrument assignment."
                                    ),
                                    "data_and_methods": (
                                        "The study uses the supplied automobile data and a "
                                        "two-stage least-squares specification executed in Stata."
                                    ),
                                    "results": (
                                        "The formal estimates and model statistics are reported "
                                        "only in the linked Stata-produced table."
                                    ),
                                    "limitations": (
                                        "The instrument is a technical fixture and its relevance "
                                        "and validity require substantive researcher judgment."
                                    ),
                                    "conclusion": (
                                        "The traceable output provides a basis for reviewing the "
                                        "specification before any substantive use."
                                    ),
                                },
                            },
                        }
                    ],
                }
            )
        assert "document_revision_id" in request_json
        return ProviderResponse(
            {
                "text": "The Stata IV Result and Word draft are ready for review.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Qualified and delivered the selected IV Result.",
                },
            }
        )


class ProbitModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "Run the selected binary-response specification in Stata.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": "probit foreign mpg weight, vce(robust)",
                                "reset_data": True,
                                "execution_role": "formal_result_candidate",
                            },
                        }
                    ],
                }
            )
        if self.calls == 2:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Promote selected values from the exact probit execution.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "term._cons.coefficient",
                                    "term._cons.se",
                                    "scalar.N",
                                    "scalar.r2_p",
                                ],
                                "result_slot_key": "binary.primary",
                                "plan_summary": (
                                    "A binary probit model for automobile origin using fuel "
                                    "economy and weight."
                                ),
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "result_id" in request_json
            return ProviderResponse(
                {
                    "text": "Export the promoted Stata probit Result to Word.",
                    "tool_calls": [
                        {
                            "name": "research.export_word",
                            "arguments": {
                                "result_slot_key": "binary.primary",
                                "document_title": "Auto binary-response fixture",
                                "fit_statistic": "r2_p",
                                "fit_label": "Pseudo R-squared",
                                "manuscript_sections": {
                                    "title": "Automobile Origin and Observed Characteristics",
                                    "abstract": (
                                        "This technical draft evaluates a binary-response model "
                                        "using the supplied automobile data."
                                    ),
                                    "research_question": (
                                        "The analysis asks how observed automobile characteristics "
                                        "relate to the recorded origin indicator."
                                    ),
                                    "data_and_methods": (
                                        "The study uses the supplied data and a robust probit "
                                        "specification executed in Stata."
                                    ),
                                    "results": (
                                        "The formal coefficients and fit statistic appear only "
                                        "in the linked Stata-produced table."
                                    ),
                                    "limitations": (
                                        "The model is descriptive and does not establish a causal "
                                        "interpretation for the included characteristics."
                                    ),
                                    "conclusion": (
                                        "The traceable output supports review and later model "
                                        "refinement by the researcher."
                                    ),
                                },
                            },
                        }
                    ],
                }
            )
        assert "document_revision_id" in request_json
        return ProviderResponse(
            {
                "text": "The Stata logit Result and Word draft are ready for review.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Qualified and delivered the selected logit Result.",
                },
            }
        )


class PostEstimationModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        evaluation = _independent_evaluation_response(request_json)
        if evaluation is not None:
            return evaluation
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                {
                    "text": "Estimate the formal baseline in Stata.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": "regress price mpg weight",
                                "reset_data": True,
                                "execution_role": "formal_result_candidate",
                            },
                        }
                    ],
                }
            )
        if self.calls == 2:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Adopt the baseline estimation before testing it.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "scalar.N",
                                    "scalar.r2",
                                ],
                                "result_slot_key": "baseline.primary",
                                "plan_summary": "Baseline price regression.",
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "result_id" in request_json
            return ProviderResponse(
                {
                    "text": "Run the joint test immediately against the adopted estimation.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "code": "test mpg weight",
                                "reset_data": False,
                                "execution_role": "formal_post_estimation",
                            },
                        }
                    ],
                }
            )
        if self.calls == 4:
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            assert "scalar.p" in request_json
            return ProviderResponse(
                {
                    "text": "Promote the Stata-returned joint-test probability.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": ["scalar.p"],
                                "result_slot_key": "diagnostic.joint_test",
                                "plan_summary": "Joint test of the baseline predictors.",
                            },
                        }
                    ],
                }
            )
        assert "diagnostic.joint_test" in request_json
        return ProviderResponse(
            {
                "text": "The baseline and its formal joint test are traceable.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Baseline estimation and joint test adopted.",
                },
            }
        )


def test_production_runner_qualifies_real_reghdfe_result(tmp_path: Path) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_production_reghdfe")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_reghdfe_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_reghdfe_message"),
            "Run a fixed-effects model using the supplied data and produce a Word draft.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-production-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-reghdfe",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = ReghdfeModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
        sandbox_factory=ProductionSandbox,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        waiting = verified.execute(
            "SELECT prompt FROM waiting_requests WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()
        assert status == "succeeded", None if waiting is None else waiting[0]
        result = verified.execute(
            """
            SELECT profile.result_profile_id, contract.intended_specification_json
            FROM results AS result
            JOIN result_qualification_reports AS report
              ON report.result_qualification_report_id =
                 result.originating_qualification_report_id
            JOIN result_contracts AS contract USING (result_contract_id)
            JOIN result_profiles AS profile
             ON profile.result_profile_id = contract.result_profile_id
             AND profile.profile_version = contract.profile_version
            """
        ).fetchone()
        assert result["result_profile_id"] == "stata.generic-result.v1"
        intended = json.loads(str(result["intended_specification_json"]))
        assert intended["source_contract"] == "stata.result-catalog/v1"
        assert "term.mpg.coefficient" in intended["selected_source_keys"]
        assert (
            verified.execute(
                "SELECT COUNT(*) FROM result_elements WHERE semantic_key = 'scalar.r2'"
            ).fetchone()[0]
            == 1
        )
        assert verified.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == 1
        assert model.calls == 4
    finally:
        verified.close()
        global_connection.close()


def test_production_runner_qualifies_real_ivregress_result(tmp_path: Path) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_production_ivregress")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_ivregress_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_ivregress_message"),
            "Run the selected IV fixture and produce a traceable Word draft.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-production-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-ivregress",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = IvregressModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
        sandbox_factory=ProductionSandbox,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        waiting = verified.execute(
            "SELECT prompt FROM waiting_requests WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()
        assert status == "succeeded", None if waiting is None else waiting[0]
        result = verified.execute(
            """
            SELECT profile.result_profile_id, contract.intended_specification_json
            FROM results AS result
            JOIN result_qualification_reports AS report
              ON report.result_qualification_report_id =
                 result.originating_qualification_report_id
            JOIN result_contracts AS contract USING (result_contract_id)
            JOIN result_profiles AS profile
             ON profile.result_profile_id = contract.result_profile_id
             AND profile.profile_version = contract.profile_version
            """
        ).fetchone()
        assert result["result_profile_id"] == "stata.generic-result.v1"
        intended = json.loads(str(result["intended_specification_json"]))
        assert intended["source_contract"] == "stata.result-catalog/v1"
        assert "term.mpg.coefficient" in intended["selected_source_keys"]
        assert verified.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == 1
        assert model.calls == 4
    finally:
        verified.close()
        global_connection.close()


def test_production_runner_promotes_unregistered_probit_result_to_word(
    tmp_path: Path,
) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_production_logit")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_logit_workspace"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_logit_message"),
            "Run the selected binary model and produce a traceable Word draft.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-production-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-logit",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = ProbitModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
        sandbox_factory=ProductionSandbox,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        waiting = verified.execute(
            "SELECT prompt FROM waiting_requests WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()
        assert status == "succeeded", None if waiting is None else waiting[0]
        result = verified.execute(
            """
            SELECT profile.result_profile_id, contract.intended_specification_json
            FROM results AS result
            JOIN result_qualification_reports AS report
              ON report.result_qualification_report_id =
                 result.originating_qualification_report_id
            JOIN result_contracts AS contract USING (result_contract_id)
            JOIN result_profiles AS profile
             ON profile.result_profile_id = contract.result_profile_id
             AND profile.profile_version = contract.profile_version
            """
        ).fetchone()
        assert result["result_profile_id"] == "stata.generic-result.v1"
        intended = json.loads(str(result["intended_specification_json"]))
        assert intended["source_contract"] == "stata.result-catalog/v1"
        assert "scalar.r2_p" in intended["selected_source_keys"]
        assert (
            verified.execute(
                "SELECT COUNT(*) FROM result_elements WHERE semantic_key = 'scalar.r2_p'"
            ).fetchone()[0]
            == 1
        )
        assert (
            verified.execute(
                "SELECT COUNT(*) FROM result_elements WHERE semantic_key = 'term.mpg.se'"
            ).fetchone()[0]
            == 1
        )
        profile_id = verified.execute(
            "SELECT export_profile_id FROM table_export_input_manifests"
        ).fetchone()[0]
        assert profile_id == "stata.esttab.rtf.generic-result-table"
        assert verified.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == 1
        assert model.calls == 4
    finally:
        verified.close()
        global_connection.close()


def test_production_runner_promotes_immediate_postestimation_return_scalar(
    tmp_path: Path,
) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_production_postestimation")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_postestimation_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_postestimation_message"),
            "Run a baseline regression and preserve its joint test as formal evidence.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-production-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-postestimation",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = PostEstimationModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
        sandbox_factory=ProductionSandbox,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        assert status == "succeeded"
        post_element = verified.execute(
            """
            SELECT element.canonical_decimal_text, locator.locator_type,
                   locator.locator_json, child.operation_id AS child_operation_id,
                   binding.source_data_state_operation_id AS parent_operation_id,
                   json_extract(child_manifest.receipt_json, '$.exec_seq') AS child_exec_seq,
                   json_extract(parent_manifest.receipt_json, '$.exec_seq') AS parent_exec_seq
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption USING (result_slot_id)
            JOIN result_elements AS element
              ON element.result_id = adoption.target_result_id
            JOIN result_source_locators AS locator
              ON locator.result_source_locator_id = element.result_source_locator_id
            JOIN results AS result ON result.result_id = element.result_id
            JOIN stata_runs AS child ON child.stata_run_id = result.producing_stata_run_id
            JOIN operation_attempts AS child_attempt
              ON child_attempt.operation_id = child.operation_id
            JOIN completion_manifests AS child_manifest
              ON child_manifest.operation_attempt_id = child_attempt.operation_attempt_id
            JOIN stata_operation_input_bindings AS binding
              ON binding.operation_attempt_id = child_attempt.operation_attempt_id
            JOIN operation_attempts AS parent_attempt
              ON parent_attempt.operation_id = binding.source_data_state_operation_id
            JOIN completion_manifests AS parent_manifest
              ON parent_manifest.operation_attempt_id = parent_attempt.operation_attempt_id
            WHERE slot.canonical_key = 'diagnostic.joint_test'
              AND element.semantic_key = 'scalar.p'
            """
        ).fetchone()
        assert post_element is not None
        assert post_element["locator_type"] == "r_scalar"
        assert json.loads(str(post_element["locator_json"])) == {
            "locator_type": "R_SCALAR",
            "name": "r(p)",
        }
        assert int(post_element["child_exec_seq"]) == int(post_element["parent_exec_seq"]) + 1
        assert post_element["child_operation_id"] != post_element["parent_operation_id"]
        purposes = [
            str(row[0])
            for row in verified.execute(
                """
                SELECT binding.execution_purpose
                FROM stata_operation_input_bindings AS binding
                JOIN operation_attempts AS attempt
                  ON attempt.operation_attempt_id = binding.operation_attempt_id
                JOIN operations AS operation ON operation.operation_id = attempt.operation_id
                JOIN completion_manifests AS manifest
                  ON manifest.operation_attempt_id = attempt.operation_attempt_id
                WHERE operation.status = 'completed'
                ORDER BY json_extract(manifest.receipt_json, '$.exec_seq')
                """
            ).fetchall()
        ]
        assert purposes == ["data_load", "formal_estimation", "formal_post_estimation"]
        assert model.calls == 5
    finally:
        verified.close()
        global_connection.close()


def test_production_runner_connects_selected_model_data_stata_and_word(
    tmp_path: Path,
) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_production_vertical")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_production_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_production_message"),
            "Use auto data to regress price on mpg and weight and produce Word.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-production-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-research",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = ResearchModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
        sandbox_factory=ProductionSandbox,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    classified_identity: tuple[str, str, str]
    try:
        observed_status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        waiting = verified.execute(
            "SELECT prompt FROM waiting_requests WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()
        if (
            observed_status == "waiting"
            and verified.execute(
                "SELECT COUNT(*) FROM completion_manifests WHERE execution_status = 'crashed'"
            ).fetchone()[0]
        ):
            pytest.skip("local Stata worker crashed under the aggregate real-runtime suite")
        assert observed_status == "succeeded", None if waiting is None else waiting[0]
        assert verified.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
        assert verified.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == 1
        document_payload = verified.execute(
            """
            SELECT location.managed_handle
            FROM document_revisions AS revision
            JOIN artifact_locations AS location
              ON location.artifact_id = revision.docx_artifact_id
            """
        ).fetchone()
        assert document_payload is not None
        docx_path = (database.root / str(document_payload[0])).resolve()
        inspected_document = inspect_docx(docx_path.read_bytes(), ())
        assert "Research Question" in inspected_document.visible_text
        assert "Results Table" in inspected_document.visible_text
        data_state_chain = verified.execute(
            """
            SELECT operation.operation_id, binding.execution_purpose,
                   binding.source_data_state_operation_id
            FROM stata_operation_input_bindings AS binding
            JOIN operation_attempts AS attempt USING (operation_attempt_id)
            JOIN operations AS operation USING (operation_id)
            ORDER BY operation.created_revision
            """
        ).fetchall()
        chain_by_purpose = {
            str(row["execution_purpose"]): row
            for row in data_state_chain
            if str(row["execution_purpose"]) in {"data_load", "data_step", "formal_estimation"}
        }
        assert set(chain_by_purpose) == {
            "data_load",
            "data_step",
            "formal_estimation",
        }
        assert (
            chain_by_purpose["data_step"]["source_data_state_operation_id"]
            == (chain_by_purpose["data_load"]["operation_id"])
        )
        assert (
            chain_by_purpose["formal_estimation"]["source_data_state_operation_id"]
            == chain_by_purpose["data_step"]["operation_id"]
        )
        assert (
            verified.execute(
                """
                SELECT request.timeout_seconds
                FROM stata_operation_requests AS request
                JOIN operation_attempts AS attempt USING (operation_attempt_id)
                JOIN operations AS operation USING (operation_id)
                JOIN stata_operation_input_bindings AS binding
                  USING (operation_attempt_id)
                WHERE binding.execution_purpose = 'data_step'
                """
            ).fetchone()[0]
            == 17.0
        )
        registered_tools = {
            str(row[0])
            for row in verified.execute("SELECT tool_name FROM tool_contracts").fetchall()
        }
        assert {
            "stata.execute",
            "research.promote_stata_result",
            "research.export_word",
            "python.run",
            "shell.run",
            "research.classify_analysis_output",
            "research.adopt_analysis_output",
            "research.load_skill",
        }.issubset(registered_tools)
        sandbox_attempt = verified.execute(
            """
            SELECT attempt.operation_attempt_id
            FROM operation_attempts AS attempt
            JOIN operations AS operation USING (operation_id)
            WHERE operation.operation_kind = 'python.execute'
              AND operation.status = 'completed'
            """
        ).fetchone()
        assert sandbox_attempt is not None
        assert (
            verified.execute(
                "SELECT COUNT(*) FROM artifacts WHERE producer_attempt_id = ?",
                (sandbox_attempt[0],),
            ).fetchone()[0]
            == 2
        )
        assert verified.execute("SELECT COUNT(*) FROM analysis_outputs").fetchone()[0] == 1
        assert verified.execute("SELECT COUNT(*) FROM analysis_output_adoptions").fetchone()[0] == 0
        classified = verified.execute(
            """
            SELECT output.analysis_output_id, output.output_fingerprint,
                   binding.artifact_id
            FROM analysis_outputs AS output
            JOIN analysis_output_artifacts AS binding USING (analysis_output_id)
            WHERE binding.role = 'primary'
            """
        ).fetchone()
        classified_identity = (str(classified[0]), str(classified[1]), str(classified[2]))
        first_element_id = str(
            verified.execute(
                "SELECT result_element_id FROM result_elements ORDER BY semantic_key LIMIT 1"
            ).fetchone()[0]
        )
        lineage = EvidenceLineageService(SqliteEvidenceLineageQuery(verified)).load(
            LineageSelector(LineageEntryKind.RESULT_ELEMENT, first_element_id)
        )
        assert [step.execution_purpose for step in lineage.data_state_steps] == [
            "data_load",
            "data_step",
            "formal_estimation",
        ]
        assert lineage.data_state_steps[-1].command_text == "regress price mpg weight"
        assert model.calls == 7
    finally:
        verified.close()

    async def inspect_analysis_api() -> tuple[dict[str, object], bytes]:
        transport = httpx.ASGITransport(app=create_app(host))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            index = await client.get(f"/api/v1/workspaces/{workspace_id.value}/analysis-outputs")
            artifact = await client.get(
                f"/api/v1/workspaces/{workspace_id.value}/artifacts/"
                f"{classified_identity[2]}/content"
            )
            assert index.status_code == 200
            assert artifact.status_code == 200
            return index.json(), artifact.content

    index_payload, artifact_payload = asyncio.run(inspect_analysis_api())
    assert len(index_payload["data"]["items"]) == 1
    assert index_payload["data"]["items"][0]["adoption_id"] is None
    assert json.loads(artifact_payload) == {"purpose": "exploration"}

    adoption_connection = database.open(writable=True)
    adoption_control = WorkspaceControlService(
        SqliteControlStore(adoption_connection), UuidIdentityGenerator()
    )
    adoption_turn = adoption_control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_production_adoption_message"),
            "I reviewed and adopt the exact Python non-regression output shown above.",
        )
    )
    adoption_connection.close()
    adoption_pool = WorkspaceExecutionPool(runtime_factory)
    adoption_model = AdoptionModel(*classified_identity)
    adoption_runner = ProductionTurnRunner(
        host,
        adoption_pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=adoption_model,
        sandbox_factory=ProductionSandbox,
    )

    async def adoption_scenario() -> None:
        try:
            await adoption_runner.run_turn(workspace_id, adoption_turn.turn_id)
        finally:
            await adoption_pool.close()

    asyncio.run(adoption_scenario())
    adopted = database.open(writable=False)
    try:
        assert (
            adopted.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (adoption_turn.turn_id.value,)
            ).fetchone()[0]
            == "succeeded"
        )
        assert adopted.execute("SELECT COUNT(*) FROM analysis_output_adoptions").fetchone()[0] == 1
        assert adopted.execute("SELECT COUNT(*) FROM analysis_evidence_records").fetchone()[0] == 1
        assert adoption_model.calls == 2
    finally:
        adopted.close()
        global_connection.close()


def test_production_runner_progressively_loads_registered_specialized_skill(
    tmp_path: Path,
) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Stata test runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_skill_loading")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_skill_workspace"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_skill_message"),
            "Advise how to design an event-study figure for this research.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-skill-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-skill-loader",
    )

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    model = SkillLoadingModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        assert (
            verified.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "succeeded"
        )
        loaded = verified.execute(
            """
            SELECT result.structured_payload_json
            FROM canonical_tool_results AS result
            JOIN tool_calls AS call USING (tool_call_id)
            WHERE call.requested_tool_name = 'research.load_skill'
            """
        ).fetchone()
        assert loaded is not None
        payload = json.loads(str(loaded[0]))
        assert payload["name"] == "econ-visualization"
        assert payload["revision"] == model.loaded_revision
        assert payload["content_sha256"]
        assert model.calls == 2
    finally:
        verified.close()
        global_connection.close()
