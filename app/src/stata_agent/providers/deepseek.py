"""DeepSeek provider（OpenAI 兼容 REST，urllib 零额外依赖）。

读取配置：DEEPSEEK_API_KEY（必须）、DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）。
实现 ProposalProvider.propose：把上下文 → ActionProposal（经 codec + 重试）。
"""

from __future__ import annotations

import json
import os
import urllib.request

from ..domain.action import ActionProposal
from .capabilities import ModelCapabilityProfile, deepseek_chat_profile
from .codec import proposal_prompt
from .llm import chat_proposal


class MissingApiKey(RuntimeError):
    pass


class DeepSeekProvider:
    provider = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        profile: ModelCapabilityProfile | None = None,
        timeout: int = 90,
        *,
        key_env: str = "DEEPSEEK_API_KEY",
        default_base: str = "https://api.deepseek.com",
    ):
        self._key = api_key or os.environ.get(key_env)
        if not self._key:
            raise MissingApiKey(f"缺少 {key_env}（env 或参数）")
        self._base = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or default_base).rstrip("/")
        self.profile = profile or deepseek_chat_profile()
        self._timeout = timeout

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat(self, messages: list[dict], *, json_mode: bool = False, tools: list[dict] | None = None) -> dict:
        """OpenAI 兼容 /chat/completions。

        返回 message dict：
          - 纯文本：{"content": str, "tool_calls": None}
          - 工具调用：{"content": None, "tool_calls": [{id,name,arguments(dict)}]}
        """
        body: dict = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"} if (json_mode and self.profile.json_mode) else None,
        }
        if tools:
            body["tools"] = tools
        body = {k: v for k, v in body.items() if v is not None}
        payload = self._post(body)
        msg = payload["choices"][0]["message"]
        content = msg.get("content") or None
        raw_calls = msg.get("tool_calls") or []
        calls = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
        return {"content": content, "tool_calls": calls or None}

    def _json_chat(self, messages: list[dict]) -> str:
        return self.chat(messages, json_mode=True)

    def propose(self, context: str) -> ActionProposal:
        # 直接复用 chat_proposal 的结构化收敛 + schema 重试（此路径开 json）
        return chat_proposal(self._json_chat, context)

    def stream_chat(self, messages: list[dict], *, tools: list[dict] | None = None):
        """流式 chat（OpenAI 兼容 stream=True）。生成器：

        - yield {"type":"text_delta","text":...} 逐 token
        - 最后 yield {"type":"done","content":str|None,"tool_calls":[...]|None}

        工具调用参数也是流式分块的，这里做拼接，最后一次给完整 arguments。
        """
        body: dict = {"model": self.profile.model, "messages": messages,
                      "temperature": 0, "stream": True}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
            method="POST",
        )
        content_parts: list[str] = []
        calls_by_idx: dict[int, dict] = {}
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    yield {"type": "text_delta", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = calls_by_idx.setdefault(idx, {"id": None, "name": "", "args": ""})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    entry["name"] += fn.get("name") or ""
                    entry["args"] += fn.get("arguments") or ""
        content = "".join(content_parts) or None
        calls = []
        for idx in sorted(calls_by_idx):
            e = calls_by_idx[idx]
            try:
                args = json.loads(e["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": e["id"], "name": e["name"], "arguments": args})
        yield {"type": "done", "content": content, "tool_calls": calls or None}


class QwenProvider(DeepSeekProvider):
    """通义/百炼（DashScope，OpenAI 兼容 v1）。deepseek 缺 key 时的降级证明。"""

    provider = "qwen"

    def __init__(self, api_key: str | None = None, profile: ModelCapabilityProfile | None = None,
                 timeout: int = 90):
        from .capabilities import qwen_dashscope_profile

        super().__init__(
            api_key=api_key,
            profile=profile or qwen_dashscope_profile(),
            timeout=timeout,
            key_env="DASHSCOPE_API_KEY",
            default_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
