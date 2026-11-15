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

# API Configuration (only needed for MODEL_LEVEL=premium)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Search: "searxng" is open source and the default; "brave" is an opt-in paid API (needs BRAVE_API_KEY)
SEARCH_PROVIDERS = [
    p.strip().lower() for p in os.getenv("SEARCH_PROVIDERS", "searxng").split(",") if p.strip()
]
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "20"))

# Time zone used to tell planning what today is (an IANA name such as Asia/Kolkata); empty means this computer's own zone
TIMEZONE = os.getenv("TITAN_TIMEZONE", "").strip()

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

# Model levels: which models do the judgement steps. Skills, ontologies and code checks are the same at every level.
#   base     = open-weight models through Ollama, on this machine (the current version; no keys, nothing leaves the machine)
#   standard = cheaper open-weight models on a hosted provider, not local (planned, not built yet)
#   premium  = Anthropic, Gemini and OpenAI models, for the tasks in PREMIUM_TASKS (Anthropic is wired today)
MODEL_LEVELS = ("base", "standard", "premium")
# Before the levels, LLM_MODE was 'standard' (local, now Base) or 'power' (Anthropic, now Premium). An old .env keeps working:
# MODEL_LEVEL wins when set; otherwise the old setting is read in its old meaning.
_LEGACY_LEVEL = {"standard": "base", "power": "premium"}
MODEL_LEVEL = (os.getenv("MODEL_LEVEL") or _LEGACY_LEVEL.get(os.getenv("LLM_MODE", "").strip().lower(), "base")).strip().lower()
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen3:8b")
# Per-task local models, "task=model,task=model". Tasks not listed use LOCAL_LLM_MODEL. Chosen from `eval_models.py`:
# a different model family as the claim-support checker caught an entity-link error the Qwen models missed.
TASK_MODELS = {
    k.strip(): v.strip() for k, v in (pair.split("=", 1) for pair in os.getenv("TASK_MODELS", "").split(",") if "=" in pair)
    if k.strip() and v.strip()
}
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Context the model actually gets; large values don't fit in 8GB VRAM
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "2048"))
# Qwen3 "thinking" mode: better reasoning, slower
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "False").lower() == "true"
# How long Ollama keeps a model in memory after a call (Ollama's default is 5 minutes; a reload costs ~20 s on 8 GB)
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# The tasks a Premium model takes over; every other task stays on the local model (POWER_MODE_TASKS is the old name, still read)
PREMIUM_TASKS = {
    t.strip() for t in (os.getenv("PREMIUM_TASKS") or os.getenv(
        "POWER_MODE_TASKS",
        "text2sql,confidence_scoring,contradiction_detection,synthesis,claim_support,capability",
    )).split(",") if t.strip()
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

    if MODEL_LEVEL not in MODEL_LEVELS:
        errors.append(f"MODEL_LEVEL must be one of {', '.join(MODEL_LEVELS)}, got '{MODEL_LEVEL}'")
    if MODEL_LEVEL == "standard":
        errors.append("MODEL_LEVEL=standard (hosted open-weight models) is planned, not built yet: use base or premium")
    if MODEL_LEVEL == "premium" and not ANTHROPIC_API_KEY.startswith("sk-"):
        errors.append("MODEL_LEVEL=premium requires ANTHROPIC_API_KEY in .env")

    if TIMEZONE:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"TITAN_TIMEZONE={TIMEZONE!r} is not a time zone name (use for example Asia/Kolkata); the computer's zone is used")

    unknown = set(SEARCH_PROVIDERS) - {"searxng", "brave", "brave_context"}
    if unknown:
        errors.append(f"Unknown SEARCH_PROVIDERS: {', '.join(sorted(unknown))}")
    if any(p.startswith("brave") for p in SEARCH_PROVIDERS) and not BRAVE_API_KEY:
        errors.append("SEARCH_PROVIDERS includes a Brave provider but BRAVE_API_KEY is empty")

    if errors:
        print("Configuration warnings:")
        for error in errors:
            print(f"  - {error}")
        print("\nCopy .env.example to .env and set MODEL_LEVEL and any keys")
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
