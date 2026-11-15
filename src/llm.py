"""Model layer: hands out LangChain chat models for the model level in use (config.MODEL_LEVEL).

    base     open-weight models through Ollama, on this machine (the current version)
    standard cheaper open-weight models on a hosted provider (planned: asking for it fails loudly, never falls back silently)
    premium  Anthropic (Gemini and OpenAI to come), only for the tasks in PREMIUM_TASKS; every other task stays local
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

import config


def get_chat_model(
    task: str = "general",
    model: Optional[str] = None,
    temperature: float = 0.2,
) -> BaseChatModel:
    """Return the chat model for a task at the configured level."""
    if config.MODEL_LEVEL == "standard":
        raise RuntimeError("MODEL_LEVEL=standard (hosted open-weight models) is planned, not built yet: set MODEL_LEVEL=base or premium")
    if config.MODEL_LEVEL == "premium" and task in config.PREMIUM_TASKS:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise RuntimeError("The Premium level needs the extra: uv sync --extra premium") from e
        return ChatAnthropic(
            model=config.ANTHROPIC_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=config.OLLAMA_NUM_PREDICT,
        )

    return ChatOllama(
        model=model or config.TASK_MODELS.get(task) or config.LOCAL_LLM_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=temperature,
        num_ctx=config.OLLAMA_NUM_CTX,
        num_predict=config.OLLAMA_NUM_PREDICT,
        reasoning=config.OLLAMA_THINK,
        keep_alive=config.OLLAMA_KEEP_ALIVE,      # loading the model costs ~20 s: keep it loaded across pauses and approvals
    )
