"""M0 applicable-set, traceability, and locked-input checks."""

import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def load_register() -> dict:
    # JSON is a strict subset of YAML; stdlib parsing keeps the build dependency-free.
    return json.loads((PROJECT_ROOT / "verification" / "register.yaml").read_text(encoding="utf-8"))


def test_register_covers_every_vr_exactly_once() -> None:
    register = load_register()
    ids = [item["id"] for item in register["requirements"]]
    assert ids == [f"VR-{number:03d}" for number in range(1, 29)]
    assert len(ids) == len(set(ids))


def test_m0_applicable_set_is_machine_computed_and_has_no_open_result() -> None:
    register = load_register()
    computed = [item["id"] for item in register["requirements"] if item["m0_applicable"]]
    assert computed == register["applicable_set"]
    for item in register["requirements"]:
        if item["m0_applicable"]:
            assert item["status"] == "PASSED"
            assert item["test_selectors"]
        else:
            assert item["status"] == "NOT_APPLICABLE_M0"


def test_every_declared_test_selector_resolves_to_a_file() -> None:
    for item in load_register()["requirements"]:
        for selector in item["test_selectors"]:
            path_text = selector.split("::", 1)[0]
            if path_text.startswith("tools/"):
                assert (PROJECT_ROOT / path_text).is_file()
            else:
                assert (PROJECT_ROOT / path_text).is_file()


def test_m1_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M1"]
    assert milestone["status"] == "PASSED"
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        assert requirement["test_selectors"]
        for selector in requirement["test_selectors"]:
            path_text = selector.split("::", 1)[0]
            assert (PROJECT_ROOT / path_text).is_file()


def test_m3_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M3"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == [
        "VR-003",
        "VR-004",
        "VR-005",
        "VR-013",
        "VR-015",
        "VR-016",
        "VR-017",
        "VR-028",
    ]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        assert requirement["test_selectors"]
        for selector in requirement["test_selectors"]:
            path_text = selector.split("::", 1)[0]
            assert (PROJECT_ROOT / path_text).is_file()


def test_m4_01_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4-01"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-018", "VR-020"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m4_02_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4-02"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-015", "VR-018", "VR-020"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m4_03_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4-03"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-019", "VR-020"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m4_04_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4-04"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-002", "VR-008", "VR-020"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m4_05_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4-05"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-002", "VR-012"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m4_exit_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M4"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-002", "VR-018", "VR-019", "VR-020"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m5_01a_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M5-01a"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-021"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_m5_01b_applicable_set_has_executable_passed_evidence() -> None:
    milestone = load_register()["milestone_results"]["M5-01b"]
    assert milestone["status"] == "PASSED"
    assert milestone["applicable_set"] == ["VR-021"]
    requirements = milestone["requirements"]
    assert list(requirements) == milestone["applicable_set"]
    for requirement in requirements.values():
        assert requirement["status"] == "PASSED"
        for selector in requirement["test_selectors"]:
            assert (PROJECT_ROOT / selector.split("::", 1)[0]).is_file()


def test_dependency_inputs_are_exactly_locked() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        *project["build-system"]["requires"],
        *project["project"]["dependencies"],
        *project["dependency-groups"]["dev"],
    ]
    assert all("==" in requirement for requirement in requirements)
    assert (PROJECT_ROOT / "uv.lock").is_file()

    package = json.loads((PROJECT_ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert all(
        not version.startswith(("^", "~", ">", "<", "*"))
        for version in package["devDependencies"].values()
    )
    package_lock = json.loads(
        (PROJECT_ROOT / "web" / "package-lock.json").read_text(encoding="utf-8")
    )
    assert package_lock["lockfileVersion"] >= 3


def test_versioned_founder_scenarios_are_checked_in_and_strictly_loadable() -> None:
    from stata_research_agent.interfaces.founder_scenario import FounderScenarioLoader

    scenarios = sorted((PROJECT_ROOT / "verification" / "scenarios").glob("*.json"))
    assert scenarios
    loaded = [FounderScenarioLoader().load(path) for path in scenarios]
    identities = [(scenario.scenario_id, scenario.version) for scenario in loaded]
    assert len(identities) == len(set(identities))
