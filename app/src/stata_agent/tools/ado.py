"""真 ado 预检：skill.requires.ados → 哪些在 Stata 里装了（DD-04/SPEC §4.6 D2）。

同一持久会话里逐条 `cap which <ado>`，随后用 `di _rc` 输出 0/非0 判定安装。
"""

from __future__ import annotations

import re

from .stata_client import StataSession


def which_ados(ados: list[str]) -> dict[str, bool]:
    """返回 {ado: 是否安装}。真跑 Stata（只读，不污染）。"""
    if not ados:
        return {}
    codes: list[str] = []
    for ado in ados:
        codes.append(f"cap which {ado}")
        codes.append("di \"ADOOK_rc=\" _rc")
    sess = StataSession()
    try:
        results = sess.run_batch(codes)
    finally:
        sess.close()
    text = "\n".join(r.text for r in results)
    rc_vals = [int(m) for m in re.findall(r"ADOOK_rc=(\d+)", text)]
    return {ado: (rc == 0) for ado, rc in zip(ados, rc_vals)}


def missing_ados(ados: list[str]) -> list[str]:
    return [a for a, ok in which_ados(ados).items() if not ok]
