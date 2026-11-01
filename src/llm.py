"""Model layer: hands out LangChain chat models for Standard (Ollama) or Power (Anthropic) mode."""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

import config


def get_chat_model(
    task: str = "general",
    model: Optional[str] = None,
    temperature: float = 0.2,
) -> BaseChatModel:
    """Return the chat model for a task.

    Standard mode always uses local Ollama. Power mode swaps in Anthropic only for
    tasks listed in POWER_MODE_TASKS; everything else stays local.
    """
    if config.LLM_MODE == "power" and task in config.POWER_MODE_TASKS:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise RuntimeError("Power mode needs the extra: uv sync --extra power") from e
        return ChatAnthropic(
            model=config.ANTHROPIC_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=config.OLLAMA_NUM_PREDICT,
        )

    return ChatOllama(
        model=model or config.LOCAL_LLM_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=temperature,
        num_ctx=config.OLLAMA_NUM_CTX,
        num_predict=config.OLLAMA_NUM_PREDICT,
        reasoning=config.OLLAMA_THINK,
    )
