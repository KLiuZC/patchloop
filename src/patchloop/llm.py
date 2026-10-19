import os
import threading

from openai import DefaultHttpxClient, OpenAI

PROVIDERS = {
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "key_env": "GLM_API_KEY",
        "model": "glm-5.3-flash",  # flagship: glm-5.3; free tier: glm-4.7-flash
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-flash",  # flagship: deepseek-v4-pro
    },
}

# CNY per million tokens: (input, cached input, output). DeepSeek figures are off-peak; peak hours double them.
PRICES = {
    "glm-5.3-flash": (0.8, 0.23, 2.8),
    "glm-5.3": (8.0, 2.0, 28.0),
    "deepseek-flash": (1.0, 0.02, 4.0),
    "deepseek-v4-pro": (4.5, 0.15, 13.5),
}


def estimate_cost(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    fresh, cached, output = PRICES[model]
    uncached = max(prompt_tokens - cached_tokens, 0)
    return (uncached * fresh + cached_tokens * cached + completion_tokens * output) / 1_000_000


class LLM:
    """Thin wrapper over an OpenAI-compatible chat endpoint."""

    def __init__(self, provider: str, model: str | None = None, temperature: float | None = None):
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}, expected one of {list(PROVIDERS)}")
        cfg = PROVIDERS[provider]
        api_key = os.environ.get(cfg["key_env"])
        if not api_key:
            raise RuntimeError(f"{cfg['key_env']} is not set; copy .env.example to .env and fill it in")
        self.provider = provider
        self.model = model or cfg["model"]
        self.temperature = temperature
        self._request_state = threading.local()
        request_state = self._request_state
        def observe_request(request):
            request_state.attempts = getattr(request_state, "attempts", 0) + 1
        # 429 / 5xx / connection errors are retried by the client with backoff
        self.client = OpenAI(base_url=cfg["base_url"], api_key=api_key, max_retries=3, timeout=120.0,
                             http_client=DefaultHttpxClient(event_hooks={"request": [observe_request]}))

    @property
    def last_http_attempts(self):
        return getattr(self._request_state, "attempts", None)

    @property
    def last_usage(self):
        return getattr(self._request_state, "usage", None)

    @property
    def last_response_received(self):
        return getattr(self._request_state, "response_received", False)

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        resp = self.chat_raw(messages, tools)
        return resp.choices[0].message, resp.usage

    def chat_raw(self, messages: list[dict], tools: list[dict] | None = None):
        """The full API response, for callers that need finish_reason and friends."""
        kwargs = {}
        if tools:
            kwargs["tools"] = tools
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        self._request_state.attempts = 0
        self._request_state.usage = None
        self._request_state.response_received = False
        response = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        self._request_state.usage = getattr(response, "usage", None)
        self._request_state.response_received = True
        return response
