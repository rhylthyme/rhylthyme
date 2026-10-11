"""Model clients for the live evaluation harness.

Three things live here:

* :class:`Client` -- the protocol the harness talks to. One method,
  :meth:`Client.complete`, taking a message list and returning a
  :class:`Completion` (text plus token counts).
* :class:`AnthropicClient` -- the real thing, wrapping the Anthropic SDK's
  Messages API. The SDK is imported lazily so the scorer and the tests
  never need it installed.
* :class:`FakeClient` -- canned responses for tests, keyed either by a
  substring of the last user message or served in sequence.

Plus a small price table so runs can log an estimated cost.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Union

Message = Dict[str, Any]

# Prices in USD per million tokens (input, output) for the first-party
# Anthropic API, as listed by the claude-api skill on 2026-06-24. Lookup
# strips any date suffix ("claude-haiku-4-5-20251001" -> "claude-haiku-4-5")
# and then falls back to the longest matching prefix, so a new snapshot of
# a known family still gets a price.
PRICE_TABLE_DATE = "2026-06-24"
PRICES_PER_MTOK: Dict[str, tuple] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # DeepSeek, from api-docs.deepseek.com/quick_start/pricing on 2026-09-19.
    # PEAK rates (off-peak is half), so a budget computed from these is an
    # upper bound. Cache-miss input price: the harness does not rely on
    # provider-side prompt caching.
    "deepseek-flash": (0.30, 1.20),
    "deepseek-v4-pro": (1.32, 3.96),
    # Gemini paid tier, from ai.google.dev/gemini-api/docs/pricing on
    # 2026-09-19. Output prices include thinking tokens.
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    # OpenAI list price (developers.openai.com/api/docs/pricing, 2026-09-19).
    "gpt-5.6-luna": (0.20, 1.20),
    # Through OpenRouter, from its public /api/v1/models listing on
    # 2026-09-19. Reasoning tokens are billed as output.
    "openai/gpt-5.6-luna": (0.20, 1.20),
    "qwen/qwen3.8-flash": (0.15, 0.47),
    "qwen/qwen3-235b-a22b-2507": (0.0875, 0.35),
    "meta-llama/llama-4-maverick": (0.188, 0.652),
    "meta-llama/llama-4-scout": (0.10, 0.30),
    "meta-llama/llama-3.3-70b-instruct": (0.10, 0.32),
}


def _env_price() -> Optional[tuple]:
    """RHYLTHYME_EVAL_PRICE_IN / _OUT (USD per million tokens) price a model
    the table does not know, e.g. one reached through an aggregator."""
    try:
        return (
            float(os.environ["RHYLTHYME_EVAL_PRICE_IN"]),
            float(os.environ["RHYLTHYME_EVAL_PRICE_OUT"]),
        )
    except (KeyError, ValueError):
        return None


def price_for(model: str) -> Optional[tuple]:
    """``(input, output)`` USD per million tokens, or ``None`` if unknown."""
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    best = None
    for key in PRICES_PER_MTOK:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return PRICES_PER_MTOK[best] if best else _env_price()


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Estimated USD for one call; ``None`` when the model is not priced."""
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


@dataclass
class Completion:
    """What a model call returns, reduced to what the harness needs."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Completion":
        return cls(
            text=str(data.get("text", "")),
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            model=str(data.get("model", "")),
            stop_reason=data.get("stop_reason"),
            raw=dict(data.get("raw") or {}),
        )


class Client(Protocol):
    """Anything that can turn a message list into a :class:`Completion`."""

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: Optional[str] = None,
        model: str,
        max_tokens: int,
    ) -> Completion:  # pragma: no cover - protocol
        ...


class AnthropicClient:
    """Messages API client. Imports the SDK on first use."""

    def __init__(self, api_key: Optional[str] = None, **client_kwargs: Any):
        self._api_key = api_key
        self._client_kwargs = client_kwargs
        self._client = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._client is None:
                try:
                    import anthropic
                except ImportError as exc:  # pragma: no cover - environment
                    raise RuntimeError(
                        "The anthropic package is required for live eval runs: "
                        "pip install anthropic"
                    ) from exc
                kwargs = dict(self._client_kwargs)
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                self._client = anthropic.Anthropic(**kwargs)
            return self._client

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: Optional[str] = None,
        model: str,
        max_tokens: int,
    ) -> Completion:
        client = self._get()
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(messages),
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = getattr(response, "usage", None)
        try:
            raw = response.to_dict()
        except Exception:  # noqa: BLE001 - raw is best-effort
            raw = {}
        return Completion(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=str(getattr(response, "model", model)),
            stop_reason=getattr(response, "stop_reason", None),
            raw=raw,
        )


# Providers that speak the OpenAI chat-completions format. A model id picks
# its provider by prefix; an id with a slash ("deepseek/deepseek-flash") goes
# to OpenRouter, which fronts most of them under one key. RHYLTHYME_EVAL_BASE_URL
# and RHYLTHYME_EVAL_API_KEY override both, for any other compatible endpoint
# (a local vLLM or Ollama server included).
OPENAI_COMPAT_PROVIDERS = [
    ("deepseek-", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    (
        "qwen",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    ("kimi-", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    ("moonshot-", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    ("glm-", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    (
        "gemini-",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
    ),
    ("gpt-", "https://api.openai.com/v1", "OPENAI_API_KEY"),
]
OPENROUTER = ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")


def provider_for(model: str) -> Optional[tuple]:
    """``(base_url, api_key_env)`` for an OpenAI-compatible model, else None."""
    if os.environ.get("RHYLTHYME_EVAL_BASE_URL"):
        return (
            os.environ["RHYLTHYME_EVAL_BASE_URL"].rstrip("/"),
            "RHYLTHYME_EVAL_API_KEY",
        )
    if model.startswith("claude-"):
        return None
    if "/" in model:
        return OPENROUTER
    for prefix, base_url, key_env in OPENAI_COMPAT_PROVIDERS:
        if model.startswith(prefix):
            return (base_url, key_env)
    return None


class OpenAICompatClient:
    """Chat-completions client for OpenAI-compatible endpoints. Standard
    library only. Retries on 429/5xx; never on a timeout, because a request
    that timed out here may still have been billed there."""

    RETRY_STATUS = (429, 500, 502, 503, 529)

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        *,
        timeout: float = 900.0,
        retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # RHYLTHYME_EVAL_TIMEOUT (seconds) overrides the per-call timeout.
        self.timeout = float(os.environ.get("RHYLTHYME_EVAL_TIMEOUT") or timeout)
        self.retries = retries

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: Optional[str] = None,
        model: str,
        max_tokens: int,
    ) -> Completion:
        chat: List[Dict[str, Any]] = (
            [{"role": "system", "content": system}] if system else []
        )
        chat += [{"role": m["role"], "content": m["content"]} for m in messages]
        body = json.dumps(
            {
                "model": model,
                "messages": chat,
                # OpenAI's current models reject `max_tokens`.
                (
                    "max_completion_tokens"
                    if "api.openai.com" in self.base_url
                    else "max_tokens"
                ): max_tokens,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in self.RETRY_STATUS and attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"{self.base_url} returned HTTP {exc.code}: {detail}"
                ) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                # A stalled provider. Retrying can double-bill a request that
                # did complete remotely, so it is opt-in (cheap models only).
                if (
                    os.environ.get("RHYLTHYME_EVAL_RETRY_TIMEOUTS")
                    and attempt < self.retries
                ):
                    continue
                raise RuntimeError(
                    f"{self.base_url} did not answer within {self.timeout:.0f}s: {exc}"
                ) from exc
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        return Completion(
            text=str((choice.get("message") or {}).get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            # Reasoning tokens are billed as output and are inside this count.
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or model),
            stop_reason={"stop": "end_turn", "length": "max_tokens"}.get(
                finish, finish
            ),
            raw=data,
        )


CannedResponse = Union[str, Completion]


@dataclass
class FakeCall:
    messages: List[Message]
    system: Optional[str]
    model: str
    max_tokens: int


class FakeClient:
    """Canned responses for tests.

    ``responses`` is either a sequence (served in order, the last one
    repeating) or a mapping from a substring to a response; the first key
    found in the last user message wins, with ``"*"`` as the fallback.
    Every call is recorded in :attr:`calls`.
    """

    def __init__(
        self,
        responses: Union[Sequence[CannedResponse], Mapping[str, CannedResponse]],
        *,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ):
        self._responses = responses
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls: List[FakeCall] = []
        self._lock = threading.Lock()

    @staticmethod
    def last_user_text(messages: Sequence[Message]) -> str:
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        str(block.get("text", ""))
                        for block in content
                        if isinstance(block, dict)
                    )
        return ""

    def _pick(self, messages: Sequence[Message], index: int) -> CannedResponse:
        if isinstance(self._responses, Mapping):
            text = self.last_user_text(messages)
            for key, value in self._responses.items():
                if key != "*" and key in text:
                    return value
            if "*" in self._responses:
                return self._responses["*"]
            raise KeyError(
                "FakeClient has no canned response for message: " + text[:120]
            )
        sequence = list(self._responses)
        if not sequence:
            raise IndexError("FakeClient has no canned responses")
        return sequence[min(index, len(sequence) - 1)]

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: Optional[str] = None,
        model: str,
        max_tokens: int,
    ) -> Completion:
        with self._lock:
            index = len(self.calls)
            self.calls.append(FakeCall(list(messages), system, model, max_tokens))
        chosen = self._pick(messages, index)
        if isinstance(chosen, Completion):
            return chosen
        return Completion(
            text=str(chosen),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            model=model,
            stop_reason="end_turn",
        )


def make_client(
    fake_responses: Optional[Any] = None, model: Optional[str] = None
) -> Client:
    """Build the client the CLI uses: a fake when canned responses are given,
    otherwise the provider the model id belongs to (Anthropic by default)."""
    if fake_responses is not None:
        return FakeClient(fake_responses)
    provider = provider_for(model or "")
    if provider is None:
        return AnthropicClient()
    base_url, key_env = provider
    api_key = os.environ.get(key_env)
    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        raise RuntimeError(
            f"Model {model!r} is served by {base_url}; set {key_env} to use it."
        )
    return OpenAICompatClient(base_url, api_key)
