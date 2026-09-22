from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    VerifyArtifactCommand,
)
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.evidence import (
    AdoptPathResultCommand,
    EvidenceSlot,
    RenderFormalResultBlockCommand,
)
from stata_research_agent.application.evidence_service import EvidenceService
from stata_research_agent.application.result_profile import (
    QualifyRegressResultCommand,
    RegisterRegressProfileCommand,
)
from stata_research_agent.application.result_profile_service import (
    RegisteredResultProfileService,
)
from stata_research_agent.application.stata_operation import ExecuteStataCommand
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.artifact_data import (
    DataVersionKind,
    VerificationPurpose,
)
from stata_research_agent.domain.evidence import NumericCoverageError
from stata_research_agent.domain.identifiers import (
    CommandId,
    DataVersionId,
    ResearchPathId,
    ResultElementId,
    ResultId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.result_profile import QualificationVerdict
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
    StataSessionCloseResult,
)
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evidence_store import SqliteEvidenceRepository
from stata_research_agent.persistence.result_profile_store import (
    SqliteResultProfileRepository,
)
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def regress_structured() -> dict[str, object]:
    terms = [
        (
            "mpg",
            -49.51222066635532,
            86.15603886718478,
            -0.5746807921692137,
            0.5673237265003676,
            [-221.30248296542823, 122.27804163271759],
        ),
        (
            "weight",
            1.7465591583457587,
            0.6413537894299924,
            2.7232382300228166,
            0.00812981347542976,
            [0.4677360234691663, 3.025382293222351],
        ),
        (
            "_cons",
            1946.0686679648597,
            3597.049598758087,
            0.5410180245045153,
            0.5901886282312142,
            [-5226.244523290592, 9118.381859220312],
        ),
    ]
    mask_hex = "ff" * 9 + "03"
    return {
        "result_profile_capability": {
            "result_profile_id": "linear_model.regress.v1",
            "profile_version": 1,
            "snapshot_schema_version": "stata.regress.snapshot/v1",
            "extractor_contract_hash": (
                "e23ec60a1ed6dda223fe8729d3f7a89a39706f506914b04f309f605264dd89f8"
            ),
        },
        "N": 74.0,
        "r2": 0.29338912319475285,
        "cmd": "regress",
        "cmdline": "regress price mpg weight",
        "depvar": "price",
        "vce": "ols",
        "coefs": [
            {"var": name, "coef": coef, "se": se, "t": t, "p": p, "ci": ci}
            for name, coef, se, t, p, ci in terms
        ],
        "scalars": {"N": 74.0, "r2": 0.29338912319475285, "df_r": 71.0},
        "stored_result_source_map": {
            "schema_version": "stata.stored-result-source-map/v1alpha1",
            "command_type": "regress",
            "scalars": {
                "N": {"locator_type": "E_SCALAR", "name": "e(N)"},
                "r2": {"locator_type": "E_SCALAR", "name": "e(r2)"},
                "df_r": {"locator_type": "E_SCALAR", "name": "e(df_r)"},
            },
            "terms": [
                {
                    "display_key": name,
                    "coefficient": {
                        "locator_type": "E_MATRIX_CELL",
                        "matrix": "e(b)",
                        "equation": "",
                        "column_key": name,
                    },
                    "variance": {
                        "locator_type": "E_MATRIX_CELL",
                        "matrix": "e(V)",
                        "row_key": name,
                        "column_key": name,
                    },
                }
                for name, *_ in terms
            ],
        },
        "estimation_sample_manifest": {
            "schema_version": "stata.estimation-sample-mask/v1alpha1",
            "source": "e(sample)",
            "row_domain": "observation_order",
            "row_count": 74,
            "included_count": 74,
            "encoding": "bitset_lsb0_hex",
            "mask_hex": mask_hex,
            "mask_sha256": hashlib.sha256(bytes.fromhex(mask_hex)).hexdigest(),
        },
        "provenance": {
            "command_hash": "abc123",
            "data_signature": "74:12:test",
            "exec_seq": 2,
        },
    }


def reghdfe_structured() -> dict[str, object]:
    structured = copy.deepcopy(regress_structured())
    structured.update(
        {
            "result_profile_capability": {
                "result_profile_id": "linear_model.reghdfe.v1",
                "profile_version": 1,
                "snapshot_schema_version": "stata.reghdfe.snapshot/v1",
                "extractor_contract_hash": (
                    "51e0365a980a58091ee0569140526e5f28612da151eb68642de8943ad9facaf0"
                ),
            },
            "cmd": "reghdfe",
            "cmdline": "reghdfe price mpg, absorb(foreign) vce(robust)",
            "vce": "robust",
            "absorbed_effects": ["foreign"],
            "cluster_variables": [],
            "r2": 0.28384746306717534,
            "r2_within": 0.2821435687264836,
            "profile_environment": {
                "dependency": "reghdfe",
                "path": "C:/ado/plus/r/reghdfe.ado",
                "version": "6.12.5",
            },
        }
    )
    structured["coefs"] = [item for item in structured["coefs"] if item["var"] in {"mpg", "_cons"}]
    scalars = structured["scalars"]
    scalars.update(
        {
            "r2": structured["r2"],
            "r2_within": structured["r2_within"],
            "df_r": 71.0,
        }
    )
    source_map = structured["stored_result_source_map"]
    source_map["command_type"] = "reghdfe"
    source_map["terms"] = [
        item for item in source_map["terms"] if item["display_key"] in {"mpg", "_cons"}
    ]
    source_map["scalars"]["r2_within"] = {
        "locator_type": "E_SCALAR",
        "name": "e(r2_within)",
    }
    source_map["macros"] = {
        "absvars": {"locator_type": "E_MACRO", "name": "e(absvars)"},
        "clustvar": {"locator_type": "E_MACRO", "name": "e(clustvar)"},
    }
    return structured


def logit_structured() -> dict[str, object]:
    structured = copy.deepcopy(regress_structured())
    structured.update(
        {
            "result_profile_capability": {
                "result_profile_id": "binary_model.logit.v1",
                "profile_version": 1,
                "snapshot_schema_version": "stata.logit.snapshot/v1",
                "extractor_contract_hash": (
                    "9ddf205980051b2ad7a02c23b21f4f9ea56c12a1422a393a64ef3f01f4573156"
                ),
            },
            "N": 74.0,
            "r2_p": 0.3965529779808924,
            "cmd": "logit",
            "cmdline": "logit foreign mpg weight, vce(robust)",
            "depvar": "foreign",
            "vce": "robust",
        }
    )
    structured["scalars"] = {
        "N": 74.0,
        "r2_p": structured["r2_p"],
    }
    source_map = structured["stored_result_source_map"]
    source_map["command_type"] = "logit"
    source_map["scalars"] = {
        "N": {"locator_type": "E_SCALAR", "name": "e(N)"},
        "r2_p": {"locator_type": "E_SCALAR", "name": "e(r2_p)"},
    }
    source_map["macros"] = {
        "vce": {"locator_type": "E_MACRO", "name": "e(vce)"},
    }
    return structured


def ivregress_structured() -> dict[str, object]:
    structured = copy.deepcopy(regress_structured())
    structured.update(
        {
            "result_profile_capability": {
                "result_profile_id": "linear_model.ivregress_2sls.v1",
                "profile_version": 1,
                "snapshot_schema_version": "stata.ivregress-2sls.snapshot/v1",
                "extractor_contract_hash": (
                    "74bd652b4211599bcb01ef55fe4cd2db1555c4d1da7eebf70a9f3820ea10fac6"
                ),
            },
            "cmd": "ivregress",
            "cmdline": ("ivregress 2sls price weight (mpg = displacement), vce(robust)"),
            "vce": "robust",
            "iv_estimator": "2sls",
            "endogenous_variables": ["mpg"],
            "included_exogenous_variables": ["weight"],
            "excluded_instruments": ["displacement"],
            "first_stage": [
                {
                    "endogenous_variable": "mpg",
                    "statistics": {
                        "partial_r2": 0.00401599,
                        "f_statistic": 0.50368579,
                        "df1": 1.0,
                        "df2": 71.0,
                        "p_value": 0.48020944,
                    },
                    "source": {
                        "locator_type": "R_MATRIX_ROW",
                        "matrix": "r(singleresults)",
                        "row_index": 1,
                        "producing_command": "estat firststage",
                    },
                }
            ],
        }
    )
    source_map = structured["stored_result_source_map"]
    source_map["command_type"] = "ivregress"
    source_map["macros"] = {
        "estimator": {"locator_type": "E_MACRO", "name": "e(estimator)"},
        "endog": {"locator_type": "E_MACRO", "name": "e(endog)"},
        "exog": {"locator_type": "E_MACRO", "name": "e(exog)"},
        "exogr": {"locator_type": "E_MACRO", "name": "e(exogr)"},
    }
    return structured


class RegressRuntime:
    def __init__(self, structured: dict[str, object], structured_status: str = "complete") -> None:
        self.structured = structured
        self.structured_status = structured_status
        self.execute_count = 0

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ) -> StataRuntimeResult:
        del artifact_outputs
        self.execute_count += 1
        return StataRuntimeResult(
            "stata-mcp.envelope/v1",
            "regression output",
            self.structured,
            StataExecutionReceipt(
                "stata.execution-receipt/v1alpha1",
                "executor-result-profile",
                session_id,
                4,
                2,
                StataExecutionStatus.SUCCEEDED,
                0,
                "complete",
                self.structured_status,
                "abc123",
                "74:12:test",
                False,
                {"stata_version": "18", "flavor": "IC", "platform": "Windows"},
                {"windows_job_object_attached": True},
            ),
            False,
        )

    async def close_session(self, *, session_id: str, reason: str):
        return StataSessionCloseResult(
            "stata.session-control/v1alpha1",
            "executor-result-profile",
            session_id,
            True,
            {"closed": True},
        )


def execute_and_qualify(
    tmp_path: Path,
    structured: dict[str, object],
    structured_status: str = "complete",
    *,
    estimator: str = "regress",
    dependent_variable: str = "price",
    command_text: str = "regress price mpg weight",
    expected_terms: tuple[str, ...] = ("mpg", "weight"),
    absorbed_effects: tuple[str, ...] = (),
    cluster_variables: tuple[str, ...] = (),
    vce: str = "ols",
    endogenous_variables: tuple[str, ...] = (),
    included_exogenous_variables: tuple[str, ...] = (),
    excluded_instruments: tuple[str, ...] = (),
):
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_result_profile")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_result_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_result_turn"), "Run baseline regression")
    )
    source = workspace_root / "auto.dta"
    source.write_bytes(b"stata-auto-fixture")
    artifact_service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(workspace_root),
        identities,
    )
    captured = artifact_service.capture_data_version(
        CaptureDataVersionCommand(
            CommandId("cmd_result_capture"),
            source,
            DataVersionKind.EXTERNAL_IMPORT,
            turn.turn_id,
        )
    )
    verified = artifact_service.verify_artifact(
        VerifyArtifactCommand(
            CommandId("cmd_result_verify"),
            captured.artifact_id,
            VerificationPurpose.FORMAL_RUN_INPUT,
        )
    )
    runtime = RegressRuntime(structured, structured_status)
    operation_service = StataOperationService(
        SqliteStataOperationRepository(connection),
        runtime,
        identities,
        FilesystemCompletionManifestStore(workspace_root),
    )
    data_load = asyncio.run(
        operation_service.execute(
            ExecuteStataCommand(
                CommandId("cmd_result_data_load"),
                turn.turn_id,
                "result-profile-session",
                f'use "{captured.managed_handle}", clear',
                30,
                input_data_version_id=captured.data_version_id,
                input_data_slot_key="analysis.primary",
                input_verification_receipt_id=verified.receipt_id,
                execution_purpose="data_load",
            )
        )
    )
    session_binding = operation_service.session_data_binding(data_load.operation_id)
    operation = asyncio.run(
        operation_service.execute(
            ExecuteStataCommand(
                CommandId("cmd_result_execute"),
                turn.turn_id,
                "result-profile-session",
                command_text,
                30,
                input_data_version_id=captured.data_version_id,
                input_data_slot_key="analysis.primary",
                input_verification_receipt_id=verified.receipt_id,
                execution_purpose="formal_estimation",
                source_data_state_operation_id=(session_binding.source_data_state_operation_id),
                expected_data_state_token=session_binding.data_state_token,
                expected_session_generation=session_binding.session_generation,
            )
        )
    )
    profile_service = RegisteredResultProfileService(
        SqliteResultProfileRepository(connection), identities
    )
    profile_service.register_builtin_profile(
        RegisterRegressProfileCommand(CommandId("cmd_result_profile_register"), turn.turn_id)
    )
    qualification_command = QualifyRegressResultCommand(
        CommandId("cmd_result_qualify"),
        operation.operation_id,
        turn.turn_id,
        initialized.main_path_id,
        dependent_variable,
        expected_terms,
        estimator=estimator,
        expected_absorbed_effects=absorbed_effects,
        expected_cluster_variables=cluster_variables,
        expected_vce=vce,
        expected_endogenous_variables=endogenous_variables,
        expected_included_exogenous_variables=included_exogenous_variables,
        expected_excluded_instruments=excluded_instruments,
    )
    outcome = profile_service.qualify(qualification_command)
    return connection, runtime, profile_service, qualification_command, outcome


def test_registered_reghdfe_profile_preserves_fixed_effect_contract(
    tmp_path: Path,
) -> None:
    connection, _, _, _, outcome = execute_and_qualify(
        tmp_path,
        reghdfe_structured(),
        estimator="reghdfe",
        command_text="reghdfe price mpg, absorb(foreign) vce(robust)",
        expected_terms=("mpg",),
        absorbed_effects=("foreign",),
        vce="robust",
    )
    try:
        assert outcome.verdict is QualificationVerdict.QUALIFIED
        assert outcome.result_id is not None
        assert outcome.element_count == 15
        assert (
            connection.execute("SELECT result_profile_id FROM result_contracts").fetchone()[0]
            == "linear_model.reghdfe.v1"
        )
        assert (
            connection.execute(
                "SELECT canonical_decimal_text FROM result_elements "
                "WHERE semantic_key = 'model.r2_within'"
            ).fetchone()[0]
            == "0.2821435687264836"
        )
        environment = connection.execute(
            "SELECT payload_json FROM environment_snapshots"
        ).fetchone()[0]
        assert '"version":"6.12.5"' in environment
    finally:
        connection.close()


def test_registered_ivregress_profile_preserves_first_stage_contract(
    tmp_path: Path,
) -> None:
    connection, _, _, _, outcome = execute_and_qualify(
        tmp_path,
        ivregress_structured(),
        estimator="ivregress",
        command_text=("ivregress 2sls price weight (mpg = displacement), vce(robust)"),
        expected_terms=("mpg", "weight"),
        vce="robust",
        endogenous_variables=("mpg",),
        included_exogenous_variables=("weight",),
        excluded_instruments=("displacement",),
    )
    try:
        assert outcome.verdict is QualificationVerdict.QUALIFIED
        assert outcome.result_id is not None
        assert outcome.element_count == 23
        assert (
            connection.execute("SELECT result_profile_id FROM result_contracts").fetchone()[0]
            == "linear_model.ivregress_2sls.v1"
        )
        diagnostics = {
            str(row[0])
            for row in connection.execute(
                "SELECT semantic_key FROM result_elements "
                "WHERE semantic_key LIKE 'iv.first_stage.%'"
            ).fetchall()
        }
        assert diagnostics == {
            "iv.first_stage.mpg.partial_r2",
            "iv.first_stage.mpg.f_statistic",
            "iv.first_stage.mpg.p_value",
        }
        locator = connection.execute(
            """
            SELECT locator.locator_json
            FROM result_elements AS element
            JOIN result_source_locators AS locator
              ON locator.result_source_locator_id = element.result_source_locator_id
            WHERE element.semantic_key = 'iv.first_stage.mpg.f_statistic'
            """
        ).fetchone()[0]
        assert '"locator_type":"R_MATRIX_CELL"' in locator
        assert '"matrix":"r(singleresults)"' in locator
        assert '"row_index":1' in locator
        assert '"column_semantic":"f_statistic"' in locator
    finally:
        connection.close()


def test_registered_logit_profile_uses_normal_derivations(tmp_path: Path) -> None:
    connection, _, _, _, outcome = execute_and_qualify(
        tmp_path,
        logit_structured(),
        estimator="logit",
        dependent_variable="foreign",
        command_text="logit foreign mpg weight, vce(robust)",
        vce="robust",
    )
    try:
        assert outcome.verdict is QualificationVerdict.QUALIFIED
        assert outcome.result_id is not None
        assert outcome.element_count == 20
        contract = connection.execute(
            """
            SELECT result_profile_id, intended_specification_json
            FROM result_contracts
            """
        ).fetchone()
        assert contract["result_profile_id"] == "binary_model.logit.v1"
        assert json.loads(str(contract["intended_specification_json"]))["vce"] == "robust"
        keys = {
            str(row[0])
            for row in connection.execute("SELECT semantic_key FROM result_elements").fetchall()
        }
        assert "model.r2_p" in keys
        assert "term.mpg.z" in keys
        assert "term.mpg.t" not in keys
        parameters = connection.execute(
            """
            SELECT parameters_json
            FROM trusted_derivation_receipts AS receipt
            JOIN result_elements AS element
              ON element.result_element_id = receipt.output_result_element_id
            WHERE element.semantic_key = 'term.mpg.p'
            """
        ).fetchone()[0]
        assert json.loads(str(parameters))["distribution"] == "normal"
    finally:
        connection.close()


def test_builtin_profile_registration_is_idempotent_across_turns(tmp_path: Path) -> None:
    connection, _, service, command, _ = execute_and_qualify(tmp_path, regress_structured())
    try:
        before_revision = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        service.register_builtin_profile(
            RegisterRegressProfileCommand(
                CommandId("cmd_register_profiles_again"), command.created_by_turn_id
            )
        )
        after_revision = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        assert after_revision == before_revision
        assert connection.execute("SELECT COUNT(*) FROM result_profiles").fetchone()[0] == 4
    finally:
        connection.close()


def test_registered_regress_profile_promotes_exact_elements_and_replays(
    tmp_path: Path,
) -> None:
    connection, runtime, service, command, outcome = execute_and_qualify(
        tmp_path, regress_structured()
    )
    try:
        assert outcome.verdict is QualificationVerdict.QUALIFIED
        assert outcome.result_id is not None
        assert outcome.element_count == 20
        replay = service.qualify(command)
        assert replay.replayed is True
        assert replay.result_id == outcome.result_id
        assert runtime.execute_count == 2
        weight = connection.execute(
            """
            SELECT canonical_binary64_bits, canonical_decimal_text
            FROM result_elements WHERE semantic_key = 'term.weight.coefficient'
            """
        ).fetchone()
        assert tuple(weight) == ("3ffbf1e80419fd36", "1.7465591583457587")
        assert (
            connection.execute("SELECT count(*) FROM result_source_locators").fetchone()[0]
            > outcome.element_count
        )
        assert (
            connection.execute("SELECT count(*) FROM trusted_derivation_receipts").fetchone()[0]
            == 15
        )
        dependency = connection.execute(
            """
            SELECT input_data_slot_key, data_version_id
            FROM result_required_data_dependencies
            """
        ).fetchone()
        assert dependency["input_data_slot_key"] == "analysis.primary"
        assert dependency["data_version_id"].startswith("data_")
    finally:
        connection.close()


def test_formal_script_may_wrap_the_stata_result_command(tmp_path: Path) -> None:
    command_text = (
        "capture log close\n"
        'log using "baseline.log", replace text\n'
        "regress price mpg weight\n"
        "estimates store baseline\n"
        "log close\n"
    )
    connection, _, _, _, outcome = execute_and_qualify(
        tmp_path,
        regress_structured(),
        command_text=command_text,
    )
    try:
        assert outcome.verdict is QualificationVerdict.QUALIFIED
        assert outcome.result_id is not None
        source = connection.execute(
            "SELECT command_text FROM executable_sources "
            "WHERE command_text = ?",
            (command_text,),
        ).fetchone()
        assert source is not None
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("mutation", "verdict", "finding"),
    [
        (
            lambda value: value.update(cmd="logit"),
            QualificationVerdict.REJECTED,
            "COMMAND_FAMILY_MISMATCH",
        ),
        (
            lambda value: value["stored_result_source_map"]["terms"][0].pop("variance"),
            QualificationVerdict.UNKNOWN,
            "SOURCE_INCOMPLETE:mpg",
        ),
        (
            lambda value: value["estimation_sample_manifest"].update(included_count=73),
            QualificationVerdict.UNKNOWN,
            "ESTIMATION_SAMPLE_COUNT_MISMATCH",
        ),
    ],
)
def test_profile_fails_closed_without_formal_result(
    tmp_path: Path, mutation, verdict: QualificationVerdict, finding: str
) -> None:
    structured = copy.deepcopy(regress_structured())
    mutation(structured)
    connection, _, _, _, outcome = execute_and_qualify(tmp_path, structured)
    try:
        assert outcome.verdict is verdict
        assert finding in outcome.findings
        assert outcome.result_id is None
        assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM result_elements").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM result_candidates").fetchone()[0] == 1
    finally:
        connection.close()


def test_parser_failure_is_unknown_even_when_preview_json_exists(tmp_path: Path) -> None:
    connection, _, _, _, outcome = execute_and_qualify(
        tmp_path,
        regress_structured(),
        structured_status="parse_failed",
    )
    try:
        assert outcome.verdict is QualificationVerdict.UNKNOWN
        assert outcome.findings == ("STRUCTURED_RESULT_INCOMPLETE",)
        assert outcome.result_id is None
    finally:
        connection.close()


def test_formal_evidence_render_has_complete_numeric_coverage_and_lineage(
    tmp_path: Path,
) -> None:
    connection, _, _, _, outcome = execute_and_qualify(tmp_path, regress_structured())
    identities = UuidIdentityGenerator()
    artifact_service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(tmp_path / "workspace"),
        identities,
    )
    evidence_service = EvidenceService(SqliteEvidenceRepository(connection), identities)
    try:
        assert outcome.result_id is not None
        context = connection.execute(
            """
            SELECT turn_id, research_path_id FROM turns
            WHERE status = 'running' ORDER BY enqueue_ordinal LIMIT 1
            """
        ).fetchone()
        dependency = connection.execute(
            """
            SELECT data_version_id, input_data_slot_key
            FROM result_required_data_dependencies WHERE result_id = ?
            """,
            (outcome.result_id.value,),
        ).fetchone()
        turn_id = TurnId(str(context["turn_id"]))
        path_id = ResearchPathId(str(context["research_path_id"]))
        artifact_service.adopt_path_data(
            AdoptPathDataCommand(
                CommandId("cmd_evidence_adopt_data"),
                path_id,
                str(dependency["input_data_slot_key"]),
                DataVersionId(str(dependency["data_version_id"])),
                0,
            )
        )
        evidence_service.adopt_path_result(
            AdoptPathResultCommand(
                CommandId("cmd_evidence_adopt_result"),
                path_id,
                "baseline.primary",
                ResultId(outcome.result_id.value),
                turn_id,
                0,
            )
        )
        elements = {
            str(row["semantic_key"]): ResultElementId(str(row["result_element_id"]))
            for row in connection.execute(
                """
                SELECT semantic_key, result_element_id FROM result_elements
                WHERE semantic_key IN (
                    'term.weight.coefficient', 'term.weight.se'
                )
                """
            )
        }
        command = RenderFormalResultBlockCommand(
            CommandId("cmd_evidence_render"),
            turn_id,
            path_id,
            "baseline.primary",
            "weight 系数为 {coef}，标准误为 {se}。",
            (
                EvidenceSlot("coef", elements["term.weight.coefficient"], 3),
                EvidenceSlot("se", elements["term.weight.se"], 3),
            ),
        )
        rendered = evidence_service.render_formal_block(command)
        assert rendered.content == "weight 系数为 1.747，标准误为 0.641。"
        assert len(set(rendered.evidence_record_ids)) == 2
        manifest = connection.execute(
            """
            SELECT coverage_status, numeric_occurrence_count, bound_occurrence_count
            FROM numeric_coverage_manifests
            WHERE numeric_coverage_manifest_id = ?
            """,
            (rendered.coverage_manifest_id.value,),
        ).fetchone()
        assert tuple(manifest) == ("complete", 2, 2)
        occurrences = connection.execute(
            """
            SELECT byte_start, numeric_lexeme FROM numeric_occurrences
            WHERE formal_result_block_id = ? ORDER BY byte_start
            """,
            (rendered.formal_result_block_id.value,),
        ).fetchall()
        assert [row["numeric_lexeme"] for row in occurrences] == ["1.747", "0.641"]
        lineage = evidence_service.lineage(
            rendered.formal_result_block_id, int(occurrences[0]["byte_start"])
        )
        assert lineage.result_element_id == elements["term.weight.coefficient"]
        assert lineage.numeric_lexeme == "1.747"
        assert lineage.command_text == "regress price mpg weight"
        assert lineage.data_version_id == str(dependency["data_version_id"])
        assert '"matrix":"e(b)"' in lineage.locator_json

        replay = evidence_service.render_formal_block(command)
        assert replay.replayed is True
        assert replay.formal_result_block_id == rendered.formal_result_block_id

        alternate = evidence_service.render_formal_block(
            RenderFormalResultBlockCommand(
                CommandId("cmd_evidence_render_other_precision"),
                turn_id,
                path_id,
                "baseline.primary",
                "估计值为 {coef}。",
                (EvidenceSlot("coef", elements["term.weight.coefficient"], 2),),
            )
        )
        assert alternate.content == "估计值为 1.75。"
        assert alternate.evidence_record_ids[0] == rendered.evidence_record_ids[0]
        assert (
            connection.execute("SELECT count(*) FROM evidence_render_receipts").fetchone()[0] == 3
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM evidence_issuance_receipts WHERE reused_existing_record = 1"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_unbound_numeric_literal_cannot_commit_formal_block(tmp_path: Path) -> None:
    connection, _, _, _, outcome = execute_and_qualify(tmp_path, regress_structured())
    identities = UuidIdentityGenerator()
    evidence_service = EvidenceService(SqliteEvidenceRepository(connection), identities)
    artifact_service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(tmp_path / "workspace"),
        identities,
    )
    try:
        assert outcome.result_id is not None
        context = connection.execute(
            "SELECT turn_id, research_path_id FROM turns WHERE status = 'running'"
        ).fetchone()
        dependency = connection.execute(
            "SELECT data_version_id, input_data_slot_key FROM result_required_data_dependencies"
        ).fetchone()
        turn_id = TurnId(str(context["turn_id"]))
        path_id = ResearchPathId(str(context["research_path_id"]))
        artifact_service.adopt_path_data(
            AdoptPathDataCommand(
                CommandId("cmd_bad_coverage_adopt_data"),
                path_id,
                str(dependency["input_data_slot_key"]),
                DataVersionId(str(dependency["data_version_id"])),
                0,
            )
        )
        evidence_service.adopt_path_result(
            AdoptPathResultCommand(
                CommandId("cmd_bad_coverage_adopt_result"),
                path_id,
                "baseline.primary",
                ResultId(outcome.result_id.value),
                turn_id,
                0,
            )
        )
        element_id = ResultElementId(
            str(
                connection.execute(
                    """
                    SELECT result_element_id FROM result_elements
                    WHERE semantic_key = 'term.weight.coefficient'
                    """
                ).fetchone()[0]
            )
        )
        with pytest.raises(NumericCoverageError):
            evidence_service.render_formal_block(
                RenderFormalResultBlockCommand(
                    CommandId("cmd_bad_coverage_render"),
                    turn_id,
                    path_id,
                    "baseline.primary",
                    "表 1 的估计值为 {coef}。",
                    (EvidenceSlot("coef", element_id, 3),),
                )
            )
        assert connection.execute("SELECT count(*) FROM formal_result_blocks").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM evidence_presentation_uses").fetchone()[0] == 0
        )
    finally:
        connection.close()
