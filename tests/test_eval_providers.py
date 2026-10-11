"""OpenAI-compatible providers for the eval harness (DeepSeek, Qwen, Kimi,
GLM, OpenRouter, local servers): routing, the client against a fake endpoint,
pricing, and the capped driver's refusal of unpriced models. No network."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from rhylthyme.cli.eval import llm

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


class FakeChat:
    def __init__(self, statuses=(200,), content="hello"):
        self.requests, self.statuses, self.content = [], list(statuses), content
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                status = (
                    fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                )
                payload = (
                    {
                        "model": body["model"],
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": fake.content,
                                    "reasoning_content": "thinking",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
                    }
                    if status == 200
                    else {"error": {"message": "slow down"}}
                )
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "RHYLTHYME_EVAL_BASE_URL",
        "RHYLTHYME_EVAL_API_KEY",
        "RHYLTHYME_EVAL_PRICE_IN",
        "RHYLTHYME_EVAL_PRICE_OUT",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_provider_routing(clean_env, monkeypatch):
    assert llm.provider_for("claude-haiku-4-5") is None
    assert llm.provider_for("deepseek-flash") == (
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
    )
    assert llm.provider_for("qwen3-max")[1] == "DASHSCOPE_API_KEY"
    assert llm.provider_for("kimi-k3")[1] == "MOONSHOT_API_KEY"
    assert llm.provider_for("glm-5")[1] == "ZAI_API_KEY"
    assert llm.provider_for("deepseek/deepseek-flash") == llm.OPENROUTER
    assert llm.provider_for("something-unknown") is None
    monkeypatch.setenv("RHYLTHYME_EVAL_BASE_URL", "http://localhost:8000/v1/")
    assert llm.provider_for("claude-haiku-4-5") == (
        "http://localhost:8000/v1",
        "RHYLTHYME_EVAL_API_KEY",
    )


def test_make_client_needs_the_providers_key(clean_env, monkeypatch):
    assert isinstance(llm.make_client(model="claude-haiku-4-5"), llm.AnthropicClient)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        llm.make_client(model="deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    client = llm.make_client(model="deepseek-flash")
    assert (
        isinstance(client, llm.OpenAICompatClient)
        and client.base_url == "https://api.deepseek.com"
    )
    monkeypatch.setenv("RHYLTHYME_EVAL_BASE_URL", "http://localhost:11434/v1")
    assert (
        llm.make_client(model="qwen-local").api_key is None
    )  # a local server needs no key


def test_client_sends_chat_format_and_reads_usage(clean_env):
    fake = FakeChat(content='```json\n{"tracks": []}\n```')
    try:
        c = llm.OpenAICompatClient(fake.url, "secret")
        out = c.complete(
            [
                {"role": "user", "content": "plan"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "go"},
            ],
            system="be exact",
            model="deepseek-flash",
            max_tokens=777,
        )
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer secret"
    assert req["body"]["max_tokens"] == 777 and req["body"]["stream"] is False
    assert [m["role"] for m in req["body"]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert req["body"]["messages"][0]["content"] == "be exact"
    assert out.text.startswith("```json") and "thinking" not in out.text
    assert (out.input_tokens, out.output_tokens, out.stop_reason, out.model) == (
        120,
        45,
        "end_turn",
        "deepseek-flash",
    )


def test_client_retries_rate_limits_but_not_client_errors(clean_env):
    flaky = FakeChat(statuses=[429, 503, 200])
    try:
        assert (
            llm.OpenAICompatClient(flaky.url, "k")
            .complete([{"role": "user", "content": "x"}], model="m", max_tokens=5)
            .text
            == "hello"
        )
        assert len(flaky.requests) == 3
    finally:
        flaky.close()
    bad = FakeChat(statuses=[400])
    try:
        with pytest.raises(RuntimeError, match="HTTP 400"):
            llm.OpenAICompatClient(bad.url, "k").complete(
                [{"role": "user", "content": "x"}], model="m", max_tokens=5
            )
        assert len(bad.requests) == 1
    finally:
        bad.close()


def test_prices_table_prefix_and_env_override(clean_env, monkeypatch):
    assert llm.price_for("deepseek-flash") == (0.30, 1.20)
    assert llm.estimate_cost("deepseek-flash", 1_000_000, 1_000_000) == pytest.approx(
        1.50
    )
    assert llm.price_for("kimi-k3") is None
    monkeypatch.setenv("RHYLTHYME_EVAL_PRICE_IN", "0.4")
    monkeypatch.setenv("RHYLTHYME_EVAL_PRICE_OUT", "2")
    assert llm.price_for("kimi-k3") == (0.4, 2.0)
    assert llm.price_for("claude-haiku-4-5") == (
        1.0,
        5.0,
    ), "the table wins over the override"


def test_capped_driver_refuses_an_unpriced_model(clean_env):
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "eval" / "run_model_comparison.py"),
            "--model",
            "mystery-9",
            "--cap",
            "1",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "No price known" in proc.stderr + proc.stdout


def test_openai_direct_uses_max_completion_tokens_and_openrouter_ids_route(
    clean_env, monkeypatch
):
    assert llm.provider_for("gpt-5.6-luna") == (
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
    )
    for model in (
        "openai/gpt-5.6-luna",
        "qwen/qwen3.8-flash",
        "meta-llama/llama-4-maverick",
    ):
        assert llm.provider_for(model) == llm.OPENROUTER
        assert llm.price_for(model) is not None, model
    fake = FakeChat()
    try:
        llm.OpenAICompatClient(fake.url, "k").complete(
            [{"role": "user", "content": "x"}], model="m", max_tokens=9
        )
        client = llm.OpenAICompatClient(fake.url, "k")
        client.base_url = fake.url  # same server, but pretend it is OpenAI's host
        monkeypatch.setattr(client, "base_url", fake.url + "/api.openai.com")
        client.complete(
            [{"role": "user", "content": "x"}], model="gpt-5.6-luna", max_tokens=9
        )
    finally:
        fake.close()
    assert (
        "max_tokens" in fake.requests[0]["body"]
        and "max_completion_tokens" not in fake.requests[0]["body"]
    )
    assert (
        fake.requests[1]["body"].get("max_completion_tokens") == 9
        and "max_tokens" not in fake.requests[1]["body"]
    )


def test_driver_flattens_vendor_prefixed_model_ids(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "eval" / "run_model_comparison.py"),
            "--model",
            "qwen/qwen3.8-flash",
            "--cap",
            "1",
            "--out",
            str(tmp_path),
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--max-tokens",
            "64000",
            "--max-programs",
            "2",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("would run") == 4


def test_timeout_is_configurable_and_retry_on_timeout_is_opt_in(clean_env, monkeypatch):
    import socket

    monkeypatch.setenv("RHYLTHYME_EVAL_TIMEOUT", "0.3")
    # A socket that accepts and never answers: every request times out.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    url = f"http://127.0.0.1:{srv.getsockname()[1]}/v1"
    try:
        client = llm.OpenAICompatClient(url, "k", retries=2)
        assert client.timeout == 0.3
        with pytest.raises(RuntimeError, match="did not answer"):
            client.complete([{"role": "user", "content": "x"}], model="m", max_tokens=5)
    finally:
        srv.close()
