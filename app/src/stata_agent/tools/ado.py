"""真 ado 预检：skill.requires.ados → 哪些在 Stata 里装了（DD-04/SPEC §4.6 D2）。

同一持久会话里逐条 `cap which <ado>`，随后用 `di _rc` 输出 0/非0 判定安装。
"""

from __future__ import annotations

import re

from .stata_client import StataSession


class AdoProbeError(RuntimeError):
    """Base error for an ado probe that cannot be trusted."""

    code = "ado_probe_failed"


class AdoProbeUnavailable(AdoProbeError):
    """The Stata/MCP runtime did not provide a usable probe response."""

    code = "runtime_unavailable"


class AdoProbeProtocolError(AdoProbeError):
    """The probe response was incomplete or malformed."""

    code = "protocol_invalid"


_RUNTIME_FAILURE_TOKENS = (
    "transport",
    "timeout",
    "timed out",
    "disconnect",
    "broken pipe",
    "eof",
    "session failed",
    "license",
    "pystata",
    "stata engine",
    "process exited",
    "引擎",
    "许可证",
    "断开",
    "超时",
)
_MISSING_ADO_TOKENS = (
    "not found",
    "not exist",
    "no ado",
    "找不到",
    "不存在",
)


def which_ados(ados: list[str]) -> dict[str, bool]:
    """返回 {ado: 是否安装}。真跑 Stata（只读，不污染）。"""
    if not ados:
        return {}
    if len(set(ados)) != len(ados):
        raise AdoProbeProtocolError("ado names must be unique")
    codes: list[str] = []
    for ado in ados:
        # The stata-mcp wrapper captures _rc per call, so cap which followed
        # by a separate display resets a missing command's rc to zero.
        codes.append(f"which {ado}")
        codes.append("di \"ADOOK_rc=\" _rc")
    try:
        sess = StataSession()
    except Exception as error:  # noqa: BLE001 - preserve a stable capability failure
        raise AdoProbeUnavailable(str(error)) from error
    try:
        try:
            results = sess.run_batch(codes)
        except Exception as error:  # noqa: BLE001 - preserve a stable capability failure
            raise AdoProbeUnavailable(str(error)) from error
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask probe outcome
            pass
    if results is None:
        raise AdoProbeProtocolError("ado probe returned no result sequence")
    result_list = list(results)
    if not result_list:
        raise AdoProbeProtocolError("ado probe returned no results")
    if len(result_list) != len(codes):
        raise AdoProbeProtocolError(
            f"ado probe result count mismatch: expected {len(codes)}, got {len(result_list)}"
        )
    texts: list[str] = []
    statuses: list[bool] = []
    for ado, which_result, marker_result in zip(
        ados, result_list[0::2], result_list[1::2], strict=True
    ):
        if which_result is None or marker_result is None:
            raise AdoProbeProtocolError("ado probe returned an empty result")
        which_text = str(getattr(which_result, "text", "") or "")
        marker_text = str(getattr(marker_result, "text", "") or "")
        texts.extend((which_text, marker_text))
        if bool(getattr(marker_result, "is_error", False)):
            raise AdoProbeUnavailable(marker_text or "MCP marker call failed")
        if "ADOOK_rc=" not in marker_text:
            raise AdoProbeProtocolError(f"missing ADOOK_rc marker for {ado}")
        lowered_which = which_text.lower()
        if any(token in lowered_which for token in _RUNTIME_FAILURE_TOKENS):
            raise AdoProbeUnavailable(which_text)
        if any(token in lowered_which for token in _MISSING_ADO_TOKENS):
            if ado.lower() not in lowered_which:
                raise AdoProbeProtocolError(f"which response does not identify {ado}")
            statuses.append(False)
            continue
        if bool(getattr(which_result, "is_error", False)):
            raise AdoProbeUnavailable(which_text or "MCP which call failed")
        if not which_text.strip():
            raise AdoProbeProtocolError(f"empty which response for {ado}")
        if ado.lower() not in lowered_which:
            raise AdoProbeProtocolError(f"which response does not identify {ado}")
        statuses.append(True)
    text = "\n".join(texts)
    lowered = text.lower()
    if any(token in lowered for token in _RUNTIME_FAILURE_TOKENS):
        raise AdoProbeUnavailable(text)
    rc_tokens = re.findall(r"ADOOK_rc=([+-]?\d+)", text)
    if len(rc_tokens) != len(ados):
        raise AdoProbeProtocolError(
            f"ado probe marker count mismatch: expected {len(ados)}, got {len(rc_tokens)}"
        )
    try:
        rc_vals = [int(token) for token in rc_tokens]
    except ValueError as error:  # pragma: no cover - regex already restricts digits
        raise AdoProbeProtocolError("ado probe marker is not an integer") from error
    return {
        ado: status and rc == 0
        for ado, status, rc in zip(ados, statuses, rc_vals, strict=True)
    }


def missing_ados(ados: list[str]) -> list[str]:
    return [a for a, ok in which_ados(ados).items() if not ok]


__all__ = [
    "AdoProbeError",
    "AdoProbeProtocolError",
    "AdoProbeUnavailable",
    "missing_ados",
    "which_ados",
]
