"""Configuration management for Project Titan"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)

# API Configuration (only needed for LLM_MODE=power)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Search: "searxng" is open source and the default; "brave" is an opt-in paid API (needs BRAVE_API_KEY)
SEARCH_PROVIDERS = [
    p.strip().lower() for p in os.getenv("SEARCH_PROVIDERS", "searxng").split(",") if p.strip()
]
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "20"))

# Identifies Titan to sites it fetches from (email or URL). Wikimedia and some other sites reject bots without one.
TITAN_CONTACT = os.getenv("TITAN_CONTACT", "").strip()

# Evidence store (append-only; git-ignored, back it up separately)
# Relative paths resolve against the project root, not whichever directory a process starts in
EVIDENCE_DB_PATH = str((PROJECT_ROOT / os.getenv("EVIDENCE_DB_PATH", "data/evidence.db")).resolve())

# Application Configuration
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Research Configuration
MAX_SEARCHES_PER_COMPANY = int(os.getenv("MAX_SEARCHES_PER_COMPANY", "5"))
RESEARCH_DEPTH = os.getenv("RESEARCH_DEPTH", "deep")

# LLM Configuration
# standard = open-source models via Ollama only; power = Anthropic for POWER_MODE_TASKS
LLM_MODE = os.getenv("LLM_MODE", "standard").lower()
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Context the model actually gets; large values don't fit in 8GB VRAM
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "2048"))
# Qwen3 "thinking" mode: better reasoning, slower
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "False").lower() == "true"

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
POWER_MODE_TASKS = {
    t.strip() for t in os.getenv(
        "POWER_MODE_TASKS",
        "text2sql,confidence_scoring,contradiction_detection,synthesis,claim_support",
    ).split(",") if t.strip()
}

AVAILABLE_MODELS = {
    "qwen3:8b": {
        "license": "Apache 2.0",
        "tools": "Native tool calling",
        "description": "Default: best tool use at this size, optional thinking mode",
    },
    "mistral:7b": {
        "license": "Apache 2.0",
        "tools": "Basic",
        "description": "Fast; for comparison runs",
    },
    "llama3.1:8b": {
        "license": "Llama Community License (open weights, not OSI open source)",
        "tools": "Native tool calling",
        "description": "Comparison only",
    },
}

# Validation
def validate_config():
    """Validate that all required configuration is present"""
    errors = []

    if LLM_MODE not in ("standard", "power"):
        errors.append(f"LLM_MODE must be 'standard' or 'power', got '{LLM_MODE}'")
    if LLM_MODE == "power" and not ANTHROPIC_API_KEY.startswith("sk-"):
        errors.append("LLM_MODE=power requires ANTHROPIC_API_KEY in .env")

    unknown = set(SEARCH_PROVIDERS) - {"searxng", "brave", "brave_context"}
    if unknown:
        errors.append(f"Unknown SEARCH_PROVIDERS: {', '.join(sorted(unknown))}")
    if any(p.startswith("brave") for p in SEARCH_PROVIDERS) and not BRAVE_API_KEY:
        errors.append("SEARCH_PROVIDERS includes a Brave provider but BRAVE_API_KEY is empty")

    if errors:
        print("Configuration warnings:")
        for error in errors:
            print(f"  - {error}")
        print("\nCopy .env.example to .env and set LLM_MODE and any keys")
        return False

    return True

if __name__ == "__main__":
    print("Project Configuration")
    print("========================\n")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Evidence DB: {EVIDENCE_DB_PATH}")
    print(f"Debug mode: {DEBUG}")
    print(f"Log level: {LOG_LEVEL}")
    print(f"\nOK   Configuration loaded")
