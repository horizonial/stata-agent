"""ModelCapabilityProfile（SPEC §4.2）：按能力编程，不按模型名编程。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ModelCapabilityProfile(BaseModel):
    provider: str
    model: str
    version: Optional[str] = None

    # 能力
    supports_tools: bool = True
    strict_schema: bool = False
    thinking_with_tools: bool = False
    streaming: bool = True
    json_mode: bool = False  # 仅 json_object（非 strict schema）

    # 资源
    max_context: int = 64000
    max_output: int = 8192
    json_schema_subset: list[str] = Field(default_factory=list)

    known_quirks: list[str] = Field(default_factory=list)
    tested_at: Optional[str] = None
    contract_test_version: int = 1

    def meets(self, requires: dict[str, bool]) -> bool:
        """requires 里键值为 True 的能力必须满足（当前只查布尔能力）。"""
        for key, want in requires.items():
            if not want:
                continue
            attr = {
                "tools": "supports_tools",
                "strict_schema": "strict_schema",
                "thinking": "thinking_with_tools",
                "streaming": "streaming",
                "json_mode": "json_mode",
            }.get(key)
            if attr is not None and not getattr(self, attr):
                return False
        return True


def deepseek_chat_profile() -> ModelCapabilityProfile:
    """deepseek-chat 起步画像（2026 初）；实测后再填 tested_at/quirks。"""
    return ModelCapabilityProfile(
        provider="deepseek",
        model="deepseek-chat",
        supports_tools=True,
        strict_schema=False,     # 官方提示工具参数可能幻觉；strict 仅子集
        thinking_with_tools=False,
        streaming=True,
        json_mode=True,          # 支持 json_object（prompt 需含 'json'）
        max_context=64000,
        max_output=8192,
        known_quirks=[
            "json_object 需 prompt 含 'json' 字样",
            "无 strict JSON-schema；结果要在应用侧校验+重试",
        ],
        contract_test_version=1,
    )


def qwen_dashscope_profile() -> ModelCapabilityProfile:
    """通义/百炼 起步画像（阿里云 DashScope OpenAI 兼容）。"""
    return ModelCapabilityProfile(
        provider="qwen",
        model="qwen-plus",
        supports_tools=True,
        strict_schema=False,
        thinking_with_tools=False,
        streaming=True,
        json_mode=True,
        max_context=32000,
        max_output=8192,
        known_quirks=["json_object 需 prompt 含 'json'；OpenAI 兼容 v1"],
        contract_test_version=1,
    )


def to_file(profile: ModelCapabilityProfile, path: str | Path) -> None:
    Path(path).write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def from_file(path: str | Path) -> ModelCapabilityProfile:
    return ModelCapabilityProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))
