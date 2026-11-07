"""SQL 生成器。

抽成一个协议，是因为生成 SQL 这一步要能换：

- :class:`OpenAICompatGenerator` —— 真实模型，OpenAI 兼容接口都能接
  （DeepSeek / 通义 / 智谱 / 本地 vLLM / Ollama）
- :class:`ScriptedGenerator` —— 固定脚本，不需要 key 也能跑通整条链路

图里只依赖 ``generate()``，换实现不动图。
"""

from __future__ import annotations

import os
import re
from typing import Protocol

from app.semantic.loader import MetricRegistry, MetricSpec

from .prompts import build_messages


class SQLGenerator(Protocol):
    def generate(
        self,
        question: str,
        spec: MetricSpec,
        registry: MetricRegistry,
        *,
        bounds: tuple,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> str:
        """返回一条 SQL 字符串。失败请抛异常，不要在内部吞掉。"""
        ...


# ---------------------------------------------------------------------------
# 真实模型
# ---------------------------------------------------------------------------


class OpenAICompatGenerator:
    """走 OpenAI 兼容的 chat/completions 接口。

    配置读环境变量::

        LLM_API_KEY   = sk-xxx
        LLM_BASE_URL  = https://api.deepseek.com/v1
        LLM_MODEL     = deepseek-chat
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 3000,
        value_hints: dict[str, list[str]] | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        #: 维度名 -> 该列在库里的真实取值，拼提示词时一并交给模型
        self.value_hints = value_hints or {}
        #: 累计用量。评测按题做差，算单题花了几次调用、多少 token。
        #: 只统计成功的请求 —— 抛异常的那次没有 usage 可读。
        self.usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    @classmethod
    def from_env(
        cls,
        *,
        value_hints: dict[str, list[str]] | None = None,
    ) -> OpenAICompatGenerator:
        api_key = os.environ.get("LLM_API_KEY")
        model = os.environ.get("LLM_MODEL")
        base_url = os.environ.get("LLM_BASE_URL") or None
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS") or 3000)

        missing = [
            name
            for name, value in (("LLM_API_KEY", api_key), ("LLM_MODEL", model))
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"缺少环境变量 {missing}。\n"
                f"两种配法，选一种：\n"
                f"  1. 写进 backend/.env（或仓库根目录 .env），一次就好：\n"
                f"       LLM_API_KEY=sk-xxx\n"
                f"       LLM_MODEL=deepseek-v4-flash\n"
                f"       LLM_BASE_URL=https://api.deepseek.com/v1\n"
                f"  2. 在当前终端里设，然后同一个终端里启动：\n"
                f"       PowerShell: $env:LLM_API_KEY=\"sk-xxx\"\n"
                f"       cmd:        set LLM_API_KEY=sk-xxx\n"
                f"或者用 python -m app.graph --demo 离线跑一遍。"
            )

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
            value_hints=value_hints,
        )

    def generate(
        self,
        question: str,
        spec: MetricSpec,
        registry: MetricRegistry,
        *,
        bounds: tuple,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> str:
        messages = build_messages(
            question,
            spec,
            registry,
            bounds=bounds,
            previous_sql=previous_sql,
            error=error,
            value_hints=self.value_hints,
        )
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        self._record_usage(response)

        choice = response.choices[0]
        content = choice.message.content or ""

        # 推理型模型（deepseek-v4-flash 之类）会先花 token 思考，
        # max_tokens 给太小就会「思考完就没额度了」，content 是空的。
        # 这种情况必须报出来，不能当成「模型返回空 SQL」草草收场。
        if not content.strip():
            hint = (
                "，推理过程把 max_tokens 用光了，调大 LLM_MAX_TOKENS 试试"
                if choice.finish_reason == "length"
                else ""
            )
            raise RuntimeError(
                f"模型没有返回内容（finish_reason={choice.finish_reason}）{hint}"
            )

        return extract_sql(content)

    def _record_usage(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0


# ---------------------------------------------------------------------------
# 离线脚本
# ---------------------------------------------------------------------------


class ScriptedGenerator:
    """按问题返回预置 SQL。

    同一个问题被调用多次（修复循环）时，依次取列表里的下一条。
    这样不带 key 也能演示「第一次写错 → 守卫拦住 → 修复」。
    """

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self.scripts = scripts
        self._used: dict[str, int] = {}

    def generate(
        self,
        question: str,
        spec: MetricSpec,
        registry: MetricRegistry,
        *,
        bounds: tuple,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> str:
        if question not in self.scripts:
            raise KeyError(f"脚本里没有这个问题的 SQL：{question}")

        index = self._used.get(question, 0)
        self._used[question] = index + 1

        candidates = self.scripts[question]
        return candidates[min(index, len(candidates) - 1)]


# ---------------------------------------------------------------------------
# 输出清洗
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:sql|SQL)?\s*(.*?)```", re.DOTALL)


def extract_sql(text: str) -> str:
    """模型经常裹一层 markdown 或者解释，这里剥掉，只留 SQL。"""
    if not text:
        return ""

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)

    text = text.strip()
    if text.lower().startswith("sql"):
        text = text[3:].lstrip(": \n")

    return text.strip().rstrip(";").strip()
