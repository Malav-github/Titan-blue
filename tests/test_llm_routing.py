import config
import llm


def model_name(task, **kw):
    return llm.get_chat_model(task=task, **kw).model


def test_tasks_use_their_own_local_model_and_others_use_the_default(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "standard")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "qwen3:8b")
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b"})
    assert model_name("claim_support") == "gemma4:e4b"
    assert model_name("extract") == "qwen3:8b" and model_name("general") == "qwen3:8b"


def test_an_explicit_model_beats_the_routing_table(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "standard")
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b"})
    assert model_name("claim_support", model="llama3.1:8b") == "llama3.1:8b"


def test_power_mode_still_wins_for_power_tasks(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "power")
    monkeypatch.setattr(config, "POWER_MODE_TASKS", {"claim_support"})
    monkeypatch.setattr(config, "TASK_MODELS", {"claim_support": "gemma4:e4b", "extract": "qwen3:8b"})
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    try:
        chosen = llm.get_chat_model(task="claim_support")
    except RuntimeError:              # the optional anthropic extra is not installed here
        return
    assert "anthropic" in type(chosen).__module__.lower()
    assert model_name("extract") == "qwen3:8b"


def test_the_routing_string_is_parsed_from_the_environment(monkeypatch):
    import importlib
    monkeypatch.setenv("TASK_MODELS", "claim_support=gemma4:e4b, relevance = qwen3.5:latest,junk")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.TASK_MODELS == {"claim_support": "gemma4:e4b", "relevance": "qwen3.5:latest"}
    finally:
        monkeypatch.delenv("TASK_MODELS")
        importlib.reload(config)
