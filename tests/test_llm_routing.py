import importlib

import pytest

import config
import llm


def model_name(task, **kw):
    return llm.get_chat_model(task=task, **kw).model


def test_tasks_use_their_own_local_model_and_others_use_the_default(monkeypatch):
    monkeypatch.setattr(config, "MODEL_LEVEL", "base")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "qwen3:8b")
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b"})
    assert model_name("claim_support") == "gemma4:e4b"
    assert model_name("extract") == "qwen3:8b" and model_name("general") == "qwen3:8b"


def test_an_explicit_model_beats_the_routing_table(monkeypatch):
    monkeypatch.setattr(config, "MODEL_LEVEL", "base")
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b"})
    assert model_name("claim_support", model="llama3.1:8b") == "llama3.1:8b"


def test_premium_wins_only_for_premium_tasks(monkeypatch):
    monkeypatch.setattr(config, "MODEL_LEVEL", "premium")
    monkeypatch.setattr(config, "PREMIUM_TASKS", {"claim_support"})
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b", "extract": "qwen3:8b"})
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    assert model_name("extract") == "qwen3:8b"                 # every other task stays local
    try:
        chosen = llm.get_chat_model(task="claim_support")
    except RuntimeError:              # the optional premium extra is not installed here
        return
    assert "anthropic" in type(chosen).__module__.lower()


def test_the_standard_level_is_not_built_and_says_so_rather_than_falling_back(monkeypatch):
    monkeypatch.setattr(config, "MODEL_LEVEL", "standard")
    with pytest.raises(RuntimeError, match="planned, not built"):
        llm.get_chat_model(task="extract")


@pytest.mark.parametrize("env, level", [
    ({"MODEL_LEVEL": "premium"}, "premium"),
    ({"LLM_MODE": "standard"}, "base"),               # an old .env: 'standard' meant local
    ({"LLM_MODE": "power"}, "premium"),               # and 'power' meant Anthropic
    ({"MODEL_LEVEL": "base", "LLM_MODE": "power"}, "base"),   # the new setting wins
    ({}, "base"),
])
def test_the_level_is_read_from_the_new_setting_or_the_old_one_in_its_old_meaning(monkeypatch, env, level):
    for k in ("MODEL_LEVEL", "LLM_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)          # the developer's own .env must not decide the test
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        assert importlib.reload(config).MODEL_LEVEL == level
    finally:
        for k in env:
            monkeypatch.delenv(k)
        importlib.reload(config)


def test_the_routing_string_is_parsed_from_the_environment(monkeypatch):
    monkeypatch.setenv("TASK_MODELS", "claim_support=gemma4:e4b, relevance = qwen3.5:latest,junk")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.TASK_MODELS == {"claim_support": "gemma4:e4b", "relevance": "qwen3.5:latest"}
    finally:
        monkeypatch.delenv("TASK_MODELS")
        importlib.reload(config)
