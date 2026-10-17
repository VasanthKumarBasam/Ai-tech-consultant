"""Claude wrapper for the agent team: forced tool use with pydantic-validated outputs.

Each agent declares an output model; the wrapper turns it into the tool's JSON
schema, validates Claude's arguments with pydantic, runs an optional business
validator, and feeds any error back once so the model can correct itself.
Usage (tokens, cost, latency, retries) is accumulated per pipeline stage.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings
from .models import Usage

log = logging.getLogger("scope.llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def inline_refs(schema: dict) -> dict:
    """Inline $defs/$ref so the tool schema is a single self-contained object."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged)
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


class UsageTracker:
    def __init__(self) -> None:
        self.usage = Usage()
        self._lock = threading.Lock()

    def add(self, stage: str, model: str, in_tok: int, out_tok: int, cost: float, ms: int, retries: int) -> None:
        with self._lock:
            u = self.usage
            u.calls += 1
            u.input_tokens += in_tok
            u.output_tokens += out_tok
            u.cost_usd = round(u.cost_usd + cost, 6)
            u.latency_ms += ms
            u.retries += retries
            s = u.by_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_ms": 0, "model": model})
            s["calls"] += 1
            s["input_tokens"] += in_tok
            s["output_tokens"] += out_tok
            s["cost_usd"] = round(s["cost_usd"] + cost, 6)
            s["latency_ms"] += ms


class ClaudeLLM:
    provider = "anthropic"

    def __init__(self, settings: Settings):
        import anthropic

        self.s = settings
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=settings.llm_timeout_s, max_retries=3)
        self._no_temperature: set[str] = set()

    def cost(self, model: str, i: int, o: int) -> float:
        pin, pout = self.s.pricing.get(model, (0.0, 0.0))
        return i / 1e6 * pin + o / 1e6 * pout

    def structured(self, *, stage: str, model: str, system: str, user: str, tool_name: str, tool_description: str,
                   output: type[T], tracker: UsageTracker, max_tokens: int = 4000,
                   validate: Callable[[T], str | None] | None = None) -> T:
        schema = inline_refs(output.model_json_schema())
        tool = {"name": tool_name, "description": tool_description, "input_schema": schema}
        messages: list[dict] = [{"role": "user", "content": user}]
        err = ""
        retries = 0
        for attempt in range(2):
            kwargs: dict[str, Any] = dict(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages, tools=[tool], tool_choice={"type": "tool", "name": tool_name},
            )
            if model not in self._no_temperature:
                kwargs["temperature"] = 0
            t0 = time.perf_counter()
            try:
                resp = self.client.messages.create(**kwargs)
            except self._anthropic.BadRequestError as e:
                if "temperature" in str(e).lower() and "temperature" in kwargs:
                    self._no_temperature.add(model)
                    kwargs.pop("temperature")
                    resp = self.client.messages.create(**kwargs)
                else:
                    raise LLMError(f"Claude rejected the request ({stage}): {e}") from e
            except self._anthropic.APIError as e:
                raise LLMError(f"Claude API error ({stage}): {e}") from e
            ms = int((time.perf_counter() - t0) * 1000)
            u = resp.usage
            i = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
            tracker.add(stage, model, i, u.output_tokens or 0, self.cost(model, i, u.output_tokens or 0), ms, retries)
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                err = "no tool call returned"
            else:
                try:
                    parsed = output.model_validate(block.input)
                    err = (validate(parsed) if validate else None) or ""
                    if not err:
                        return parsed
                except ValidationError as ve:
                    err = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in ve.errors()[:8])
            retries += 1
            log.warning("%s: invalid output (attempt %s): %s", stage, attempt + 1, err)
            messages.append({"role": "assistant", "content": resp.content})
            if block is None:
                messages.append({"role": "user", "content": f"You must call {tool_name}."})
            else:
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                              "content": f"Invalid: {err}. Call {tool_name} again with corrected input."}]})
        raise LLMError(f"{stage}: model output failed validation twice: {err}")


class UnconfiguredLLM:
    provider = "unconfigured"

    def structured(self, **_: Any):
        raise LLMError("ANTHROPIC_API_KEY is not set. Add it to .env, or run with LLM_PROVIDER=fake for a demo.")


def build_llm(settings: Settings):
    if settings.llm_provider == "fake":
        from .fake_llm import FakeLLM

        return FakeLLM()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return UnconfiguredLLM()
    return ClaudeLLM(settings)
