"""One-shot completions against an OpenAI- or Anthropic-style endpoint.

llm.recurse.com, Ollama (`http://localhost:11434/v1`) and llama.cpp's server
all speak the OpenAI chat-completions shape; set `api = "anthropic"` if RC's
endpoint turns out to want the Messages shape instead.
"""

from __future__ import annotations

import httpx

from crier import USER_AGENT
from crier.config import LLMConfig
from crier.guard import check_llm_url

CUT_OFF = "_(Answer cut off at llm.max_output_tokens; raise it in the config.)_"


class LLMError(Exception):
    """The model endpoint failed. The message never carries prompt text."""


def strip_reasoning(text: str) -> str:
    """Drop a <think>...</think> preamble, which reasoning models like GLM emit."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif "<think>" in text:  # cut off mid-thought
        text = text.split("<think>", 1)[0]
    return text.strip()


class LLM:
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        check_llm_url(cfg.base_url)
        self.cfg = cfg
        headers = {"User-Agent": USER_AGENT}
        if cfg.api == "openai":
            self._path = "/chat/completions"
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            self._path = "/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            if api_key:
                headers["x-api-key"] = api_key
        self._http = httpx.Client(
            base_url=cfg.base_url,
            headers=headers,
            timeout=cfg.timeout,
            transport=transport,
            follow_redirects=False,
        )
        self.calls = 0

    def close(self) -> None:
        self._http.close()

    def complete(self, system: str, user: str) -> str:
        c = self.cfg
        if c.api == "openai":
            body = {
                "model": c.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": c.temperature,
                "max_tokens": c.max_output_tokens,
            }
            if c.reasoning_effort:
                body["reasoning_effort"] = c.reasoning_effort
        else:
            body = {
                "model": c.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": c.temperature,
                "max_tokens": c.max_output_tokens,
            }
        self.calls += 1
        try:
            r = self._http.post(self._path, json=body)
        except httpx.TimeoutException:
            raise LLMError(f"timed out after {c.timeout:.0f}s") from None
        except httpx.HTTPError as e:
            raise LLMError(type(e).__name__) from None
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {_error_text(r)}")
        try:
            data = r.json()
            if c.api == "openai":
                choice = data["choices"][0]
                text = choice["message"].get("content") or ""
                cut = choice.get("finish_reason") == "length"
            else:
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
                cut = data.get("stop_reason") == "max_tokens"
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("response wasn't in the expected shape") from None
        text = strip_reasoning(text)
        if cut:
            text += "\n" + CUT_OFF
        return text


def _error_text(r: httpx.Response) -> str:
    """The server's own error message, short; never the request we sent."""
    try:
        err = r.json().get("error", "")
        if isinstance(err, dict):
            err = err.get("message") or err.get("type") or ""
        return str(err)[:200]
    except (ValueError, AttributeError):
        return ""
