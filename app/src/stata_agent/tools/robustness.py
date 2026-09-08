"""Robustness Checker：稳健性变体与主结果的一致性（符号/显著性不翻车）。"""

from __future__ import annotations

import math


def stars(coef: float, se: float | None) -> str:
    if not se:
        return ""
    t = abs(coef) / se
    return "***" if t > 2.576 else ("**" if t > 1.96 else ("*" if t > 1.645 else ""))


def check_robustness(results: dict[str, dict], *, main_id: str | None = None) -> dict:
    ok_items = {k: v for k, v in results.items() if v.get("ok") and v.get("machine")}
    if not ok_items:
        return {"main_id": None, "stable": False, "rows": [], "note": "无可比较的结果"}
    main_id = main_id if main_id in ok_items else next(iter(ok_items))
    main_m = ok_items[main_id]["machine"]
    main_coef, main_se = main_m.get("coef"), main_m.get("se")
    rows = []
    for vid, v in ok_items.items():
        m = v["machine"]
        sign_same = main_coef is not None and m.get("coef") is not None and (
            (main_coef >= 0) == (m["coef"] >= 0))
        sig_main = main_se and abs(main_coef) / main_se > 1.96
        sig_this = m.get("se") and abs(m["coef"]) / m["se"] > 1.96
        stable = (vid == main_id) or (sign_same and bool(sig_main) == bool(sig_this))
        rows.append({"variant": vid, "label": v.get("label", vid), "coef": m.get("coef"),
                     "se": m.get("se"), "stars": stars(m.get("coef"), m.get("se")),
                     "sign_same": sign_same, "sig_same": bool(sig_main) == bool(sig_this),
                     "stable": stable})
    stable_all = all(r["stable"] for r in rows)
    return {"main_id": main_id, "stable": stable_all, "rows": rows, "note": ""}
