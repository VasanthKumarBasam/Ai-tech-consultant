"""Claude wrapper: structured output via forced tool use, usage + cost accounting.

Every model call in the Knowledge Brain goes through `LLM.structured`, which
  * forces Claude to answer through a JSON-schema'd tool (no free-text parsing),
  * retries transient API errors (SDK) and one schema/validation failure (here),
  * returns token usage and USD cost so evals can report cost per task.

`FakeLLM` is a deterministic stand-in used by the unit tests and by
LLM_PROVIDER=fake demo mode (clearly labelled in the UI) — it never pretends
to be a real model in evaluation output.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("kb.llm")


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    retries: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.latency_ms += other.latency_ms
        self.cost_usd += other.cost_usd
        self.retries += other.retries


@dataclass
class StructuredResult:
    data: dict[str, Any]
    usage: Usage


class LLMError(RuntimeError):
    pass


def cost_for(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = settings.pricing.get(model, (0.0, 0.0))
    return round(input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out, 6)


def image_block(data: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
    }


class LLM:
    provider = "anthropic"

    def __init__(self, settings: Settings):
        self.settings = settings
        import anthropic  # imported lazily so tests don't need a key

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=settings.llm_timeout_s, max_retries=3)
        self._omit_temperature: set[str] = set()

    @property
    def configured(self) -> bool:
        return True

    def structured(
        self,
        *,
        model: str,
        system: str,
        content: list[dict] | str,
        tool_name: str,
        tool_description: str,
        schema: dict,
        max_tokens: int = 1024,
        validate: Callable[[dict], str | None] | None = None,
    ) -> StructuredResult:
        """Call Claude and return the forced tool input as a dict.

        `validate` may return an error string; we then retry once, telling the
        model exactly what was wrong (a cheap, measurable self-correction step).
        """
        messages: list[dict] = [{"role": "user", "content": content}]
        tool = {"name": tool_name, "description": tool_description, "input_schema": schema}
        total = Usage(model=model)
        for attempt in range(2):
            t0 = time.perf_counter()
            kwargs: dict[str, Any] = dict(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
            )
            if model not in self._omit_temperature:
                kwargs["temperature"] = 0
            try:
                resp = self.client.messages.create(**kwargs)
            except self._anthropic.BadRequestError as e:  # some models reject temperature
                if "temperature" in str(e).lower() and "temperature" in kwargs:
                    self._omit_temperature.add(model)
                    kwargs.pop("temperature")
                    resp = self.client.messages.create(**kwargs)
                else:
                    raise LLMError(f"Claude rejected the request: {e}") from e
            except self._anthropic.APIError as e:
                raise LLMError(f"Claude API error: {e}") from e
            u = resp.usage
            in_tok = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + (
                getattr(u, "cache_creation_input_tokens", 0) or 0
            )
            step = Usage(
                model=model,
                input_tokens=in_tok,
                output_tokens=u.output_tokens or 0,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            step.cost_usd = cost_for(self.settings, model, step.input_tokens, step.output_tokens)
            total.add(step)
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                err = "No tool call returned."
                data: dict = {}
            else:
                data = dict(block.input)
                err = validate(data) if validate else None
            if not err:
                return StructuredResult(data=data, usage=total)
            log.warning("structured output invalid (attempt %s): %s", attempt + 1, err)
            total.retries += 1
            if block is None:
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": f"You must call the {tool_name} tool. {err}"})
            else:
                messages.append({"role": "assistant", "content": resp.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "is_error": True,
                                "content": f"Invalid output: {err} Call {tool_name} again with corrected input.",
                            }
                        ],
                    }
                )
        raise LLMError(f"Model output failed validation twice: {err}")


class FakeLLM:
    """Deterministic offline model for tests / demo-without-key.

    Handlers are keyed by tool name; each receives the user content and schema.
    """

    provider = "fake"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings
        self.calls: list[dict] = []
        self.handlers: dict[str, Callable[[Any], dict]] = {
            "submit_answer": _fake_answer,
            "describe_image": lambda content: {
                "description": "Screenshot of an admin settings screen (fake vision output).",
                "ui_labels": ["Settings"],
                "steps_shown": [],
                "legible": True,
                "uncertainty": "",
            },
        }

    @property
    def configured(self) -> bool:
        return True

    def structured(self, *, model: str, system: str, content, tool_name: str, schema: dict,
                   validate: Callable[[dict], str | None] | None = None, **_: Any) -> StructuredResult:
        self.calls.append({"tool": tool_name, "model": model, "content": content})
        handler = self.handlers.get(tool_name)
        if handler is None:
            raise LLMError(f"FakeLLM has no handler for {tool_name}")
        text = content if isinstance(content, str) else " ".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
        data = handler(text)
        err = validate(data) if validate else None
        if err:
            raise LLMError(f"Model output failed validation: {err}")
        return StructuredResult(data=data, usage=Usage(model="fake", input_tokens=len(text) // 4, output_tokens=50))


def _fake_answer(text: str) -> dict:
    """Answer with the first evidence sentence that shares words with the question."""
    q = re.search(r"<question>(.*?)</question>", text, re.S)
    question = (q.group(1) if q else "").lower()
    qwords = {w for w in re.findall(r"[a-z]{4,}", question)}
    best = None
    for m in re.finditer(r'<source id="(S\d+)"[^>]*>(.*?)</source>', text, re.S):
        sid, body = m.group(1), m.group(2)
        overlap = len(qwords & set(re.findall(r"[a-z]{4,}", body.lower())))
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, sid, body.strip())
    if not best or best[0] < 2:
        return {
            "status": "insufficient_evidence",
            "answer": "The knowledge base does not contain enough information to answer this.",
            "citations": [],
            "unsupported_points": [question[:200]],
            "escalation_reason": "No retrieved source addresses the question (fake model).",
        }
    sentence = re.split(r"(?<=[.!?])\s+", best[2])[0][:400]
    return {
        "status": "answered",
        "answer": f"[FAKE MODEL] {sentence} [{best[1]}]",
        "citations": [{"source_id": best[1], "supporting_quote": sentence[:120]}],
        "unsupported_points": [],
        "escalation_reason": None,
    }


class UnconfiguredLLM:
    """Used when no ANTHROPIC_API_KEY is set: retrieval still works, answering fails loudly."""

    provider = "unconfigured"
    configured = False

    def structured(self, **_: Any) -> StructuredResult:
        raise LLMError("ANTHROPIC_API_KEY is not set; cannot call Claude. Set it in .env or use LLM_PROVIDER=fake.")


def build_llm(settings: Settings):
    import os

    if settings.llm_provider == "fake":
        return FakeLLM(settings)
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY missing - answering disabled")
        return UnconfiguredLLM()
    return LLM(settings)
