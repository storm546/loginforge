"""OpenRouter chat client with tool calling and free-model resolution."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests
from openai import OpenAI

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Preference order for text-only runs (cheap, tool-calling capable).
PREFERRED_TEXT = [
    "nex-agi/nex-n2.5-mini:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "thinkingmachines/inkling-small:free",
    "google/gemma-4-31b-it:free",
]
# Preference order when --vision is used.
PREFERRED_VISION = [
    "nex-agi/nex-n2.5-mini:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "thinkingmachines/inkling-small:free",
]


class LLMError(RuntimeError):
    pass


def list_models(api_key: str) -> list[dict[str, Any]]:
    r = requests.get(
        f"{OPENROUTER_BASE}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def credits(api_key: str) -> dict[str, Any]:
    r = requests.get(
        f"{OPENROUTER_BASE}/credits",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def resolve_model(api_key: str, want_vision: bool, override: str | None = None) -> str:
    if override:
        return override

    prefs = PREFERRED_VISION if want_vision else PREFERRED_TEXT
    try:
        models = list_models(api_key)
    except Exception:
        return prefs[0]

    free = [m for m in models if m.get("id", "").endswith(":free")]
    usable: list[str] = []
    for m in free:
        params = m.get("supported_parameters") or []
        if params and "tools" not in params:
            continue
        modalities = (m.get("architecture") or {}).get("input_modalities") or []
        if want_vision and "image" not in modalities:
            continue
        usable.append(m["id"])

    for p in prefs:
        if p in usable:
            return p
    if usable:
        return usable[0]
    raise LLMError("no usable free model found on OpenRouter for this account")


class LLM:
    def __init__(self, api_key: str, model: str, vision: bool = False) -> None:
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self.model = model
        self.vision = vision
        # Free OpenRouter models are capped at ~20 requests/minute account-wide.
        self.min_interval = float(os.environ.get("FORGE_LLM_MIN_INTERVAL", "3.2"))
        self._last_call = 0.0
        self.client = OpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE,
            default_headers={
                "HTTP-Referer": "https://stormlabs.cloud",
                "X-Title": "loginforge",
            },
        )

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        for key in ("retry-after", "x-ratelimit-reset"):
            raw = headers.get(key)
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if key == "x-ratelimit-reset" and value > 1e9:  # epoch milliseconds
                return max(1.0, value / 1000.0 - time.time())
            return value
        return None

    def _pace(self) -> None:
        delta = time.time() - self._last_call
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_call = time.time()

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             max_retries: int = 5) -> Any:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            self._pace()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.1,
                    max_tokens=1200,
                )
                if not resp.choices:
                    raise LLMError("empty completion")
                return resp.choices[0].message
            except LLMError:
                raise
            except Exception as exc:
                last_exc = exc
                text = str(exc).lower()
                retryable = any(t in text for t in ("429", "rate limit", "502", "503", "504", "timed out", "timeout"))
                if not retryable or attempt == max_retries:
                    raise
                wait = min(max(self._retry_after(exc) or 2 ** attempt * 5, 2.0), 90.0)
                print(
                    f"[llm] {type(exc).__name__} (attempt {attempt + 1}/{max_retries}) - "
                    f"waiting {wait:.0f}s",
                    flush=True,
                )
                time.sleep(wait)
        raise LLMError(str(last_exc))

    @staticmethod
    def dump(msg: Any) -> dict[str, Any]:
        """Serialise an assistant message back into the conversation."""
        out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if getattr(msg, "tool_calls", None):
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        return out


def parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except json.JSONDecodeError:
        return {"_raw": raw}
