import json

import httpx
import pytest

from bot.backends import OllamaBackend, OpenAICompatBackend, make_backend
from bot.config import ConfigError, config_from_mapping, load_config

MINIMAL = {"radio": {"port": "/dev/fake"}}


def test_defaults_match_the_agreed_setup():
    cfg = config_from_mapping(MINIMAL, env={})
    assert cfg.channel_idx == 1
    assert cfg.bot_name == "Mesh Potato"
    assert cfg.trigger_prefix == ""
    assert cfg.backend == "ollama"
    assert cfg.model == "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert cfg.ollama_think == "off"
    assert cfg.reply_max_chars == 147
    assert cfg.reply_delay_s == 8.0
    assert cfg.global_rate_per_min == 4.0
    assert cfg.sender_rate_per_min == 4.0
    assert cfg.injection_threshold == 0.45
    assert cfg.default_persona == "nice" and set(cfg.personas) == {"nice", "funny", "snarky", "marvin", "pirate", "haiku", "serious"}
    assert cfg.persona_timeout_min == 120 and cfg.command_prefix == "/"
    assert cfg.model_timeout_s == 25.0 and cfg.web_enabled is True
    assert cfg.adaptive_enabled is True
    assert (cfg.duty_low, cfg.duty_high, cfg.tx_duty_budget) == (0.05, 0.15, 0.02)
    assert (cfg.utilization_poll_s, cfg.utilization_window_s) == (10.0, 120.0)


def test_sections_are_flattened_and_env_overrides_win():
    doc = {"radio": {"port": "/dev/a", "channel_idx": 0}, "model": {"model": "x"}}
    env = {"MESHAI_CHANNEL_IDX": "2", "MESHAI_MODEL": "qwen2.5:14b", "MESHAI_ADAPTIVE_ENABLED": "false", "MESHAI_TEMPERATURE": "0.7"}
    cfg = config_from_mapping(doc, env=env)
    assert cfg.channel_idx == 2
    assert cfg.model == "qwen2.5:14b"
    assert cfg.adaptive_enabled is False
    assert cfg.temperature == 0.7


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown config key"):
        config_from_mapping({"radio": {"port": "/dev/a", "chanel_idx": 1}}, env={})


def test_missing_port_is_rejected():
    with pytest.raises(ConfigError, match="port is required"):
        config_from_mapping({}, env={})


@pytest.mark.parametrize(
    "bad",
    [
        {"backend": "anthropic"},
        {"channel_idx": 300},
        {"reply_max_chars": 0},
        {"reply_max_chars": 148},  # Mesh Potato: 160 - 11 - 2 = 147 fits
        {"bot_name": "A very long node name here", "reply_max_chars": 150},
        {"injection_threshold": 1.5},
        {"ollama_think": "maybe"},
        {"global_burst": 0},
        {"duty_low": 0.2, "duty_high": 0.1},
        {"tx_duty_budget": 0},
        {"tx_duty_budget": 0.9},
        {"default_persona": "nope"},
        {"bot_name": "Mesh:AI"},
        {"apology": "x" * 140},
        {"personas": {"Bad Name": "x"}},
        {"personas": {"help": "x"}, "default_persona": "help"},
        {"persona_timeout_min": 0},
        {"fortune_time": "6am"},
        {"fortune_prompt": "no subject here"},
        {"fortune_prompt": "about {subject} at {when}"},
        {"fortune_prefix": "x" * 100, "fortune_fallback": "y" * 60},
        {"command_prefix": ""},
        {"duty_high": 1.5},
        {"utilization_poll_s": 0},
        {"utilization_poll_s": 30, "utilization_window_s": 10},
    ],
)
def test_invalid_values_are_rejected(bad):
    with pytest.raises(ConfigError):
        config_from_mapping({**MINIMAL, **bad}, env={})


def test_bad_env_type_is_a_config_error():
    with pytest.raises(ConfigError, match="expected an integer"):
        config_from_mapping(MINIMAL, env={"MESHAI_CHANNEL_IDX": "one"})


def test_load_config_from_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[radio]\nport = "/dev/cu.test"\nchannel_idx = 1\n[bot]\ntrigger_prefix = "!ai "\n')
    cfg = load_config(path, env={})
    assert cfg.port == "/dev/cu.test"
    assert cfg.trigger_prefix == "!ai "


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


class _StubClient:
    """Stands in for ollama.AsyncClient / httpx.AsyncClient so no SSL context or socket is created."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def test_backend_selection_ollama(monkeypatch):
    # Importing the real `ollama` package builds an HTTP client at import time; stub the module.
    import sys
    import types

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.AsyncClient = _StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
    cfg = config_from_mapping({**MINIMAL, "backend": "ollama", "ollama_host": "http://box:11434"}, env={})
    backend = make_backend(cfg, env={})
    assert isinstance(backend, OllamaBackend)
    assert backend.name == "ollama"
    assert backend.model == "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert backend._client.kwargs == {"host": "http://box:11434"}
    assert backend._think is False


def test_backend_selection_openai_reads_key_from_env_only(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    cfg = config_from_mapping({**MINIMAL, "backend": "openai", "model": "local-model"}, env={})
    backend = make_backend(cfg, env={"MESHAI_OPENAI_API_KEY": "sk-test"})
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.name == "openai"
    assert backend._client.kwargs["base_url"] == "http://127.0.0.1:1234/v1"
    assert backend._client.kwargs["headers"] == {"Authorization": "Bearer sk-test"}
    assert "sk-test" not in repr(cfg)


def test_openai_backend_without_key_sends_no_auth_header(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    cfg = config_from_mapping({**MINIMAL, "backend": "openai"}, env={})
    backend = make_backend(cfg, env={})
    assert backend._client.kwargs["headers"] == {}


async def test_openai_backend_posts_chat_completions():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Four."}}]})

    client = httpx.AsyncClient(base_url="http://llm.local/v1", transport=httpx.MockTransport(handler))
    backend = OpenAICompatBackend("http://llm.local/v1", "m", 0.2, 50, api_key=None, http_client=client)
    out = await backend.complete([{"role": "user", "content": "2+2?"}])
    assert out.text == "Four." and not out.truncated
    assert seen["url"] == "http://llm.local/v1/chat/completions"
    body = json.loads(seen["json"])
    assert body["max_tokens"] == 50
    assert body["stream"] is False
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "2+2?"}]
    await backend.complete([{"role": "user", "content": "Web evidence"}], max_tokens=384)
    assert json.loads(seen["json"])["max_tokens"] == 384
    await backend.complete([{"role": "user", "content": "Normal reply"}])
    assert json.loads(seen["json"])["max_tokens"] == 50  # override does not leak into ordinary replies
    await backend.aclose()


async def test_openai_backend_raises_on_http_error():
    client = httpx.AsyncClient(
        base_url="http://llm.local/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    backend = OpenAICompatBackend("http://llm.local/v1", "m", 0.2, 50, api_key=None, http_client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await backend.complete([{"role": "user", "content": "x"}])
