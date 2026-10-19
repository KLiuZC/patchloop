import httpx2
import json
from concurrent.futures import ThreadPoolExecutor
import pytest

import patchloop.llm as language
from patchloop.loop import run
from patchloop.eval_usage import recover_usage


def test_successful_sdk_retry_marks_unreported_attempt_without_network(tmp_path, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(500, json={"error": {"message": "temporary"}})
        return httpx2.Response(200, json={
            "id": "fake", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })
    client_factory = language.DefaultHttpxClient
    monkeypatch.setattr(language, "DefaultHttpxClient", lambda **kwargs: client_factory(transport=httpx2.MockTransport(respond), **kwargs))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = language.LLM("deepseek")
    monkeypatch.setattr(model.client, "_calculate_retry_timeout", lambda *args, **kwargs: 0)
    class Runner:
        environment = "fake"
        workdir = "/testbed"
        def describe(self):
            return {"sandbox": "fake"}
        def diff(self):
            return ""
    try:
        result = run("test", model, Runner(), traj_dir=tmp_path, run_id="retry")
    finally:
        model.client.close()
    assert len(requests) == 2 and result.model_calls_attempted == 1
    assert result.http_attempts == 2 and result.unreported_http_attempts == 1
    usage = recover_usage(tmp_path / "retry.jsonl", "retry", model.model)
    assert usage["usage_status"] == "incomplete" and usage["cost"] is None
    assert usage["known_cost"] > 0 and usage["prompt_tokens"] == 100


def test_retry_observation_is_isolated_between_worker_threads(monkeypatch):
    def respond(request):
        label = json.loads(request.content)["messages"][0]["content"]
        if label == "retry" and request.headers.get("x-stainless-retry-count") == "0":
            return httpx2.Response(500, json={"error": {"message": "temporary"}})
        return httpx2.Response(200, json={
            "id": "fake", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    factory = language.DefaultHttpxClient
    monkeypatch.setattr(language, "DefaultHttpxClient", lambda **kwargs: factory(transport=httpx2.MockTransport(respond), **kwargs))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = language.LLM("deepseek")
    monkeypatch.setattr(model.client, "_calculate_retry_timeout", lambda *args, **kwargs: 0)
    def invoke(label):
        model.chat([{"role": "user", "content": label}])
        return model.last_http_attempts
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert list(workers.map(invoke, ["retry", "normal"] * 4)) == [2, 1] * 4
    finally:
        model.client.close()


@pytest.mark.parametrize("choices", [[], [{"index": 0, "message": {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "broken", "type": "function", "function": None}]}, "finish_reason": "tool_calls"}]])
def test_malformed_response_keeps_received_usage_before_decoding(tmp_path, monkeypatch, choices):
    def respond(request):
        return httpx2.Response(200, json={"id": "fake", "object": "chat.completion", "created": 1,
                               "model": "deepseek-flash", "choices": choices,
                               "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
    factory = language.DefaultHttpxClient
    monkeypatch.setattr(language, "DefaultHttpxClient", lambda **kwargs: factory(transport=httpx2.MockTransport(respond), **kwargs))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = language.LLM("deepseek")
    class Runner:
        environment = "fake"
        workdir = "/testbed"
        def describe(self):
            return {"sandbox": "fake"}
        def diff(self):
            return ""
    try:
        result = run("test", model, Runner(), traj_dir=tmp_path, run_id="bad-response", capture_errors=True)
    finally:
        model.client.close()
    assert result.error and result.prompt_tokens == 100 and result.completion_tokens == 20
    usage = recover_usage(tmp_path / "bad-response.jsonl", "bad-response", model.model)
    assert usage["known_cost"] > 0 and usage["cost"] is None
