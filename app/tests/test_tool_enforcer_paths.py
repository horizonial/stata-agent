"""ToolEnforcer 路径/权限/URL 专项单测（含 ../ 相对穿越回归）。"""

from __future__ import annotations

import tempfile
import pathlib

import pytest

from stata_agent.harness.tool_enforcer import ToolEnforcer, validate_path, validate_url
from stata_agent.toolkit import ToolContext, default_tools


@pytest.fixture
def root():
    return pathlib.Path(tempfile.mkdtemp())


@pytest.fixture
def ws_ctx(root):
    return ToolContext(run_root=root, allowed_roots=[root], privacy_mode="local_strict")


def _enforce(name, args, ctx):
    return ToolEnforcer(default_tools()).execute(name, args, ctx)


def test_abs_path_escape_rejected(ws_ctx):
    evil = str(ws_ctx.run_root.parent / "secret.txt")
    r = _enforce("read_artifact", {"path": evil}, ws_ctx)
    assert r["ok"] is False
    assert "工作区根目录" in r["error"]["message"]


def test_relative_dotdot_in_arg_rejected(root):
    # 相对路径经 resolve 后 containment 应拦下（参数层）
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    r = _enforce("write_artifact", {"path": "../evil.txt", "content": "x"}, ctx)
    assert r["ok"] is False


def test_dotdot_traversal_in_stata_code_rejected(root):
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    # 回归：此前 `../` 相对穿越在 code 里放行
    r = _enforce("run_stata", {"code": 'insheet using "../..//etc/passwd"'}, ctx)
    assert r["ok"] is False
    assert "穿越" in r["error"]["message"]


def test_dotdot_in_comment_allowed(root):
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    # 注释里的 `../` 不应误伤
    r = _enforce("run_stata", {"code": "* see ../docs for rationale\nreg y x"}, ctx)
    # 无 executor -> 未知，但不应是"穿越被拒"
    assert "穿越" not in str(r.get("error", {}).get("message", ""))


def test_abs_path_inside_code_rejected(root):
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    r = _enforce("run_stata", {"code": 'use "C:/Windows/win.ini"'}, ctx)
    assert r["ok"] is False


def test_schema_missing_and_extra_rejected(root):
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    assert _enforce("run_stata", {}, ctx)["ok"] is False
    assert _enforce("run_stata", {"code": "reg y x", "hack": 1}, ctx)["ok"] is False


def test_network_private_and_local_strict_rejected(root):
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="local_strict")
    assert _enforce("fetch_source", {"url": "http://example.com"}, ctx)["ok"] is False  # local_strict
    ctx2 = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote", network_available=True)
    r = _enforce("fetch_source", {"url": "http://169.254.169.254/latest"}, ctx2)
    assert r["ok"] is False and "私" in r["error"]["message"] or "拒绝" in r["error"]["message"]
    assert validate_url("http://10.0.0.1/x") is not None
    assert validate_url("http://example.com/x") is None


def test_permission_phase_gate(root):
    # execute 工具在非估计阶段应拒（phase 明确时）
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote",
                      executor=None, phase="IDEA")
    # executor=None 所以 run_stata enabled=false → 未暴露；validate 层面仍应因 phase 拒
    r = _enforce("run_stata", {"code": "reg y x"}, ctx)
    # enabled 过滤器在 loop，enforcer.validate 独立校验 phase
    assert r["ok"] is False


def test_normal_abs_path_inside_root_allowed(root):
    (root / "ok.txt").write_text("hi", encoding="utf-8")
    ctx = ToolContext(run_root=root, allowed_roots=[root], privacy_mode="approved_remote")
    r = _enforce("read_artifact", {"path": str(root / "ok.txt")}, ctx)
    assert r["ok"] is True and r["data"]["content"] == "hi"
