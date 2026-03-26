"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

import os
import json
import requests
import base64
import httpx
from typing import Optional, Any, Awaitable, Callable
from openai import AsyncOpenAI
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

class CustomProvider(LLMProvider):
    def __init__(self, api_key: str = "no-key", api_base: str = "https://console.his.huawei.com/agi/agi_agent/infer/sandbox/v1", default_model: str = "default"):
        os.environ["NO_PROXY"] = "1"
        super().__init__(api_key, api_base)
        self.default_model = default_model
        token = self.fetch_token()
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            http_client=httpx.AsyncClient(verify=False),
            default_headers={"Authorization": token}
        )

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
                   model: Optional[str] = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: Optional[str] = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": False
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            arguments = tc.function.arguments
            if isinstance(arguments, str):
                try:
                    # 尝试解析 JSON
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    # 如果解析失败，保持为字符串
                    # 或者根据你的需求，可以记录日志、抛出异常或进行其他处理
                    pass
            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments
                )
            )
        u = response.usage
        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage={
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens
            } if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: Optional[str] = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> LLMResponse:
        """Stream a chat completion, calling on_token for each text token."""
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

        accumulated_content = ""
        finish_reason = "stop"
        # tool_calls_acc: index -> {id, name, arguments}
        tool_calls_acc: dict[int, dict[str, str]] = {}

        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta.content:
                    accumulated_content += delta.content
                    if on_token:
                        await on_token(delta.content)
                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": getattr(tc_delta, "id", "") or "",
                                "name": getattr(tc_delta.function, "name", "") or "",
                                "arguments": "",
                            }
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments
        except Exception as e:
            return LLMResponse(
                content=accumulated_content or f"Streaming error: {e}",
                finish_reason="error",
            )

        tool_calls = []
        for tc in tool_calls_acc.values():
            args = tc["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    pass
            tool_calls.append(ToolCallRequest(
                id=tc["id"] or tc["name"],
                name=tc["name"],
                arguments=args,
            ))

        return LLMResponse(
            content=accumulated_content or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
        )

    def get_default_model(self) -> str:
        return self.default_model

    def fetch_token(self) -> str:
        static_token = '4gOSfDAHLq_dWIEvFIR4h9Iv-qx6wFQi2F87t-2Z1xpFd7sMHu0Wrr0jRE4JZGkYxg9eW5cWVqNpE4LmDf1TGg'
        url = 'http://oauth2-his.huawei.com/ApiCommonQuery/appToken/getRestAppDynamicToken'
        headers = {"Content-Type": "application/json"}
        body = {
            "appId": "com.huawei.ekooverse",
            "credential": base64.b64encode(static_token.encode()).decode()
        }
        try:
            response = requests.post(url=url, json=body, headers=headers, timeout=100)
            response.raise_for_status()
            result = response.json()
            token = result['result']
            return token
        except requests.exceptions.RequestException as e:
            raise e

async def main():
    provider = CustomProvider(
        api_key="sk-xxx",
        api_base="https://console.his.huawei.com/agi/agi_agent/infer/sandbox/v1",
        default_model="GLM-V4.7"
    )
    messages = [{"role": "user", "content": "你好"}]
    try:
        response = await provider.chat(messages=messages)
        print("响应内容:", response.content)
    except Exception as e:
        print(f"调用失败: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())