"""ToolEnforcer 健壮性 + Toolkit 结果契约单测（防御异常输入不崩）。"""

from __future__ import annotations

import tempfile
import pathlib

import pytest

from stata_agent.harness.tool_enforcer import (
    ToolEnforcer,
    normalize_arguments,
)
from stata_agent.toolkit import Tool, ToolContext, default_tools, err, ok


def _ctx(root, **kw):
    base = dict(run_root=root, allowed_roots=[root], privacy_mode="local_strict")
    base.update(kw)
    return ToolContext(**base)


def _enforce(name, args, root):
    return ToolEnforcer(default_tools()).execute(name, args, _ctx(root))


# ---- normalize_arguments 健壮性
@pytest.mark.parametrize("bad", [None, "not-dict", 5, ["list"], b"bytes"])
def test_normalize_arguments_non_dict(bad):
    args, err_ = normalize_arguments(bad)
    assert err_ is not None


def test_normalize_arguments_string():
    # 字符串先当 JSON 解析：合法 JSON → dict；非 JSON → err（fail-closed）
    args, err_ = normalize_arguments('{"code":"reg y x"}')
    assert err_ is None and args == {"code": "reg y x"}
    _, err2 = normalize_arguments("reg y x")  # 非 JSON → 拒
    assert err2 is not None


# ---- 非法工具输入
def test_empty_arguments_dict(tmp_path):
    args, err_ = normalize_arguments({})
    assert err_ is None and args == {}


def test_unknown_permission_registry_fails_closed(tmp_path):
    root = pathlib.Path(tmp_path)
    bogus = Tool(
        name="boom", description="x", input_schema={"type": "object", "properties": {}},
        handler=lambda a, c: ok(), permission="not-a-perm")
    r = ToolEnforcer({"boom": bogus}).execute("boom", {}, _ctx(root))
    assert r["ok"] is False


# ---- 结果契约 ok/err
def test_ok_err_shapes():
    o = ok({"a": 1})
    assert o == {"ok": True, "data": {"a": 1}}
    e = err("bad", type="stata_error", retryable=True, suggestion="重试")
    assert e["ok"] is False
    assert e["error"]["type"] == "stata_error"
    assert e["error"]["retryable"] is True


def test_non_dict_handler_result_is_error(tmp_path):
    root = pathlib.Path(tmp_path)
    bad = Tool(name="b", description="x",
               input_schema={"type": "object", "properties": {}},
               handler=lambda a, c: "not a dict")
    r = ToolEnforcer({"b": bad}).execute("b", {}, _ctx(root))
    assert r["ok"] is False


def test_handler_raise_becomes_tool_error(tmp_path):
    root = pathlib.Path(tmp_path)
    boom = Tool(name="boom", description="x",
                input_schema={"type": "object", "properties": {}},
                handler=lambda a, c: (_ for _ in ()).throw(RuntimeError("kaboom")))
    r = ToolEnforcer({"boom": boom}).execute("boom", {}, _ctx(root))
    assert r["ok"] is False and "kaboom" in str(r["error"])


def test_result_to_context_truncation():
    big = ok({"data": "x" * 50000})
    t = Tool(name="t", description="x", input_schema={"type": "object", "properties": {}},
             handler=lambda a, c: ok(), max_result_chars=200)
    text = t.result_to_context(big)
    assert len(text) <= 200 + 4
