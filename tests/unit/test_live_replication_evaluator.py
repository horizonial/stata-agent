from tools.evaluate_live_replication import _observed_oracle


def test_powerful_experiments_oracle_uses_explicit_result_slots() -> None:
    values = {
        ("employee_summary", "scalar.mean"): ("result_employee", 72.7091836735),
        ("export_summary", "scalar.mean"): ("result_export", 338.3285926342),
        ("winsorized_export_summary", "scalar.mean"): ("result_winsorized", 220.614),
    }

    assert _observed_oracle(
        values, "powerful-experiments-wp-2025", "EMPLOYEE_MEAN"
    ) == ("result_employee", 72.7091836735)
    assert _observed_oracle(
        values, "powerful-experiments-wp-2025", "EXPORT_MEAN"
    ) == ("result_export", 338.3285926342)
    assert _observed_oracle(
        values, "powerful-experiments-wp-2025", "WINSORIZED_EXPORT_MEAN"
    ) == ("result_winsorized", 220.614)
