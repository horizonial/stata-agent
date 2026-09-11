"""DeepSeek provider（OpenAI 兼容 REST，urllib 零额外依赖）。

读取配置：DEEPSEEK_API_KEY（必须）、DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）。
实现 ProposalProvider.propose：把上下文 → ActionProposal（经 codec + 重试）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..domain.action import ActionProposal
from .capabilities import ModelCapabilityProfile, deepseek_chat_profile
from .llm import chat_proposal
from .protocol import ProviderError, provider_error_from_exception


class MissingApiKey(ProviderError):
    """Provider configuration is incomplete without exposing the key detail."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__("credentials_missing", provider="provider", detail=message)


class StreamProtocolError(ProviderError):
    """The provider closed a stream without a complete terminal response."""

    def __init__(self, message: str | None = None, *, provider: str = "", attempt: int = 0,
                 request_id: str | None = None):
        del message  # preserve the old positional constructor without leaking it
        super().__init__(
            "protocol",
            provider=provider,
            attempt=attempt,
            request_id=request_id,
        )


class DeepSeekProvider:
    provider = "deepseek"
    is_remote = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        profile: ModelCapabilityProfile | None = None,
        timeout: int = 90,
        *,
        key_env: str = "DEEPSEEK_API_KEY",
        default_base: str = "https://api.deepseek.com",
        base_env: str = "DEEPSEEK_BASE_URL",
    ):
        self._key = api_key or os.environ.get(key_env)
        if not self._key:
            raise MissingApiKey(f"缺少 {key_env}（env 或参数）")
        self._base = (base_url or os.environ.get(base_env) or default_base).rstrip("/")
        self.profile = profile or deepseek_chat_profile()
        self._timeout = timeout

    def _post(self, body: dict) -> dict:
        try:
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
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        except Exception as error:  # noqa: BLE001 - hide SDK/transport detail
            raise provider_error_from_exception(error, provider=self.provider) from None
        if not isinstance(parsed, dict):
            raise ProviderError("invalid_response", provider=self.provider)
        return parsed

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
        try:
            choices = payload["choices"]
            msg = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        if not isinstance(msg, dict):
            raise ProviderError("invalid_response", provider=self.provider)
        content = msg.get("content") or None
        if content is not None and not isinstance(content, str):
            raise ProviderError("invalid_response", provider=self.provider)
        raw_calls = msg.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ProviderError("protocol", provider=self.provider)
        calls = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                raise ProviderError("protocol", provider=self.provider)
            fn = tc.get("function") or {}
            if not isinstance(fn, dict) or not fn.get("name") or not tc.get("id"):
                raise ProviderError("protocol", provider=self.provider)
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError) as error:
                raise provider_error_from_exception(error, provider=self.provider) from None
            if not isinstance(args, dict):
                raise ProviderError("protocol", provider=self.provider)
            calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
        return {"content": content, "tool_calls": calls or None}

    def _json_chat(self, messages: list[dict]) -> str:
        """Adapt the tool-oriented ``chat`` dict to ``chat_proposal``'s text API."""

        response = self.chat(messages, json_mode=True)
        if isinstance(response, str):
            return response
        if not isinstance(response, dict):
            from .codec import StructuredOutputError

            raise StructuredOutputError("provider chat response must be an object")
        content = response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False)
        # Some OpenAI-compatible adapters expose the structured object under
        # ``json`` rather than ``content``; support it without accepting an
        # arbitrary response as a successful proposal.
        structured = response.get("json")
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False)
        from .codec import StructuredOutputError

        raise StructuredOutputError("provider response has no structured content")

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
        content_parts: list[str] = []
        calls_by_idx: dict[int, dict] = {}
        saw_done_marker = False
        finish_reason: str | None = None
        try:
            req = urllib.request.Request(
                f"{self._base}/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        saw_done_marker = True
                        break
                    try:
                        obj = json.loads(data)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise provider_error_from_exception(error, provider=self.provider) from None
                    if not isinstance(obj, dict):
                        raise StreamProtocolError(provider=self.provider)
                    choices = obj.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        raise StreamProtocolError(provider=self.provider)
                    choice = choices[0] or {}
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice.get("finish_reason"))
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise StreamProtocolError(provider=self.provider)
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        yield {"type": "text_delta", "text": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            raise StreamProtocolError(provider=self.provider)
                        idx = tc.get("index", 0)
                        entry = calls_by_idx.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if not isinstance(fn, dict):
                            raise StreamProtocolError(provider=self.provider)
                        entry["name"] += fn.get("name") or ""
                        entry["args"] += fn.get("arguments") or ""
        except urllib.error.HTTPError as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise provider_error_from_exception(error, provider=self.provider) from None
        except Exception as error:  # noqa: BLE001 - hide SDK/transport detail
            raise provider_error_from_exception(error, provider=self.provider) from None
        if not saw_done_marker:
            raise StreamProtocolError(provider=self.provider)
        if finish_reason not in {"stop", "tool_calls"}:
            raise StreamProtocolError(provider=self.provider)
        content = "".join(content_parts) or None
        calls = []
        for idx in sorted(calls_by_idx):
            e = calls_by_idx[idx]
            try:
                args = json.loads(e["args"] or "{}")
            except (TypeError, json.JSONDecodeError):
                # Preserve the public stream-specific exception while keeping
                # its message transport-safe.  The router still classifies
                # this as a non-transient protocol failure.
                raise StreamProtocolError(provider=self.provider) from None
            if not isinstance(args, dict) or not e.get("id") or not e.get("name"):
                raise StreamProtocolError(provider=self.provider)
            calls.append({"id": e["id"], "name": e["name"], "arguments": args})
        yield {"type": "done", "content": content, "tool_calls": calls or None}


class QwenProvider(DeepSeekProvider):
    """通义/百炼（DashScope，OpenAI 兼容 v1）。deepseek 缺 key 时的降级证明。"""

    provider = "qwen"

    def __init__(self, api_key: str | None = None, profile: ModelCapabilityProfile | None = None,
                 timeout: int = 90, base_url: str | None = None):
        from .capabilities import qwen_dashscope_profile

        super().__init__(
            api_key=api_key,
            profile=profile or qwen_dashscope_profile(),
            timeout=timeout,
            key_env="DASHSCOPE_API_KEY",
            default_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            base_url=base_url,
            base_env="DASHSCOPE_BASE_URL",
        )
